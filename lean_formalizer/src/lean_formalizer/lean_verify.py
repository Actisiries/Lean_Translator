"""Step 6 – Lean 4 verification + automatic repair loop.

How the translator uses Lean
----------------------------
1. Looks up the `lean` binary via LEAN_PATH or PATH (your elan install).
2. If LEAN_PROJECT points at a Lake+Mathlib project (the one you use in VS Code),
   the candidate is written into that project and checked with `lake env lean`
   so `import Mathlib...` resolves.
3. Otherwise falls back to bare `lean file.lean` in a temp dir (no Mathlib).
4. If no Lean checker exists, reports that formal verification is unavailable.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import time
import hashlib
import json
import difflib
import threading
import signal
import os as _os
from pathlib import Path
from typing import Any, Callable, Optional

from .llm import LLMClient
from .models import Candidate
from .prompts import (
    REPAIR_PROMPT,
    IMPORT_REPAIR_PROMPT,
    TIMEOUT_RECOVERY_PROMPT,
    COMPLETE_PROOF_PROMPT,
    SYSTEM_LEAN_ONLY,
)
from .mathlib_hints import mathlib_hints_for_statement
from .self_fix import self_fix_lean


# Known Mathlib module moves: rewrite a few stable renames.
# The active project (and its Mathlib rev) is chosen at first-time setup /
# via LEAN_PROJECT; the repair LLM handles any remaining import drift.
_IMPORT_ALIASES = {
    "Mathlib.Data.Nat.Parity": "Mathlib.Algebra.Ring.Parity",
    "Mathlib.Topology.Algebra.Order.IntermediateValue": "Mathlib.Topology.Order.IntermediateValue",
    "Mathlib.Topology.StoneWeierstrass": "Mathlib.Topology.ContinuousMap.StoneWeierstrass",
    "Mathlib.Analysis.Topology.StoneWeierstrass": "Mathlib.Topology.ContinuousMap.StoneWeierstrass",
    "Mathlib.Topology.ContinuousFunction.StoneWeierstrass": "Mathlib.Topology.ContinuousMap.StoneWeierstrass",
}

# These standard natural-number identities are provided by Lean's core library.
# An LLM often adds the umbrella `import Mathlib` out of habit, which makes a
# tiny verification load the whole Mathlib environment (several GB on modest
# machines).  Limit the optimization to exact, known core theorem references;
# all other imports are preserved for correctness.
_CORE_NAT_IMPORTS = frozenset({
    "Nat.add_zero",
    "Nat.zero_add",
    "Nat.add_comm",
    "Nat.add_assoc",
    "Nat.mul_zero",
    "Nat.zero_mul",
    "Nat.mul_one",
    "Nat.one_mul",
    "Nat.mul_comm",
    "Nat.mul_assoc",
})


# A lightweight, process-local index of the active Mathlib checkout.  It is
# deliberately source based (rather than relying on Lean internals) so it can
# answer import/path questions before the expensive compiler process starts.
_PROJECT_INDEX_CACHE: dict[str, tuple[tuple[int, int], dict[str, Any]]] = {}
_PROJECT_INDEX_LOCK = threading.Lock()


def _mathlib_root(project: Optional[Path] = None) -> Optional[Path]:
    p = project or lean_project_dir()
    if not p:
        return None
    root = p / ".lake" / "packages" / "mathlib" / "Mathlib"
    return root if root.is_dir() else None


def _build_project_index(project: Optional[Path] = None) -> dict[str, Any]:
    """Index local Mathlib modules and common declarations once per checkout."""
    root = _mathlib_root(project)
    if root is None:
        return {"root": "", "modules": set(), "declarations": set(), "count": 0}
    try:
        stat = root.stat()
        stamp = (int(stat.st_mtime_ns), int(stat.st_size))
    except OSError:
        stamp = (0, 0)
    key = str(root.resolve())
    with _PROJECT_INDEX_LOCK:
        cached = _PROJECT_INDEX_CACHE.get(key)
        if cached and cached[0] == stamp:
            return cached[1]
    # `Mathlib` itself is the umbrella module one level above the source tree.
    modules: set[str] = {"Mathlib"}
    declarations: set[str] = set()
    try:
        for dirpath, _dirnames, filenames in _os.walk(root):
            for filename in filenames:
                if not filename.endswith(".lean"):
                    continue
                path = Path(dirpath) / filename
                rel = path.relative_to(root).with_suffix("")
                module = "Mathlib." + ".".join(rel.parts)
                modules.add(module)
            # Keep startup/preflight cheap: module paths are the critical
            # deterministic check. Declaration names are populated lazily by
            # identifier_suggestions when a repair actually needs them.
    except OSError:
        pass
    result = {"root": str(root), "modules": modules, "declarations": declarations, "count": len(modules)}
    with _PROJECT_INDEX_LOCK:
        _PROJECT_INDEX_CACHE[key] = (stamp, result)
    return result


def project_index_summary(project: Optional[Path] = None, limit: int = 24) -> str:
    """Return compact, prompt-safe facts about the local Mathlib checkout."""
    index = _build_project_index(project)
    if not index.get("root"):
        return "Local Mathlib index: unavailable (no configured project checkout)."
    modules = sorted(index.get("modules", set()))
    return (
        f"Local Mathlib index: {index.get('count', len(modules))} modules at "
        f"{index['root']}. Valid examples: " + ", ".join(modules[:limit])
    )


def _module_path_suggestions(module: str, index: dict[str, Any]) -> list[str]:
    modules = sorted(index.get("modules", set()))
    close = difflib.get_close_matches(module, modules, n=4, cutoff=0.45)
    # Directory modules commonly have a Basic leaf in this Mathlib revision.
    prefix = module + "."
    close += [m for m in modules if m.startswith(prefix) and m.endswith(".Basic")][:2]
    return list(dict.fromkeys(close))


def preflight_lean_code(code: str, project: Optional[Path] = None) -> dict[str, Any]:
    """Validate/normalize imports without starting Lean.

    Returns ``code`` (possibly with a directory import redirected to Basic),
    an error, and actionable local suggestions.  This is intentionally
    conservative: declaration names are suggestions only; Lean remains the
    authority for typechecking.
    """
    normalized = normalize_lean_imports(code or "")
    index = _build_project_index(project)
    if not index.get("modules"):
        return {"code": normalized, "ok": True, "imports": [], "suggestions": []}
    imports = []
    for line in _extract_imports(normalized):
        module = line[len("import "):].strip()
        imports.append(module)
        if not module.startswith("Mathlib") or module == "Mathlib":
            continue
        if module in index["modules"]:
            continue
        suggestions = _module_path_suggestions(module, index)
        if suggestions:
            # Deterministic safe rewrite only when the matching leaf is clear.
            basic = next((m for m in suggestions if m == module + ".Basic"), None)
            if basic:
                normalized = re.sub(
                    rf"(?m)^\s*import\s+{re.escape(module)}\s*$",
                    f"import {basic}", normalized,
                )
                continue
        hint = ", ".join(suggestions) or "no close local module"
        return {
            "code": normalized,
            "ok": False,
            "imports": imports,
            "module": module,
            "suggestions": suggestions,
            "error": f"error: unknown module `{module}` (local Mathlib index); suggestions: {hint}",
        }
    return {"code": normalized, "ok": True, "imports": imports, "suggestions": []}


def use_umbrella_mathlib_import(code: str) -> str:
    """Ensure a Mathlib import without widening a valid specific import.

    Older versions of the pipeline replaced every import with the umbrella
    ``import Mathlib``.  That loads the complete library and can take minutes
    on a local machine.  Preserve any existing ``Mathlib.*`` import; add the
    umbrella only when the model supplied no import at all.  Import validity is
    still checked later by :func:`preflight_lean_code` and Lean itself.
    """
    source = (code or "").lstrip()
    imports = _extract_imports(source)
    if imports:
        return normalize_lean_imports(source)
    # Do not add the umbrella automatically. A Mathlib-dependent snippet
    # without an import is rejected by compile_lean and gets one bounded
    # import-repair request instead.
    return source


def has_umbrella_mathlib_import(code: str) -> bool:
    """Whether code contains the exact, disabled umbrella import."""
    return bool(re.search(r"(?m)^\s*import\s+Mathlib\s*$", code or ""))


def import_policy_error(code: str) -> str:
    """Return a fast local error for imports that exceed the local policy."""
    if has_umbrella_mathlib_import(code):
        return (
            "Umbrella import `Mathlib` is disabled because it exceeds the local "
            "compile budget. Replace it with fine-grained Mathlib imports."
        )
    if _needs_mathlib(code) and not _extract_imports(code):
        return (
            "Mathlib declarations are used without a fine-grained import. "
            "Add the exact local Mathlib module(s) required by the code."
        )
    return ""


def query_local_lean_facts(
    statement: str, project: Optional[Path] = None, timeout: int = 20,
) -> list[str]:
    """Ask the configured Lean project for a small set of relevant #check facts.

    This is a bounded hint query, not proof search. Failures are ignored so it
    cannot block ordinary generation.
    """
    lower = (statement or "").lower()
    checks: list[str] = []
    if any(x in lower for x in ("bounded", "sequence", "convergen", "compact", "real")):
        checks += ["isCompact_Icc", "IsCompact.isSeqCompact", "Metric.compact_iff_seqCompact"]
    if any(x in lower for x in ("injective", "surjective", "linear", "vector")):
        checks += ["LinearMap.injective_iff_surjective", "LinearMap.ker_eq_bot"]
    if any(x in lower for x in ("finite-dimensional", "dimension", "rank")):
        checks += ["Module.finrank", "LinearMap.finrank_range_add_finrank_ker"]
    if any(x in lower for x in ("group", "subgroup", "normal", "quotient")):
        checks += ["Subgroup.Normal", "QuotientGroup.quotientKerEquivRange"]
    checks = list(dict.fromkeys(checks))[:5]
    if not checks or project is None:
        return []
    code = "import Mathlib\n\n" + "\n".join(f"#check {name}" for name in checks)
    ok, output = _compile_in_lake_project(code, project, timeout=max(5, timeout))
    if not ok:
        return []
    return [line.strip() for line in output.splitlines() if line.strip() and not line.startswith("[scratch file:")][:16]


def identifier_suggestions(error: str, project: Optional[Path] = None) -> list[str]:
    """Suggest local declaration names for an unknown-identifier diagnostic."""
    match = re.search(r"Unknown (?:identifier|constant) [`']?([A-Za-z_][A-Za-z0-9_']*)", error or "", re.I)
    if not match:
        return []
    name = match.group(1)
    index = _build_project_index(project)
    # The cached index stores a set, but convert defensively: older in-memory
    # index values (and tests/extensions) may provide a list.  The targeted
    # scan below adds names, so it must operate on a mutable set rather than a
    # sorted list.  Sorting belongs only at the final close-match lookup.
    declarations = set(index.get("declarations") or ())
    if not declarations and index.get("root"):
        # A targeted search is much cheaper than indexing every source file at
        # startup and is only paid for unknown-identifier repairs.
        wanted = name.lower()
        try:
            for path in Path(index["root"]).rglob("*.lean"):
                if wanted not in path.name.lower() and len(declarations) >= 200:
                    continue
                text = path.read_text(encoding="utf-8", errors="ignore")
                for found in re.finditer(
                    r"(?m)^\s*(?:theorem|lemma|def|abbrev|class|structure|inductive|notation)\s+"
                    r"([A-Za-z_][A-Za-z0-9_']*)", text,
                ):
                    declarations.add(found.group(1))
                if len(declarations) >= 200:
                    break
        except OSError:
            pass
    return difflib.get_close_matches(name, sorted(declarations), n=6, cutoff=0.55)


def _remove_unneeded_mathlib_import(code: str) -> str:
    """Drop only an umbrella import for simple Lean-core Nat identities."""
    out = code or ""
    if not re.search(r"(?m)^\s*import\s+Mathlib\s*$", out):
        return out
    references = set(re.findall(r"\bNat\.[A-Za-z0-9_']+", out))
    if len(references) != 1 or not references <= _CORE_NAT_IMPORTS:
        return out
    # Tactics, typeclasses, or another Mathlib namespace in the code body make
    # this unsafe.  Exclude the import line itself from this check.
    body = re.sub(r"(?m)^\s*import\s+Mathlib\s*$", "", out)
    if re.search(
        r"\b(?:by|simp|omega|linarith|ring|norm_num|aesop|Mathlib|Finset|Set|Real|Int)\b",
        body,
    ):
        return out
    return re.sub(r"(?m)^\s*import\s+Mathlib\s*\n+", "", out).lstrip()


def normalize_lean_imports(code: str) -> str:
    """Rewrite known old Mathlib import paths to current module names."""
    out = code or ""
    for old, new in _IMPORT_ALIASES.items():
        out = re.sub(
            rf"import\s+{re.escape(old)}(\s|$)",
            lambda m: f"import {new}{m.group(1)}",
            out,
        )
    return _remove_unneeded_mathlib_import(out)


_THINK_TAG_RE = re.compile(
    r"<think>[\s\S]*?</think>"
    r"|\[think\][\s\S]*?\[/think\]"
    r"|\[reasoning\][\s\S]*?\[/reasoning\]",
    re.IGNORECASE,
)
_CHATTER_LINE_RE = re.compile(
    r"^(?:Here is|Here's|Sure,? here|Certainly|The following is|Below is|"
    r"This is|Let me provide|I will provide|I'll provide)[^\n]*$",
    re.IGNORECASE | re.MULTILINE,
)
_TRAILING_PROSE_RE = re.compile(
    r"^(?:I hope|Hope this|Let me know|Please let me|Feel free|Happy to|"
    r"This completes|That completes|The proof is complete|In summary|"
    r"Output only|Here is the final|The final Lean|If you need|"
    r"You can copy|Make sure to|Note that this proof|This proof uses|"
    r"We can also|Good luck|This proves|This shows|The result follows|"
    r"Thus|Therefore|Hence|That is all)[^\n]*$",
    re.IGNORECASE,
)
_DECLARATION_RE = re.compile(
    r"\b(?:import|theorem|lemma|example|def|instance)\b",
    re.IGNORECASE,
)


def _strip_chatter(text: str) -> str:
    text = _THINK_TAG_RE.sub("", text or "")
    marker = _DECLARATION_RE.search(text) or re.search(r"```", text)
    if marker:
        prefix, rest = text[: marker.start()], text[marker.start() :]
    else:
        prefix, rest = text, ""
    return _CHATTER_LINE_RE.sub("", prefix) + rest


def _trim_trailing_prose(code: str) -> str:
    lines = (code or "").rstrip().splitlines()
    while lines:
        line = lines[-1].strip()
        if (
            line
            and not line.startswith("--")
            and not line.startswith("/-")
            and _TRAILING_PROSE_RE.match(line)
        ):
            lines.pop()
        else:
            break
    return "\n".join(lines).strip()


def _clean_lean_code(code: str) -> str:
    code = _THINK_TAG_RE.sub("", code or "").strip()
    m = _DECLARATION_RE.search(code)
    if m:
        code = code[m.start() :]
    code = _trim_trailing_prose(code)
    return normalize_lean_imports(code.strip())


def _looks_like_lean(code: str) -> bool:
    c = code or ""
    if _DECLARATION_RE.search(c):
        return True
    return bool(
        re.search(r":=\s*\S", c)
        or re.search(r"(?m)^\s*by\b", c)
        or re.search(r"(?<!\w)\bby\b", c)
    )


def extract_lean_code(text: str) -> Optional[str]:
    """Extract a Lean snippet from an LLM reply.

    Handles prose before code, closed and unclosed fences, thinking/chatter
    wrappers, and replies that start directly at `import`/`theorem`.
    """
    if not text or not text.strip():
        return None
    cleaned = _strip_chatter(text).strip()
    if not cleaned:
        return None

    m = re.search(
        r"```\s*(?:lean(?:\s*4)?)\s*\n?(.*?)```",
        cleaned,
        re.DOTALL | re.IGNORECASE,
    )
    if m:
        candidate = _clean_lean_code(m.group(1))
        if _looks_like_lean(candidate):
            return candidate

    m = re.search(r"```\s*(.*?)```", cleaned, re.DOTALL)
    if m:
        candidate = _clean_lean_code(m.group(1))
        if _looks_like_lean(candidate):
            return candidate

    m = re.search(
        r"```\s*(?:lean(?:\s*4)?)\s*\n?(.*)$",
        cleaned,
        re.DOTALL | re.IGNORECASE,
    )
    if m:
        candidate = _clean_lean_code(m.group(1))
        if _looks_like_lean(candidate):
            return candidate

    m = re.search(r"```\s*(.*)$", cleaned, re.DOTALL)
    if m:
        candidate = _clean_lean_code(m.group(1))
        if _looks_like_lean(candidate):
            return candidate

    m = re.search(
        r"(?:^|\n)((?:import\s+[^\n]*(?:\n|$))+[\s\S]*?\b(?:theorem|lemma|example|def|instance)\b[\s\S]*)",
        cleaned,
    )
    if m:
        candidate = _clean_lean_code(m.group(1))
        if _looks_like_lean(candidate):
            return candidate

    m = re.search(
        r"(?:^|\n)(\b(?:theorem|lemma|example|def|instance)\b[\s\S]*)",
        cleaned,
    )
    if m:
        candidate = _clean_lean_code(m.group(1))
        if _looks_like_lean(candidate):
            return candidate
    return None


def _strip_lean_comments(code: str) -> str:
    code = re.sub(r"/-[\s\S]*?-/", "", code)
    code = re.sub(r"--[^\n]*", "", code)
    return code


def _block_has_proof_body(block: str) -> bool:
    b = _strip_lean_comments(block or "").strip()
    m = re.search(r":=", b)
    if m:
        tail = b[m.end() :].strip()
        tail = re.sub(r"^by\s*", "", tail).strip()
        return bool(tail) and "..." not in tail
    return bool(
        re.search(r"(?m)^\s*by\b", b) or re.search(r"(?<!\w)\bby\b", b)
    )


def has_proof_body(code: str) -> bool:
    """True when every declaration in the snippet has a non-empty body."""
    c = code or ""
    markers = list(
        re.finditer(r"\b(?:theorem|lemma|example|def|instance)\b", c)
    )
    if not markers:
        return False
    for i, m in enumerate(markers):
        end = markers[i + 1].start() if i + 1 < len(markers) else len(c)
        if not _block_has_proof_body(c[m.start() : end]):
            return False
    return True


def declaration_name_matches(old_code: str, new_code: str) -> bool:
    """True when a rewritten snippet keeps at least one original theorem name."""
    def names(code: str) -> set[str]:
        return set(
            re.findall(
                r"\b(?:theorem|lemma|example)\s+([A-Za-z_][A-Za-z0-9_']*)",
                _strip_lean_comments(code or ""),
            )
        )

    old_names = names(old_code)
    if not old_names:
        return True
    return bool(old_names & names(new_code))


def strip_incomplete_proof(code: str) -> str:
    """Drop a `:= ...` placeholder body so complete-proof can fill it in."""
    c = (code or "").rstrip()
    m = re.search(r":=", c)
    if m and "..." in c[m.end() :]:
        return c[: m.start()].rstrip()
    return c


def proof_self_references(code: str) -> bool:
    """True when a declaration's proof body names the declaration itself."""
    c = _strip_lean_comments(code or "")
    markers = list(
        re.finditer(
            r"\b(?:theorem|lemma|example)\s+([A-Za-z_][A-Za-z0-9_']*)",
            c,
        )
    )
    for i, m in enumerate(markers):
        end = markers[i + 1].start() if i + 1 < len(markers) else len(c)
        block = c[m.start() : end]
        body = re.search(r":=", block)
        if body and re.search(
            rf"\b{re.escape(m.group(1))}\b", block[body.end() :]
        ):
            return True
    return False


