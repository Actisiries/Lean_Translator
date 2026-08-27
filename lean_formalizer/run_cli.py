#!/usr/bin/env python3
"""One-click launcher for the Lean Formalizer CLI.

Usage (from this folder):
    python run_cli.py formalize --text "Every natural number is either even or odd."
    python run_cli.py doctor
    python run_cli.py memory-stats
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import os
os.environ.setdefault("MEMORY_DIR", str(ROOT / "memory"))


def _load_local_config() -> None:
    from lean_formalizer.autoconfig import load_local_config_env

    for key, value in load_local_config_env(ROOT / "local_config.env").items():
        if key not in os.environ:
            os.environ[key] = value


def _ensure_autoconfig() -> None:
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
            return
        write_local_config_env(ROOT, env, overwrite=False)
        try:
            write_local_config_ps1(ROOT, env, overwrite=False)
        except Exception:
            pass
        for k, v in env.items():
            if k and v and k not in os.environ:
                os.environ[k] = v
        print(f"[autoconfig] Detected LEAN_PROJECT={env.get('LEAN_PROJECT')}", flush=True)
    except Exception as ex:
        print(f"[autoconfig] skipped: {ex}", flush=True)


_load_local_config()
_ensure_autoconfig()
_load_local_config()

if not os.environ.get("LLM_BACKEND"):
    if os.environ.get("OPENAI_BASE_URL") or os.environ.get("OPENAI_API_KEY"):
        os.environ["LLM_BACKEND"] = "openai"
    elif os.environ.get("LLM_HTTP_URL"):
        os.environ["LLM_BACKEND"] = "http"

from lean_formalizer.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
