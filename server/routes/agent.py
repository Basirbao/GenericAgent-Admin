"""Agent + LLM routes."""
from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect

from ..schemas import ChatRetryConfigReq, ChatSubmit, LLMSwitch
from ..services.agent_service import AgentService
from ..services.chat_retry import load_chat_retry_config, save_chat_retry_config
from ..services.event_bus import bus

log = logging.getLogger(__name__)
router = APIRouter()


def svc() -> AgentService:
    return AgentService.instance()


@router.get("/api/agent/status")
async def status():
    return svc().status().__dict__


@router.post("/api/agent/abort")
async def abort():
    svc().abort()
    return {"ok": True}


@router.post("/api/agent/new")
async def new_conv():
    msg = svc().new_conversation()
    return {"ok": True, "message": msg}


@router.get("/api/agent/history")
async def history():
    return {"history": svc().get_history()}


@router.get("/api/agent/chat-retry-config")
async def get_chat_retry_config():
    return load_chat_retry_config().to_dict()


@router.put("/api/agent/chat-retry-config")
async def put_chat_retry_config(req: ChatRetryConfigReq):
    return save_chat_retry_config(req.model_dump()).to_dict()


@router.get("/api/agent/sessions")
async def sessions():
    """Recoverable model_responses snapshots (used by /continue)."""
    from frontends.continue_cmd import list_sessions
    out = list_sessions()
    return {
        "sessions": [
            {"path": p, "mtime": int(m), "preview": preview, "rounds": n}
            for (p, m, preview, n) in out
        ]
    }


@router.post("/api/agent/sessions/{idx}/restore")
async def restore_session(idx: int):
    from frontends.continue_cmd import list_sessions, restore
    sessions = list_sessions()
    if idx < 0 or idx >= len(sessions):
        raise HTTPException(404, "session index out of range")
    s = svc()
    msg, full = restore(s.agent, sessions[idx][0])
    # Reset live chat snapshots — the agent's history is now a different
    # conversation, so any in-flight UI bubbles would be misleading.
    with s._lock:
        s._snapshots.clear()
    bus.publish("chat:reset", {"reason": "session_restored"})
    return {"ok": True, "message": msg, "full": full}


# ── LLMs ─────────────────────────────────────────────────────────
@router.get("/api/llms")
async def list_llms():
    return {"llms": svc().list_llms()}


@router.post("/api/llms/switch")
async def switch_llm(req: LLMSwitch):
    try:
        return svc().switch_llm(req.index)
    except RuntimeError as e:
        raise HTTPException(409, str(e))


@router.post("/api/llms/{idx}/test")
async def test_llm(idx: int):
    """Fire a tiny ping at the LLM at index `idx`.

    We bypass the agent's real history: the underlying backend session has
    a `history` list we save + restore so the test message never lands in
    the user's conversation. tools=None so we don't pay the schema cost.

    Returns: {ok, latency_ms, preview, model, error?}
    """
    import time
    s = svc()
    clients = getattr(s.agent, "llmclients", None)
    if clients is None or idx < 0 or idx >= len(clients):
        raise HTTPException(404, f"llm index out of range: {idx}")

    client = clients[idx]
    backend = getattr(client, "backend", None)
    if backend is None:
        return {"ok": False, "error": "client has no backend"}

    saved_history = list(getattr(backend, "history", []))
    saved_tools = getattr(backend, "tools", None)
    try:
        # Best-effort reset to neutral; not every backend has these.
        if hasattr(backend, "history"): backend.history = []
        if hasattr(backend, "tools"): backend.tools = None

        start = time.time()
        # client.chat is a generator — exhaust it. We do this in a thread
        # because raw_ask issues a blocking HTTP request.
        messages = [
            {"role": "system", "content": "You are a connectivity probe. Reply with exactly one word: pong."},
            {"role": "user", "content": "ping"},
        ]
        text = ""
        try:
            gen = client.chat(messages=messages, tools=None)
            # `gen` may yield streaming chunks then return a response object.
            # For the test we only care that *something* came back without
            # exception, plus the first ~80 chars as a preview.
            for chunk in gen:
                if isinstance(chunk, str): text += chunk
                if len(text) > 80: break
        except StopIteration as si:
            resp = si.value
            text = (getattr(resp, "content", "") or "")[:80]
        elapsed_ms = int((time.time() - start) * 1000)
        return {
            "ok": True,
            "latency_ms": elapsed_ms,
            "preview": (text or "").strip()[:120],
            "model": s.agent.get_llm_name(client, model=True),
            "name": s.agent.get_llm_name(client),
        }
    except Exception as e:
        return {
            "ok": False,
            "error": f"{type(e).__name__}: {e}",
            "name": s.agent.get_llm_name(client) if client else "?",
        }
    finally:
        try:
            if hasattr(backend, "history"): backend.history = saved_history
            if hasattr(backend, "tools"): backend.tools = saved_tools
        except Exception:
            pass


# ── chat WebSocket ───────────────────────────────────────────────
# Architecture:
#   • All submissions (webui, autonomous, wechat, reflect) flow through
#     AgentService.submit, which spawns a fan-out drainer that publishes
#     chat:* events to the bus.
#   • This WS subscribes to chat:* and forwards to the client. So a single
#     socket sees *everything* the agent does — autonomous-evolution
#     triggers show up alongside user prompts.
#   • On connect we send AgentService.chat_state_snapshot() as a single
#     {type: 'snapshot', streams: [...]} message so a reconnecting tab
#     rebuilds in-flight + recent state atomically.
@router.websocket("/ws/chat")
async def ws_chat(ws: WebSocket):
    await ws.accept()
    s = svc()

    # 1. Send current chat state snapshot so a tab-switch / reload doesn't
    #    lose the running conversation.
    try:
        await ws.send_json({"type": "snapshot", "streams": s.chat_state_snapshot()})
    except Exception:
        log.exception("ws_chat snapshot failed")

    # 2. Subscribe to chat:* events and forward to this socket.
    async def _forward():
        async for evt in bus.subscribe("chat:"):
            topic = evt.topic.split(":", 1)[1] if ":" in evt.topic else evt.topic
            msg = {"type": topic, **evt.payload}
            try:
                await ws.send_json(msg)
            except Exception:
                return

    forward_task = asyncio.create_task(_forward())

    # 3. Handle incoming submit/abort/ping.
    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except Exception:
                await ws.send_json({"type": "error", "error": "bad_json"})
                continue
            mt = msg.get("type")
            if mt == "submit":
                payload = ChatSubmit(**{k: v for k, v in msg.items() if k != "type"})
                # Just kick it off — events come back via bus subscription above.
                s.submit(payload.text, source=payload.source, images=payload.images)
            elif mt == "abort":
                s.abort()
                # The agent's run loop emits a final {'done': ...} which the
                # fan-out drainer turns into a chat:done frame.
            elif mt == "ping":
                await ws.send_json({"type": "pong"})
            else:
                await ws.send_json({"type": "error", "error": f"unknown_type:{mt}"})
    except WebSocketDisconnect:
        return
    except Exception as e:
        log.exception("ws_chat crashed: %s", e)
        try:
            await ws.send_json({"type": "error", "error": str(e)})
        except Exception:
            pass
    finally:
        forward_task.cancel()
        try:
            await forward_task
        except Exception:
            pass