def _rename_colliding_declaration(code: str, error: str) -> tuple[str, list[str]]:
    """Rename a generated declaration when Mathlib already owns its name.

    LLMs often copy a theorem's Mathlib name and then try to prove it by
    referring to itself.  Lean rejects the declaration before checking that
    proof.  A private wrapper name preserves the requested proposition while
    allowing the existing Mathlib theorem to be used in the body.
    """
    if not re.search(r"already been declared|already declared", error or "", re.I):
        return code, []
    m = re.search(
        r"(?m)^(\s*)(theorem|lemma|def|example)\s+"
        r"([A-Za-z_][A-Za-z0-9_']*(?:\.[A-Za-z_][A-Za-z0-9_']*)*)",
        code or "",
    )
    if not m:
        return code, []
    old = m.group(3)
    new = old + "_formalized"
    # Change only the declaration identifier; keep references to the original
    # Mathlib theorem in the proof body intact.
    start, end = m.start(3), m.end(3)
    out = code[:start] + new + code[end:]
    return out, [f"renamed colliding declaration {old} to {new}"]


def recover_import_failure(
    code: str, error: str, project: Optional[Path] = None,
) -> tuple[str, list[str]]:
    """Fix an invalid import locally before consuming an LLM repair round."""
    match = re.search(
        r"(?:of module|unknown module)\s+`?([A-Za-z][A-Za-z0-9_.]*)`?",
        error or "", re.IGNORECASE,
    )
    if not match:
        return code, []
    module = match.group(1)
    import_line = re.compile(rf"(?m)^\s*import\s+{re.escape(module)}\s*\n?")
    if not import_line.search(code or ""):
        return code, []
    # The universal Mathlib import already supplies the declarations.  Removing
    # the broken specific import is exact and does not depend on guessing a
    # sibling module name.
    if re.search(r"(?m)^\s*import\s+Mathlib\s*$", code or ""):
        return import_line.sub("", code).lstrip(), [f"removed invalid import {module}; import Mathlib is present"]
    root = project / ".lake" / "packages" / "mathlib" / "Mathlib" if project else None
    if root is not None and root.is_dir() and module.startswith("Mathlib."):
        stem = module[len("Mathlib."):].replace(".", os.sep)
        direct = root / f"{stem}.lean"
        basic = root / stem / "Basic.lean"
        replacement = None
        if direct.is_file():
            replacement = module
        elif basic.is_file():
            replacement = module + ".Basic"
        if replacement:
            return import_line.sub(f"import {replacement}\n", code), [f"replaced invalid import {module} with {replacement}"]
    return code, []


