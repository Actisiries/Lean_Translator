#!/usr/bin/env python3
"""Import LeanDojo / Mathlib-style JSONL into the formalizer memory bank.

Usage:
  python scripts/import_leandojo.py path/to/pairs.jsonl
  python scripts/import_leandojo.py path/to/pairs.jsonl --max 500 --domain number_theory
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from lean_formalizer.leandojo_bridge import import_leandojo_jsonl, lean_dojo_available


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        print("lean_dojo installed:", lean_dojo_available())
        return 2
    path = Path(argv[1])
    max_items = None
    domain = "mathlib"
    mem = ROOT / "memory"
    if "--max" in argv:
        max_items = int(argv[argv.index("--max") + 1])
    if "--domain" in argv:
        domain = argv[argv.index("--domain") + 1]
    if "--memory-dir" in argv:
        mem = Path(argv[argv.index("--memory-dir") + 1])
    stats = import_leandojo_jsonl(path, mem, max_items=max_items, domain=domain)
    print(stats)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
