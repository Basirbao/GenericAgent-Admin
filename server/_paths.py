"""Path discovery & configuration for GenericAgent-Admin.

The admin tool lives in its own directory (sibling of the GenericAgent
project, or anywhere on disk). It discovers the GenericAgent project root
in this order:

    1. ``$GA_ROOT`` environment variable (highest priority — useful for tests)
    2. Saved config: ``~/.genericagent-admin/config.json`` ``{"ga_root": ...}``
    3. Common candidate locations (sibling, ~, ~/Desktop, ...)

If none of the above resolves, ``GA_ROOT`` is ``None`` and the backend
runs in **setup mode** — only ``/api/setup/*`` endpoints respond. The
desktop launcher (or the Settings page in the SPA) prompts the user to
pick a directory, then calls ``set_ga_root()`` and restarts the backend.

ADMIN_DATA = ``~/.genericagent-admin/`` holds:

    config.json                 → discovered ga_root, port, etc.
    autonomous_schedules.json   → admin-managed self-evolution schedules
    autonomous_runs.jsonl       → trigger history
    uploads/                    → files pasted/dragged in the React UI

Crucially, NOTHING is written into the GenericAgent repo from admin code,
so ``git pull`` on GA never conflicts.
"""
from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path

log = logging.getLogger(__name__)

# ── fixed paths ─────────────────────────────────────────────────
ADMIN_ROOT = Path(__file__).resolve().parent.parent             # the admin checkout
ADMIN_DATA = Path(os.environ.get("GA_ADMIN_DATA") or (Path.home() / ".genericagent-admin")).resolve()
CONFIG_FILE = ADMIN_DATA / "config.json"


# ── config load/save ────────────────────────────────────────────
def load_config() -> dict:
    if CONFIG_FILE.is_file():
        try:
            return json.loads(CONFIG_FILE.read_text("utf-8"))
        except Exception as e:
            log.warning("config.json unreadable: %s", e)
    return {}


def save_config(cfg: dict) -> None:
    ADMIN_DATA.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), "utf-8")
    tmp.replace(CONFIG_FILE)


# ── GA root validation & discovery ──────────────────────────────
def is_valid_ga_root(p: Path | str | None) -> bool:
    if not p:
        return False
    pp = Path(p).expanduser()
    return (pp / "agentmain.py").is_file() and (pp / "memory").is_dir()


def candidate_paths() -> list[Path]:
    """Likely locations for GenericAgent on this machine."""
    home = Path.home()
    out: list[Path] = []
    seen: set = set()
    for c in [
        Path.cwd() / "GenericAgent",
        Path.cwd().parent / "GenericAgent",
        ADMIN_ROOT.parent / "GenericAgent",
        ADMIN_ROOT.parent.parent / "GenericAgent",
        home / "GenericAgent",
        home / "Desktop" / "GenericAgent",
        home / "Desktop" / "HH" / "GenericAgent",
        home / "Documents" / "GenericAgent",
        home / "Code" / "GenericAgent",
        home / "src" / "GenericAgent",
    ]:
        rp = c.resolve()
        if rp in seen:
            continue
        seen.add(rp)
        out.append(c)
    return out


def discover_ga_root() -> Path | None:
    # 1. env override
    env = os.environ.get("GA_ROOT", "").strip()
    if env:
        if is_valid_ga_root(env):
            return Path(env).expanduser().resolve()
        log.warning("$GA_ROOT=%s is not a valid GenericAgent directory; ignoring", env)

    # 2. saved config
    saved = load_config().get("ga_root")
    if saved and is_valid_ga_root(saved):
        return Path(saved).expanduser().resolve()

    # 3. common candidates
    for c in candidate_paths():
        if is_valid_ga_root(c):
            return c.resolve()
    return None


