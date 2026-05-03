"""GenericAgent Web 管理后台 · 一键启动入口

启动顺序：
  1. 检查是否已配置 GA_ROOT。未配置 → pywebview 弹文件夹选择 → 保存配置
  2. 自动收尸：如果 8765/8766 还卡着前一次的僵尸进程（Ctrl+C 异常退出
     等情况），且当前端口持有者不响应 /api/status，则强制 kill 之
  3. 拉起 server.run 子进程（FastAPI + agent + 微信轮询 + 自主调度器）
  4. 等待 /api/status 200 OK
  5. 决定 UI 来源：
        · webui/dist 已构建  → 单端口模式，http://127.0.0.1:8765
        · 否则尝试 vite dev   → 双端口模式，http://localhost:5173
  6. 用 pywebview 打开原生窗口
  7. 窗口关闭/收到 SIGINT/SIGTERM 时清理所有子进程

不依赖任何全局命令；唯一前置条件是 `pip install -e .` + 前端构建。
"""
from __future__ import annotations

import atexit
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

WINDOW_WIDTH, WINDOW_HEIGHT = 1320, 860


def _resource_root() -> Path:
    """Return the directory containing bundled resources (webui/dist, server/).

    Under PyInstaller (frozen), bundled data lives at ``sys._MEIPASS``.
    In dev mode it's the directory of this script.
    """
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS)  # type: ignore[attr-defined]
    return Path(os.path.dirname(os.path.abspath(__file__)))


SCRIPT_DIR = _resource_root()
WEBUI_DIR = SCRIPT_DIR / "webui"
DIST_INDEX = WEBUI_DIR / "dist" / "index.html"


def _helper_executable() -> str | None:
    """Return path to the nested Helper.app's Mach-O launcher, or None.

    On macOS frozen builds the main bundle ships
    ``GenericAgent Admin.app/Contents/Frameworks/GenericAgent Admin Helper.app/``
    whose ``Info.plist`` sets ``LSUIElement=True``. Spawning the backend
    via the helper's binary instead of ``sys.executable`` keeps the FastAPI
    subprocess off the Dock — the main app stays the only visible icon.

    Returns ``None`` outside frozen-mac, or when the nested helper isn't
    present (e.g. ``pyinstaller`` without the helper BUNDLE block, or a
    dev run). Callers should fall back to ``sys.executable`` in that case.
    """
    if not (sys.platform == "darwin" and getattr(sys, "frozen", False)):
        return None
    try:
        main_macos = Path(sys.executable).resolve().parent             # …/Contents/MacOS
        contents = main_macos.parent                                   # …/Contents
        helper_app = contents / "Frameworks" / "GenericAgent Admin Helper.app"
        helper_bin = helper_app / "Contents" / "MacOS" / Path(sys.executable).name
        return str(helper_bin) if helper_bin.is_file() else None
    except Exception:
        return None

BACKEND_HOST = "127.0.0.1"
BACKEND_PORT = 8765
LOCK_PORT = BACKEND_PORT + 1   # matches server/run.py:_ensure_single_instance
BACKEND_URL = f"http://{BACKEND_HOST}:{BACKEND_PORT}"
DEV_URL = "http://localhost:5173"


# ── helpers ─────────────────────────────────────────────────────
def _have(cmd: str) -> bool:
    return shutil.which(cmd) is not None


