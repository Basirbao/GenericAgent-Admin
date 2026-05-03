"""AgentService — singleton wrapper around GeneraticAgent.

Owns one GeneraticAgent instance for the whole web backend. Bridges its
blocking ``put_task → display_queue.get()`` flow to async consumers via
asyncio.to_thread polling.

Imports `agentmain` and `frontends.continue_cmd` from the GenericAgent
project at the path resolved by ``_paths.GA_ROOT``. This module fails
fast if GA_ROOT is not configured — the backend should run in setup mode
in that case (see server.main).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import queue as _q
import re as _re
import sys
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, AsyncIterator

from .. import _paths

if _paths.GA_ROOT is None:
    raise RuntimeError("AgentService imported before GA_ROOT is configured")

from agentmain import GeneraticAgent  # noqa: E402  (resolved via _paths sys.path)
from frontends.continue_cmd import install as install_continue, reset_conversation  # noqa: E402

from .event_bus import bus  # noqa: E402

log = logging.getLogger(__name__)


# ── Suppress GA-spawned subprocess UI side-effects (per-platform) ────────────
def _patch_ga_subprocess() -> None:
    """Patch ``ga.subprocess.Popen`` so ``code_run`` doesn't pollute the UI.

    Two distinct problems on two platforms, same patch site (GA's module-
    level ``import subprocess``, see ``ga.py:4`` and the ``subprocess.Popen``
    call at ``ga.py:52``):

    **Windows** — ``ga.py:code_run`` builds Popen with only
    ``startupinfo.wShowWindow = SW_HIDE``. SW_HIDE is read by the child
    *after* conhost has already attached a console. When the parent
    (this server, started by pythonw / a no-console frozen exe) lacks a
    console of its own, Windows allocates a fresh one for the child and
    the user sees a black window flash on every code_run. Adding
    ``CREATE_NO_WINDOW`` (0x08000000) tells the OS not to allocate the
    console at all, which is the actual fix.

    **macOS frozen .app** — ``ga.py:27`` builds
    ``[sys.executable, "-X", "utf8", "-u", tmp]`` to run user Python.
    In a PyInstaller-frozen ``.app`` build, ``sys.executable`` points at
    the bundle's Mach-O launcher, not Python. The launcher inherits the
    GUI ``Info.plist`` so each spawn shows up as a fresh Dock icon, and
    ``-X``/``-u`` aren't understood — argv just gets fed back into
    ``launch_webui.pyw:main`` which fails to run the user's code. Fix:
    rewrite ``argv[0]`` to a real ``python3`` interpreter discovered by
    ``_paths.discover_user_python()``. We don't touch the helper-app
    path because user code routinely imports user-pip-installed packages
    (numpy, requests, …) that the frozen interpreter doesn't have.

    Patching strategy is the same on both: override the ``Popen``
    reference inside ``ga``'s module namespace at admin-startup time, so
    the GA repo stays untouched on disk and ``git pull`` on GA never
    conflicts with admin.
    """
    if os.name == "nt":
        try:
            import ga as _ga  # type: ignore  # resolved via GA sys.path
            import subprocess as _sp
            _orig_popen = _sp.Popen
            CREATE_NO_WINDOW = 0x08000000

            def _no_window_popen(*args, **kwargs):
                cf = kwargs.get("creationflags") or 0
                kwargs["creationflags"] = cf | CREATE_NO_WINDOW
                return _orig_popen(*args, **kwargs)

            _ga.subprocess.Popen = _no_window_popen
            log.info("patched ga.subprocess.Popen with CREATE_NO_WINDOW")
        except Exception:
            log.exception("could not patch ga.subprocess.Popen — code_run may flash console windows")
        return

    if sys.platform != "darwin" or not getattr(sys, "frozen", False):
        # Dev mode (running from source) on macOS or Linux: sys.executable
        # is already a real Python interpreter. Nothing to fix.
        return

    # macOS frozen .app: swap argv[0] off the bundle launcher.
    from .. import _paths
    real_python = _paths.discover_user_python()
    if not real_python:
        log.error(
            "macOS frozen build: no system python3 found for GA code_run. "
            "Install Python 3.10+ (https://www.python.org) or set $GA_PYTHON. "
            "code_run will fall back to sys.executable and likely fail."
        )
        return
    try:
        import ga as _ga  # type: ignore  # resolved via GA sys.path
        import subprocess as _sp
        _orig_popen = _sp.Popen
        _frozen_exe = sys.executable

        def _swap_popen(*args, **kwargs):
            cmd = args[0] if args else kwargs.get("args")
            if isinstance(cmd, (list, tuple)) and cmd and cmd[0] == _frozen_exe:
                new_cmd = [real_python, *cmd[1:]]
                if args:
                    args = (new_cmd, *args[1:])
                else:
                    kwargs["args"] = new_cmd
            return _orig_popen(*args, **kwargs)

        _ga.subprocess.Popen = _swap_popen
        log.info("patched ga.subprocess.Popen to use %s for code_run", real_python)
    except Exception:
        log.exception("could not patch ga.subprocess.Popen for macOS frozen build")


_patch_ga_subprocess()


@dataclass
class AgentStatus:
    is_running: bool
    llm_no: int
    llm_name: str
    llm_model: str
    last_reply_time: int
    queued_tasks: int
    history_lines: int


@dataclass
class StreamHandle:
    """A live agent task stream. Multiple WS clients may attach to the same stream."""
    stream_id: str
    display_queue: "_q.Queue"
    started_at: float = field(default_factory=time.time)
    finished: bool = False
    last_chunk: str = ""
    final_text: str = ""


# ── chat replay snapshot (so a /ws/chat client that reconnects after a tab
#    switch can rebuild the running conversation) ───────────────────────
@dataclass
class ChatSnapshot:
    stream_id: str
    source: str
    query: str
    started_at: float
    content: str = ""           # latest cumulative assistant text
    done: bool = False
    finished_at: float = 0.0
    aborted: bool = False


class AgentService:
    _instance: "AgentService | None" = None
    _SNAPSHOT_CAP = 20  # keep last N submissions for /ws/chat replay

    def __init__(self) -> None:
        # Patch /continue and /new before instantiating
        install_continue(GeneraticAgent)
        self.agent = GeneraticAgent()
        # Default to incremental output for WS streaming
        self.agent.inc_out = False
        self.agent.verbose = False
        self._streams: dict[str, StreamHandle] = {}
        # Per-stream UI snapshot (LRU-capped) for replay on reconnect
        self._snapshots: "OrderedDict[str, ChatSnapshot]" = OrderedDict()
        self._lock = threading.Lock()
        self._next_id = 0
        self._run_thread: threading.Thread | None = None

        # last_reply_time may be missing on older agentmain.py — patch defensively.
        if not hasattr(self.agent, "last_reply_time"):
            self.agent.last_reply_time = int(time.time())

        # Wire turn_end_hook to broadcast events
        if not hasattr(self.agent, "_turn_end_hooks"):
            self.agent._turn_end_hooks = {}
        self.agent._turn_end_hooks["webui"] = self._on_turn_end

        # User-preferred LLM. Persisted to admin config so it survives restarts;
        # also used to detect (and log) drift caused by other call sites
        # (autonomous tasks, /llm wechat command, code_run inline_eval, etc.)
        self._wrap_next_llm_with_persistence()
        self._restore_preferred_llm()

    # ── lifecycle ────────────────────────────────────────────────
    @classmethod
    def instance(cls) -> "AgentService":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def start_run_thread(self) -> None:
        if self._run_thread and self._run_thread.is_alive():
            return
        self._run_thread = threading.Thread(target=self.agent.run, daemon=True, name="agent-run")
        self._run_thread.start()
        log.info("agent run thread started")

    # ── status ───────────────────────────────────────────────────
    def status(self) -> AgentStatus:
        a = self.agent
        try:
            llm_name = a.get_llm_name()
            llm_model = a.get_llm_name(model=True)
        except Exception:
            llm_name = "unknown"
            llm_model = "unknown"
        return AgentStatus(
            is_running=bool(a.is_running),
            llm_no=int(a.llm_no),
            llm_name=llm_name,
            llm_model=llm_model,
            last_reply_time=int(getattr(a, "last_reply_time", 0)),
            queued_tasks=a.task_queue.qsize() if hasattr(a, "task_queue") else 0,
            history_lines=len(a.history) if hasattr(a, "history") else 0,
        )

    def list_llms(self) -> list[dict]:
        out = []
        for i, name, current in self.agent.list_llms():
            out.append({"index": int(i), "name": name, "current": bool(current)})
        return out

    def switch_llm(self, n: int) -> dict:
        self.agent.next_llm(int(n))
        # Mark this as the user's explicit preference (separate from the
        # transient agent.llm_no which any code path can mutate).
        self._save_preferred_llm(int(self.agent.llm_no))
        return {"llm_no": self.agent.llm_no, "name": self.agent.get_llm_name()}

    # ── llm preference persistence ───────────────────────────────
    def _save_preferred_llm(self, n: int) -> None:
        try:
            cfg = _paths.load_config()
            if cfg.get("preferred_llm_no") == n:
                return
            cfg["preferred_llm_no"] = int(n)
            _paths.save_config(cfg)
            log.info("preferred_llm_no persisted: %s", n)
        except Exception as e:
            log.warning("failed to persist preferred_llm_no=%s: %s", n, e)

    def _restore_preferred_llm(self) -> None:
        try:
            saved = _paths.load_config().get("preferred_llm_no")
            if saved is None:
                return
            n = int(saved)
            clients = getattr(self.agent, "llmclients", None) or []
            if n < 0 or n >= len(clients):
                log.info("preferred_llm_no=%s out of range (have %d), skipping", saved, len(clients))
                return
            if int(self.agent.llm_no) == n:
                return
            self.agent.next_llm(n)
            log.info("restored preferred_llm_no=%s (%s)", n, self.agent.get_llm_name())
        except Exception as e:
            log.warning("failed to restore preferred llm: %s", e)

    def _wrap_next_llm_with_persistence(self) -> None:
        """Monkey-patch ``agent.next_llm`` so any caller — admin, wechat slash
        command, autonomous SOP via code_run inline_eval, etc. — surfaces the
        change in admin logs and (optionally) updates the user-preferred slot.

        We only persist on USER-initiated changes (``switch_llm`` calls this
        helper directly via ``_save_preferred_llm``). For other sources we
        just log so the user can spot unexpected drift.
        """
        import inspect, traceback
        original = self.agent.next_llm
        # Already wrapped (singleton may be re-init'd in tests)
        if getattr(original, "_admin_wrapped", False):
            return

        def _wrapped(n: int = -1):
            before = int(getattr(self.agent, "llm_no", 0))
            ret = original(n)
            after = int(self.agent.llm_no)
            if before != after:
                # Capture caller frame for diagnostic — skip our own frames.
                stack = traceback.extract_stack(limit=8)[:-1]
                tail = " <- ".join(f"{os.path.basename(f.filename)}:{f.lineno}" for f in stack[-4:])
                try: name = self.agent.get_llm_name()
                except Exception: name = "?"
                log.info("agent.llm_no %s → %s (%s)  caller: %s", before, after, name, tail)
            return ret

        _wrapped._admin_wrapped = True  # type: ignore[attr-defined]
        self.agent.next_llm = _wrapped  # type: ignore[assignment]

    def abort(self) -> None:
        self.agent.abort()
        bus.publish("agent:abort", {"ts": time.time()})
        # The agent's run loop will emit a final {'done': ...} which our
        # fan-out drainer turns into a chat:done — we deliberately don't
        # publish a parallel chat:aborted to keep the event stream simple.

    # ── tasks ────────────────────────────────────────────────────
    def submit(self, query: str, *, source: str = "user", images: list[str] | None = None) -> StreamHandle:
        """Enqueue a task; spawn a single fan-out drainer that publishes
        chat:* events to the bus AND keeps the StreamHandle's display_queue
        full for legacy sync consumers (wechat_service)."""
        # User-initiated submissions reassert the persisted preference so that
        # autonomous tasks (or other call sites) can't strand the user on a
        # different LLM than they picked. Other sources (autonomous, wechat,
        # reflect) keep whatever llm_no is currently active.
        if source in ("user", "webui"):
            self._restore_preferred_llm()
        with self._lock:
            self._next_id += 1
            sid = f"s{self._next_id:08d}"
        # The agent's own queue (drained by our fan-out below).
        src_q = self.agent.put_task(query, source=source, images=images or [])
        # The handle queue we hand to callers (kept in sync by the drainer).
        out_q: "_q.Queue" = _q.Queue()
        h = StreamHandle(stream_id=sid, display_queue=out_q)
        snap = ChatSnapshot(
            stream_id=sid, source=source, query=query or "",
            started_at=time.time(),
        )
        with self._lock:
            self._streams[sid] = h
            self._snapshots[sid] = snap
            # LRU eviction
            while len(self._snapshots) > self._SNAPSHOT_CAP:
                self._snapshots.popitem(last=False)
        bus.publish("agent:submit", {"stream_id": sid, "source": source, "query_preview": (query or "")[:120]})
        bus.publish("chat:started", {
            "stream_id": sid, "source": source,
            "query": query or "",
            "ts": snap.started_at,
        })
        threading.Thread(
            target=self._fanout, args=(src_q, out_q, h, snap),
            daemon=True, name=f"agent-fanout-{sid}",
        ).start()
        return h

    def _fanout(self, src_q: "_q.Queue", out_q: "_q.Queue", h: StreamHandle, snap: ChatSnapshot) -> None:
        """Drain the agent's display_queue. For each item:
          - mirror to the StreamHandle's queue (for legacy sync consumers).
          - update the snapshot.
          - publish chat:next / chat:done to the bus.
        """
        try:
            while True:
                try:
                    item = src_q.get(timeout=300)
                except _q.Empty:
                    # Liveness ping so subscribers know we're still here.
                    bus.publish("chat:heartbeat", {"stream_id": h.stream_id})
                    continue
                # mirror first so wechat_service keeps working
                out_q.put(item)
                if "next" in item:
                    content = item["next"]
                    h.last_chunk = content
                    snap.content = content
                    bus.publish("chat:next", {
                        "stream_id": h.stream_id,
                        "source": snap.source,
                        "content": content,
                    })
                if "done" in item:
                    content = item["done"]
                    h.final_text = content
                    h.finished = True
                    snap.content = content
                    snap.done = True
                    snap.finished_at = time.time()
                    self.agent.last_reply_time = int(time.time())
                    bus.publish("chat:done", {
                        "stream_id": h.stream_id,
                        "source": snap.source,
                        "content": content,
                    })
                    bus.publish("agent:done", {"stream_id": h.stream_id, "len": len(content)})
                    return
        except Exception as e:
            log.exception("fanout crashed for %s: %s", h.stream_id, e)
            snap.done = True
            snap.aborted = True
            snap.finished_at = time.time()
            bus.publish("chat:done", {
                "stream_id": h.stream_id,
                "source": snap.source,
                "content": snap.content + f"\n[stream error: {e}]",
            })

    # ── replay (used by /ws/chat on connect) ────────────────────
    def chat_state_snapshot(self) -> list[dict]:
        """Return a flat list of recent / in-flight chat streams so a
        reconnecting WS client can rebuild its UI atomically.

        Order is insertion order (oldest → newest). Each entry is a single
        stream's full state — frontend replaces its message list with this.
        """
        out: list[dict] = []
        with self._lock:
            snaps = list(self._snapshots.values())
        for snap in snaps:
            content = snap.content
            out.append({
                "stream_id": snap.stream_id,
                "source": snap.source,
                "query": snap.query,
                "content": content,
                "done": snap.done,
                "started_at": snap.started_at,
                "finished_at": snap.finished_at,
            })
        return out

    async def stream(self, h: StreamHandle, *, poll_interval: float = 0.4) -> AsyncIterator[dict]:
        """Async generator that yields {next:...} chunks then a final {done:...}."""
        while True:
            try:
                item = await asyncio.to_thread(h.display_queue.get, True, poll_interval)
            except _q.Empty:
                # heartbeat
                yield {"type": "heartbeat", "stream_id": h.stream_id}
                continue
            if "next" in item:
                h.last_chunk = item["next"]
                yield {"type": "next", "stream_id": h.stream_id, "content": item["next"], "source": item.get("source")}
            if "done" in item:
                h.final_text = item["done"]
                h.finished = True
                self.agent.last_reply_time = int(time.time())
                bus.publish("agent:done", {"stream_id": h.stream_id, "len": len(item["done"])})
                yield {"type": "done", "stream_id": h.stream_id, "content": item["done"], "source": item.get("source")}
                return

    # ── conversation control ────────────────────────────────────
    def new_conversation(self) -> str:
        # Persist the soon-to-be-discarded WebUI conversation into
        # memory/chat_history.json so it shows up under "对话管理".
        # Without this hook chat_history.json is only ever written by the
        # Qt desktop app (frontends/qtapp.py), and Web sessions stay invisible
        # in the management view forever.
        try:
            self._archive_snapshots_to_chat_history()
        except Exception as e:
            log.warning("archive snapshots to chat_history.json failed: %s", e)
        # Wipe per-stream UI snapshots so a reconnecting WS doesn't replay
        # stale bubbles from the previous conversation.
        with self._lock:
            self._snapshots.clear()
        bus.publish("chat:reset", {"reason": "new_conversation"})
        return reset_conversation(self.agent)

    # ---- helpers for archive on /new ----
    @staticmethod
    def _strip_webui_prompt_artifacts(s: str) -> str:
        """Remove the file-marker scaffolding LiveChat appends to user prompts
        so saved messages read like what the user actually typed."""
        if not s:
            return ""
        # Drop the 'If you need to show files...' preamble injected by LiveChat
        s = _re.sub(
            r"^If you need to show files to user, use \[FILE:filepath\] in your response\.\s*",
            "",
            s,
        )
        # Drop trailing "[用户发送文件: <path>]" markers, possibly multiple
        s = _re.sub(r"(?:\n|^)\[用户发送文件:[^\]]*\]\s*", "", s)
        return s.strip()

    def _archive_snapshots_to_chat_history(self) -> None:
        """Dump the current chat snapshots as one new entry in chat_history.json.

        Schema matches what frontends/qtapp.py writes (so the same file is
        readable by the Qt app and by /api/conversations):

            {"id", "title", "messages": [{role, content}, ...], "updatedAt"}
        """
        with self._lock:
            snaps = [s for s in self._snapshots.values() if s.done and (s.query or s.content)]
        if not snaps:
            return  # nothing meaningful to save

        messages: list[dict] = []
        first_user_text = ""
        for snap in snaps:
            user_text = self._strip_webui_prompt_artifacts(snap.query)
            if user_text:
                messages.append({"role": "user", "content": user_text})
                if not first_user_text:
                    first_user_text = user_text
            if snap.content:
                messages.append({"role": "assistant", "content": snap.content})
        if not messages:
            return

        title = (first_user_text[:30].replace("\n", " ") or "Web 对话")
        if len(first_user_text) > 30:
            title += "…"
        entry = {
            "id": datetime.now().strftime("%Y%m%d_%H%M%S_%f"),
            "title": title,
            "messages": messages,
            "updatedAt": datetime.now().isoformat(),
            "source": "webui",
        }

        hf = str(_paths.memory_dir() / "chat_history.json")
        all_: list[dict] = []
        if os.path.isfile(hf):
            try:
                with open(hf, encoding="utf-8") as f:
                    loaded = json.load(f)
                if isinstance(loaded, list):
                    all_ = loaded
            except Exception as e:
                log.warning("chat_history.json unreadable, starting fresh: %s", e)
        all_.append(entry)

        os.makedirs(os.path.dirname(hf), exist_ok=True)
        tmp = hf + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(all_, f, ensure_ascii=False, indent=2)
        os.replace(tmp, hf)
        log.info("archived webui conversation %s (%d msgs) to chat_history.json",
                 entry["id"], len(messages))

    def get_history(self) -> list[str]:
        return list(getattr(self.agent, "history", []))

    # ── hooks ────────────────────────────────────────────────────
    def _on_turn_end(self, ctx: dict) -> None:
        bus.publish("agent:turn", {
            "turn": ctx.get("turn"),
            "summary": ctx.get("summary"),
            "exit_reason": ctx.get("exit_reason"),
        })


# convenience accessor
def get_agent_service() -> AgentService:
    return AgentService.instance()
