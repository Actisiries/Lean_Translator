#!/usr/bin/env python3
"""One-click launcher for the Lean Formalizer Web UI.

Usage (from this folder):
    python run_web.py
    python run_web.py --port 8765
    python run_web.py --no-browser

On first start (or whenever LEAN_PROJECT is unset), auto-detects a sibling
Lake+Mathlib project (e.g. Lean_Work/mathlib_project next to lean_formalizer)
and writes local_config.env.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Ensure src/ is on sys.path so `lean_formalizer` is importable
ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

# Default memory dir next to this script
import os
os.environ.setdefault("MEMORY_DIR", str(ROOT / "memory"))


def _load_local_config() -> None:
    """Load local config in either .env or legacy PowerShell syntax."""
    from lean_formalizer.autoconfig import load_local_config_env

    for key, value in load_local_config_env(ROOT / "local_config.env").items():
        if key not in os.environ:
            os.environ[key] = value


def _ensure_autoconfig() -> None:
    """
    If LEAN_PROJECT is still unset after loading local_config, detect a sibling
    Lake project (Lean_Work/lean_formalizer + Lean_Work/mathlib_project) and
    write/merge local_config.env so the next steps see it.
    """
    if os.environ.get("LEAN_PROJECT") or os.environ.get("MATHLIB_PROJECT"):
        return
    try:
        from lean_formalizer.autoconfig import (
            build_autoconfig,
            write_local_config_env,
            write_local_config_ps1,
        )

        info = build_autoconfig(repo_root=ROOT)
        env = info.get("suggested_env") or {}
        if not env.get("LEAN_PROJECT"):
            # Still nothing found — leave unset; lean_verify will explain.
            return
        path = write_local_config_env(ROOT, env, overwrite=False)
        try:
            write_local_config_ps1(ROOT, env, overwrite=False)
        except Exception:
            pass
        # Apply into this process immediately
        for k, v in env.items():
            if k and v and k not in os.environ:
                os.environ[k] = v
        print(
            f"[autoconfig] Detected LEAN_PROJECT={env.get('LEAN_PROJECT')}",
            flush=True,
        )
        print(f"[autoconfig] Wrote/merged {path}", flush=True)
        for n in info.get("notes") or []:
            print(f"[autoconfig] {n}", flush=True)
    except Exception as ex:
        print(f"[autoconfig] skipped: {ex}", flush=True)


_load_local_config()
_ensure_autoconfig()
# Re-load in case autoconfig just wrote LEAN_PROJECT into the file
_load_local_config()

if not os.environ.get("LLM_BACKEND"):
    if os.environ.get("OPENAI_BASE_URL") or os.environ.get("OPENAI_API_KEY"):
        os.environ["LLM_BACKEND"] = "openai"
    elif os.environ.get("LLM_HTTP_URL"):
        os.environ["LLM_BACKEND"] = "http"

from lean_formalizer.webapp import main

if __name__ == "__main__":
    raise SystemExit(main())
