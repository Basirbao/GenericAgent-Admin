# Quality Guidelines

> Code quality standards for backend development.

---

## Overview

<!--
Document your project's quality standards here.

Questions to answer:
- What patterns are forbidden?
- What linting rules do you enforce?
- What are your testing requirements?
- What code review standards apply?
-->

(To be filled by the team)

---

## Forbidden Patterns

<!-- Patterns that should never be used and why -->

(To be filled by the team)

---

## Required Patterns

<!-- Patterns that must always be used -->

### Scenario: GenericAgent Python Tool Execution Environment

#### 1. Scope / Trigger
- Trigger: Any change to GenericAgent tool execution, packaged runtime startup, setup/config endpoints, or code paths that affect `ga.py:code_run`.
- Contract: GenericAgent-Admin must not require user/SOP Python dependencies to be bundled into the Admin package. GA Python code execution must use an external interpreter.

#### 2. Signatures
- `server._paths.discover_user_python(ga_root: Path | None = None) -> str | None`
- `server._paths.external_python_site_paths(ga_root: Path | None = None) -> list[str]`
- `server._paths.bootstrap_sys_path(ga_root: Path | None = None) -> Path | None`
- `server._paths.python_status(ga_root: Path | None = None) -> dict[str, str | None]`
- `POST /api/setup/save` request fields:
  - `ga_root: string`
  - `python_path?: string | null`
- `GET /api/setup/status` and `GET /api/status` response fields:
  - `python_path: string | null`
  - `resolved_python: string | null`
  - `resolved_python_source: string`

#### 3. Contracts
- Python interpreter resolution order:
  1. `GA_PYTHON` environment variable.
  2. Saved Admin config `python_path`.
  3. Common GenericAgent virtualenvs under `GA_ROOT`: `.venv`, `venv`, `env`.
  4. Known platform install locations, including Python.org macOS framework paths.
  5. Local `python3` / `python` on `PATH`.
  6. Only then fall back to the current process executable.
- Runtime patching must replace GA `code_run` commands whose argv starts with Admin's `sys.executable` with the resolved external interpreter.
- Runtime patching must prepend the resolved interpreter directory to child-process `PATH` and set `GA_PYTHON`, so shell tools such as `pip`, `pip3`, and `python3 -m pip` align with the same environment where possible.
- In-process GA tools can import optional packages before spawning a child process. `bootstrap_sys_path()` must add the resolved external Python environment's site-packages/user-site paths so tools such as `web_scan` can import dependencies like `simple_websocket_server` from the same environment that `code_run` uses.
- Windows runtime patching must preserve `CREATE_NO_WINDOW` while still doing interpreter replacement.
- Do not modify the GenericAgent repository to achieve this; patch at Admin startup.

#### 4. Validation & Error Matrix
- Empty `python_path` in setup save -> remove saved override, use auto discovery.
- Non-empty `python_path` that is not a file -> HTTP 400 / `ValueError`, config must not be partially written.
- Invalid `GA_PYTHON` env or stale saved `python_path` during discovery -> log warning and continue to the next source.
- No external interpreter found -> report `resolved_python: null`, runtime may fall back to current executable as last resort.

#### 5. Good/Base/Bad Cases
- Good: `GA_ROOT/.venv/bin/python3` exists and no override is set -> `resolved_python_source == "ga_venv"`.
- Base: no GA venv exists but `python3` is on `PATH` -> use `PATH:python3`.
- Bad: packaged Admin `sys.executable` is used for SOP `code_run` while a GA venv or configured interpreter exists.
- Bad: packaged Admin can launch external `code_run`, but in-process `web_scan` still fails importing `simple_websocket_server` because external site-packages were not added to `sys.path`.
- Bad: GA shell `code_run` uses an unrelated `pip` from PATH, installs a dependency, and `web_scan` still cannot import it from the resolved Python environment.

#### 6. Tests Required
- Unit tests for discovery priority: env > config > GA venv > PATH.
- Unit tests that invalid explicit `python_path` is rejected before config write.
- Unit tests that `bootstrap_sys_path()` appends external Python site-packages.
- Frontend type/build check when setup/status response fields change.

#### 7. Wrong vs Correct
#### Wrong
```python
cmd = [sys.executable, "-X", "utf8", "-u", tmp_path]
# In packaged Admin, this can launch the Admin runtime and miss user deps.
```

#### Correct
```python
real_python = _paths.discover_user_python()
# The Admin startup patch rewrites GA code_run subprocess argv[0] to real_python
# whenever GA tries to launch Admin's current sys.executable.
```

---

## Testing Requirements

<!-- What level of testing is expected -->

(To be filled by the team)

---

## Code Review Checklist

<!-- What reviewers should check -->

(To be filled by the team)