def _wait_url(url: str, timeout: float = 30.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urlopen(url, timeout=1.5).read()
            return True
        except URLError:
            time.sleep(0.4)
        except Exception:
            time.sleep(0.4)
    return False


def _popen(cmd: list[str], cwd: Path, env_extra: dict | None = None,
           log_path: Path | None = None) -> subprocess.Popen:
    kwargs: dict = {"cwd": str(cwd)}
    if env_extra:
        env = os.environ.copy()
        env.update(env_extra)
        kwargs["env"] = env
    if os.name == "nt":
        kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
    if log_path is not None:
        # In frozen .app / .pyw builds stdout & stderr go to /dev/null —
        # impossible to debug post-mortem. Pipe both to a rolling log file
        # in ADMIN_DATA so we always have a traceback to read after a 500.
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            f = open(log_path, "ab", buffering=0)
            kwargs["stdout"] = f
            kwargs["stderr"] = f
        except Exception as e:
            print(f"[Launch] could not open backend log {log_path}: {e}", file=sys.stderr)
    return subprocess.Popen(cmd, **kwargs)


# Windows-only: extra Popen kwargs to suppress the brief console window that
# pops up when pythonw (.pyw, no parent console) spawns a console subprocess
# like netstat / taskkill / lsof. CREATE_NO_WINDOW = 0x08000000.
_NO_WINDOW: dict = {"creationflags": 0x08000000} if os.name == "nt" else {}


def _check_python_deps() -> tuple[bool, str]:
    try:
        import fastapi  # noqa: F401
        import uvicorn  # noqa: F401
        return True, ""
    except ImportError as e:
        return False, (
            "Web admin Python deps missing.\n"
            f"  Reason: {e}\n"
            "Run:  pip install -e .\n"
            "      (or use install_webui.sh / install_webui.bat)"
        )


def _backend_alive(url: str = BACKEND_URL) -> bool:
    try:
        urlopen(f"{url}/api/status", timeout=1.5).read()
        return True
    except Exception:
        return False


# ── stale-instance cleanup ──────────────────────────────────────
def _find_port_holders(port: int) -> list[int]:
    """Return PIDs of processes LISTENing on ``port`` (de-duplicated, in
    discovery order). Uses ``lsof`` on macOS/Linux and ``netstat -ano`` on
    Windows. Returns ``[]`` if the port is free or the lookup tool isn't
    available — we never raise.
    """
    pids: list[int] = []
    try:
        if os.name == "nt":
            out = subprocess.check_output(
                ["netstat", "-ano"],
                stderr=subprocess.DEVNULL, text=True, timeout=3,
                **_NO_WINDOW,
            )
            needle = f":{port}"
            for line in out.splitlines():
                parts = line.split()
                if (len(parts) >= 5 and parts[0].upper() == "TCP"
                        and parts[1].endswith(needle)
                        and parts[3].upper() == "LISTENING"):
                    try:
                        pids.append(int(parts[4]))
                    except ValueError:
                        pass
        else:
            out = subprocess.check_output(
                ["lsof", f"-tiTCP:{port}", "-sTCP:LISTEN"],
                stderr=subprocess.DEVNULL, text=True, timeout=3,
            )
            for line in out.splitlines():
                line = line.strip()
                if line:
                    try:
                        pids.append(int(line))
                    except ValueError:
                        pass
    except subprocess.CalledProcessError:
        pass  # lsof returns non-zero when nothing matches
    except FileNotFoundError:
        # lsof / netstat not installed — give up silently, the bind error
        # in server/run.py still tells the user what to do manually.
        pass
    except Exception as e:
        print(f"[Launch] _find_port_holders({port}) error: {e}", file=sys.stderr)
    seen: set[int] = set()
    uniq: list[int] = []
    for p in pids:
        if p == os.getpid() or p in seen:
            continue
        seen.add(p)
        uniq.append(p)
    return uniq


def _kill_pid(pid: int) -> bool:
    """SIGTERM, wait up to 1.5 s, SIGKILL. Returns True if the process is
    gone afterwards. Never touches our own PID."""
    if pid == os.getpid():
        return False
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                timeout=5,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                **_NO_WINDOW,
            )
            return True
        os.kill(pid, signal.SIGTERM)
        for _ in range(15):  # 1.5 s
            time.sleep(0.1)
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return True
        os.kill(pid, signal.SIGKILL)
        time.sleep(0.2)
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        return False
    except ProcessLookupError:
        return True
    except PermissionError as e:
        print(f"[Launch] kill {pid} denied: {e}", file=sys.stderr)
        return False
    except Exception as e:
        print(f"[Launch] kill {pid} error: {e}", file=sys.stderr)
        return False


