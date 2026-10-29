"""Parser diagnostics for the Python process actually serving file uploads."""
from __future__ import annotations

import importlib
from pathlib import Path
import sys


def parser_status() -> dict:
    installed = {}
    for name in ("pylatexenc.latexwalker", "pdfplumber", "pypdf"):
        try:
            importlib.import_module(name)
            installed[name] = True
        except ImportError:
            installed[name] = False
    return {
        "python": sys.executable,
        "tex": installed["pylatexenc.latexwalker"],
        "pdf": installed["pdfplumber"] or installed["pypdf"],
        "parsers": installed,
        "install_command": install_command(),
    }


def install_command() -> str:
    target = Path(__file__).resolve().parents[2] / ".runtime_dependencies"
    return (f'& "{sys.executable}" -m pip install --target "{target}" '
            '\'pylatexenc>=2.10,<3\' \'pypdf>=4.0.0\'')


def missing_parser_message(kind: str) -> str:
    requirement = ("TeX support requires pylatexenc. Install the project requirements first."
                   if kind == "tex" else "PDF input requires pdfplumber or pypdf.")
    return (f"{requirement}\nServer Python: {sys.executable}\n"
            f"Install file support with PowerShell:\n{install_command()}\n"
            "Then restart the formalizer server. If this Python has no pip, use a "
            "pip-enabled Python to run the same install command with the same --target directory.")