def complete_proof_with_known_lemmas(
    declaration: str, statement: str = ""
) -> str:
    """Fill a few well-known declarations with real Mathlib proof bodies."""
    code = (declaration or "").strip()
    if not code:
        return ""
    # Continuous image of a compact set.  In this Mathlib revision the
    # reliable API is `image_of_continuousOn`; `IsCompact.image` may be the
    # declaration currently being generated and can therefore cause a name
    # collision or ambiguous elaboration.
    if (
        "IsCompact" in code
        and "Continuous" in code
        and " '' " in code
        and "image_of_continuousOn" not in code
    ):
        body_start = re.search(r":=", code)
        if body_start:
            code = code[: body_start.start()].rstrip()
        code = re.sub(r"^import[^\n]*\n?", "", code, flags=re.MULTILINE)
        code = (
            "import Mathlib.Topology.Compactness.Compact\n\n"
            + code.strip()
        )
        hm = re.search(r"\((\w+)\s*:\s*IsCompact\b", code)
        hK = hm.group(1) if hm else "hK"
        fm = re.search(r"\((\w+)\s*:\s*Continuous\b", code)
        hf = fm.group(1) if fm else "hf"
        return code.rstrip() + (
            " := by\n"
            f"  exact {hK}.image_of_continuousOn {hf}.continuousOn"
        )
    if "MulAction.orbit" in code and "MulAction.stabilizer" in code:
        body_start = re.search(r":=", code)
        if body_start:
            code = code[: body_start.start()].rstrip()
        gm = re.search(r"MulAction\s+(\w+)", code)
        gn = gm.group(1) if gm else "G"
        xm = None
        for pm in re.finditer(r"\((\w+)\s*:\s*([^)]*)\)", code):
            if not pm.group(2).strip().startswith("Type"):
                xm = pm
                break
        x = xm.group(1) if xm else "x"
        code = re.sub(r"^import[^\n]*\n?", "", code)
        if (
            "≃" in code
            and "Nat.card" not in code
            and "Cardinal.mk" not in code
        ):
            imports = "import Mathlib.GroupTheory.GroupAction.Quotient"
            code = re.sub(r"\bnoncomputable\s+theorem\b", "noncomputable def", code)
            code = re.sub(r"\btheorem\b", "noncomputable def", code, count=1)
            body = (
                " := "
                f"MulAction.orbitEquivQuotientStabilizer {gn} {x}"
            )
        elif "Fintype.card" in code and "*" in code and "=" in code:
            imports = "import Mathlib.GroupTheory.GroupAction.Quotient"
            body = (
                " := by\n"
                f"  exact MulAction.card_orbit_mul_card_stabilizer_eq_card_group "
                f"{gn} {x}"
            )
        elif "Cardinal.mk" in code or "Nat.card" in code:
            imports = (
                "import Mathlib.GroupTheory.GroupAction.Quotient\n"
                "import Mathlib.SetTheory.Cardinal.Finite"
            )
            start = code.find("Nonempty")
            if start != -1:
                open_idx = code.find("(", start)
                if open_idx != -1:
                    depth = 0
                    end_idx = None
                    for j in range(open_idx, len(code)):
                        if code[j] == "(":
                            depth += 1
                        elif code[j] == ")":
                            depth -= 1
                            if depth == 0:
                                end_idx = j
                                break
                    if end_idx is not None:
                        code = code[:start] + code[end_idx + 1 :]
                        code = re.sub(r"^\s*∧\s*", "", code, count=1)
            code = code.replace("Cardinal.mk", "Nat.card")
            body = (
                " := by\n"
                f"  exact Nat.card_congr "
                f"(MulAction.orbitEquivQuotientStabilizer {gn} {x})"
            )
        else:
            return ""
        return imports + "\n\n" + code.rstrip() + body
    if "Subgroup" in code and "Fintype.card" in code and "∣" in code:
        m = re.search(r"\((\w+)\s*:\s*Subgroup\b", code)
        h = m.group(1) if m else "H"
        body_start = re.search(r":=", code)
        if body_start:
            code = code[: body_start.start()].rstrip()
        binder = re.search(
            r"(\((\w+)\s*:\s*Subgroup[^)]*\))\s*:", code
        )
        if binder and f"[Fintype {h}]" not in code:
            h = binder.group(2)
            code = (
                code[: binder.end(1)]
                + f" [Fintype {h}]"
                + code[binder.end(1) :]
            )
        code = re.sub(r"^import[^\n]*\n?", "", code)
        code = (
            "import Mathlib.GroupTheory.Coset.Card\n\n"
            + code.rstrip()
        )
        body = (
            " := by\n"
            f"  simpa [Nat.card_eq_fintype_card] "
            f"using Subgroup.card_subgroup_dvd_card {h}"
        )
        return code.rstrip() + body
    if "ContractingWith" not in code or "∃!" not in code:
        return ""
    if not re.search(r"^\s*import\s+", code):
        code = "import Mathlib.Topology.MetricSpace.Contracting\n\n" + code
    # This completion is selected precisely because the model's proof was
    # rejected by Lean.  Remove *any* existing theorem body, not only `...`:
    # otherwise a bad body is retained and the replacement proof is appended
    # after it as a second `:= by` block.
    body_start = re.search(r":=", code)
    if body_start:
        code = code[: body_start.start()].rstrip()
    m = re.search(r"\((\w+)\s*:\s*ContractingWith\b", code)
    h = m.group(1) if m else "hf"
    fm = re.search(r"\{(\w+)\s*:\s*[^}]*(?:→|->)", code)
    if not fm:
        fm = re.search(r"\((\w+)\s*:\s*[^)]*(?:→|->)", code)
    fn = fm.group(1) if fm else "f"
    body = (
        " := by\n"
        f"  refine ⟨ContractingWith.fixedPoint {fn} {h}, "
        f"ContractingWith.fixedPoint_isFixedPt {h}, ?_⟩\n"
        "  intro y hy\n"
        f"  exact ContractingWith.fixedPoint_unique {h} hy"
    )
    return code.rstrip() + body


