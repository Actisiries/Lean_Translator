from __future__ import annotations

import argparse
import json
from pathlib import Path

from .orchestrator import Paper2Lean


def main() -> int:
    parser = argparse.ArgumentParser(description="Analyze and formalize a PDF/TeX paper.")
    parser.add_argument("path", type=Path)
    parser.add_argument("--output", type=Path, default=Path("paper_runs"))
    parser.add_argument("--target")
    parser.add_argument("--max-units", type=int)
    parser.add_argument("--unit-budget-seconds", type=float, default=None)
    parser.add_argument("--analyze-only", action="store_true")
    args = parser.parse_args()
    runner = Paper2Lean(args.output, unit_time_budget_seconds=args.unit_budget_seconds)
    model = runner.analyze(args.path)
    result = model.to_dict() if args.analyze_only else runner.run(model, target=args.target, max_units=args.max_units)
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
