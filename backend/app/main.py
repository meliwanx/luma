"""FastAPI application entry point for the Luma personal assistant."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager
from typing import Optional

import anyio
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import telemetry
from . import auth as auth_module
from . import provider
from .admin import router as admin_router
from .config import APP_VERSION, WEB_ROOT, cors_origins, env_int
from .db import ensure_db
from .plugins import load_plugins, shutdown_plugins, startup_plugins
from .storage import get_storage
from . import runtime as runtime_module
from .runtime import worker_loop
from .scheduler import scheduler_loop
from .services.memory import shutdown_memory_workers
from .services.seed import ensure_default_data
from .services import generation
from .upload_limit import UploadSizeLimitMiddleware
from .auth_providers import mount_providers
from .routers import account, artifacts, auth, chat, connectors, dashboard, export, files, goals, health, ideas, library, memories, notifications, push, routines, runtime, sandbox, search, sessions, tasks, usage, voice
from .routers import feed, proactive

logger = logging.getLogger(__name__)

# Compatibility exports for callers/tests that historically imported helpers from app.main.
from .services.chat import conversation_messages  # noqa: F401,E402


async def runtime_worker_loop(stop_event: Optional[asyncio.Event] = None) -> None:
    await worker_loop(stop_event or asyncio.Event())


async def browser_connection_loop() -> None:
    from .services.browser import reap_connections

    while True:
        await asyncio.sleep(30)
        try:
            await reap_connections()
        except Exception as exc:
            logger.debug("browser connection maintenance failed: %s", type(exc).__name__)


@asynccontextmanager
async def lifespan(_: FastAPI):
    auth_module.validate_auth_configuration()
    # Validate the selected storage configuration before serving requests.
    # Existing rows can still use another backend when they are read later.
    get_storage()
    limiter = anyio.to_thread.current_default_thread_limiter()
    limiter.total_tokens = max(1, env_int("THREADPOOL_SIZE", 64))
    ensure_db()
    await startup_plugins()
    await generation.startup_recover()
    runtime_module.start_runtime()
    worker_stop = asyncio.Event()
    worker = asyncio.create_task(runtime_worker_loop(worker_stop), name="luma-runtime-worker")
    scheduler_stop = asyncio.Event()
    scheduler = asyncio.create_task(scheduler_loop(scheduler_stop), name="luma-scheduler")
    telemetry_task = asyncio.create_task(telemetry.telemetry_loop(), name="luma-telemetry")
    browser_task = asyncio.create_task(browser_connection_loop(), name="luma-browser-connections")
    try:
        yield
    finally:
        # Generation tasks own provider streams and must be stopped before the
        # runtime/scheduler workers and the async provider client are closed.
        # FastAPI is leaving the lifespan context, so no new HTTP requests
        # should enter after this gate is set.
        await generation.shutdown()
        worker_stop.set()
        scheduler_stop.set()
        # Cancelling an asyncio.to_thread await does not stop its SQL thread.
        # Let both loops finish their current pass before exiting the lifespan.
        await asyncio.gather(worker, scheduler, return_exceptions=True)
        await shutdown_plugins()
        telemetry_task.cancel()
        browser_task.cancel()
        try:
            await telemetry_task
        except asyncio.CancelledError:
            pass
        try:
            await browser_task
        except asyncio.CancelledError:
            pass
        # Join synchronous executors off the event loop: active jobs can need
        # that loop to finish provider I/O or their completion callbacks.
        await asyncio.to_thread(runtime_module.shutdown_runtime)
        await asyncio.to_thread(shutdown_memory_workers)
        # Running detached callbacks can enqueue model measurements after the
        # telemetry loop's cancellation flush. Drain them after both executors
        # have joined, while database connections are still available.
        await telemetry._run_telemetry_work(telemetry.flush)
        from .services.browser import close_connections

        await close_connections()
        await provider.close_async_client()


app = FastAPI(
    title="Personal Assistant API",
    version=APP_VERSION,
    description="Local-first API shared by the web and Flutter clients.",
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Has-More", "X-Oldest-Id"],
)


@app.middleware("http")
async def collect_telemetry(request: Request, call_next):
    """Time API requests without reading their bodies or query parameters."""
    if not request.url.path.startswith("/api/"):
        return await call_next(request)
    started = time.perf_counter()
    status_code, error = 500, None
    telemetry.request_started()
    try:
        response = await call_next(request)
        auth_module.refresh_session_cookie(request, response)
        status_code = response.status_code
        return response
    except Exception as exc:
        error = type(exc).__name__
        raise
    finally:
        telemetry.request_finished(request, status_code, (time.perf_counter() - started) * 1000, error)


@app.middleware("http")
async def prevent_stale_web_shell(request: Request, call_next):
    response = await call_next(request)
    content_type = response.headers.get("content-type", "")
    if "text/html" in content_type:
        response.headers["Cache-Control"] = "no-store, max-age=0"
        response.headers["Pragma"] = "no-cache"
    return response


# Keep the upload guard at the raw ASGI receive boundary so multipart parsing
# cannot write an oversized request to Starlette's temporary directory first.
app.add_middleware(UploadSizeLimitMiddleware)


# Include routers in the historical order. Runtime is split into status and durable phases.
app.include_router(dashboard.router)
app.include_router(health.router)
app.include_router(runtime.status_router)
app.include_router(sandbox.router)
app.include_router(auth.router)
mount_providers(app)
app.include_router(account.router)
app.include_router(sessions.router)
app.include_router(search.router)
app.include_router(chat.router)
app.include_router(memories.router)
app.include_router(tasks.router)
app.include_router(goals.router)
app.include_router(artifacts.router)
app.include_router(files.router)
app.include_router(library.router)
app.include_router(ideas.router)
app.include_router(voice.router)
app.include_router(usage.router)
app.include_router(connectors.router)
app.include_router(notifications.router)
app.include_router(push.router)
app.include_router(routines.router)
app.include_router(proactive.router)
app.include_router(feed.router)
app.include_router(tasks.legacy_router)
app.include_router(memories.legacy_router)
app.include_router(export.router)
app.include_router(runtime.router)
app.include_router(admin_router)
load_plugins(app)

if WEB_ROOT.exists():
    app.mount("/app", StaticFiles(directory=WEB_ROOT, html=True), name="web-app")
    react_assets = WEB_ROOT / "assets"
    if react_assets.exists() and WEB_ROOT.name == "dist":
        app.mount("/assets", StaticFiles(directory=react_assets), name="react-assets")


@app.get("/", include_in_schema=False)
def web_index() -> FileResponse:
    index = WEB_ROOT / "index.html"
    if not index.exists():
        raise HTTPException(status_code=404, detail="Web client has not been built")
    return FileResponse(index)


@app.get("/{spa_path:path}", include_in_schema=False)
def spa_fallback(spa_path: str) -> FileResponse:
    if spa_path == "api" or spa_path.startswith("api/"):
        raise HTTPException(status_code=404, detail="Not found")
    index = WEB_ROOT / "index.html"
    if not index.exists():
        raise HTTPException(status_code=404, detail="Web client has not been built")
    return FileResponse(index)