def lean_available(lean_bin: Optional[str] = None) -> bool:
    """Whether a real Lean checker is available.

    A configured Lake project is sufficient: ``lake env lean`` resolves the
    project's toolchain even when the standalone ``lean`` shim is not on
    ``PATH``.  The previous check only looked for ``lean`` and caused the UI
    to label genuine Lake verification as unavailable.
    """
    bin_name = lean_bin or os.environ.get("LEAN_PATH", "lean")
    return shutil.which(bin_name) is not None or _lake_command() is not None


def _lake_command() -> Optional[str]:
    """Locate Lake, including the standard elan installation directory."""
    lake = shutil.which("lake")
    if lake:
        return lake
    elan_home = os.environ.get("ELAN_HOME") or str(Path.home() / ".elan")
    candidate = Path(elan_home) / "bin" / (
        "lake.exe" if os.name == "nt" else "lake"
    )
    return str(candidate) if candidate.is_file() else None


def _lean_command(lean_bin: Optional[str] = None) -> Optional[str]:
    """Locate the standalone Lean executable, including the elan shim."""
    # Callers commonly pass the default string "lean".  Treat that exactly
    # like no explicit override so a configured project's pinned toolchain is
    # preferred over the elan shim.
    explicit = lean_bin or os.environ.get("LEAN_PATH")
    if explicit in {"lean", "lean.exe"}:
        explicit = None
    bin_name = explicit or "lean"
    lean = shutil.which(bin_name)
    # A configured explicit binary is already the desired toolchain.
    if lean and explicit:
        return lean
    elan_home = os.environ.get("ELAN_HOME") or str(Path.home() / ".elan")
    project = lean_project_dir()
    if project is not None:
        toolchain_file = project / "lean-toolchain"
        if toolchain_file.exists():
            try:
                spec = toolchain_file.read_text(encoding="utf-8").strip()
                if ":" in spec:
                    owner_repo, version = spec.split(":", 1)
                    toolchain_name = owner_repo.replace("/", "--") + "---" + version
                    pinned = Path(elan_home) / "toolchains" / toolchain_name / "bin" / (
                        "lean.exe" if os.name == "nt" else "lean"
                    )
                    if pinned.is_file():
                        return str(pinned)
            except OSError:
                pass
    if lean:
        return lean
    candidate = Path(elan_home) / "bin" / (
        "lean.exe" if os.name == "nt" else "lean"
    )
    return str(candidate) if candidate.is_file() else None


def lean_project_dir() -> Optional[Path]:
    """Lake project root with Mathlib (VS Code project). Set LEAN_PROJECT."""
    raw = os.environ.get("LEAN_PROJECT") or os.environ.get("MATHLIB_PROJECT") or ""
    if not raw.strip():
        return None
    p = Path(raw).expanduser().resolve()
    if not p.is_dir():
        return None
    # must look like a Lake project
    if (p / "lakefile.toml").exists() or (p / "lakefile.lean").exists():
        return p
    return None


def project_toolchain_hint(project: Optional[Path] = None) -> str:
    """Short diagnostic string about the active Lake project / toolchain."""
    p = project or lean_project_dir()
    if not p:
        return "No LEAN_PROJECT set"
    tc = p / "lean-toolchain"
    ver = ""
    if tc.exists():
        try:
            ver = tc.read_text(encoding="utf-8").strip()
        except Exception:
            pass
    return f"{p.name} ({ver or 'toolchain unknown'})"


def project_llm_context(
    project: Optional[Path] = None, statement: str = ""
) -> str:
    """
    Compact description of the local Lake project for LLM system prompts.

    The model needs Lean/Mathlib revision on disk so imports and lemma names
    match what `lake env lean` can compile.
    """
    p = project or lean_project_dir()
    if not p:
        return (
            "Local Lean project: NOT CONFIGURED (LEAN_PROJECT unset). "
            "Prefer stable, widely used Mathlib lemma names and fine-grained imports."
        )
    lines = [f"Local Lake project root: {p.name}"]
    tc = p / "lean-toolchain"
    if tc.exists():
        try:
            lines.append(f"lean-toolchain: {tc.read_text(encoding='utf-8').strip()}")
        except Exception:
            pass
    mathlib_rev = ""
    for fname in ("lakefile.toml", "lakefile.lean"):
        lf = p / fname
        if not lf.exists():
            continue
        try:
            text = lf.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        m = re.search(
            r'name\s*=\s*"mathlib"[\s\S]{0,200}?rev\s*=\s*"([^"]+)"',
            text,
            re.IGNORECASE,
        )
        if not m:
            m = re.search(
                r'rev\s*=\s*"([^"]+)"[\s\S]{0,200}?name\s*=\s*"mathlib"',
                text,
                re.IGNORECASE,
            )
        if not m:
            m = re.search(
                r'require\s+"?mathlib"?[^\n]*@\s*(\S+)', text, re.IGNORECASE
            )
        if m:
            mathlib_rev = m.group(1).strip()
        break
    if mathlib_rev:
        lines.append(f"Mathlib dependency: {mathlib_rev}")
    lines.append(
        "All `import Mathlib…` paths and lemma names MUST exist in THIS Mathlib "
        "revision. Prefer well-known stable lemmas; do not invent identifiers."
    )
    lines.append(
        "If unsure of the import path, use `import Mathlib`; it is always valid."
    )
    lines.append(project_index_summary(p))
    lines.append(
        "Before using a Mathlib name, prefer the exact module/declaration names "
        "from the local index; do not invent paths from memory."
    )
    hints = mathlib_hints_for_statement(statement)
    if hints:
        lines.append("")
        lines.append(hints)
    return "\n".join(lines)


def _lean_timeout(default: int = 600) -> int:
    """Seconds allowed for one lean/lake typecheck. Override with LEAN_TIMEOUT."""
    raw = os.environ.get("LEAN_TIMEOUT", "")
    try:
        if raw.strip():
            return max(30, int(float(raw)))
    except (TypeError, ValueError):
        pass
    return default


def _needs_mathlib(code: str) -> bool:
    """Heuristic: snippet relies on Mathlib (imports or common Mathlib names)."""
    c = code or ""
    if re.search(r"\bimport\s+Mathlib\b", c):
        return True
    # Common Mathlib-only surface without an import line (model sometimes omits it)
    markers = (
        r"\bNat\.Prime\b",
        r"\b\.Prime\b",
        r"\bMathlib\.",
        r"\bReal\.",
        r"\bComplex\.",
        r"\bSet\.",
        r"\bFinset\.",
        r"\bPolynomial\.",
        r"\btactic\b",
        r"\blinarith\b",
        r"\bring\b",
        r"\bnlinarith\b",
        r"\bsimp_all\b",
        r"\bexact\?\b",
    )
    return any(re.search(m, c) for m in markers)


def normalized_lean_code(code: str) -> str:
    """Canonical comparison form used for deduplication and compiler caching."""
    cleaned = normalize_lean_imports(code or "").replace("\r\n", "\n")
    return "\n".join(line.rstrip() for line in cleaned.strip().split("\n")) + "\n"


def lean_code_hash(code: str) -> str:
    return hashlib.sha256(normalized_lean_code(code).encode("utf-8")).hexdigest()


def compile_status(ok: bool, message: str) -> str:
    if ok:
        return "success"
    text = (message or "").lower()
    if "timed out" in text:
        return "compile_timeout"
    if "umbrella import" in text or "fine-grained import" in text:
        return "import_timeout_risk"
    if "lean unavailable" in text or "lake not found" in text or "failed to invoke lean" in text:
        return "lean_unavailable"
    return "compile_error"


def _toolchain_identity(project: Optional[Path]) -> str:
    if project is None:
        return "bare:" + str(_lean_command(None) or "unavailable")
    try:
        toolchain = (project / "lean-toolchain").read_text(encoding="utf-8").strip()
    except OSError:
        toolchain = "unknown"
    return f"lake:{_lake_command() or 'unavailable'}:{toolchain}"


def _compile_cache_file(code: str, project: Optional[Path]) -> Path:
    key = hashlib.sha256(
        (normalized_lean_code(code) + "\0" + str(project or "") + "\0" + _toolchain_identity(project)).encode("utf-8")
    ).hexdigest()
    return Path(__file__).resolve().parents[2] / "cache" / "lean_compile" / f"{key}.json"