def _cleanup_stale_backend() -> None:
    """If our backend ports are held but the existing instance is unhealthy
    (no /api/status response), force-kill the holders so we can rebind.

    A *healthy* prior instance is left alone — ``main()`` will reuse it via
    ``_backend_alive()``. This means:
      • clean Ctrl+C exits → ports already free → no-op.
      • zombie / wedged backend → killed automatically.
      • another tab already running fine → reused, never killed.
    """
    if _backend_alive():
        return  # healthy — reuse, don't touch
    holders = list(dict.fromkeys(
        _find_port_holders(BACKEND_PORT) + _find_port_holders(LOCK_PORT)
    ))
    if not holders:
        return
    print(
        f"[Launch] stale backend on port {BACKEND_PORT}/{LOCK_PORT} "
        f"(PIDs {holders}); cleaning up…",
        file=sys.stderr,
    )
    for pid in holders:
        if _kill_pid(pid):
            print(f"[Launch]   killed PID {pid}")
        else:
            print(f"[Launch]   could NOT kill PID {pid}", file=sys.stderr)
    # Wait up to 3 s for the OS to actually free the ports.
    for _ in range(30):
        if not _find_port_holders(BACKEND_PORT) and not _find_port_holders(LOCK_PORT):
            return
        time.sleep(0.1)
    print(
        f"[Launch] ports {BACKEND_PORT}/{LOCK_PORT} still busy after kill; "
        f"continuing — server.run may still error",
        file=sys.stderr,
    )


# ── GA root setup ───────────────────────────────────────────────
def _ensure_ga_root() -> str | None:
    """Make sure server has a valid GA root. If not, prompt user with a
    native folder picker (via pywebview). Returns the resolved path, or
    None if user cancelled / setup mode should run instead.

    Falls back to setup-mode (returns "") when pywebview isn't available
    so the SPA setup page can collect the path interactively.
    """
    sys.path.insert(0, str(SCRIPT_DIR))
    from server import _paths

    ga = _paths.discover_ga_root()
    if ga is not None:
        print(f"[Launch] GA_ROOT = {ga}")
        return str(ga)

    print("[Launch] GA_ROOT not configured; prompting for folder…")
    try:
        import webview  # type: ignore
    except ImportError:
        print(
            "[Launch] pywebview not available; backend will start in setup mode.\n"
            "         Use the SPA setup page to pick a directory.",
            file=sys.stderr,
        )
        return ""  # let setup-mode happen

    chosen: list[str] = []

    def _on_load(window):
        try:
            sel = window.create_file_dialog(
                webview.FOLDER_DIALOG,
                allow_multiple=False,
                directory=str(Path.home() / "Desktop"),
            )
            if sel and sel[0]:
                chosen.append(sel[0])
        finally:
            window.destroy()

    win = webview.create_window(
        title="GenericAgent · 选择项目目录",
        html="""
        <html><body style='font-family:system-ui;background:#0b0d10;color:#e2e8f0;
        display:flex;align-items:center;justify-content:center;height:100vh;margin:0'>
        <div style='text-align:center'>
        <h2 style='margin:0 0 10px'>请选择 GenericAgent 项目目录</h2>
        <p style='color:#94a3b8;font-size:14px'>必须包含 agentmain.py 与 memory/</p>
        <p style='color:#64748b;font-size:12px'>正在打开文件夹选择器…</p>
        </div></body></html>
        """,
        width=560, height=320, resizable=False,
    )
    webview.start(_on_load, win)

    if not chosen:
        print("[Launch] 用户取消；进入 setup 模式（可在 UI 内补选）。", file=sys.stderr)
        return ""

    try:
        p = _paths.set_ga_root(chosen[0])
        print(f"[Launch] saved ga_root = {p}")
        return str(p)
    except ValueError as e:
        print(f"[Launch] {e}", file=sys.stderr)
        try:
            import webview as _wv
            _wv.create_window(
                title="GenericAgent · 路径无效",
                html=(
                    f"<pre style='padding:24px;font-family:system-ui;color:#ef4444'>{e}</pre>"
                    f"<p style='padding:0 24px;color:#94a3b8'>窗口将进入 setup 模式，"
                    f"你可以在 UI 中重新选择。</p>"
                ),
                width=560, height=240, resizable=False,
            )
            _wv.start()
        except Exception:
            pass
        return ""  # let setup-mode happen


