import logging
import os

from aiohttp import web
import aiomysql
import aiozipkin as az
from arq import ArqRedis

logger = logging.getLogger(__name__)


async def rest_update_counter_handler(request: web.Request):
    tracer = az.get_tracer(request.app)
    span = az.request_span(request)
    with tracer.new_child(span.context) as child_span:
        child_span.name("parse request")
        request_data = await request.json()
        user_id = request_data.get('user_id')
        friend_id = request_data.get('friend_id')
        chat_id = request_data.get('chat_id')
        unread_messages = request_data.get('unread_messages')

        child_span.tag('user_id', user_id)
        child_span.tag('friend_id', friend_id)
        child_span.tag('chat_id', chat_id)
        child_span.tag('unread_messages', unread_messages)

        if not user_id:
            return web.json_response({'user_id': 'required'}, status=400)
        if not friend_id:
            return web.json_response({'friend_id': 'required'}, status=400)

    with tracer.new_child(span.context) as child_span:
        child_span.name("mysql:update:user_unread_counters")
        pool: aiomysql.pool.Pool = request.app['db']
        conn: aiomysql.connection.Connection
        async with pool.acquire() as conn:
            cur: aiomysql.cursors.Cursor
            async with conn.cursor() as cur:
                await cur.execute(
                    "INSERT INTO user_unread_counters (user_id, friend_id, chat_id, unread_message_count) "
                    " VALUES (%(user_id)s, %(friend_id)s, %(chat_id)s, %(unread_message_count)s) "
                    " ON DUPLICATE KEY UPDATE unread_message_count=%(unread_message_count)s",
                    dict(
                        user_id=user_id,
                        friend_id=friend_id,
                        chat_id=chat_id,
                        unread_message_count=unread_messages,
                    )
                )
                await conn.commit()

    redis: ArqRedis = request.app.get("arq_pool")
    if redis:
        with tracer.new_child(span.context) as child_span:
            child_span.name("redis:update:user_unread_counters")
            key = f'unread:{user_id}:{friend_id}'
            child_span.tag('key', key)
            child_span.tag('value', unread_messages)
            res = await redis.set(key, unread_messages, expire=60, exist=redis.SET_IF_EXIST)
            child_span.tag('success', res)

    return web.json_response({'success': True})


async def make_rest(host, port, app):
    rest = web.Application()
    rest['instance_id'] = os.getenv('INSTANCE_ID', '1')
    jaeger_address = os.getenv('JAEGER_ADDRESS')
    if jaeger_address:
        endpoint = az.create_endpoint(f"counters_rest_{rest['instance_id']}", ipv4=host, port=port)
        tracer = await az.create(jaeger_address, endpoint, sample_rate=1.0)

    rest['db'] = app['db']
    if app.get('arq_pool'):
        rest['arq_pool'] = app['arq_pool']

    rest.add_routes([
        web.post("/update_counter/", rest_update_counter_handler)
    ])
    if jaeger_address:
        az.setup(rest, tracer)
    return rest


