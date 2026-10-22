"""Bambuddy's view of Spoolman: a loopback pass-through that keeps usage single-owner.

Bambuddy in Spoolman mode deducts each print's usage itself, and there is no
setting to stop it. On the X2D it usually cannot read the print file at print
start, so its numbers are partial, while the bridge books usage from
ha-bambulab's per-tray grams. Pointing Bambuddy at this proxy instead of at
Spoolman keeps everything else live (spools, slots, edits, labels) and
answers its usage deductions with the spool as it stands, without applying
them. Bound to 127.0.0.1: only processes on the HA host (Bambuddy runs with
host networking) can reach it.
"""

from __future__ import annotations

from collections.abc import Callable
import json
import logging
import re

import aiohttp
from aiohttp import web

_LOGGER = logging.getLogger(__name__)

_USE_RE = re.compile(r"^/api/v1/spool/(\d+)/use$")
# Not forwarded: aiohttp sets these for the response it actually sends.
_HOP_HEADERS = {"connection", "content-length", "content-encoding", "keep-alive", "transfer-encoding"}


class SpoolmanProxy:
    def __init__(self, session: aiohttp.ClientSession, upstream: str, host: str, port: int,
                 on_usage: Callable[[int, float], None]) -> None:
        self._session = session
        self._upstream = upstream.rstrip("/")
        self._host, self._port = host, port
        self._on_usage = on_usage
        self._runner: web.AppRunner | None = None

    @property
    def url(self) -> str:
        return f"http://{self._host}:{self._port}"

    async def start(self) -> None:
        app = web.Application(client_max_size=16 * 1024 * 1024)
        app.router.add_route("*", "/{tail:.*}", self._handle)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self._host, self._port)
        await site.start()
        if self._port == 0:  # tests ask the OS for a free port
            self._port = site._server.sockets[0].getsockname()[1]  # noqa: SLF001

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    async def _handle(self, request: web.Request) -> web.StreamResponse:
        body = await request.read()
        if request.method == "PUT" and (match := _USE_RE.match(request.path)):
            return await self._usage(int(match.group(1)), body)
        return await self._forward(request.method, request.path_qs, body, request.headers.get("Content-Type"))

    async def _usage(self, spool_id: int, body: bytes) -> web.StreamResponse:
        try:
            grams = float(json.loads(body or b"{}").get("use_weight") or 0)
        except (ValueError, AttributeError):
            grams = 0.0
        self._on_usage(spool_id, grams)
        # Spoolman answers /use with the updated spool; the unchanged one keeps
        # Bambuddy's flow (cost, alerts) going without moving the weight.
        return await self._forward("GET", f"/api/v1/spool/{spool_id}", b"", None)

    async def _forward(self, method: str, path_qs: str, body: bytes, content_type: str | None) -> web.StreamResponse:
        headers = {"Content-Type": content_type} if content_type else {}
        try:
            async with self._session.request(
                method, f"{self._upstream}{path_qs}", data=body or None, headers=headers,
                timeout=aiohttp.ClientTimeout(total=30), allow_redirects=False,
            ) as resp:
                payload = await resp.read()
                out = {k: v for k, v in resp.headers.items() if k.lower() not in _HOP_HEADERS}
                return web.Response(status=resp.status, body=payload, headers=out)
        except (aiohttp.ClientError, TimeoutError) as err:
            _LOGGER.warning("Spoolman unreachable for Bambuddy (%s %s): %s", method, path_qs, err)
            return web.json_response({"message": f"Spoolman unreachable: {err}"}, status=502)