# ── main ────────────────────────────────────────────────────────
def main() -> int:
    # Dual-mode dispatch. The launcher and the backend share one binary in
    # frozen builds (PyInstaller produces a single .app/.exe and we re-spawn
    # ourselves with this flag instead of forking a Python interpreter,
    # because sys.executable points at the frozen binary, not Python).
    # In dev mode this is also a convenient way to run "server only" without
    # a separate entrypoint.
    if "--server-mode" in sys.argv[1:] or os.environ.get("GA_ADMIN_SERVER_MODE") == "1":
        # Defensive: even though Helper.app's Info.plist sets LSUIElement=True,
        # this also runs when the launcher itself is started with --server-mode
        # for headless/dev use. Setting the activation policy to "Prohibited"
        # (= 2) before any AppKit init ensures we never grab a Dock icon or
        # steal focus, regardless of which bundle's Info.plist applied.
        if sys.platform == "darwin":
            try:
                from AppKit import NSApplication  # type: ignore
                NSApplication.sharedApplication().setActivationPolicy_(2)
            except Exception:
                pass
        if getattr(sys, "frozen", False):
            # uvicorn does string-import "server.main:app"; ensure the bundle
            # root is on sys.path so that resolves against the frozen modules.
            sys.path.insert(0, str(_resource_root()))
        from server.run import main as _server_main
        return int(_server_main() or 0)

    ok, msg = _check_python_deps()
    if not ok:
        print(msg, file=sys.stderr)
        try:
            import webview
            webview.create_window(
                title="GenericAgent · 缺少依赖",
                html=f"<pre style='padding:24px;font-family:system-ui'>{msg}</pre>",
                width=560, height=240, resizable=False,
            )
            webview.start()
        except Exception:
            pass
        return 1

    ga_root = _ensure_ga_root()
    if ga_root is None:
        return 3   # explicit cancellation, no UI fallback wanted

    backend: subprocess.Popen | None = None
    env_extra = {"GA_ROOT": ga_root} if ga_root else None

    # Auto-collect zombies from a previous Ctrl+C / crashed run before we
    # try to bind. Healthy backends are left alone (reused below).
    _cleanup_stale_backend()

    if _backend_alive():
        print(f"[Launch] backend already running at {BACKEND_URL}, reusing it")
    else:
        print("[Launch] starting backend (server.run)...")
        # Frozen builds re-spawn this same binary with --server-mode (see
        # main() dispatch). On macOS we prefer the nested Helper.app
        # launcher (LSUIElement=True) so the backend doesn't show up as
        # a second Dock icon. Falls back to sys.executable if the helper
        # isn't bundled (older builds / dev). In dev we use the regular
        # Python module form.
        if getattr(sys, "frozen", False):
            backend_cmd = [_helper_executable() or sys.executable, "--server-mode"]
        else:
            backend_cmd = [sys.executable, "-m", "server.run"]
        backend = _popen(
            backend_cmd,
            cwd=SCRIPT_DIR,
            env_extra=env_extra,
            log_path=Path.home() / ".genericagent-admin" / "backend.log",
        )
        atexit.register(lambda: _safe_term(backend))
        # SIGINT / SIGTERM handler so terminal Ctrl+C tears the child down
        # too — without this, pywebview's main loop sometimes swallows the
        # signal and the backend orphan keeps holding the lock port.
        # We just call sys.exit so registered atexit handlers fire (which
        # already terminate backend + dev_proc).
        def _on_signal(signum, _frame):
            print(f"\n[Launch] received signal {signum}; shutting down…",
                  file=sys.stderr)
            sys.exit(130 if signum == signal.SIGINT else 143)
        for _sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(_sig, _on_signal)
            except (ValueError, OSError):
                # Signals not settable in some embedded contexts; ignore.
                pass

        if not _wait_url(f"{BACKEND_URL}/api/status", timeout=40):
            print("[Launch] backend failed to start within 40s", file=sys.stderr)
            rc = backend.poll() if backend else None
            if rc is not None:
                print(f"[Launch] server.run exited with code {rc}", file=sys.stderr)
                print(
                    "[Launch] hint: another copy of server.run may be holding the port.\n"
                    "        macOS/Linux:  lsof -iTCP:8765 -iTCP:8766 -sTCP:LISTEN  →  kill -9 <PID>\n"
                    "        Windows:      netstat -ano | findstr :8766",
                    file=sys.stderr,
                )
            _safe_term(backend)
            return 2
        print(f"[Launch] backend ready at {BACKEND_URL}")

    # decide UI source
    target_url = BACKEND_URL
    dev_proc: subprocess.Popen | None = None

    if DIST_INDEX.is_file():
        print("[Launch] using built UI (webui/dist)")
    else:
        print("[Launch] webui/dist not found, trying vite dev server...")
        for tool in ("pnpm", "npm"):
            if not _have(tool):
                continue
            try:
                dev_proc = _popen([tool, "run", "dev"], cwd=WEBUI_DIR)
            except FileNotFoundError:
                continue
            atexit.register(lambda: _safe_term(dev_proc))
            if _wait_url(DEV_URL, timeout=60):
                target_url = DEV_URL
                print(f"[Launch] vite dev ready at {DEV_URL}")
                break
            else:
                _safe_term(dev_proc)
                dev_proc = None
        if target_url == BACKEND_URL and not DIST_INDEX.is_file():
            print(
                "[Launch] webui not built and no Node.js found.\n"
                "         Run install_webui.sh / install_webui.bat or install Node.js.\n"
                "         Falling back to API docs view.",
                file=sys.stderr,
            )
            target_url = f"{BACKEND_URL}/docs"

    # open native window
    try:
        import webview  # type: ignore
    except ImportError:
        print("[Launch] pywebview not installed; opening default browser instead.")
        import webbrowser
        webbrowser.open(target_url)
        try:
            if backend:
                backend.wait()
        except KeyboardInterrupt:
            pass
        return 0

    win = webview.create_window(
        title="GenericAgent · 管理控制台",
        url=target_url,
        width=WINDOW_WIDTH,
        height=WINDOW_HEIGHT,
        resizable=True,
        text_select=True,
    )
    _arm_macos_activation(win)
    tray = _try_start_windows_tray(win) if os.name == "nt" else None
    try:
        webview.start()
    finally:
        if tray is not None:
            try:
                tray.stop()
            except Exception:
                pass
    return 0