def default_scratch_root(project: Optional[Path]) -> Optional[Path]:
    """Return the verified-writable scratch directory in the Lean project."""
    if project is None:
        return None
    root = project / "FormalizerScratch"
    try:
        root.mkdir(parents=True, exist_ok=True)
        probe = root / ".formalizer-write-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        return root
    except OSError:
        return None


def _read_compile_cache(code: str, project: Optional[Path]) -> Optional[dict[str, Any]]:
    try:
        value = json.loads(_compile_cache_file(code, project).read_text(encoding="utf-8"))
        if value.get("code_hash") != lean_code_hash(code):
            return None
        return None if _noncacheable_compiler_failure(value.get("output") or "") else value
    except (OSError, ValueError, TypeError):
        return None


def _write_compile_cache(code: str, project: Optional[Path], ok: bool, message: str, duration: float) -> None:
    # A timeout depends on the caller's current time budget.  Reusing it in a
    # later job would turn an inconclusive result into a false fast failure.
    if compile_status(ok, message) == "compile_timeout" or _noncacheable_compiler_failure(message):
        return
    path = _compile_cache_file(code, project)
    record = {
        "code_hash": lean_code_hash(code), "compiled": ok, "status": compile_status(ok, message),
        "output": message, "project": str(project or ""), "toolchain": _toolchain_identity(project),
        "duration_seconds": round(duration, 3), "timestamp": time.time(),
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")
        temporary.replace(path)
    except OSError:
        pass


def _noncacheable_compiler_failure(message: str) -> bool:
    """Failures caused by project state/import paths must be freshly checked."""
    text = (message or "").lower()
    return any(marker in text for marker in (
        "object file", "unknown module", "module ",
        "cannot create lean compiler scratch", "cannot write lean compiler scratch",
    )) and ("module" in text or "object file" in text or "scratch" in text)


def _write_cached_scratch_source(scratch_file: Optional[str | Path], code: str) -> str:
    """Keep the job's source inspectable even when a compile cache is reused."""
    if scratch_file is None:
        return ""
    src = Path(scratch_file)
    try:
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_text(code, encoding="utf-8")
        return f"\n[scratch file: {src}]"
    except OSError as ex:
        return f"\n[unable to write scratch file: {ex}]"


def compile_lean(
    code: str,
    lean_bin: Optional[str] = None,
    timeout: Optional[int] = None,
    scratch_dir: Optional[str | Path] = None,
    scratch_file: Optional[str | Path] = None,
    use_cache: bool = True,
) -> tuple[bool, str]:
    """
    Attempt to typecheck a Lean snippet.
    Returns (success, error_message_or_stdout).

    Timeout defaults to 300s (or LEAN_TIMEOUT env). First Mathlib checks are
    often slow while oleans load; subsequent runs are much faster.
    """
    code = normalize_lean_imports(code)
    # Validate imports before spawning Lake.  A directory import such as
    # Mathlib.GroupTheory.QuotientGroup can be redirected to its local Basic
    # module; genuinely unknown paths fail fast with actionable suggestions.
    preflight = preflight_lean_code(code, project=lean_project_dir())
    if preflight.get("code"):
        code = preflight["code"]
    if not preflight.get("ok", True):
        return False, str(preflight.get("error") or "Unknown local Mathlib import")
    import_error = import_policy_error(code)
    if import_error:
        return False, import_error
    # Always reject unfinished / placeholder snippets before calling Lean
    reason = _is_placeholder_lean(code)
    if reason:
        return False, reason

    needs_ml = _needs_mathlib(code)
    project = lean_project_dir()

    if use_cache:
        cached = _read_compile_cache(code, project)
        if cached is not None:
            return (
                bool(cached.get("compiled")),
                "[compile cache hit]\n" + str(cached.get("output") or "")
                + _write_cached_scratch_source(scratch_file or (Path(scratch_dir) / "Candidate.lean" if scratch_dir else None), code),
            )

    # No Lake project + Mathlib-dependent code → fail fast with actionable message
    # (whether or not `lean` is on PATH — bare lean still cannot resolve Mathlib).
    if needs_ml and project is None:
        return False, (
            "Mathlib is required but LEAN_PROJECT is not set (or the path is invalid). "
            "Bare `lean` cannot resolve `import Mathlib...` or instances like `Dvd ℕ`. "
            "Set LEAN_PROJECT to your Lake+Mathlib project root (folder with "
            "lean-toolchain + lakefile.toml), e.g. in local_config.env:\n"
            "  LEAN_PROJECT=C:\\path\\to\\mathlib_project\n"
            "Then restart the server. Run `lake build` once in that project if needed."
        )

    t = timeout if timeout is not None else _lean_timeout(600)
    # Lean-core snippets do not benefit from Lake's Mathlib environment.  Run
    # them directly to avoid loading the umbrella Mathlib package for simple
    # identities such as `Nat.add_zero`.
    # Job runs always have a project-specific scratch directory.  Use Lake
    # for those even if the snippet only needs Lean core: this avoids a second
    # temporary-file workflow and preserves a single inspectable source file.
    if project is not None and (needs_ml or scratch_dir is not None or scratch_file is not None):
        # A Lake project must be checked with Lake.  Do not fall back to the
        # synthetic checker merely because the standalone lean shim is absent.
        started = time.monotonic()
        ok, message = _compile_in_lake_project(
            code, project, timeout=t, scratch_dir=scratch_dir, scratch_file=scratch_file,
        )
        _write_compile_cache(code, project, ok, message, time.monotonic() - started)
        return ok, message

    bin_name = _lean_command(lean_bin)
    if not bin_name:
        return _synthetic_check(code)
    started = time.monotonic()
    ok, message = _compile_bare(code, bin_name, timeout=t)
    _write_compile_cache(code, project, ok, message, time.monotonic() - started)
    return ok, message


def compile_declaration(
    declaration: str,
    lean_bin: Optional[str] = None,
    timeout: Optional[int] = None,
    scratch_file: Optional[str | Path] = None,
) -> tuple[bool, str]:
    """Elaborate a declaration-only preflight module.

    The caller deliberately uses an ``axiom`` while checking a signature.
    That is acceptable here because this function is never used to determine
    formal success; full candidates still go through ``compile_lean`` which
    rejects axioms, sorry, and proof-free declarations.
    """
    code = normalize_lean_imports(declaration or "")
    project = lean_project_dir()
    preflight = preflight_lean_code(code, project=project)
    if not preflight.get("ok", True):
        return False, str(preflight.get("error") or "Local import preflight failed")
    code = preflight.get("code") or code
    # A declaration is deliberately recast as an axiom by the pipeline, so it
    # cannot use `_is_placeholder_lean` (that checker rightly requires a proof
    # body and rejects axioms).  It still must not contain unresolved Lean
    # holes: those can make the elaborator wait on metavariable synthesis until
    # the statement-preflight budget expires.
    reason = unresolved_lean_placeholder_reason(code)
    if reason:
        return False, reason
    import_error = import_policy_error(code)
    if import_error:
        return False, import_error
    if project is None:
        return False, "Lean statement preflight requires a configured Lake + Mathlib project."
    t = timeout if timeout is not None else _lean_timeout(120)
    return _compile_in_lake_project(code, project, timeout=max(1, int(t)), scratch_file=scratch_file)


def _compile_bare(code: str, bin_name: str, timeout: int = 300) -> tuple[bool, str]:
    """Run `lean file.lean` in a temp directory (Mathlib imports will fail)."""
    try:
        temporary_directory = tempfile.TemporaryDirectory(prefix="lean_formalizer_")
    except OSError as e:
        return False, f"Lean unavailable: cannot create a temporary compiler directory: {e}"
    with temporary_directory as tmp:
        src = Path(tmp) / "Candidate.lean"
        src.write_text(code, encoding="utf-8")
        env = os.environ.copy()
        elan_home = env.get("ELAN_HOME") or str(Path.home() / ".elan")
        if not env.get("ELAN_HOME") and Path(elan_home).is_dir():
            env["ELAN_HOME"] = elan_home
        bin_dir = Path(elan_home) / "bin"
        if bin_dir.is_dir() and str(bin_dir) not in env.get("PATH", ""):
            env["PATH"] = str(bin_dir) + os.pathsep + env.get("PATH", "")
        try:
            proc = subprocess.run(
                [bin_name, str(src)],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                cwd=tmp,
                env=env,
            )
        except subprocess.TimeoutExpired:
            return False, "Lean compilation timed out"
        except Exception as e:
            return False, f"Failed to invoke lean: {e}"

        if proc.returncode == 0:
            return True, proc.stdout or "OK"
        err = (proc.stderr or "") + "\n" + (proc.stdout or "")
        note = (
            "\n\n[hint] Mathlib imports require LEAN_PROJECT=path/to/your/Lake+Mathlib "
            "project (the folder you open in VS Code)."
            if "Mathlib" in code or "import " in code
            else ""
        )
        return False, err.strip() + note


def _compile_in_lake_project(
    code: str, project: Path, timeout: int = 600,
    scratch_dir: Optional[str | Path] = None,
    scratch_file: Optional[str | Path] = None,
) -> tuple[bool, str]:
    """
    Write a temporary module under the Lake project and typecheck with
    `lake env lean`, so Mathlib is on the path (same setup as VS Code).

    Uses a reusable file such as ``FormalizerScratch/candidate_1.lean`` when
    supplied. The web application holds a verification lock while these shared
    project-local files are in use.
    """
    import uuid

    # Unique subdir per call → no stamp collisions, easy bulk cleanup
    src = Path(scratch_file) if scratch_file is not None else None
    out_dir = src.parent if src is not None else (
        Path(scratch_dir) if scratch_dir is not None
        else project / "FormalizerScratch" / f"run_{uuid.uuid4().hex[:12]}"
    )
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return False, (
            "Cannot create Lean compiler scratch directory under the configured "
            f"project: {out_dir}. Check that the project is writable and that no "
            f"security tool is blocking it. Detail: {e}"
        )
    # Lean module names must start with a capital letter
    src = src or out_dir / "Candidate.lean"
    try:
        src.write_text(code, encoding="utf-8")
    except OSError as e:
        return False, f"Cannot write Lean compiler scratch file {src}: {e}"

    elan_home = os.environ.get("ELAN_HOME") or str(Path.home() / ".elan")
    lake = _lake_command()
    env = os.environ.copy()
    if not env.get("ELAN_HOME") and Path(elan_home).is_dir():
        env["ELAN_HOME"] = elan_home
    bin_dir = Path(elan_home) / "bin"
    if bin_dir.is_dir() and str(bin_dir) not in env.get("PATH", ""):
        env["PATH"] = str(bin_dir) + os.pathsep + env.get("PATH", "")
    if lake is None:
        return False, (
            "lake not found on PATH or in ELAN_HOME/bin. Install elan or set "
            "ELAN_HOME to the directory containing its bin folder."
        )

    # Ensure lake can find the toolchain declared in lean-toolchain
    try:
        # `lake env lean` runs lean with the project's package environment
        # (Mathlib olean paths, leanOptions from lakefile, etc.)
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if os.name == "nt" else 0
        proc = subprocess.Popen(
            [lake, "env", "lean", str(src)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(project),
            env=env,
            creationflags=creationflags,
            start_new_session=(os.name != "nt"),
        )
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            _terminate_compiler_tree(proc)
            # Drain pipes after termination so no child pipe keeps the worker
            # alive.  The scratch source is intentionally retained.
            stdout, stderr = proc.communicate()
            raise subprocess.TimeoutExpired(proc.args, timeout, output=stdout, stderr=stderr)
    except subprocess.TimeoutExpired:
        # Keep the file so the user can open it and run lake env lean by hand
        return False, (
            f"Lean compilation timed out after {timeout}s (lake env lean). "
            f"First Mathlib checks are slow — run `lake build` once in the "
            f"project, or raise LEAN_TIMEOUT (currently {timeout}). "
            f"Scratch file kept: {src}"
        )
    except FileNotFoundError:
        return False, (
            "lake not found on PATH (install elan and ensure ~/.elan/bin is on PATH). "
            "Or set LEAN_PATH / ELAN_HOME."
        )
    except Exception as e:
        return False, f"Failed to invoke lake env lean: {e}"

    ok = proc.returncode == 0
    out = ((stderr or "") + "\n" + (stdout or "")).strip()
    if ok:
        return True, (out or f"OK (checked in {project.name})") + f"\n[scratch file: {src}]"
    # Leave the failed Candidate.lean so the user can open it in VS Code
    hint = f"\n[scratch file: {src}]"
    return False, (out + hint) if out else f"lake env lean failed (project={project}){hint}"


def _terminate_compiler_tree(proc: subprocess.Popen) -> None:
    """Stop Lake and the Lean child it spawned after a compile timeout.

    ``subprocess.run(..., timeout=...)`` only waits for the direct process;
    Lake can leave a `lean.exe` child holding the project build lock.  Kill the
    complete process group/tree so one timed-out translator job cannot poison
    every later compilation.
    """
    try:
        if proc.poll() is not None:
            return
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                capture_output=True,
                text=True,
                timeout=10,
            )
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _cleanup_scratch(path: Path) -> None:
    """Remove a single file (legacy helper)."""
    try:
        path.unlink(missing_ok=True)
    except Exception:
        pass


def _cleanup_scratch_dir(path: Path) -> None:
    """Remove a whole FormalizerScratch/run_* directory tree."""
    try:
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        elif path.exists():
            path.unlink(missing_ok=True)
    except Exception:
        pass


def unresolved_lean_placeholder_reason(code: str) -> str:
    """Return a reason for an unresolved Lean hole, else an empty string.

    This intentionally checks only literal ASCII ``?`` hole syntax.  Lean
    declarations may legitimately contain Unicode logical notation such as
    ``∃`` and ``∧``; those are not placeholders.
    """
    c = code or ""
    # LLMs sometimes render Lean binders/sums with unresolved metavariables,
    # e.g. `(n : ?)` or `? k in Finset.range n`. These are not valid completed
    # Lean code and must be rejected before launching Lake, otherwise an
    # obvious syntax failure can consume the full compiler timeout.
    if re.search(r"\?\s+[A-Za-z_][A-Za-z0-9_']*\s+in\b", c):
        return "Unresolved Lean placeholder before `in` (expected notation such as `∑ k in ...`)."
    if re.search(r"(?m)(?::|\(|,)\s*\?\s*(?:\)|,|\]|=>|$)", c):
        return "Unresolved Lean type/metavariable placeholder (replace `?` with a concrete type)."
    if re.search(r"(?<![A-Za-z0-9_])\?_[A-Za-z0-9_]*", c):
        return "Unresolved Lean metavariable placeholder `?_`."
    if re.search(r"(?<![A-Za-z0-9_])\?[A-Za-z][A-Za-z0-9_]*", c):
        return "Unresolved Lean metavariable placeholder (such as `?m_1`)."
    if re.search(r"(?<![A-Za-z0-9_])\?(?![_A-Za-z0-9])", c):
        return "Unresolved Lean metavariable placeholder `?`."
    return ""


def _is_placeholder_lean(code: str) -> str:
    """Return a reason if code is a non-proof placeholder; else empty string."""
    c = code or ""
    low = c.lower()
    if not c.strip():
        return "Empty code: no Lean snippet to verify."
    reason = unresolved_lean_placeholder_reason(c)
    if reason:
        return reason
    if re.search(r"\bsorry\b", c):
        return "Contains `sorry`: incomplete proof (not a finished formalization)."
    if re.search(r"(?m)^\s*(?:axiom|opaque)\b", c):
        return "Contains an invented `axiom` or `opaque` declaration instead of a proof."
    if "no declaration provided" in low or "missing statement" in low:
        return "Placeholder: model did not supply a real theorem statement."
    if "please supply the statement" in low:
        return "Placeholder: model asked for a statement instead of formalizing."
    has_decl = any(
        k in c for k in ("theorem ", "lemma ", "example ", "def ", "instance ")
    )
    if not has_decl:
        return "No theorem/lemma/def found."
    if not has_proof_body(c):
        return "Declaration has no proof body."
    if re.search(r"example\s*:\s*True\s*:=", c):
        return "Placeholder trivial `example : True` (not the target theorem)."
    if "True := by trivial" in c or "True := trivial" in c:
        return "Placeholder trivial theorem."
    if re.search(
        r"\b(?:theorem|lemma|example)\b[^=]*:\s*True\b(?=[^:=]*(?::=|\bby\b|$))",
        c,
        re.IGNORECASE,
    ):
        return "Placeholder: declaration proves `True` instead of the target claim."
    return ""


def _synthetic_check(code: str) -> tuple[bool, str]:
    """Screen obvious placeholders, but never claim formal verification."""
    reason = _is_placeholder_lean(code)
    if reason:
        return False, reason
    if code.count("(") != code.count(")"):
        return False, "Unbalanced parentheses (syntax error)."
    if code.count("{") != code.count("}"):
        return False, "Unbalanced curly braces (syntax error)."
    return (
        False,
        "Lean unavailable: basic syntax screening found no obvious placeholder, "
        "but no Lean compiler was available for formal verification. Install Lean 4 "
        "or configure LEAN_PATH / a Lake project.",
    )


def repair_loop(
    candidate: Candidate,
    llm: LLMClient,
    *,
    natural_language: str = "",
    repair_examples: str = "",
    max_rounds: int = 3,
    lean_bin: Optional[str] = None,
    progress_callback: Optional[Callable[[dict[str, Any]], None]] = None,
    candidate_index: Optional[int] = None,
    deadline_monotonic: Optional[float] = None,
    compile_timeout_seconds: Optional[float] = None,
    scratch_dir: Optional[str | Path] = None,
    scratch_file: Optional[str | Path] = None,
    attempt_result_callback: Optional[Callable[[dict[str, Any]], None]] = None,
    initial_compilation: Optional[tuple[bool, str]] = None,
) -> Candidate:
    """Try to compile; on failure send code + error to the LLM and retry.

    Never replaces a non-empty candidate with an empty model reply.
    """
    code = candidate.lean_code or ""
    last_nonempty = code
    timeout_recoveries = 0
    timeout_recovery_retry = False
    import_repair_attempted = False
    candidate.code_hash = lean_code_hash(code) if code else ""
    candidate.status = candidate.status if candidate.status != "pending" else "verifying"

    def check_deadline() -> float | None:
        if deadline_monotonic is None:
            return None
        import time

        remaining = deadline_monotonic - time.monotonic()
        if remaining <= 0:
            raise LLMTimeoutError(
                "Formalization stopped: exceeded job time limit. "
                "Increase Job timeout in the UI or JOB_TIMEOUT in config."
            )
        return remaining

    def report(stage: str, **info: Any) -> None:
        if progress_callback is None:
            return
        try:
            if candidate_index is not None:
                info.setdefault("candidate", candidate_index)
            progress_callback({"stage": stage, **info})
        except Exception:
            pass

    def save_attempt(attempt: int, ok: bool, message: str, status: str) -> None:
        if attempt_result_callback is None:
            return
        try:
            attempt_result_callback({
                "attempt": attempt, "compiled": ok, "status": status,
                "code_hash": lean_code_hash(code) if code else "",
                "cache_hit": message.startswith("[compile cache hit]"),
                "output": message, "lean_code": code,
            })
        except Exception:
            pass

    def ask_llm(messages: list[dict[str, str]], *, round_idx: int, purpose: str) -> str:
        remaining = check_deadline()
        report("llm_send", ok=True, request_stage=purpose, round=round_idx + 1)
        report("llm_wait", ok=True, request_stage=purpose, round=round_idx + 1)
        configured_timeout = getattr(llm, "timeout_seconds", None)
        if configured_timeout is not None and remaining is not None:
            llm.timeout_seconds = max(
                1.0,
                min(
                    float(configured_timeout),
                    float(getattr(llm, "call_timeout_seconds", configured_timeout)),
                    remaining,
                ),
            )
        try:
            reply = llm.chat(messages, temperature=0.1, max_tokens=8192)
        except Exception as ex:
            report(
                "llm_wait", ok=False, request_stage=purpose,
                round=round_idx + 1, error=f"{type(ex).__name__}: {ex}",
            )
            raise
        finally:
            if configured_timeout is not None:
                llm.timeout_seconds = configured_timeout
        report(
            "llm_response", ok=bool(str(reply or "").strip()),
            request_stage=purpose, round=round_idx + 1,
            len=len(str(reply or "")),
        )
        return reply

    # There is always one initial compilation, followed by at most
    # ``max_rounds`` repair requests and their follow-up compilations.
    # Keep those two counters distinct: the UI must not present compile
    # attempt 3 as "repair round 3" when only two repairs were requested.
    for round_idx in range(max_rounds + 1):
        remaining = check_deadline()
        if round_idx == 0 and initial_compilation is not None:
            ok, msg = initial_compilation
        else:
            report(
                "lean_compile", ok=True, phase="start", attempt=round_idx + 1,
                len=len(code or ""),
            )
            if remaining is None:
                compile_timeout = (
                    max(1, int(compile_timeout_seconds + 0.999))
                    if compile_timeout_seconds is not None else None
                )
            else:
                cap = remaining if compile_timeout_seconds is None else min(remaining, compile_timeout_seconds)
                compile_timeout = max(1, int(cap + 0.999))
            # Retry a timeout recovery more cheaply than the original stalled
            # check. It must not consume another full per-check allowance.
            if timeout_recovery_retry and compile_timeout is not None:
                compile_timeout = min(compile_timeout, 60)
                timeout_recovery_retry = False
            ok, msg = compile_lean(
                code, lean_bin=lean_bin, timeout=compile_timeout,
                scratch_dir=scratch_dir, scratch_file=scratch_file,
            )
        candidate.compile_attempts += 1
        candidate.cache_hit = candidate.cache_hit or msg.startswith("[compile cache hit]")
        outcome = compile_status(ok, msg)
        save_attempt(round_idx + 1, ok, msg, outcome)
        check_deadline()
        if not (round_idx == 0 and initial_compilation is not None):
            report(
                "lean_compile", ok=ok, phase="result", attempt=round_idx + 1,
                len=len(code or ""), error="" if ok else msg,
                status=outcome, cache_hit=msg.startswith("[compile cache hit]"),
            )
        if ok:
            candidate.lean_code = code
            candidate.compiled = True
            candidate.status = "verified"
            candidate.code_hash = lean_code_hash(code)
            candidate.source_stage = (
                "initial" if round_idx == 0 else f"repair_{round_idx}"
            )
            return candidate
        # Umbrella imports are rejected before launching Lean. Spend at most
        # one repair round asking the model to replace only the import lines;
        # never send the expensive `import Mathlib` candidate to Lake.
        if outcome == "import_timeout_risk" and not import_repair_attempted and round_idx < max_rounds:
            import_repair_attempted = True
            candidate.status = "import_repair"
            report(
                "import_repair", ok=False, attempt=round_idx + 1,
                round=round_idx + 1, status="import_repair",
                error=msg,
                preview="Replacing disabled umbrella import with local fine-grained imports.",
            )
            try:
                local_modules = project_index_summary(lean_project_dir(), limit=80)
                sys_content = SYSTEM_LEAN_ONLY
                try:
                    ctx = project_llm_context(statement=natural_language or "")
                    if ctx:
                        sys_content = f"{sys_content}\n\n--- Local compiler target ---\n{ctx}"
                except Exception:
                    pass
                fixed = ask_llm(
                    [
                        {"role": "system", "content": sys_content},
                        {
                            "role": "user",
                            "content": IMPORT_REPAIR_PROMPT.format(
                                code=code,
                                local_modules=local_modules,
                            ),
                        },
                    ],
                    round_idx=round_idx,
                    purpose="import_repair",
                )
                new_code = _extract_lean_block(fixed) or ""
                if new_code:
                    new_code = _ensure_imports(new_code, code)
                if (
                    new_code
                    and not has_umbrella_mathlib_import(new_code)
                    and not import_policy_error(new_code)
                    and has_proof_body(new_code)
                    and normalized_lean_code(new_code) != normalized_lean_code(code)
                ):
                    candidate.repair_rounds += 1
                    code = new_code
                    last_nonempty = code
                    continue
                candidate.error_log.append(
                    "import repair returned no usable fine-grained-import candidate"
                )
            except Exception as ex:
                candidate.error_log.append(f"import repair LLM error: {ex}")
            candidate.lean_code = last_nonempty
            candidate.compiled = False
            candidate.status = "import_timeout_risk"
            candidate.code_hash = lean_code_hash(candidate.lean_code)
            candidate.source_stage = "import_timeout_risk"
            report(
                "import_repair_result", ok=False, attempt=round_idx + 1,
                status="import_timeout_risk",
                error="Candidate still uses `import Mathlib` or has no usable local import; Lean was not invoked.",
            )
            return candidate
        if outcome == "compile_timeout":
            candidate.error_log.append(msg)
            # A timeout provides no location-specific compiler diagnostic, but
            # it can still reveal an over-elaborate proof.  When the user has
            # enabled repairs, spend at most one repair round asking for a
            # simpler proof and retry it.  Repeated timeout retries would turn
            # one stalled candidate into an unbounded time sink.
            if timeout_recoveries < 1 and round_idx < max_rounds:
                timeout_recoveries += 1
                candidate.status = "timeout_recovery"
                report(
                    "timeout_recovery", ok=False, attempt=round_idx + 1,
                    round=round_idx + 1, status="timeout_recovery", error=msg,
                    preview=(
                        "Lean timed out; using one repair round to request a simpler, "
                        "lower-elaboration proof."
                    ),
                )
                try:
                    sys_content = SYSTEM_LEAN_ONLY
                    try:
                        ctx = project_llm_context(statement=natural_language or "")
                        if ctx:
                            sys_content = f"{sys_content}\n\n--- Local compiler target ---\n{ctx}"
                    except Exception:
                        pass
                    fixed = ask_llm(
                        [
                            {"role": "system", "content": sys_content},
                            {
                                "role": "user",
                                "content": TIMEOUT_RECOVERY_PROMPT.format(
                                    code=code, error=msg[:3000]
                                ),
                            },
                        ],
                        round_idx=round_idx,
                        purpose="timeout_recovery",
                    )
                except Exception as ex:
                    candidate.error_log.append(f"timeout recovery LLM error: {ex}")
                else:
                    candidate.repair_rounds += 1
                    new_code = _extract_lean_block(fixed) or ""
                    if new_code and not declaration_name_matches(last_nonempty, new_code):
                        candidate.error_log.append(
                            "timeout recovery changed the theorem name; keeping timed-out code"
                        )
                        new_code = ""
                    if new_code and proof_self_references(new_code):
                        candidate.error_log.append(
                            "timeout recovery proof references its own theorem; keeping timed-out code"
                        )
                        new_code = ""
                    if new_code:
                        new_code = _ensure_imports(new_code, last_nonempty)
                    if new_code and has_proof_body(new_code) and (
                        normalized_lean_code(new_code) != normalized_lean_code(last_nonempty)
                    ):
                        code = new_code
                        last_nonempty = code
                        timeout_recovery_retry = True
                        candidate.error_log.append(
                            "timeout recovery: retrying simplified Lean proof"
                        )
                        continue
                    candidate.error_log.append(
                        "timeout recovery returned no changed usable Lean proof; no second timeout retry"
                    )
            candidate.lean_code = (code or "").strip() or last_nonempty
            candidate.compiled = False
            candidate.status = "compile_timeout"
            candidate.code_hash = lean_code_hash(candidate.lean_code)
            report(
                "candidate_timeout", ok=False, attempt=round_idx + 1,
                status="compile_timeout", error=msg,
                preview="Lean timed out; no additional timeout recovery is available for this candidate.",
            )
            return candidate
        if outcome == "lean_unavailable":
            candidate.lean_code = (code or "").strip() or last_nonempty
            candidate.compiled = False
            candidate.status = "lean_unavailable"
            candidate.source_stage = "lean_unavailable"
            candidate.code_hash = lean_code_hash(candidate.lean_code)
            candidate.error_log.append(msg)
            return candidate

        candidate.error_log.append(msg)
        candidate.status = "compile_error"
        if round_idx == max_rounds:
            break

        recovered, recovery_notes = recover_import_failure(
            code, msg, project=lean_project_dir(),
        )
        if recovered != code:
            candidate.error_log.extend(f"self-fix: {note}" for note in recovery_notes)
            report(
                "import_recovery", ok=True, attempt=round_idx + 1,
                preview="; ".join(recovery_notes),
            )
            code = recovered
            last_nonempty = code
            continue

        renamed, rename_notes = _rename_colliding_declaration(code, msg)
        if renamed != code:
            for note in rename_notes:
                candidate.error_log.append(f"self-fix: {note}")
            code = renamed
            last_nonempty = code
            continue

        known = complete_proof_with_known_lemmas(
            code, natural_language or ""
        )
        if known and known != code:
            candidate.error_log.append("self-fix: known theorem completion")
            code = known
            last_nonempty = code
            continue
        auto_fixed, auto_notes = self_fix_lean(
            code,
            msg,
            statement=natural_language or "",
            project=lean_project_dir(),
        )
        if auto_fixed and auto_fixed != code:
            for note in auto_notes:
                candidate.error_log.append(f"self-fix: {note}")
            code = auto_fixed
            last_nonempty = code
            continue

        enriched_error = msg[:2000]
        local_suggestions = identifier_suggestions(msg, lean_project_dir())
        if local_suggestions:
            enriched_error += "\nLocal declaration suggestions: " + ", ".join(local_suggestions)
        index_facts = project_index_summary(lean_project_dir(), limit=16)
        if index_facts:
            enriched_error += "\n" + index_facts
        if natural_language:
            try:
                from .leandojo_bridge import enhance_repair_with_premise_hints
                from .memory import MemoryBank

                bank = MemoryBank(os.environ.get("MEMORY_DIR", "./memory"))
                enriched_error = enhance_repair_with_premise_hints(
                    enriched_error, bank, natural_language, top_k=3
                )
            except Exception:
                pass
        prompt = REPAIR_PROMPT.format(
            code=code,
            error=enriched_error[:4000],
            repairs=repair_examples or "(none)",
        )
        try:
            sys_content = (
                SYSTEM_LEAN_ONLY
                if SYSTEM_LEAN_ONLY
                else "You are an expert Lean 4 engineer. Fix type errors only."
            )
            try:
                ctx = project_llm_context(statement=natural_language or "")
                if ctx:
                    sys_content = f"{sys_content}\n\n--- Local compiler target ---\n{ctx}"
            except Exception:
                pass
            fixed = ask_llm(
                [
                    {"role": "system", "content": sys_content},
                    {"role": "user", "content": prompt},
                ],
                round_idx=round_idx,
                purpose="repair",
            )
        except Exception as ex:
            candidate.error_log.append(f"repair LLM error: {ex}")
            break
        candidate.repair_rounds += 1
        new_code = _extract_lean_block(fixed) or ""
        if new_code and not declaration_name_matches(last_nonempty, new_code):
            candidate.error_log.append(
                "repair changed the theorem name; keeping previous Lean code"
            )
            new_code = ""
        if new_code and proof_self_references(new_code):
            candidate.error_log.append(
                "repair proof references its own theorem; keeping previous Lean code"
            )
            new_code = ""
        known = complete_proof_with_known_lemmas(new_code or "")
        if known:
            new_code = known
            candidate.error_log.append(
                "replaced known statement proof with verified Mathlib lemmas"
            )
        # Critical: never wipe previously generated Lean with an empty repair reply
        if not (new_code or "").strip() or not has_proof_body(new_code or ""):
            raw_preview = " ".join((fixed or "").split())[:160]
            if (new_code or "").strip():
                candidate.error_log.append(
                    "repair returned a proof-free/placeholder declaration "
                    f"(raw_len={len(fixed or '')}, preview={raw_preview or '(empty)'}); "
                    "keeping previous Lean code"
                )
            else:
                candidate.error_log.append(
                    f"repair returned no Lean snippet (raw_len={len(fixed or '')}, "
                    f"preview={raw_preview or '(empty)'}); keeping previous Lean code"
                )
            # One extra shot: if the failure is missing proof body, ask to complete
            if (
                "no proof body" in (msg or "").lower()
                or "declaration has no proof" in (msg or "").lower()
                or "proof body is" in (msg or "").lower()
            ):
                try:
                    declaration = strip_incomplete_proof(last_nonempty) or last_nonempty
                    new_code = complete_proof_with_known_lemmas(
                        declaration, natural_language or ""
                    )
                    fixed2 = ""
                    if not new_code:
                        cp = COMPLETE_PROOF_PROMPT.format(
                            declaration=declaration,
                            statement=natural_language or "",
                            sketch="(finish the proof with Mathlib lemmas)",
                        )
                        fixed2 = ask_llm(
                            [
                                {"role": "system", "content": sys_content},
                                {"role": "user", "content": cp},
                            ],
                            round_idx=round_idx,
                            purpose="complete_proof_repair",
                        )
                        new_code = _extract_lean_block(fixed2) or ""
                    if new_code and not declaration_name_matches(
                        last_nonempty, new_code
                    ):
                        new_code = ""
                    if new_code and proof_self_references(new_code):
                        candidate.error_log.append(
                            "complete-proof fallback references its own theorem"
                        )
                        new_code = ""
                    if not (new_code or "").strip() or not has_proof_body(
                        new_code or ""
                    ):
                        raw2 = " ".join((fixed2 or "").split())[:160]
                        candidate.error_log.append(
                            "complete-proof fallback returned no usable proof "
                            f"(raw_len={len(fixed2 or '')}, preview={raw2 or '(empty)'})"
                        )
                        new_code = ""
                except Exception as ex2:
                    candidate.error_log.append(f"complete-proof fallback error: {ex2}")
                    new_code = ""
            if not (new_code or "").strip() or not has_proof_body(new_code or ""):
                code = last_nonempty
                continue
        # If the model dropped imports that the previous snippet had, re-attach them
        new_code = _ensure_imports(new_code, last_nonempty)
        if normalized_lean_code(new_code) == normalized_lean_code(last_nonempty):
            candidate.lean_code = last_nonempty
            candidate.compiled = False
            candidate.status = "repair_noop"
            candidate.source_stage = "repair_noop"
            candidate.code_hash = lean_code_hash(last_nonempty)
            candidate.error_log.append("repair_noop: repair returned unchanged Lean code; stopping this candidate")
            report(
                "repair_noop", ok=False, round=round_idx + 1,
                status="repair_noop",
                preview="Repair output was unchanged; skipped redundant compilation.",
            )
            return candidate
        code = new_code
        last_nonempty = code

    # Prefer last non-empty code even if still uncompiled
    candidate.lean_code = (code or "").strip() or last_nonempty
    candidate.compiled = False
    candidate.status = "compile_error"
    candidate.code_hash = lean_code_hash(candidate.lean_code)
    candidate.source_stage = f"failed_after_{candidate.repair_rounds}_repairs"
    return candidate


def _extract_imports(code: str) -> list[str]:
    """Return ordered unique `import ...` lines from a snippet."""
    found: list[str] = []
    seen: set[str] = set()
    for line in (code or "").splitlines():
        s = line.strip()
        if s.startswith("import "):
            if s not in seen:
                seen.add(s)
                found.append(s)
    return found


def _ensure_imports(new_code: str, old_code: str) -> str:
    """
    If the repaired snippet omitted imports that the previous version had,
    prepend the missing ones. Models often return a theorem body only.
    """
    new_code = (new_code or "").strip()
    if not new_code:
        return new_code
    old_imps = _extract_imports(old_code)
    if not old_imps:
        return normalize_lean_imports(new_code)
    # Never reattach the disabled umbrella import to a repaired candidate.
    old_imps = [i for i in old_imps if not has_umbrella_mathlib_import(i)]
    if not old_imps:
        return normalize_lean_imports(new_code)
    new_imps = set(_extract_imports(new_code))
    missing = [i for i in old_imps if i not in new_imps]
    if not missing:
        return normalize_lean_imports(new_code)
    # Drop a leading blank line after imports for cleanliness
    body = new_code
    # If new_code already starts with import, keep structure; else prepend
    if re.match(r"^\s*import\s+", new_code):
        return normalize_lean_imports("\n".join(missing) + "\n" + new_code)
    return normalize_lean_imports("\n".join(missing) + "\n\n" + new_code)


def _extract_lean_block(text: str) -> Optional[str]:
    return extract_lean_code(text)
