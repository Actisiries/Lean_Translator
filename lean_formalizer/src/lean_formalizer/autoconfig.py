"""Auto-detect Lean project, elan, and local OpenAI-compatible proxies (e.g. CC Switch).

Writes local_config.env (and optionally suggests PowerShell lines) so users
do not hand-edit paths/URLs every time.
"""

from __future__ import annotations

import json
import os
import re
import socket
import urllib.request
import urllib.error
from pathlib import Path
from typing import Any, Optional


# Common local proxy ports used by CC Switch / OpenAI-compatible gateways
_COMMON_PORTS = (3010, 3000, 3088, 8080, 8000, 7890, 4000, 5000, 11434)


def load_local_config_env(path: Path) -> dict[str, str]:
    """Read a local config file without overwriting process environment.

    ``local_config.env`` is normally a ``KEY=VALUE`` file.  Early versions of
    the auto-configurator accidentally wrote PowerShell assignments to this
    file, for example ``$env:LEAN_PROJECT = \"C:\\project\"``.  Accept that
    legacy form as well, so an existing installation continues to use its
    configured LLM and Lean project instead of silently falling back to mock
    mode.
    """
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    ps_assignment = re.compile(
        r"^\s*\$env:([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$",
        re.IGNORECASE,
    )
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return values
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = ps_assignment.match(line)
        if match:
            key, value = match.group(1), match.group(2)
        elif "=" in line:
            key, value = line.split("=", 1)
            key = key.strip()
        else:
            continue
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            continue
        value = value.strip().strip("\"").strip("'")
        if value:
            values[key] = value
    return values


def _tcp_open(host: str, port: int, timeout: float = 0.35) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _http_json(url: str, timeout: float = 1.5) -> Optional[dict]:
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None


def find_elan_bin() -> Optional[str]:
    home = Path.home()
    candidates = [
        home / ".elan" / "bin",
        home / ".local" / "bin",
    ]
    # Windows-style under USERPROFILE already covered by Path.home()
    for d in candidates:
        lean = d / "lean"
        lean_exe = d / "lean.exe"
        if lean.exists() or lean_exe.exists():
            return str(d)
    return None


def _is_lake_project(path: Path) -> bool:
    """True if path looks like a Lake project root (toolchain + lakefile)."""
    if not path.is_dir():
        return False
    has_tc = (path / "lean-toolchain").exists()
    has_lake = (path / "lakefile.toml").exists() or (path / "lakefile.lean").exists()
    return has_tc or has_lake


def find_sibling_lean_projects(repo_root: Path) -> list[str]:
    """
    Detect Lake projects that sit next to lean_formalizer.

    Expected layout (any parent name, e.g. Lean_Work):
        Lean_Work/
          lean_formalizer/     ← repo_root
          mathlib_project/     ← sibling with lean-toolchain / lakefile
    """
    found: list[str] = []
    parent = repo_root.resolve().parent
    if not parent.is_dir():
        return found
    try:
        for child in sorted(parent.iterdir()):
            if not child.is_dir():
                continue
            # Skip ourselves
            if child.resolve() == repo_root.resolve():
                continue
            if _is_lake_project(child):
                found.append(str(child.resolve()))
    except OSError:
        pass
    return found


def find_lean_projects(search_roots: Optional[list[Path]] = None, max_depth: int = 3) -> list[str]:
    """Find directories that contain lean-toolchain (Lake/Mathlib projects)."""
    roots = search_roots or []
    home = Path.home()
    defaults = [
        home / "Documents",
        home / "Desktop",
        home / "Projects",
        home / "projects",
        home / "Lean",
        home / "lean",
        home / "source",
        home / "src",
        home / "dev",
        Path.cwd(),
        Path.cwd().parent,
    ]
    for r in defaults:
        if r not in roots:
            roots.append(r)

    found: list[str] = []
    seen: set[str] = set()
    for root in roots:
        if not root.exists() or not root.is_dir():
            continue
        try:
            for dirpath, dirnames, filenames in os.walk(root):
                rel = Path(dirpath).relative_to(root)
                if len(rel.parts) > max_depth:
                    dirnames.clear()
                    continue
                # skip heavy / irrelevant trees
                skip = {
                    ".git",
                    ".lake",
                    "node_modules",
                    "__pycache__",
                    ".venv",
                    "venv",
                    "FormalizerScratch",
                }
                dirnames[:] = [d for d in dirnames if d not in skip]
                if "lean-toolchain" in filenames or "lakefile.toml" in filenames:
                    p = str(Path(dirpath).resolve())
                    if p not in seen:
                        seen.add(p)
                        found.append(p)
        except OSError:
            continue
    return found


def probe_openai_compatible(host: str = "127.0.0.1") -> list[dict[str, Any]]:
    """Probe localhost ports for OpenAI-compatible /v1/models."""
    hits: list[dict[str, Any]] = []
    for port in _COMMON_PORTS:
        if not _tcp_open(host, port):
            continue
        base = f"http://{host}:{port}/v1"
        models = _http_json(f"{base}/models")
        entry: dict[str, Any] = {
            "base_url": base,
            "chat_url": f"{base}/chat/completions",
            "port": port,
            "models": [],
        }
        if models and isinstance(models.get("data"), list):
            entry["models"] = [
                m.get("id") for m in models["data"] if isinstance(m, dict) and m.get("id")
            ]
        hits.append(entry)
    return hits


