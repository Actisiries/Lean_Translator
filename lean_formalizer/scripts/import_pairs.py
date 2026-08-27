#!/usr/bin/env python3
"""Import NL→Lean pairs from a JSONL file into the memory bank.

Each line must be a JSON object with at least:
  natural_language, lean_code
Optional: domain, difficulty, tags

Example:
  python scripts/import_pairs.py examples/nl_lean_pairs.jsonl
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from lean_formalizer.memory import MemoryBank


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("Usage: python scripts/import_pairs.py <pairs.jsonl> [--memory-dir DIR]")
        return 2
    path = Path(argv[1])
    mem_dir = ROOT / "memory"
    if "--memory-dir" in argv:
        i = argv.index("--memory-dir")
        mem_dir = Path(argv[i + 1])

    bank = MemoryBank(mem_dir)
    existing = {e.natural_language for e in bank._success}
    added = 0
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            nl = obj.get("natural_language") or obj.get("nl") or ""
            lean = obj.get("lean_code") or obj.get("lean") or ""
            if not nl or not lean:
                print("skip incomplete line")
                continue
            if nl in existing:
                print("skip duplicate:", nl[:60])
                continue
            bank.save_success(
                natural_language=nl,
                lean_code=lean,
                domain=obj.get("domain", "general"),
                difficulty=obj.get("difficulty", "medium"),
                tags=list(obj.get("tags") or []),
                metadata={"source": str(path.name)},
            )
            existing.add(nl)
            added += 1
            print("added:", nl[:60])
    print("Added", added, "pairs. Stats:", bank.stats())
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
