"""Embedded uvicorn lifecycle (SPED §13.7).

Runs the FastAPI app produced by :func:`river_gang.http.app.create_app`
on the orchestrator's event loop without forking a separate process.
The orchestrator can stop the server cleanly via
:meth:`ServerHandle.stop` as part of the §16.1 graceful shutdown
sequence (`server.should_exit = True` then await the task).

Loopback default (``127.0.0.1``): never bind to ``0.0.0.0`` implicitly
— operators must opt in by passing an explicit host. The dashboard /
JSON API are observability surfaces, not auth-gated services, so
external exposure must be a deliberate decision.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass

import uvicorn
from fastapi import FastAPI

logger = logging.getLogger(__name__)

_STARTUP_POLL_INTERVAL_S = 0.01
_STARTUP_TIMEOUT_S = 10.0


@dataclass
class ServerHandle:
    """Live uvicorn server handle owned by the orchestrator.

    ``port`` reflects the actually-bound port (useful when callers
    request ``port=0`` to grab an ephemeral port). ``stop()`` is
    idempotent — calling it twice is safe.
    """

    port: int
    task: asyncio.Task[None]
    server: uvicorn.Server

    async def stop(self) -> None:
        """Signal uvicorn to exit and await the serve task."""
        if self.task.done():
            return
        self.server.should_exit = True
        try:
            await asyncio.wait_for(self.task, timeout=_STARTUP_TIMEOUT_S)
        except TimeoutError:
            logger.warning(
                "uvicorn server task did not exit within %.0fs — cancelling",
                _STARTUP_TIMEOUT_S,
            )
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self.task


async def start_server(
    app: FastAPI,
    *,
    host: str = "127.0.0.1",
    port: int = 0,
) -> ServerHandle:
    """Start uvicorn embedded on the running event loop.

    Polls ``server.started`` until the listener is up so callers can
    rely on "after this returns, requests will be accepted". If the
    serve task ends before the started flag flips (e.g. port already
    bound), the original exception is re-raised.

    Args:
        app: FastAPI app produced by :func:`create_app`.
        host: bind address. Defaults to loopback for safety.
        port: ``0`` means "let the OS pick a free port"; the actually-
            bound port is read back from the live socket and surfaced
            via :attr:`ServerHandle.port`.
    """
    config = uvicorn.Config(
        app=app,
        host=host,
        port=port,
        log_level="warning",
        lifespan="on",
    )
    server = uvicorn.Server(config)

    task = asyncio.create_task(server.serve(), name="river-gang-http")

    deadline = asyncio.get_running_loop().time() + _STARTUP_TIMEOUT_S
    while not server.started:
        if task.done():
            # Surface the underlying error rather than a generic
            # "task ended before startup".
            await task
            raise RuntimeError(
                "uvicorn serve task ended before server.started flipped"
            )
        if asyncio.get_running_loop().time() > deadline:
            task.cancel()
            raise RuntimeError(
                f"uvicorn did not start within {_STARTUP_TIMEOUT_S:.0f}s"
            )
        await asyncio.sleep(_STARTUP_POLL_INTERVAL_S)

    bound_port = _read_bound_port(server, fallback=port)
    return ServerHandle(port=bound_port, task=task, server=server)


def _read_bound_port(server: uvicorn.Server, *, fallback: int) -> int:
    """Pull the actually-bound port off the live uvicorn socket.

    Uvicorn's ``Server.servers`` is the list of asyncio servers it
    started — for an HTTP listener there is one, with a single socket.
    ``getsockname()`` returns ``(host, port)``.
    """
    servers = getattr(server, "servers", None)
    if not servers:
        return fallback
    sockets = getattr(servers[0], "sockets", None)
    if not sockets:
        return fallback
    sockname = sockets[0].getsockname()
    if isinstance(sockname, tuple) and len(sockname) >= 2:
        port = sockname[1]
        if isinstance(port, int):
            return port
    return fallback


__all__ = [
    "ServerHandle",
    "start_server",
]
