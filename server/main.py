"""FastAPI app for the GenericAgent Web Admin.

Two startup modes:

* **Setup mode**: when ``_paths.GA_ROOT`` is None — only mounts ``/api/setup/*``
  plus a static SPA. The user picks a GenericAgent directory; backend then
  needs to be restarted to enter normal mode.

* **Normal mode**: full router set, agent + wechat + scheduler bootstrap.

This module is path-agnostic: it doesn't compute its own ROOT, instead it
relies on ``server._paths`` for everything that involves the GA project.
The webui ``dist/`` folder lives next to ``server/`` in the admin checkout
(``ADMIN_ROOT/webui/dist``).
"""
from __future__ import annotations

import asyncio
import logging
import mimetypes
from pathlib import Path
from typing import Any

from fastapi import APIRouter, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import _paths
from .routes import events as event_routes  # safe to import in setup mode
from .services.event_bus import bus

log = logging.getLogger(__name__)

# Force-correct MIME types for SPA assets. On Windows, the registry can have
# .js mapped to text/plain (legacy IIS / dev-tool installs), which makes
# Starlette serve ES modules with the wrong Content-Type and the browser
# refuses to execute them with a strict-MIME error → black screen.
# add_type() overrides whatever the registry says.
for _ext, _mime in (
    (".js", "application/javascript"),
    (".mjs", "application/javascript"),
    (".css", "text/css"),
    (".wasm", "application/wasm"),
    (".svg", "image/svg+xml"),
    (".json", "application/json"),
    (".map", "application/json"),
):
    mimetypes.add_type(_mime, _ext)

WEBUI_DIST = _paths.ADMIN_ROOT / "webui" / "dist"


class SetupReq(BaseModel):
    ga_root: str


def _setup_router() -> APIRouter:
    """Endpoints available in BOTH setup mode and normal mode.

    Lets the React UI:
      * read the current configured ga_root + suggested candidates
      * test a path for validity (without saving)
      * save a path → backend will need to restart to pick it up
    """
    r = APIRouter(prefix="/api/setup", tags=["setup"])

    @r.get("/status")
    async def setup_status():
        # Per-path try/except so a single broken path (e.g. an unmounted
        # volume on macOS, a permission error on Windows) doesn't 500 the
        # whole endpoint and leave the SPA stuck on "正在连接后端…".
        candidates = []
        try:
            for c in _paths.candidate_paths():
                try:
                    candidates.append({"path": str(c), "valid": _paths.is_valid_ga_root(c)})
                except Exception as e:
                    log.warning("candidate_paths probe failed for %r: %s", c, e)
                    candidates.append({"path": str(c), "valid": False, "error": str(e)})
        except Exception as e:
            log.exception("candidate_paths enumeration failed")
            candidates = [{"path": "<error>", "valid": False, "error": str(e)}]
        return {
            "configured": _paths.GA_ROOT is not None,
            "ga_root": str(_paths.GA_ROOT) if _paths.GA_ROOT else None,
            "admin_data": str(_paths.ADMIN_DATA),
            "candidates": candidates,
        }

    @r.post("/validate")
    async def setup_validate(req: SetupReq):
        return {
            "valid": _paths.is_valid_ga_root(req.ga_root),
            "resolved": str(Path(req.ga_root).expanduser().resolve()),
        }

    @r.post("/save")
    async def setup_save(req: SetupReq):
        try:
            p = _paths.set_ga_root(req.ga_root)
        except ValueError as e:
            raise HTTPException(400, str(e))
        return {"ok": True, "ga_root": str(p), "restart_required": True}

    return r


