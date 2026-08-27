"""
LeanDojo integration for the formalization pipeline.

LeanDojo (https://leandojo.org / https://github.com/lean-dojo/LeanDojo) provides:
  1. Data extraction from Mathlib (states, tactics, premises)
  2. Programmatic interaction with Lean proofs (Dojo REPL)

This module:
  - Optionally imports lean_dojo when installed + Lean is available
  - Imports LeanDojo Benchmark 4 style JSON into our MemoryBank
  - Offers a Dojo-backed verifier that can replace/augment synthetic checks

Install (on a machine with network + Lean):
  pip install lean-dojo
  # also need elan, git, and GITHUB_ACCESS_TOKEN for tracing repos

Benchmark data (pre-extracted Mathlib4):
  See LeanDojo Benchmark 4 / generate-benchmark-lean4 notebooks on GitHub.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Optional

from .memory import MemoryBank
from .models import MemoryEntry


def lean_dojo_available() -> bool:
    try:
        import lean_dojo  # noqa: F401
        return True
    except ImportError:
        return False


# ---------------------------------------------------------------------------
# Import LeanDojo-style datasets into MemoryBank
# ---------------------------------------------------------------------------
def import_leandojo_jsonl(
    path: str | Path,
    memory_dir: str | Path = "./memory",
    *,
    max_items: Optional[int] = None,
    domain: str = "mathlib",
    min_nl_len: int = 8,
) -> dict[str, int]:
    """
    Import pairs from a JSONL file whose records look like LeanDojo / custom exports.

    Accepted field aliases per line (JSON object):
      natural_language | nl | informal | docstring | full_name
      lean_code | lean | formal | theorem (code block)
      domain | file_path (used as soft domain tag)

    If only `full_name` + formal statement exist, we synthesize a short NL
    description from the name (Mathlib-style camelCase → words).
    """
    path = Path(path)
    bank = MemoryBank(memory_dir)
    existing = {e.natural_language for e in bank._success}
    added = skipped = 0

    with path.open(encoding="utf-8") as f:
        for i, line in enumerate(f):
            if max_items is not None and added >= max_items:
                break
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                skipped += 1
                continue

            nl = (
                obj.get("natural_language")
                or obj.get("nl")
                or obj.get("informal")
                or obj.get("docstring")
                or ""
            )
            lean = (
                obj.get("lean_code")
                or obj.get("lean")
                or obj.get("formal")
                or obj.get("theorem")
                or ""
            )
            full_name = obj.get("full_name") or obj.get("name") or ""

            if not lean and full_name and obj.get("statement"):
                # LeanDojo-ish: statement is the type, not full theorem
                lean = f"theorem {full_name} : {obj['statement']} := by sorry"

            if not nl and full_name:
                nl = _name_to_nl(full_name)
            if not nl or not lean or len(nl) < min_nl_len:
                skipped += 1
                continue
            if nl in existing:
                skipped += 1
                continue

            dom = obj.get("domain") or domain
            if obj.get("file_path"):
                dom = _domain_from_path(str(obj["file_path"])) or dom

            bank.save_success(
                natural_language=nl.strip(),
                lean_code=lean.strip(),
                domain=dom,
                difficulty=obj.get("difficulty", "medium"),
                tags=list(obj.get("tags") or ["leandojo"]),
                metadata={
                    "source": "leandojo_import",
                    "full_name": full_name,
                    "file_path": obj.get("file_path"),
                },
                natural_language_proof=(
                    obj.get("natural_language_proof")
                    or obj.get("nl_proof")
                    or obj.get("proof")
                    or ""
                ).strip(),
            )
            existing.add(nl.strip())
            added += 1

    return {"added": added, "skipped": skipped, **bank.stats()}


def _name_to_nl(name: str) -> str:
    """Best-effort: Nat.add_comm → 'Nat add comm' style readable phrase."""
    # Drop namespace noise lightly
    base = name.split(".")[-1] if "." in name else name
    # split CamelCase and underscores
    base = re.sub(r"([a-z])([A-Z])", r"\1 \2", base)
    base = base.replace("_", " ")
    return f"Mathlib lemma `{name}`: {base}"


def _domain_from_path(path: str) -> str:
    p = path.replace("\\", "/").lower()
    if "numbertheory" in p or "/nat" in p or "parity" in p:
        return "number_theory"
    if "/logic" in p:
        return "logic"
    if "/set" in p:
        return "set_theory"
    if "/analysis" or "/real" in p:
        return "analysis"
    if "/algebra" in p or "/group" in p or "/ring" in p:
        return "algebra"
    if "/list" in p or "/combinator" in p:
        return "combinatorics"
    if "/topology" in p:
        return "topology"
    return "mathlib"


# ---------------------------------------------------------------------------
# Optional live Dojo interaction (requires lean-dojo + Lean install)
# ---------------------------------------------------------------------------
class DojoVerifier:
    """
    Thin wrapper around LeanDojo interaction.

    When LeanDojo is not installed, raises RuntimeError with install hints.
    Prefer using lean_verify.compile_lean for the default pipeline; switch to
    this when you have a traced theorem and want tactic-level feedback.
    """

    def __init__(self, repo_url: Optional[str] = None, commit: Optional[str] = None):
        if not lean_dojo_available():
            raise RuntimeError(
                "lean-dojo is not installed. pip install lean-dojo "
                "and ensure Lean/elan is available. See https://leandojo.org"
            )
        from lean_dojo import LeanGitRepo, trace  # type: ignore

        self._LeanGitRepo = LeanGitRepo
        self._trace = trace
        self.repo_url = repo_url or os.environ.get(
            "LEANDOJO_REPO", "https://github.com/leanprover-community/mathlib4"
        )
        self.commit = commit or os.environ.get("LEANDOJO_COMMIT")
        self._traced = None

    def trace_repo(self):
        """Trace the configured repo (expensive; cache results on disk)."""
        if self.commit:
            repo = self._LeanGitRepo(self.repo_url, self.commit)
        else:
            repo = self._LeanGitRepo(self.repo_url)
        self._traced = self._trace(repo)
        return self._traced

    def check_snippet(self, lean_code: str) -> tuple[bool, str]:
        """
        Fallback: if full Dojo interaction is not set up, delegate to
        lean_verify.compile_lean so callers have one interface.
        """
        from .lean_verify import compile_lean

        return compile_lean(lean_code)


def enhance_repair_with_premise_hints(
    error_message: str,
    memory: MemoryBank,
    query: str,
    top_k: int = 3,
) -> str:
    """
    Build extra context for the LLM repair prompt from memory (simulates
    premise retrieval in ReProver / LeanDojo-style retrieval-augmented proving).
    """
    hits = memory.retrieve(query, top_k_success=top_k, top_k_repair=1)
    blocks = []
    for e in hits["success"]:
        nl_proof = getattr(e, "natural_language_proof", "") or ""
        proof_line = f"-- NL proof: {nl_proof[:200]}\n" if nl_proof else ""
        blocks.append(
            f"-- premise example\n-- NL: {e.natural_language[:120]}\n{proof_line}{e.lean_code}"
        )
    for e in hits["repair"]:
        blocks.append(
            f"-- past repair\n-- error was: {e.error_message[:160]}\n"
            f"-- fixed:\n{e.corrected_code}"
        )
    if not blocks:
        return error_message
    return (
        error_message
        + "\n\nRelevant premises / past repairs from memory:\n"
        + "\n\n".join(blocks)
    )