def discover_user_python() -> str | None:
    """Resolve a system Python suitable for running GA's ``code_run``.

    Why this exists: in a PyInstaller-frozen ``.app`` build, ``sys.executable``
    points at the bundle's Mach-O launcher, not a real Python. Passing it to
    ``[sys.executable, "-X", "utf8", "-u", script.py]`` (what ``ga.py:code_run``
    does) re-launches the GUI launcher with garbage argv — every code_run
    spawns a fresh Dock icon and never executes the user's code. The fix is
    to swap argv[0] for an actual ``python3`` interpreter; this helper finds
    one. Resolution order:

      1. ``$GA_PYTHON`` env override (escape hatch for tests / weird setups)
      2. saved config: ``python_path`` (UI-configured path, future-proofing)
      3. ``shutil.which("python3")`` against the parent process's PATH
      4. macOS Homebrew (Apple Silicon then Intel), pyenv shim, Apple stub

    Returns absolute path, or ``None`` if nothing usable was found. Callers
    should treat ``None`` as "fall back to ``sys.executable``" — acceptable
    in dev (where ``sys.executable`` is already a real interpreter) but a
    fatal misconfiguration in frozen-mac builds.
    """
    import shutil
    env = os.environ.get("GA_PYTHON", "").strip()
    if env and Path(env).is_file():
        return env
    saved = load_config().get("python_path")
    if saved and Path(str(saved)).is_file():
        return str(saved)
    found = shutil.which("python3")
    if found:
        return found
    candidates = [
        "/opt/homebrew/bin/python3",        # Apple Silicon Homebrew
        "/usr/local/bin/python3",           # Intel Homebrew / python.org installer
        f"{Path.home()}/.pyenv/shims/python3",
        "/usr/bin/python3",                 # Apple stub (last resort, no user pkgs)
    ]
    for cand in candidates:
        if Path(cand).is_file():
            return cand
    return None


def set_ga_root(path: str) -> Path:
    """Validate path, persist to config, return resolved Path. Raises ValueError on bad input."""
    if not path or not str(path).strip():
        raise ValueError("路径为空")
    p = Path(path).expanduser().resolve()
    if not p.is_dir():
        raise ValueError(f"目录不存在：{p}")
    if not is_valid_ga_root(p):
        raise ValueError(
            f"该目录不是 GenericAgent 项目根目录\n"
            f"（缺少 agentmain.py 或 memory/）：{p}"
        )
    cfg = load_config()
    cfg["ga_root"] = str(p)
    save_config(cfg)
    return p


# ── module-level resolved value (may be None until configured) ──
GA_ROOT: Path | None = discover_ga_root()


def bootstrap_sys_path(ga_root: Path | None = None) -> Path | None:
    """Insert GA root + frontends/ into sys.path so we can import agentmain etc.

    Idempotent. Returns the path used (or None if not configured).
    """
    target = ga_root or GA_ROOT
    if target is None:
        return None
    target = Path(target).resolve()
    for p in (str(target), str(target / "frontends")):
        if p not in sys.path:
            sys.path.insert(0, p)
    return target


# eagerly bootstrap so downstream imports just work when GA_ROOT is known
if GA_ROOT is not None:
    bootstrap_sys_path(GA_ROOT)


# ── derived helpers ─────────────────────────────────────────────
def memory_dir() -> Path:
    if GA_ROOT is None:
        raise RuntimeError("GA_ROOT not configured")
    return GA_ROOT / "memory"


def temp_dir() -> Path:
    if GA_ROOT is None:
        raise RuntimeError("GA_ROOT not configured")
    p = GA_ROOT / "temp"
    p.mkdir(parents=True, exist_ok=True)
    return p


def admin_uploads_dir() -> Path:
    p = ADMIN_DATA / "uploads"
    p.mkdir(parents=True, exist_ok=True)
    return p


def schedules_file() -> Path:
    return ADMIN_DATA / "autonomous_schedules.json"


def runs_file() -> Path:
    return ADMIN_DATA / "autonomous_runs.jsonl"


def reports_dir() -> Path:
    """GenericAgent's autonomous reports directory (under GA's temp/)."""
    return temp_dir() / "autonomous_reports"
