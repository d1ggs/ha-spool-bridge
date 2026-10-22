import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestServer

from custom_components.spool_bridge.spoolman_proxy import SpoolmanProxy


async def fake_spoolman():
    """A Spoolman stand-in that records every request it gets."""
    seen: list[tuple[str, str, bytes]] = []
    spool = {"id": 7, "remaining_weight": 400.0, "used_weight": 600.0}

    async def handler(request: web.Request) -> web.Response:
        body = await request.read()
        seen.append((request.method, request.path_qs, body))
        if request.path == "/api/v1/spool/7" and request.method == "GET":
            return web.json_response(spool, headers={"X-Total-Count": "1"})
        if request.path == "/api/v1/spool/7/use":
            spool["remaining_weight"] -= 999  # must never be reached through the proxy
            return web.json_response(spool)
        if request.path == "/api/v1/spool/8":
            return web.json_response({"message": "not found"}, status=404)
        return web.json_response({"echo": request.path_qs, "body": body.decode()}, status=201)

    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", handler)
    server = TestServer(app, host="127.0.0.1")
    await server.start_server()
    return server, seen, spool


async def start_proxy(session, upstream, usage):
    proxy = SpoolmanProxy(session, str(upstream.make_url("/")), "127.0.0.1", 0,
                          lambda spool_id, grams: usage.append((spool_id, grams)))
    await proxy.start()
    return proxy


async def test_forwards_requests_unchanged(socket_enabled):
    upstream, seen, _ = await fake_spoolman()
    usage: list = []
    session = aiohttp.ClientSession()
    proxy = await start_proxy(session, upstream, usage)
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(f"{proxy.url}/api/v1/spool/7?allow_archived=true") as r:
                assert r.status == 200
                assert r.headers["X-Total-Count"] == "1"
                assert (await r.json())["remaining_weight"] == 400.0
            async with s.patch(f"{proxy.url}/api/v1/spool/7", json={"extra": {"tag": '"AB"'}}) as r:
                assert r.status == 201
                assert (await r.json())["body"] == '{"extra": {"tag": "\\"AB\\""}}'
        assert seen[0][:2] == ("GET", "/api/v1/spool/7?allow_archived=true")
        assert seen[1][0] == "PATCH"
        assert usage == []
    finally:
        await proxy.stop()
        await session.close()
        await upstream.close()


async def test_bambuddy_usage_is_acknowledged_not_applied(socket_enabled):
    # The bridge books print usage; Bambuddy's own deduction would count it twice.
    upstream, seen, spool = await fake_spoolman()
    usage: list = []
    session = aiohttp.ClientSession()
    proxy = await start_proxy(session, upstream, usage)
    try:
        async with aiohttp.ClientSession() as s:
            async with s.put(f"{proxy.url}/api/v1/spool/7/use", json={"use_weight": 12.5}) as r:
                assert r.status == 200
                assert (await r.json())["remaining_weight"] == 400.0  # what Bambuddy reads back
            async with s.put(f"{proxy.url}/api/v1/spool/8/use", json={"use_weight": 1}) as r:
                assert r.status == 404  # unknown spool still fails like Spoolman
        assert spool["remaining_weight"] == 400.0
        assert not any(path.endswith("/use") for _, path, _ in seen)
        assert usage == [(7, 12.5), (8, 1.0)]
    finally:
        await proxy.stop()
        await session.close()
        await upstream.close()


async def test_upstream_down_is_a_bad_gateway(socket_enabled):
    session = aiohttp.ClientSession()
    proxy = SpoolmanProxy(session, "http://127.0.0.1:9", "127.0.0.1", 0, lambda *a: None)
    await proxy.start()
    try:
        async with aiohttp.ClientSession() as s, s.get(f"{proxy.url}/api/v1/spool") as r:
            assert r.status == 502
    finally:
        await proxy.stop()
        await session.close()
