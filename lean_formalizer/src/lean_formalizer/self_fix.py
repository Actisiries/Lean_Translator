"""Local self-fixing helpers for Lean code that fails to compile."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Optional


_MATHLIB_MARKERS = (
    r"\bMathlib\.",
    r"\bReal\.",
    r"\bComplex\.",
    r"\bSet\.",
    r"\bFinset\.",
    r"\bPolynomial\.",
    r"\bMulAction\.",
    r"\bSubgroup\.",
    r"\bContractingWith\b",
    r"\bLipschitzWith\b",
    r"\bNat\.card\b",
)


def _needs_mathlib(code: str) -> bool:
    c = code or ""
    return any(re.search(m, c) for m in _MATHLIB_MARKERS)


def _mathlib_root(project: Optional[Path]) -> Optional[Path]:
    if project is None:
        return None
    root = project / ".lake" / "packages" / "mathlib" / "Mathlib"
    return root if root.is_dir() else None


def _module_from_file(file: Path, root: Path) -> str:
    rel = file.resolve().relative_to(root.resolve()).with_suffix("")
    return "Mathlib." + ".".join(rel.parts)


def search_mathlib_imports(
    identifier: str,
    project: Optional[Path] = None,
) -> list[str]:
    """Find Mathlib modules that declare an identifier."""
    root = _mathlib_root(project)
    if root is None or not identifier:
        return []
    name = identifier.split(".")[-1]
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_']*", name):
        return []
    pattern = (
        r"\b(?:theorem|lemma|def|class|structure|inductive|instance)\s+"
        + re.escape(name)
        + r"\b"
    )
    files: list[str] = []
    rg = shutil.which("rg")
    if rg:
        try:
            proc = subprocess.run(
                [rg, "-l", "--glob", "*.lean", pattern, str(root)],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=15,
            )
            files = [
                line.strip()
                for line in proc.stdout.splitlines()
                if line.strip()
            ][:10]
        except Exception:
            files = []
    if not files:
        try:
            for p in root.rglob("*.lean"):
                try:
                    text = p.read_text(encoding="utf-8", errors="ignore")
                except Exception:
                    continue
                if re.search(pattern, text):
                    files.append(str(p))
                    if len(files) >= 10:
                        break
        except Exception:
            pass
    return [
        _module_from_file(Path(f), root)
        for f in files
        if Path(f).suffix == ".lean"
    ]


def _unknown_identifiers(error: str) -> list[str]:
    found: list[str] = []
    for m in re.finditer(
        r"unknown\s+(?:constant|identifier|declaration)\s+`?([\w.']+)`?",
        error or "",
        re.IGNORECASE,
    ):
        ident = m.group(1).strip("`")
        if ident and ident not in found:
            found.append(ident)
    return found


def _missing_module(error: str) -> Optional[str]:
    m = re.search(
        r"object file '[^']+' of module ([\w.]+)", error or ""
    )
    if m:
        return m.group(1)
    m = re.search(r"unknown module prefix '?([\w.]+)'?", error or "")
    return m.group(1) if m else None


def _import_exists(code: str, module: str) -> bool:
    return any(
        line.strip() == f"import {module}" for line in (code or "").splitlines()
    )


def _prepend_imports(code: str, modules: list[str]) -> str:
    missing = [m for m in modules if m and not _import_exists(code, m)]
    if not missing:
        return code
    return "\n".join(f"import {m}" for m in missing) + "\n\n" + code


def _fix_noncomputable_theorem(code: str) -> str:
    if "noncomputable theorem" not in code:
        return code
    if "≃" in code or "≅" in code or "×" in code or "⊕" in code:
        return code.replace("noncomputable theorem", "noncomputable def", 1)
    return code.replace("noncomputable theorem", "theorem", 1)


def _normalize_placeholder(code: str) -> str:
    return re.sub(r":=\s*(?:by\s*)?\.\.\.\s*$", "", code.rstrip()).rstrip()


def self_fix_lean(
    code: str,
    error: str = "",
    statement: str = "",
    project: Optional[Path] = None,
) -> tuple[str, list[str]]:
    """Apply deterministic fixes and local Mathlib import search."""
    original = code or ""
    fixed = original
    notes: list[str] = []

    fixed = _fix_noncomputable_theorem(fixed)
    fixed = _normalize_placeholder(fixed)

    modules: list[str] = []
    for ident in _unknown_identifiers(error):
        hits = search_mathlib_imports(ident, project)
        for mod in hits:
            if mod not in modules:
                modules.append(mod)
    missing_mod = _missing_module(error)
    if missing_mod and missing_mod not in modules:
        modules.append(missing_mod)

    if modules:
        fixed = _prepend_imports(fixed, modules)
        notes.append(
            "added local Mathlib imports: " + ", ".join(modules[:5])
        )

    if _needs_mathlib(fixed) and not re.search(
        r"^\s*import\s+", fixed, re.MULTILINE
    ):
        fixed = "import Mathlib\n\n" + fixed
        notes.append("added umbrella import Mathlib as a last-resort fallback")

    if fixed == original:
        return original, []
    return fixed, notes
