"""Compile diagnostics: structured reports + natural-language explanations.

Goal: when formalization fails (or succeeds), tell the user *what is wrong*
with the mathematical proof / Lean encoding in plain language — not only a
raw compiler dump.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict
from typing import Any, Optional

from .llm import LLMClient
from .prompts import SYSTEM_MATH


@dataclass
class Issue:
    severity: str  # error | warning | info
    category: str  # syntax | unknown_ident | type_mismatch | incomplete | sorry | logic | other
    message: str
    hint: str = ""
    lean_fragment: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class VerificationReport:
    """Human-readable + machine-readable verification result."""

    compiled: bool
    lean_available: bool
    raw_message: str = ""
    summary: str = ""                 # short NL summary for the UI
    math_feedback: str = ""           # compiler-derived formalization feedback
    issues: list[Issue] = field(default_factory=list)
    suggestions: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        return d


# ---------------------------------------------------------------------------
# Rule-based classification of Lean / synthetic errors
# ---------------------------------------------------------------------------

_PATTERNS: list[tuple[str, str, str, str]] = [
    # category, regex, short message, hint
    (
        "sorry",
        r"\bsorry\b|declaration uses 'sorry'",
        "Proof is incomplete (`sorry`).",
        "Replace `sorry` with a real tactic proof or a Mathlib lemma (`exact …`).",
    ),
    (
        "unknown_ident",
        r"unknown (constant|identifier|declaration)|failed to synthesize|invalid field",
        "Unknown name / missing import.",
        "Check Mathlib imports and spelling (e.g. `Nat.even_or_odd`, `Set.mem_union_left`).",
    ),
    (
        "type_mismatch",
        r"type mismatch|has type|expected type|application type mismatch",
        "Type mismatch — arguments or conclusion do not fit.",
        "Compare the expected type with what you provided; add coercions or adjust quantifiers.",
    ),
    (
        "incomplete",
        r"unsolved goals|tactic failed|no goals to be solved|failed is false|unexpected end of input|unexpected end|incomplete declaration",
        "The Lean statement or proof is incomplete.",
        "Complete the declaration and close every binder, proposition, and proof goal before checking mathematical correctness.",
    ),
    (
        "syntax",
        r"unexpected token|expected |invalid.*parser|Unbalanced parentheses|No theorem",
        "Syntax / structure problem in the Lean code.",
        "Ensure `theorem`/`lemma` has a type and a proof body (`:= by …` or `:= …`).",
    ),
    (
        "import",
        r"unknown module|object file|failed to load|no such file|umbrella import|fine-grained import",
        "Import / module load failure.",
        "Use fine-grained Mathlib imports and a project with Mathlib (Lake) configured.",
    ),
]


def classify_error(raw: str, code: str = "") -> list[Issue]:
    issues: list[Issue] = []
    text = (raw or "") + "\n" + (code or "")
    lower = text.lower()
    seen = set()
    for cat, pat, msg, hint in _PATTERNS:
        if re.search(pat, text, re.IGNORECASE):
            if cat in seen:
                continue
            seen.add(cat)
            issues.append(
                Issue(severity="error", category=cat, message=msg, hint=hint)
            )
    if "sorry" in (code or "") and "sorry" not in seen:
        issues.append(
            Issue(
                severity="error",
                category="sorry",
                message="Proof contains `sorry`.",
                hint="A compiling proof must not leave goals unfinished.",
            )
        )
    if not issues and raw and "ok" not in lower and "synthetic-ok" not in lower:
        issues.append(
            Issue(
                severity="error",
                category="other",
                message="Compilation failed.",
                hint=raw[:300],
            )
        )
    return issues


def rule_based_summary(compiled: bool, issues: list[Issue], lean_available: bool) -> str:
    if compiled:
        if lean_available:
            return "Lean compilation succeeded. The formal proof type-checks."
        return (
            "Synthetic check passed (Lean binary not installed). "
            "Install Lean 4 + Mathlib for real verification."
        )
    if not issues:
        return "Verification failed."
    parts = [i.message for i in issues[:3]]
    return "Verification failed: " + " ".join(parts)


def rule_based_math_feedback(
    compiled: bool, issues: list[Issue], natural_language: str = ""
) -> str:
    if compiled:
        return (
            "The formal statement is consistent with a type-checked proof. "
            "This does not by itself certify that the *informal* claim matches "
            "your intended mathematics — spot-check the theorem type."
        )
    lines = []
    cats = {i.category for i in issues}
    if "sorry" in cats or "incomplete" in cats:
        lines.append(
            "The mathematical argument is unfinished in Lean: some goals were not closed. "
            "Often this means a missing case, an unused hypothesis, or a gap that would "
            "be a ‘hand-wavy’ step on paper."
        )
    if "type_mismatch" in cats:
        lines.append(
            "A type mismatch usually means the formal statement does not line up with the "
            "proof step (wrong quantifier order, missing assumption, or applying a lemma "
            "to the wrong arguments) — analogous to an invalid inference on paper."
        )
    if "unknown_ident" in cats or "import" in cats:
        lines.append(
            "Lean cannot find a definition or lemma you named. On paper this is like citing "
            "a theorem that is not in scope; fix imports or use the Mathlib name."
        )
    if "syntax" in cats:
        lines.append(
            "The code is not well-formed Lean, so the *proof idea* was not even checked. "
            "Fix structure before judging mathematical correctness."
        )
    if not lines:
        lines.append(
            "The formalization did not check. See the detailed issues and raw compiler output."
        )
    if natural_language:
        lines.append(f"Claim under consideration: {natural_language[:200]}")
    return " ".join(lines)


def build_report(
    *,
    compiled: bool,
    raw_message: str,
    code: str = "",
    natural_language: str = "",
    lean_available: bool = False,
    llm: Optional[LLMClient] = None,
) -> VerificationReport:
    issues = [] if compiled else classify_error(raw_message, code)
    if not compiled and not (code or "").strip() and (raw_message or "").strip():
        summary = raw_message.strip()[:300]
    else:
        summary = rule_based_summary(compiled, issues, lean_available)
    math_feedback = rule_based_math_feedback(compiled, issues, natural_language)
    suggestions = [i.hint for i in issues if i.hint]

    # The final explanation must remain reproducible from the local Lean
    # checker.  In particular, do not let an API response turn a compiler
    # failure into a claim about the mathematical proof.

    return VerificationReport(
        compiled=compiled,
        lean_available=lean_available,
        raw_message=(raw_message or "")[:4000],
        summary=summary,
        math_feedback=math_feedback,
        issues=issues,
        suggestions=suggestions[:6],
    )


DIAGNOSE_PROMPT = """\
A student tried to formalize a mathematical claim in Lean 4. Compilation failed.
Explain in clear English what is wrong, mixing *mathematical* and *formalization* issues.

Claim (natural language):
{statement}

Lean code:
```lean
{code}
```

Compiler / checker message:
```
{error}
```

Write 3–6 sentences:
1. What the error means for the proof.
2. Whether the informal idea might still be right but badly encoded.
3. Concrete next steps (lemma to use, import, tactic, or fix to the statement).

Do not output Lean code. Plain prose only.
"""


def llm_explain_failure(
    llm: LLMClient,
    *,
    natural_language: str,
    code: str,
    error: str,
    fallback: str,
) -> str:
    prompt = DIAGNOSE_PROMPT.format(
        statement=natural_language[:600] or "(not provided)",
        code=(code or "")[:2500],
        error=(error or "")[:1500],
    )
    reply = llm.chat(
        [
            {"role": "system", "content": SYSTEM_MATH},
            {"role": "user", "content": prompt},
        ],
        temperature=0.2,
        max_tokens=800,
    )
    text = (reply or "").strip()
    return text[:2000] if text else fallback
