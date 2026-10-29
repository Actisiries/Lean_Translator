from __future__ import annotations

import re
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from .runtime import UnitBudget


def audit_code(code: str) -> dict[str, Any]:
    text = code or ""
    forbidden = []
    for token in ("sorry", "admit", "axiom", "by_contra!?"):
        if re.search(rf"\b{re.escape(token)}\b", text):
            forbidden.append(token)
    # `exact?`, `apply?`, etc. are legitimate Lean tactics, not holes.
    placeholders = bool(re.search(r"\?m\.[A-Za-z0-9_]+|\?_[A-Za-z0-9_]*|(?<![\w'])\?(?![\w'])|\.\.\.", text))
    return {
        "has_code": bool(text.strip()),
        "forbidden_tokens": forbidden,
        "placeholders": placeholders,
        "trusted": bool(text.strip()) and not forbidden and not placeholders,
    }


def compile_code(code: str, *, scratch_file: str | Path | None = None,
                 timeout: float = 120, budget: UnitBudget | None = None) -> tuple[bool, str]:
    if budget:
        timeout = min(timeout, budget.remaining())
    audit = audit_code(code)
    if not audit["trusted"]:
        return False, "Code failed deterministic trust audit: " + str(audit)
    project = Path(os.environ.get("LEAN_PROJECT", "")).expanduser() if os.environ.get("LEAN_PROJECT") else None
    scratch = Path(scratch_file) if scratch_file else Path(tempfile.mkdtemp()) / "Candidate.lean"
    scratch.parent.mkdir(parents=True, exist_ok=True)
    scratch.write_text(code, encoding="utf-8")
    if project and any((project / name).exists() for name in ("lakefile.toml", "lakefile.lean")):
        command = ["lake", "env", "lean", str(scratch)]
        cwd = str(project)
    else:
        command = [os.environ.get("LEAN_PATH", "lean"), str(scratch)]
        cwd = str(scratch.parent)
    try:
        proc = subprocess.Popen(
            command,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except FileNotFoundError:
        return False, "Lean compiler unavailable. Set LEAN_PROJECT or LEAN_PATH."
    try:
        if budget:
            timeout = min(timeout, budget.remaining())
        stdout, stderr = proc.communicate(timeout=timeout)
    except (subprocess.TimeoutExpired, TimeoutError):
        # Lake starts Lean as a child process. Killing only Lake can leave Lean
        # running and its inherited output pipes open after the budget expires.
        if os.name == "nt":
            try:
                subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                               capture_output=True, timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                proc.kill()
        else:
            proc.kill()
        try:
            proc.communicate(timeout=1)
        except subprocess.TimeoutExpired:
            for stream in (proc.stdout, proc.stderr):
                if stream:
                    stream.close()
        return False, f"Lean compilation timed out after {timeout:g}s."
    output = ((stdout or "") + "\n" + (stderr or "")).strip()
    return proc.returncode == 0, output or ("compiled" if proc.returncode == 0 else "Lean compilation failed")


def failure_fingerprint(diagnostic: str) -> str:
    """Ignore scratch filenames and source positions when comparing failures."""
    errors = [line for line in diagnostic.splitlines() if "error" in line.lower()]
    text = "\n".join(errors) if errors else diagnostic
    text = re.sub(r"^.*?\.lean:\d+:\d+:\s*", "", text, flags=re.M)
    return re.sub(r"\s+", " ", text).strip()


def validate_mathlib_names(plan: dict[str, Any], *, compiler, scratch_file: Path) -> dict[str, Any]:
    """Ask the installed Lean environment to resolve explicitly proposed names.

    Only an actual unknown-identifier diagnostic is evidence of a missing name;
    unavailable Lean or a broken import is reported as an unavailable check.
    """
    identifier = re.compile(r"[A-Za-z_][A-Za-z0-9_']*(?:\.[A-Za-z_][A-Za-z0-9_']*)*\Z")
    proposed = plan.get("mathlib_names", [])
    if not isinstance(proposed, list):
        proposed = []
    names = list(dict.fromkeys(name for name in proposed if isinstance(name, str) and identifier.fullmatch(name)))[:30]
    imports = plan.get("imports", [])
    if not isinstance(imports, list):
        imports = []
    modules = [name.removeprefix("import ").strip() for name in imports if isinstance(name, str)]
    modules = [name for name in modules if identifier.fullmatch(name)] or ["Mathlib"]
    if not names:
        return {"status": "skipped", "reason": "No Mathlib names proposed", "names": []}
    header = "\n".join(f"import {module}" for module in modules)
    ok, diagnostic = compiler(header + "\n" + "\n".join(f"#check {name}" for name in names), scratch_file)
    missing = [name for name in names if re.search(
        rf"(?:Unknown constant|Unknown identifier|unknown constant|unknown identifier)\s+[`'‘]?{re.escape(name)}(?:[`'’]|\s|$)", diagnostic)]
    return {
        "status": "checked" if ok or missing else "unavailable",
        "names": names, "missing": missing,
        "confirmed": names if ok else [], "diagnostic": diagnostic[:3000],
    }


def axiom_audit(code: str) -> dict[str, Any]:
    """Conservative source audit; exact used-axiom inspection belongs to Lean."""
    declared = re.findall(r"(?m)^\s*axiom\s+([A-Za-z0-9_']+)", code or "")
    names = re.findall(r"(?m)^\s*(?:theorem|lemma)\s+([A-Za-z0-9_']+)", code or "")
    return {"declared_axioms": declared, "targets": names, "policy": "reject_new_axioms", "status": "rejected" if declared else "pending_exact_audit"}


def exact_axiom_audit(code: str, *, compiler=None) -> dict[str, Any]:
    names = re.findall(r"(?m)^\s*(?:theorem|lemma)\s+([A-Za-z0-9_']+)", code or "")
    if not names:
        return {"status": "no_targets", "targets": []}
    query = code.rstrip() + "\n" + "\n".join(f"#print axioms {name}" for name in names) + "\n"
    try:
        check = compiler or compile_code
        ok, output = check(query)
        return {"status": "checked" if ok else "unavailable", "targets": names, "output": output[-6000:]}
    except Exception as exc:
        return {"status": "unavailable", "targets": names, "error": str(exc)}


def correspondence_audit(statement: str, code: str) -> dict[str, Any]:
    """Cheap deterministic correspondence signal used before optional LLM review."""
    names = re.findall(r"(?m)^\s*(?:theorem|lemma)\s+([A-Za-z0-9_']+)", code or "")
    return {
        "statement_present": bool((statement or "").strip()),
        "lean_declaration_found": bool(names),
        "declaration_names": names,
        "status": "pending_review" if names and statement.strip() else "insufficient_source",
    }
