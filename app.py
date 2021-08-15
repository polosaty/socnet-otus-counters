import asyncio
import base64
import json
import logging
import os
import signal

from aiohttp import web
import aiohttp_session
from aiohttp_session.cookie_storage import EncryptedCookieStorage
import aiomysql
import aiozipkin as az
import anyio
import arq
from arq import ArqRedis
from cryptography import fernet

from login import require_login
from rest import make_rest
from utils import close_db_pool
from utils import extract_database_credentials
from zipkin_monkeypatch import _set_span_properties

logger = logging.getLogger(__name__)


@require_login
async def handle_get_counters(request: web.Request):
    session = await aiohttp_session.get_session(request)
    uid = session["uid"]
    friends = None
    user_id = None
    # TODO: make decorator
    cors_headers = {
        'Access-Control-Allow-Credentials': 'true',
    }
    headers_origin = request.headers.get('Origin')
    if headers_origin:
        cors_headers['Access-Control-Allow-Origin'] = headers_origin

    try:
        user_id = int(request.query.get('userId'))
        friends = list(map(int, request.query.get('friends').split(',')))
    except Exception as ex:
        logger.warning('handle_get_counters: %r', ex)

    if not friends or not user_id:
        return web.HTTPBadRequest(reason='user_id and friends required', headers=cors_headers)

    if uid != user_id:
        raise web.HTTPForbidden(reason='wrong session', headers=cors_headers)

    # TODO: select from redis or mysql
    counters = {}
    redis: ArqRedis = request.app.get("arq_pool")
    if redis:
        for friend_id in friends:
            key = f'unread:{user_id}:{friend_id}'
            unread_messages = int(await redis.get(key) or 0)
            counters[friend_id] = unread_messages

        logging.debug('counters from redis: %r', counters)

    if not counters or not all(counters.values()):
        pool: aiomysql.pool.Pool = request.app['db_ro_pool']
        query_params = dict(
            friend_ids=friends,
            user_id=user_id
        )
        async with pool.acquire() as conn:
            async with conn.cursor(aiomysql.SSDictCursor) as cur:
                await cur.execute("SELECT id, user_id, chat_id, friend_id, unread_message_count "
                                  " FROM user_unread_counters "
                                  " WHERE user_id=%(user_id)s AND friend_id in %(friend_ids)s",
                                  query_params)
                result = await cur.fetchall()

                pipeline = None
                for row in result:
                    friend_id = row['friend_id']
                    unread_messages = row['unread_message_count']
                    counters[friend_id] = unread_messages
                    if redis:
                        key = f'unread:{user_id}:{friend_id}'
                        if unread_messages:
                            if not pipeline:
                                pipeline = redis.pipeline()
                            pipeline.set(key, unread_messages, expire=60)
                if redis and pipeline:
                    await pipeline.execute()
                logging.debug('counters from mysql: %r', counters)

    return web.json_response(counters or {}, headers=cors_headers)


async def migrate_schema(pool):
    logging.debug('migrate schema')
    conn: aiomysql.connection.Connection
    async with pool.acquire() as conn:
        cur: aiomysql.cursors.Cursor
        async with conn.cursor() as cur:
            try:
                await cur.execute("SELECT * FROM user_unread_counters LIMIT 1")
                await cur.fetchone()
                logging.debug('migrate schema not needed')
            except Exception:
                with open("schema.sql") as f:
                    schema = f.read()
                    await cur.execute(schema)
                logging.debug('migrate schema finished')


class EncryptedSessionStorage(EncryptedCookieStorage):

    def load_cookie(self, request):
        return request.query.get('session')