def _arm_macos_activation(window) -> None:
    """Workaround for the macOS PyWebView/WKWebView first-responder issue.

    Verbose by design — without prints we can't tell whether the events
    fired, whether NSApp.windows() saw our window, and whether the
    activation actually flipped keyWindow/firstResponder. Each line is
    cheap (<200 bytes) and only prints during launch.
    """
    if sys.platform != "darwin":
        return

    print("[Launch][activate] arming macOS activation workaround", file=sys.stderr)

    state = {"fired": False, "ok": False}

    def _do_activate(reason: str):
        try:
            # ── Step 1: AppleScript bridge ─────────────────────────────────
            # When Python is launched from Terminal, NSApp / NSRunningApplication
            # activation calls are silently no-op'd by the OS — the process is
            # treated as a child of Terminal's activation tree, and `appActive`
            # never flips to True. AppleScript via osascript routes through the
            # System Events apple-event server (LaunchServices path), which is
            # NOT subject to that restriction and reliably promotes the target
            # PID to frontmost. Costs ~30ms; failures (perms denied, sandbox)
            # are non-fatal — the AppKit calls below still run.
            try:
                import subprocess as _sp
                _pid = os.getpid()
                _script = (
                    f'tell application "System Events" to set frontmost of '
                    f'(first process whose unix id is {_pid}) to true'
                )
                _r = _sp.run(["osascript", "-e", _script],
                             capture_output=True, text=True, timeout=2.0)
                if _r.returncode != 0:
                    err = (_r.stderr or "").strip().splitlines()[-1:] or [""]
                    print(f"[Launch][activate] osascript rc={_r.returncode}: {err[0]}", file=sys.stderr)
            except Exception as e:
                print(f"[Launch][activate] osascript failed: {e}", file=sys.stderr)

            # ── Step 2: AppKit fallbacks ───────────────────────────────────
            from AppKit import (
                NSApplication, NSRunningApplication,
                NSApplicationActivateIgnoringOtherApps, NSApplicationActivateAllWindows,
            )
            app = NSApplication.sharedApplication()
            try:
                app.setActivationPolicy_(0)
            except Exception:
                pass
            # Newer activation API. The legacy NSApp.activateIgnoringOtherApps_
            # has been observed to silently no-op when the launching context
            # is a Terminal-spawned Python (likely because the OS still treats
            # us as a child of Terminal's activation tree). NSRunningApplication
            # routes through the WindowServer directly.
            try:
                NSRunningApplication.currentApplication().activateWithOptions_(
                    NSApplicationActivateIgnoringOtherApps
                    | NSApplicationActivateAllWindows
                )
            except Exception:
                pass
            # Belt-and-braces: also call the legacy path.
            try:
                app.activateIgnoringOtherApps_(True)
            except Exception:
                pass
            target = None
            visible_count = 0
            for w in app.windows():
                try:
                    if w.isVisible() and not w.isMiniaturized():
                        visible_count += 1
                        target = target or w
                except Exception:
                    continue
            if target is None:
                print(f"[Launch][activate] {reason}: no visible window yet "
                      f"(NSApp.windows()={len(list(app.windows()))})", file=sys.stderr)
                return
            # Force a full focus cycle.
            target.orderFrontRegardless()
            target.makeKeyAndOrderFront_(None)
            try:
                target.makeKeyWindow()
            except Exception:
                pass
            try:
                target.makeMainWindow()
            except Exception:
                pass
            try:
                cv = target.contentView()
                set_fr_ok = bool(target.makeFirstResponder_(cv)) if cv else False
            except Exception:
                set_fr_ok = False
            try:
                key = bool(target.isKeyWindow())
                main = bool(target.isMainWindow())
                fr_cls = type(target.firstResponder()).__name__ if target.firstResponder() else "<nil>"
                can_key = bool(target.canBecomeKeyWindow())
                can_main = bool(target.canBecomeMainWindow())
                app_active = bool(app.isActive())
                title = str(target.title()) if target.title() else "<no-title>"
            except Exception:
                key = main = can_key = can_main = app_active = False
                fr_cls = title = "?"
            print(f"[Launch][activate] {reason}: vis={visible_count} title={title!r} "
                  f"appActive={app_active} key={key} main={main} canKey={can_key} canMain={can_main} "
                  f"firstResp={fr_cls} setFR_ok={set_fr_ok}",
                  file=sys.stderr)
            state["ok"] = state["ok"] or (app_active and key and set_fr_ok)
        except Exception as e:
            print(f"[Launch][activate] AppKit call failed: {e}", file=sys.stderr)

    def _schedule(reason: str):
        try:
            from PyObjCTools import AppHelper
            AppHelper.callAfter(_do_activate, reason)
            return True
        except Exception as e:
            print(f"[Launch][activate] AppHelper unavailable: {e}", file=sys.stderr)
            return False

    def _hook_factory(name):
        def _hook(*_args, **_kw):
            print(f"[Launch][activate] event '{name}' fired", file=sys.stderr)
            state["fired"] = True
            _schedule(f"event:{name}")
        return _hook

    for ev in ("shown", "loaded"):
        try:
            getattr(window.events, ev).__iadd__(_hook_factory(ev))
            print(f"[Launch][activate] hooked window.events.{ev}", file=sys.stderr)
        except Exception as e1:
            try:
                getattr(window.events, ev).connect(_hook_factory(ev))
                print(f"[Launch][activate] connected window.events.{ev}", file=sys.stderr)
            except Exception as e2:
                print(f"[Launch][activate] cannot hook events.{ev}: {e1!r} / {e2!r}",
                      file=sys.stderr)

    # Watchdog: retry until the OS confirms keyWindow + firstResponder is
    # the WKWebView. Whichever path wins (event hook OR watchdog) is fine.
    def _watchdog():
        for delay in (0.3, 0.8, 1.5, 2.5, 4.0):
            time.sleep(delay)
            if state["ok"]:
                return
            _schedule(f"watchdog@{delay}s")

    threading.Thread(target=_watchdog, daemon=True, name="macos-activate").start()


