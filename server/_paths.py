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
import shutil
import subprocess
import sys
from pathlib import Path

log = logging.getLogger(__name__)

# ── fixed paths ─────────────────────────────────────────────────
ADMIN_ROOT = Path(__file__).resolve().parent.parent             # the admin checkout
ADMIN_DATA = Path(os.environ.get("GA_ADMIN_DATA") or (Path.home() / ".genericagent-admin")).resolve()
CONFIG_FILE = ADMIN_DATA / "config.json"
_UNSET = object()


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


def validate_python_path(path: str | None) -> str | None:
    """Normalize an optional Python interpreter path from user config."""
    raw = str(path or "").strip()
    if not raw:
        return None
    p = Path(raw).expanduser().resolve()
    if not p.is_file():
        raise ValueError(f"Python 解释器不存在：{p}")
    return str(p)


def _ga_venv_python_candidates(ga_root: Path | None) -> list[Path]:
    if ga_root is None:
        return []
    roots = [ga_root / name for name in (".venv", "venv", "env")]
    out: list[Path] = []
    for root in roots:
        if os.name == "nt":
            out.append(root / "Scripts" / "python.exe")
            out.append(root / "Scripts" / "python3.exe")
        else:
            out.append(root / "bin" / "python3")
            out.append(root / "bin" / "python")
    return out


def _known_python_candidates() -> list[str]:
    candidates = [
        "/Library/Frameworks/Python.framework/Versions/3.12/bin/python3",
        "/Library/Frameworks/Python.framework/Versions/3.11/bin/python3",
        "/Library/Frameworks/Python.framework/Versions/3.10/bin/python3",
        "/opt/homebrew/bin/python3",        # Apple Silicon Homebrew
        "/usr/local/bin/python3",           # Intel Homebrew / python.org installer
        f"{Path.home()}/.pyenv/shims/python3",
        "/usr/bin/python3",                 # Apple stub (last resort, no user pkgs)
    ]
    if os.name == "nt":
        candidates.extend([
            str(Path.home() / "AppData/Local/Programs/Python/Python312/python.exe"),
            str(Path.home() / "AppData/Local/Programs/Python/Python311/python.exe"),
            str(Path.home() / "AppData/Local/Programs/Python/Python310/python.exe"),
        ])
    return candidates


def _discover_user_python_with_source(ga_root: Path | None = None) -> tuple[str | None, str]:
    target_root = ga_root or GA_ROOT

    env = os.environ.get("GA_PYTHON", "").strip()
    if env:
        try:
            return validate_python_path(env), "GA_PYTHON"
        except ValueError:
            log.warning("$GA_PYTHON=%s is not a valid Python executable; ignoring", env)

    saved = load_config().get("python_path")
    if saved:
        try:
            return validate_python_path(str(saved)), "config.python_path"
        except ValueError:
            log.warning("configured python_path=%s is not a valid Python executable; ignoring", saved)

    for cand in _ga_venv_python_candidates(target_root):
        if cand.is_file():
            return str(cand.resolve()), "ga_venv"

    for cand in _known_python_candidates():
        if Path(cand).is_file():
            return str(Path(cand).resolve()), "known_location"

    for name in ("python3", "python"):
        found = shutil.which(name)
        if found:
            return str(Path(found).resolve()), f"PATH:{name}"
    return None, "current_process"


