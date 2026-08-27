#!/usr/bin/env python3
"""Command-line interface for the formalization pipeline."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# Allow running as `python -m lean_formalizer.cli` from the src tree
if __name__ == "__main__" and __package__ is None:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lean_formalizer.pipeline import FormalizationPipeline
from lean_formalizer.llm import get_llm
from lean_formalizer.lean_verify import lean_available
from lean_formalizer.memory import MemoryBank


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="High-accuracy NL / PDF → Lean 4 formalization pipeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = p.add_subparsers(dest="command", required=True)

    # formalize
    f = sub.add_parser("formalize", help="Run the full pipeline")
    f.add_argument("--text", type=str, help="Natural-language mathematical text")
    f.add_argument("--pdf", type=str, help="Path to a PDF file")
    f.add_argument("--domain", type=str, default="general", help="Domain tag")
    f.add_argument("--max-pages", type=int, default=None)
    f.add_argument(
        "--memory-dir",
        type=str,
        default=os.environ.get("MEMORY_DIR", "./memory"),
    )
    f.add_argument("--candidates", type=int, default=4)
    f.add_argument("--repairs", type=int, default=3)
    f.add_argument("--no-save", action="store_true", help="Do not write to memory")
    f.add_argument(
        "--output",
        "-o",
        type=str,
        default=None,
        help="Write best Lean code to this file",
    )
    f.add_argument(
        "--json",
        action="store_true",
        help="Print full FormalizationResult as JSON",
    )

    # memory stats
    m = sub.add_parser("memory-stats", help="Show memory bank statistics")
    m.add_argument(
        "--memory-dir",
        type=str,
        default=os.environ.get("MEMORY_DIR", "./memory"),
    )

    # doctor
    sub.add_parser("doctor", help="Check environment (Lean, LLM backend, …)")

    return p


def cmd_formalize(args: argparse.Namespace) -> int:
    if not args.text and not args.pdf:
        print("Error: provide --text or --pdf", file=sys.stderr)
        return 2

    llm = get_llm()
    pipe = FormalizationPipeline(
        llm=llm,
        memory_dir=args.memory_dir,
        num_candidates=args.candidates,
        max_repair_rounds=args.repairs,
    )
    result = pipe.formalize(
        text=args.text,
        pdf_path=args.pdf,
        domain=args.domain,
        max_pages=args.max_pages,
        save_to_memory=not args.no_save,
    )

    if args.json:
        print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False))
    else:
        print("=" * 60)
        print(f"Success : {result.success}")
        print(f"Domain  : {result.domain}")
        print(f"Repairs : {result.repair_count}")
        print(f"Message : {result.message}")
        if result.structured:
            print("-" * 40)
            print("Main theorem (NL):")
            print(result.structured.main_theorem)
        print("-" * 40)
        if result.best_code:
            print("Best Lean code:")
            print(result.best_code)
        else:
            print("No compiling candidate produced.")
            for i, c in enumerate(result.candidates):
                print(f"\n--- Candidate {i} (compiled={c.compiled}) ---")
                print(c.lean_code[:500])
                if c.error_log:
                    print("Last error:", c.error_log[-1][:300])
        print("=" * 60)

    if args.output and result.best_code:
        Path(args.output).write_text(result.best_code, encoding="utf-8")
        print(f"Wrote {args.output}")

    return 0 if result.success else 1


def cmd_memory_stats(args: argparse.Namespace) -> int:
    bank = MemoryBank(args.memory_dir)
    stats = bank.stats()
    print(json.dumps(stats, indent=2))
    return 0


def cmd_doctor(_: argparse.Namespace) -> int:
    print("Lean binary available :", lean_available())
    backend = os.environ.get("LLM_BACKEND", "mock")
    print("LLM_BACKEND           :", backend)
    try:
        llm = get_llm(backend)
        reply = llm.chat(
            [{"role": "user", "content": "Reply with the single word: pong"}]
        )
        print("LLM smoke test        :", reply.strip()[:80])
    except Exception as e:
        print("LLM smoke test FAILED :", e)
    print("Memory dir default    :", os.environ.get("MEMORY_DIR", "./memory"))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "formalize":
        return cmd_formalize(args)
    if args.command == "memory-stats":
        return cmd_memory_stats(args)
    if args.command == "doctor":
        return cmd_doctor(args)
    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
