import os
from typing import Any, Awaitable, Callable, Dict

from aiohttp import web
import aiohttp_session
from yarl import URL

_WebHandler = Callable[[web.Request], Awaitable[web.StreamResponse]]


def require_login(func: _WebHandler) -> _WebHandler:
    func.__require_login__ = True  # type: ignore
    return func

@web.middleware
async def check_login(request: web.Request,
                      handler: _WebHandler) -> web.StreamResponse:
    is_require_login = getattr(handler, "__require_login__", False)
    session = await aiohttp_session.get_session(request)
    username = session.get("username")
    login_url = os.getenv('LOGIN_URL')
    if is_require_login:
        if not username:
            if not login_url:
                raise web.HTTPForbidden(reason='correct session required')

            location = URL(login_url).with_query(dict(next=str(request.rel_url)))
            raise web.HTTPSeeOther(location=location)
    if request.match_info.route.resource:
        return await handler(request)
    else:
        # to prevent assertion error in aiozipkin on /favicon.ico
        raise web.HTTPNotFound()