def discover_user_python(ga_root: Path | None = None) -> str | None:
    """Resolve a system Python suitable for running GA's ``code_run``.

    Why this exists: in a PyInstaller-frozen ``.app`` build, ``sys.executable``
    points at the bundle's Mach-O launcher, not a real Python. Passing it to
    ``[sys.executable, "-X", "utf8", "-u", script.py]`` (what ``ga.py:code_run``
    does) re-launches the GUI launcher with garbage argv — every code_run
    spawns a fresh Dock icon and never executes the user's code. The fix is
    to swap argv[0] for an actual ``python3`` interpreter; this helper finds
    one. Resolution order:

      1. ``$GA_PYTHON`` env override (escape hatch for tests / weird setups)
      2. saved config: ``python_path`` (UI-configured path)
      3. GenericAgent project virtualenvs (``.venv``, ``venv``, ``env``)
      4. ``python3`` / ``python`` against the parent process's PATH
      5. macOS Homebrew, pyenv shim, Apple stub, common Windows installs

    Returns absolute path, or ``None`` if nothing usable was found. Callers
    should treat ``None`` as "fall back to ``sys.executable``" — acceptable
    in dev (where ``sys.executable`` is already a real interpreter) but a
    fatal misconfiguration in frozen-mac builds.
    """
    return _discover_user_python_with_source(ga_root)[0]


def python_status(ga_root: Path | None = None) -> dict[str, str | None]:
    resolved, source = _discover_user_python_with_source(ga_root)
    saved = load_config().get("python_path")
    return {
        "python_path": str(saved) if saved else None,
        "resolved_python": resolved,
        "resolved_python_source": source,
    }


def external_python_site_paths(ga_root: Path | None = None) -> list[str]:
    """Return import paths from the resolved external Python environment.

    Some GA tools (notably ``web_scan``) import optional packages in the
    Admin backend process instead of inside ``code_run``. Adding the resolved
    interpreter's site-packages to ``sys.path`` lets those in-process GA tools
    use the same environment that ``code_run`` will launch.
    """
    python = discover_user_python(ga_root)
    if not python:
        return []
    try:
        if Path(python).resolve() == Path(sys.executable).resolve():
            return []
    except Exception:
        pass

    code = (
        "import json, site, sys\n"
        "paths = []\n"
        "try: paths.extend(site.getsitepackages())\n"
        "except Exception: pass\n"
        "try: paths.append(site.getusersitepackages())\n"
        "except Exception: pass\n"
        "print(json.dumps([p for p in paths if p and p not in sys.path]))\n"
    )
    try:
        kwargs: dict = dict(text=True, stderr=subprocess.DEVNULL, timeout=5)
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        out = subprocess.check_output(
            [python, "-c", code],
            **kwargs,
        )
        paths = json.loads(out)
    except Exception as e:
        log.warning("failed to inspect external Python site-packages %s: %s", python, e)
        return []

    result: list[str] = []
    for raw in paths:
        p = Path(str(raw)).expanduser()
        if p.is_dir():
            sp = str(p.resolve())
            if sp not in result:
                result.append(sp)
    return result


def set_ga_root(path: str, python_path: str | None | object = _UNSET) -> Path:
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
    normalized_python = None
    if python_path is not _UNSET:
        normalized_python = validate_python_path(python_path if isinstance(python_path, str) else None)
    cfg = load_config()
    cfg["ga_root"] = str(p)
    if python_path is not _UNSET:
        if normalized_python:
            cfg["python_path"] = normalized_python
        else:
            cfg.pop("python_path", None)
    save_config(cfg)
    return p


# ── module-level resolved value (may be None until configured) ──
GA_ROOT: Path | None = discover_ga_root()


def bootstrap_sys_path(ga_root: Path | None = None) -> Path | None:
    """Insert GA root, frontends/, and external Python packages into sys.path.

    Idempotent. Returns the path used (or None if not configured).
    """
    target = ga_root or GA_ROOT
    if target is None:
        return None
    target = Path(target).resolve()
    for p in (str(target), str(target / "frontends")):
        if p not in sys.path:
            sys.path.insert(0, p)
    for p in external_python_site_paths(target):
        if p not in sys.path:
            sys.path.append(p)
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


def tasks_schedules_file() -> Path:
    return ADMIN_DATA / "tasks_schedules.json"


def tasks_runs_file() -> Path:
    return ADMIN_DATA / "tasks_runs.jsonl"


def email_config_file() -> Path:
    return ADMIN_DATA / "email_config.json"


def reports_dir() -> Path:
    """GenericAgent's autonomous reports directory (under GA's temp/)."""
    return temp_dir() / "autonomous_reports"