def _try_start_windows_tray(window):
    """Attach a Windows system-tray icon to ``window``.

    Behavior change vs default pywebview:
      • clicking the X button hides the window instead of destroying it,
        so the backend keeps running in the background;
      • left-click on the tray icon (or "显示主窗口") brings the window
        back;
      • only "退出" actually destroys the window — which lets
        ``webview.start()`` return so atexit handlers tear down the
        backend / dev_proc subprocesses.

    Returns the started ``pystray.Icon`` (so caller can ``.stop()`` it on
    exit), or ``None`` if pystray / Pillow aren't available — in that
    case we leave the window's default close behavior intact (= quit).
    """
    try:
        import pystray  # type: ignore
        from PIL import Image, ImageDraw, ImageFont  # type: ignore
    except ImportError as e:
        print(f"[Launch] pystray/Pillow unavailable, tray disabled: {e}",
              file=sys.stderr)
        return None

    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([(0, 0), (63, 63)], radius=14, fill=(91, 155, 255, 255))
    try:
        font = ImageFont.truetype("arial.ttf", 32)
    except Exception:
        font = ImageFont.load_default()
    try:
        bbox = d.textbbox((0, 0), "GA", font=font)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        d.text(((64 - tw) // 2 - bbox[0], (64 - th) // 2 - bbox[1]),
               "GA", font=font, fill=(255, 255, 255, 255))
    except Exception:
        d.text((16, 18), "GA", fill=(255, 255, 255, 255))

    def _show(icon, item):
        try:
            window.show()
        except Exception as e:
            print(f"[Launch] tray show failed: {e}", file=sys.stderr)

    def _quit(icon, item):
        try:
            window.destroy()
        except Exception:
            pass
        try:
            icon.stop()
        except Exception:
            pass

    def _on_closing():
        # Returning False cancels the default destroy; we hide instead so
        # the tray icon remains the single way to fully quit.
        try:
            window.hide()
        except Exception as e:
            print(f"[Launch] hide on close failed: {e}", file=sys.stderr)
        return False

    try:
        window.events.closing += _on_closing
    except Exception:
        try:
            window.events.closing.connect(_on_closing)
        except Exception as e:
            print(f"[Launch] could not hook closing event ({e}); "
                  f"close button will quit (no tray hide).",
                  file=sys.stderr)

    menu = pystray.Menu(
        pystray.MenuItem("显示主窗口", _show, default=True),
        pystray.MenuItem("退出", _quit),
    )
    icon = pystray.Icon(
        "GenericAgent-Admin", img,
        "GenericAgent · 管理控制台", menu,
    )
    icon.run_detached()
    print("[Launch] Windows tray icon started", file=sys.stderr)
    return icon


def _safe_term(proc: subprocess.Popen | None) -> None:
    if not proc:
        return
    try:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
    except Exception:
        pass


if __name__ == "__main__":
    sys.exit(main())