async def make_app(host, port):
    database_url = os.getenv('DATABASE_URL', None)
    app = web.Application()
    app['instance_id'] = os.getenv('INSTANCE_ID', '1')
    jaeger_address = os.getenv('JAEGER_ADDRESS')
    az.aiohttp_helpers._set_span_properties = _set_span_properties
    if jaeger_address:
        endpoint = az.create_endpoint(f"counters_{app['instance_id']}", ipv4=host, port=port)
        tracer = await az.create(jaeger_address, endpoint, sample_rate=1.0)

    app.add_routes(
        [
            web.get("/get_counters/", handle_get_counters, name='get_counters'),
        ])

    fernet_key = os.getenv('FERNET_KEY', fernet.Fernet.generate_key())
    secret_key = base64.urlsafe_b64decode(fernet_key)
    aiohttp_session.setup(app, EncryptedSessionStorage(secret_key))

    app.on_shutdown.append(stop_tasks)

    pool = await aiomysql.create_pool(
        **extract_database_credentials(database_url),
        maxsize=50,
        autocommit=True)
    app['db'] = pool
    app.on_shutdown.append(lambda _app: close_db_pool(_app['db']))

    databse_ro_url = os.getenv('DATABASE_RO_URL', None)
    if databse_ro_url:
        ro_pool = await aiomysql.create_pool(
            **extract_database_credentials(databse_ro_url),
            maxsize=50,
            autocommit=True)

        app['db_ro_pool'] = ro_pool
        app.on_shutdown.append(lambda _app: close_db_pool(_app['db_ro_pool']))
    else:
        logging.warning('DATABASE_RO_URL not set')
        app['db_ro_pool'] = pool

    redis_url = os.getenv('REDIS_URL', None)
    if redis_url:
        app['arq_pool'] = await arq.create_pool(arq.connections.RedisSettings.from_dsn(redis_url))

        async def close_arq_pool(_app):
            _app['arq_pool'].close()
            await _app['arq_pool'].wait_closed()

        app.on_shutdown.append(close_arq_pool)
    app['tasks'] = []
    # app.on_startup.append(start_background_task)

    if jaeger_address:
        az.setup(app, tracer)
    return app


async def stop_tasks(app):
    logger.debug('stopping tasks')
    t: asyncio.Task
    for t in app['tasks']:
        logger.debug('cancel task: %r', t)
        t.cancel()
        await t
        logger.debug('cancel task: %r [OK]', t)

    # await asyncio.gather(*app['tasks'])
    logger.debug('stopping tasks [OK]')


async def run_app(public_port=8080, public_host='0.0.0.0', rest_port=8081, rest_host='0.0.0.0'):
    public_port = public_port or int(os.getenv('PORT', 8080))
    rest_port = rest_port or int(os.getenv('REST_PORT', 8081))
    try:
        app_runner = web.AppRunner(await make_app(public_host, public_port))
        await app_runner.setup()
        site = web.TCPSite(app_runner, public_host, public_port)
        await site.start()

        rest_runner = web.AppRunner(await make_rest(rest_host, rest_port, app_runner.app))
        await rest_runner.setup()
        rest = web.TCPSite(rest_runner, rest_host, rest_port)
        await rest.start()

        stop_event = asyncio.Event()

        def stop():
            stop_event.set()

        loop = asyncio.get_event_loop()
        loop.add_signal_handler(signal.SIGTERM, stop)

        try:
            await stop_event.wait()
        except (asyncio.CancelledError, KeyboardInterrupt) as ex:
            logger.debug('Stopping app: %r', ex)

        logger.debug('shutdown rest')
        await rest_runner.shutdown()
        await rest_runner.cleanup()
        logger.debug('shutdown rest [OK]')
        try:
            await asyncio.wait_for(app_runner.shutdown(), timeout=3)
            await asyncio.wait_for(app_runner.cleanup(), timeout=3)
        except asyncio.TimeoutError:
            logger.debug('tasks: %r', asyncio.all_tasks())

        logger.debug('shutdown app [OK]')

    except asyncio.CancelledError as ex:
        logger.exception('run_app: %r', ex)
    except Exception as ex:
        logger.exception('run_app: %r', ex)


def main():
    logging.basicConfig(level=os.getenv('LOG_LEVEL', logging.DEBUG))
    anyio.run(run_app)
    logger.debug('App shutdown')


if __name__ == '__main__':
    main()