def _mount_static(app: FastAPI) -> None:
    if not WEBUI_DIST.is_dir():
        @app.get("/", include_in_schema=False)
        async def _root_hint():
            return {
                "hint": "webui/dist not built. Run install_webui.sh / install_webui.bat",
                "docs": "/docs",
                "configured": _paths.GA_ROOT is not None,
            }
        return

    app.mount("/assets", StaticFiles(directory=str(WEBUI_DIST / "assets")), name="assets")

    @app.get("/", include_in_schema=False)
    async def _root():
        return FileResponse(str(WEBUI_DIST / "index.html"))

    @app.get("/{path:path}", include_in_schema=False)
    async def _spa(path: str):
        full = WEBUI_DIST / path
        if full.is_file():
            return FileResponse(str(full))
        idx = WEBUI_DIST / "index.html"
        if idx.is_file():
            return FileResponse(str(idx))
        return JSONResponse({"detail": "not found"}, status_code=404)


def create_app() -> FastAPI:
    setup_mode = _paths.GA_ROOT is None
    app = FastAPI(
        title="GenericAgent Admin API" + (" (setup mode)" if setup_mode else ""),
        version="0.2.0",
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"], allow_credentials=False,
        allow_methods=["*"], allow_headers=["*"],
    )

    @app.on_event("startup")
    async def _startup():
        bus.attach_loop(asyncio.get_running_loop())
        if setup_mode:
            log.warning(
                "GA_ROOT not configured — running in SETUP MODE.\n"
                "  Open the UI to pick your GenericAgent directory,\n"
                "  or set $GA_ROOT and restart."
            )
            return

        # Lazy imports after _paths is set
        from .services.agent_service import AgentService
        from .services.autonomous_scheduler import AutonomousScheduler
        from .services.wechat_service import WeChatService

        agent_svc = AgentService.instance()
        agent_svc.start_run_thread()

        try:
            try:
                import mykey  # type: ignore
                allowed = getattr(mykey, "wechat_allowed_users", None)
            except Exception:
                allowed = None
            wx = WeChatService.instance(agent_svc)
            if allowed is not None:
                wx.set_allowlist(allowed)
            if wx.bot.has_token:
                wx.start_polling()
                log.info("wechat polling auto-resumed (bot_id=%s)", wx.bot.bot_id)
        except Exception as e:
            log.warning("wechat init skipped: %s", e)

        try:
            sched = AutonomousScheduler.instance(agent_svc)
            sched.start()
            log.info("autonomous scheduler started (%d schedules)", len(sched.schedules))
        except Exception as e:
            log.warning("autonomous scheduler init skipped: %s", e)

    @app.on_event("shutdown")
    async def _shutdown():
        if not setup_mode:
            try:
                from .services.autonomous_scheduler import AutonomousScheduler
                AutonomousScheduler.instance().shutdown()
            except Exception:
                pass

    # ── always-available endpoints ──
    app.include_router(_setup_router())
    app.include_router(event_routes.router)

    @app.get("/api/status")
    async def status():
        out: dict[str, Any] = {
            "configured": _paths.GA_ROOT is not None,
            "ga_root": str(_paths.GA_ROOT) if _paths.GA_ROOT else None,
        }
        if setup_mode:
            out["mode"] = "setup"
            return out

        from .services.agent_service import AgentService
        out["agent"] = AgentService.instance().status().__dict__
        try:
            from .services.wechat_service import WeChatService
            out["wechat"] = WeChatService.instance().status()
        except Exception as e:
            out["wechat"] = {"error": str(e)}
        try:
            from .services.autonomous_scheduler import AutonomousScheduler
            out["autonomous"] = {
                "schedule_count": len(AutonomousScheduler.instance().schedules),
            }
        except Exception:
            pass
        return out

    if not setup_mode:
        from .routes import (
            agent as agent_routes,
            autonomous as autonomous_routes,
            conversations as conv_routes,
            logs as log_routes,
            memory as memory_routes,
            mykey as mykey_routes,
            notify as notify_routes,
            upload as upload_routes,
            wechat as wechat_routes,
        )
        app.include_router(agent_routes.router)
        app.include_router(wechat_routes.router)
        app.include_router(conv_routes.router)
        app.include_router(memory_routes.router)
        app.include_router(autonomous_routes.router)
        app.include_router(upload_routes.router)
        app.include_router(log_routes.router)
        app.include_router(mykey_routes.router)
        app.include_router(notify_routes.router)

    _mount_static(app)
    return app


app = create_app()
