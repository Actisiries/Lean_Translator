#!/usr/bin/env python3
"""Detect Lean project + local API proxy and write local_config.env / .ps1"""
from pathlib import Path
import sys
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))
from lean_formalizer.autoconfig import build_autoconfig, write_local_config_env, write_local_config_ps1

def main():
    overwrite = "--force" in sys.argv
    info = build_autoconfig(repo_root=ROOT)
    env = info["suggested_env"]
    p1 = write_local_config_env(ROOT, env, overwrite=overwrite)
    p2 = write_local_config_ps1(ROOT, env, overwrite=overwrite)
    print("=== Auto config ===")
    for n in info["notes"]:
        print("-", n)
    print("Wrote:", p1)
    print("Wrote:", p2)
    print("Suggested env:")
    for k, v in env.items():
        print(f"  {k}={v if 'KEY' not in k else (v[:4]+'…' if v else '(empty — fill in)')}")
    if not env.get("OPENAI_API_KEY"):
        print("\nEdit local_config.env and set OPENAI_API_KEY, then run start_translator.bat")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