def build_autoconfig(
    repo_root: Optional[Path] = None,
    prefer_project: Optional[str] = None,
) -> dict[str, Any]:
    repo_root = (repo_root or Path.cwd()).resolve()
    elan = find_elan_bin()

    # 1) Siblings first (Lean_Work/lean_formalizer + Lean_Work/mathlib_project)
    siblings = find_sibling_lean_projects(repo_root)
    # 2) Then a shallow walk of parent / home
    walked = find_lean_projects(
        search_roots=[repo_root, repo_root.parent, Path.home()],
        max_depth=3,
    )
    # Merge, siblings first, dedupe
    seen: set[str] = set()
    projects: list[str] = []
    for p in siblings + walked:
        if p not in seen:
            seen.add(p)
            projects.append(p)

    proxies = probe_openai_compatible()

    # Prefer Lake projects that depend on Mathlib; strong boost for true siblings.
    def _score(p: str) -> int:
        path = Path(p)
        score = 0
        if p in siblings:
            score += 50  # layout the user described
        if (path / "lakefile.toml").exists() or (path / "lakefile.lean").exists():
            score += 5
        if (path / "lean-toolchain").exists():
            score += 3
        try:
            for fname in ("lakefile.toml", "lakefile.lean"):
                lf = path / fname
                if lf.exists() and "mathlib" in lf.read_text(
                    encoding="utf-8", errors="ignore"
                ).lower():
                    score += 15
                    break
        except Exception:
            pass
        name = path.name.lower()
        if "mathlib" in name:
            score += 8
        return score

    if projects:
        projects = sorted(projects, key=_score, reverse=True)

    lean_project = prefer_project
    if not lean_project and projects:
        lean_project = projects[0]

    proxy = proxies[0] if proxies else None
    model = None
    if proxy and proxy.get("models"):
        # Prefer deepseek flash if listed
        ids = proxy["models"]
        for cand in ("deepseek-v4-flash", "deepseek-chat", "gpt-4o", "gpt-4o-mini"):
            if cand in ids:
                model = cand
                break
        if not model:
            model = ids[0]

    env: dict[str, str] = {
        "MEMORY_DIR": str(repo_root / "memory"),
    }
    if lean_project:
        env["LEAN_PROJECT"] = lean_project
    if proxy:
        env["LLM_BACKEND"] = "openai"
        env["OPENAI_BASE_URL"] = proxy["base_url"]
        env["OPENAI_MODEL"] = model or "deepseek-v4-flash"
        # key left empty — user must fill
        if "OPENAI_API_KEY" not in env:
            env["OPENAI_API_KEY"] = ""
    else:
        # Leave LLM_BACKEND unset when no LLM endpoint is found; launchers infer
        # a configured backend from OPENAI_*/LLM_HTTP_* later.
        env.pop("LLM_BACKEND", None)

    return {
        "elan_bin": elan,
        "lean_projects": projects,
        "lean_project": lean_project,
        "proxies": proxies,
        "suggested_env": env,
        "notes": _notes(elan, lean_project, proxy),
    }


def _notes(elan: Optional[str], project: Optional[str], proxy: Optional[dict]) -> list[str]:
    notes = []
    if elan:
        notes.append(f"Found elan/lean bin dir: {elan}")
    else:
        notes.append("elan/lean not found under ~/.elan/bin — install elan or fix PATH.")
    if project:
        notes.append(f"Using LEAN_PROJECT={project}")
        notes.append(
            "Tip: add 'FormalizerScratch/' to the Lean project's .gitignore "
            "to keep temporary check files out of version control."
        )
        notes.append(
            "If this is the first use of that project, run `lake build` once inside it."
        )
    else:
        notes.append(
            "No Lake project found as a sibling of lean_formalizer "
            "(expected e.g. Lean_Work/mathlib_project next to Lean_Work/lean_formalizer). "
            "Set LEAN_PROJECT manually in local_config.env."
        )
    if proxy:
        notes.append(
            f"Found local API at {proxy['base_url']} "
            f"(models: {', '.join(proxy.get('models') or ['unknown']) or 'unknown'})"
        )
        notes.append("Set OPENAI_API_KEY to your CC Switch / provider key.")
    else:
        notes.append("No local OpenAI-compatible proxy detected — using mock or set URL manually.")
    return notes


def write_local_config_env(repo_root: Path, env: dict[str, str], overwrite: bool = False) -> Path:
    path = repo_root / "local_config.env"
    if path.exists() and not overwrite:
        # Merge legacy PowerShell-style entries under their real key names too.
        existing = load_local_config_env(path)
        merged = dict(env)
        merged.update({k: v for k, v in existing.items() if v})  # keep user non-empty
        # still apply new detections for empty keys
        for k, v in env.items():
            if not merged.get(k):
                merged[k] = v
        env = merged

    lines = [
        "# Auto-generated / merged by lean_formalizer autoconfig",
        "# Edit OPENAI_API_KEY (or LLM_HTTP_KEY) by hand if empty.",
        "",
    ]
    for k, v in env.items():
        lines.append(f"{k}={v}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def write_local_config_ps1(repo_root: Path, env: dict[str, str], overwrite: bool = False) -> Path:
    path = repo_root / "local_config.ps1"
    if path.exists() and not overwrite:
        return path
    lines = [
        "# Auto-generated by lean_formalizer autoconfig",
        "",
    ]
    for k, v in env.items():
        # PowerShell single-quoted escape
        vv = (v or "").replace("'", "''")
        lines.append(f"$env:{k} = '{vv}'")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path
