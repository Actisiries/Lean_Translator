import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from paper2lean.cli import main as cli_main
from paper2lean.webapp import main as web_main


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run independent Paper2Lean CLI or web UI.")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("web", help="start the independent Paper2Lean web UI")
    args, rest = parser.parse_known_args()
    if args.command == "web":
        raise SystemExit(web_main())
    raise SystemExit(cli_main())
