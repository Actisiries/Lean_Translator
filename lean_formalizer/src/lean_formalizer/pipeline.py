"""Full end-to-end orchestration of the NL → Lean 4 formalization workflow.

Pipeline
--------
1. Extract / clean text
2. Structure & decompose (statement, lemmas, sketch, domain, difficulty)
3. Retrieve memory: parallel NL statement + NL proof + Lean triples
4. Multi-stage LLM: understand → statement → proof candidates → critique
5. Verify + repair loop
6. Rank / select
7. Quality-gated save (statement + generated NL proof + Lean)
"""

from __future__ import annotations

import copy
from contextlib import nullcontext
import json
import os
import re
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from threading import Lock
from typing import Any, Callable, Optional

from .extract import load_text_or_pdf
from .structure import decompose
from .memory import MemoryBank
from .llm import LLMClient, get_llm, QuotaExceededError, LLMTimeoutError
from .models import Candidate, FormalizationResult, ProofAudit, ProofStep, StructuredMath
from .prompts import (
    SYSTEM_MATH,
    SYSTEM_LEAN_ONLY,
    UNDERSTAND_PROMPT,
    STATEMENT_PROMPT,
    PROOF_PROMPT,
    DIVERSIFY_PROOF_PROMPT,
    CRITIQUE_PROMPT,
    COMPLETE_PROOF_PROMPT,
    STATEMENT_REPAIR_PROMPT,
    NL_PROOF_FROM_LEAN_PROMPT,
    ONESHOT_PROMPT,
    PROOF_AUDIT_PROMPT,
)
from .lean_verify import (
    repair_loop,
    compile_lean,
    compile_declaration,
    lean_available,
    normalize_lean_imports,
    project_llm_context,
    extract_lean_code,
    has_proof_body,
    declaration_name_matches,
    strip_incomplete_proof,
    proof_self_references,
    complete_proof_with_known_lemmas,
    compile_status,
    lean_code_hash,
    lean_project_dir,
    default_scratch_root,
    preflight_lean_code,
    use_umbrella_mathlib_import,
    has_umbrella_mathlib_import,
    query_local_lean_facts,
)
from .rank import rank_candidates
from .diagnose import build_report

import time as _time


def _candidate_strategy(index: int) -> str:
    """Give each candidate a genuinely different initial search preference."""
    strategies = (
        "Prefer the shortest confirmed Mathlib theorem or equivalence; do not invent a lemma name.",
        "Prefer a direct tactic proof using basic, established Mathlib lemmas rather than copying another candidate.",
        "Prefer an explicit construction only when its supporting API is known; otherwise use a different confirmed library route.",
    )
    return strategies[(max(1, index) - 1) % len(strategies)]


def _parse_timeout(value, default: float) -> float:
    try:
        if value is None or value == "":
            return default
        return max(5.0, float(value))
    except (TypeError, ValueError):
        return default


def _check_deadline(deadline: float | None) -> None:
    """Raise LLMTimeoutError if wall-clock deadline passed."""
    if deadline is not None and _time.monotonic() >= deadline:
        raise LLMTimeoutError(
            "Formalization stopped: exceeded job time limit. "
            "Increase Job timeout in the UI or JOB_TIMEOUT in config."
        )




def _is_simple_claim(text: str) -> bool:
    """Heuristic: short elementary claims should not burn many LLM calls."""
    t = (text or "").strip()
    if not t:
        return False
    if len(t) > 220:
        return False
    if t.count("\n") > 6:
        return False
    lower = t.lower()
    heavy = (
        "galois", "banach", "lebesgue", "stokes", "yoneda", "noetherian",
        "rademacher", "picard", "hahn–banach", "hahn-banach", "abel",
        "fundamental theorem of algebra", "manifold", "primary decomposition",
    )
    if any(h in lower for h in heavy):
        return False
    if len(t) <= 100:
        return True
    easy_markers = (
        "even or odd", "commutative", "associative", "modus ponens",
        "1+1", "1 + 1", "a^2+b^2", "a^2 + b^2", "pythagorean", "n + 0",
        "zero is even", "subset", "intersection", "union", "de morgan",
        "two even", "natural number",
    )
    return any(m in lower for m in easy_markers)


def _has_unresolved_lean_notation(text: str) -> bool:
    """Detect malformed Lean-like notation in an LLM structure response.

    This is deliberately conservative: it does not rewrite the mathematics,
    it only prevents the cheap one-shot path from bypassing declaration
    generation and statement preflight when the structure contains holes.
    """
    value = text or ""
    return bool(
        re.search(r"\?\s+[A-Za-z_][A-Za-z0-9_']*\s+in\b", value)
        or re.search(r"(?m)(?::|\(|,)\s*\?\s*(?:\)|,|\]|=>|$)", value)
        or re.search(r"(?<![A-Za-z0-9_])\?(?![_A-Za-z0-9])", value)
    )


class FormalizationPipeline:
    def __init__(
        self,
        llm: Optional[LLMClient] = None,
        memory_dir: str | Path = "./memory",
        num_candidates: int = 3,
        max_repair_rounds: int = 3,
        temperature: float = 0.2,
        lean_bin: Optional[str] = None,
        progress_callback: Optional[Callable[[dict[str, Any]], None]] = None,
        max_parallel_candidates: Optional[int] = None,
        lean_compile_workers: Optional[int] = None,
        lean_compile_timeout: Optional[float] = None,
        verification_lock: Any = None,
    ):
        self.llm = llm or get_llm()
        self.memory = MemoryBank(memory_dir)
        self.num_candidates = int(os.environ.get("NUM_CANDIDATES", num_candidates))
        self.max_repair_rounds = int(
            os.environ.get("MAX_REPAIR_ROUNDS", max_repair_rounds)
        )
        self.temperature = float(os.environ.get("TEMPERATURE", temperature))
        self.lean_bin = lean_bin or os.environ.get("LEAN_PATH", "lean")
        self.progress_callback = progress_callback
        requested_parallelism = (
            max_parallel_candidates
            if max_parallel_candidates is not None
            else os.environ.get("MAX_PARALLEL_CANDIDATES", 1)
        )
        try:
            self.max_parallel_candidates = max(1, int(requested_parallelism))
        except (TypeError, ValueError):
            self.max_parallel_candidates = 1
        try:
            self.lean_compile_workers = max(
                1,
                int(
                    lean_compile_workers
                    if lean_compile_workers is not None
                    else os.environ.get("LEAN_COMPILE_WORKERS", 1)
                ),
            )
        except (TypeError, ValueError):
            self.lean_compile_workers = 1
        try:
            self.lean_compile_timeout = max(
                15.0,
                float(
                    lean_compile_timeout
                    if lean_compile_timeout is not None
                    else os.environ.get("LEAN_TIMEOUT", 120)
                ),
            )
        except (TypeError, ValueError):
            self.lean_compile_timeout = 120.0
        self.verification_lock = verification_lock

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def formalize(
        self,
        *,
        text: Optional[str] = None,
        pdf_path: Optional[str | Path] = None,
        domain: str = "general",
        max_pages: Optional[int] = None,
        save_to_memory: bool = True,
        target_statement: Optional[str] = None,
        deadline_monotonic: Optional[float] = None,
    ) -> FormalizationResult:
        cleaned = load_text_or_pdf(text=text, pdf_path=pdf_path, max_pages=max_pages)
        if not cleaned:
            return FormalizationResult(
                success=False, message="No text extracted", domain=domain
            )

        if hasattr(self.llm, "reset_usage"):
            self.llm.reset_usage()
        self._deadline = deadline_monotonic
        self._stage_trace: list[dict[str, Any]] = []
        # Candidate-generation workers share one trace.  Record timestamps and
        # append under the same lock so the completed report is chronological.
        self._trace_lock = Lock()
        self._trace_started_at = _time.monotonic()
        project = lean_project_dir()
        scratch_root = default_scratch_root(project)
        self._scratch_dir = scratch_root
        self._project_writable = self._scratch_dir is not None
        self._active_candidate = None
        self._candidate_llm_error = ""
        self._local_lean_facts: list[str] = []
        statement_repair_attempts = 0
        # The API timeout is a per-request limit. Do not divide it by the
        # number of candidates or repair rounds: doing so made a configured
        # 200-second request stop at an artificial 70-second budget. The
        # overall job deadline remains the hard wall-clock limit; _chat clips
        # only the current request to the time actually remaining.
        configured_llm_timeout = float(getattr(self.llm, "timeout_seconds", 300.0))
        if deadline_monotonic is not None:
            # Leave a small wall-clock margin for result serialization and
            # status updates, but never divide the configured API timeout by
            # candidate/repair counts.
            remaining = max(1.0, deadline_monotonic - _time.monotonic())
            self._llm_call_budget = min(
                max(5.0, configured_llm_timeout - 0.1),
                max(5.0, remaining - 0.1),
            )
        else:
            self._llm_call_budget = configured_llm_timeout
        # Also expose the cap to structure decomposition and repair_loop,
        # which receive the client directly rather than calling ``_chat``.
        self.llm.call_timeout_seconds = self._llm_call_budget
        # A Lean check gets the explicitly configured per-check allowance,
        # clipped only by the *actual* remaining job time.  Do not divide the
        # job budget by hypothetical candidates/repairs: a 660s job with
        # three candidates and three repairs previously collapsed this to 44s
        # even though a single Mathlib import may legitimately need longer.
        self._lean_compile_budget = self.lean_compile_timeout
        self._trace(
            "input",
            ok=True,
            len=len(cleaned),
            preview=(cleaned[:160].replace("\n", " ") if cleaned else "(empty)"),
        )
        self._trace(
            "compiler_context",
            ok=self._project_writable,
            scratch_dir=str(self._scratch_dir or ""),
            preview=(
                f"Compiler sources will be retained in {self._scratch_dir} as candidate_1.lean, candidate_2.lean, etc."
                if self._project_writable
                else "Configured Lean project has no writable FormalizerScratch directory. Lean verification is unavailable for this run."
            ),
        )
        self._trace(
            "timeout_budget",
            ok=True,
            requested_candidates=max(1, self.num_candidates),
            max_repair_rounds=max(0, self.max_repair_rounds),
            llm_call_seconds=round(self._llm_call_budget, 1),
            configured_llm_timeout=round(configured_llm_timeout, 1),
            job_deadline_seconds=(
                round(max(0.0, deadline_monotonic - _time.monotonic()), 1)
                if deadline_monotonic is not None else None
            ),
            lean_compile_seconds=round(self._lean_compile_budget, 1),
            lean_compile_workers=self.lean_compile_workers,
            llm_candidate_workers=self.max_parallel_candidates,
            preview=(
                f"Requested {max(1, self.num_candidates)} candidates, up to "
                f"{max(0, self.max_repair_rounds)} repairs each; generating with "
                f"{self.max_parallel_candidates} LLM worker(s), compiling with "
                f"{self.lean_compile_workers} Lean worker(s). Each LLM call is "
                f"capped at {self._llm_call_budget:.0f}s (or the remaining job "
                f"time) and each Lean check at "
                f"{self._lean_compile_budget:.0f}s."
            ),
        )

        # Fast path for mock backends (responsive offline UI)
        if getattr(self.llm, "backend", None) == "mock" or type(self.llm).__name__ == "MockLLM":
            return self._mock_formalize(cleaned, domain, save_to_memory)

        structured = decompose(
            cleaned,
            self.llm,
            domain=domain,
            progress_callback=self._trace_entry,
            deadline_monotonic=getattr(self, "_deadline", None),
        )
        domain = structured.domain or domain
        difficulty = getattr(structured, "difficulty", None) or "medium"
        self._trace(
            "structure",
            ok=bool(structured.main_theorem),
            len=len(structured.main_theorem or ""),
            difficulty=difficulty,
            domain=domain,
            preview=(structured.main_theorem or "")[:160],
            sketch_steps=len(structured.proof_sketch or []),
        )
        # Optional user-provided formal / precise statement
        ts = (target_statement or "").strip()
        if ts:
            # If it looks like Lean, treat as declaration target; else as NL claim
            if any(k in ts for k in ("theorem ", "lemma ", "example ", "def ")):
                structured.main_theorem = structured.main_theorem or cleaned[:300]
                structured.proof_sketch = structured.proof_sketch or [
                    "Prove the user-provided Lean declaration"
                ]
                # stash lean target on structured via raw_text note
                structured.definitions = list(structured.definitions) + [
                    f"[user_lean_statement]\n{ts}"
                ]
            else:
                structured.main_theorem = ts
        # Keep the exact source for the post-compilation proof review.  The
        # review must compare it with final Lean code and compiler feedback,
        # so it runs after candidate selection rather than guiding generation.
        audit_source = cleaned
        if ts:
            audit_source = f"Theorem target:\n{ts}\n\nSubmitted text:\n{cleaned}"
        query = structured.main_theorem or cleaned[:500]

        hits = self.memory.retrieve(
            query=query,
            domain=domain,
            top_k_success=2,
            top_k_repair=1,
            strict_style=True,
        )
        # Keep memory context short so HTTP models do not drop the reply.
        examples_str = self._format_success_examples(hits["success"], max_lean=600)
        repairs_str = self._format_repair_examples(hits["repair"], max_code=400)

        # Instant path: high-similarity memory hit on simple claims
        if hits["success"] and _is_simple_claim(cleaned):
            top = hits["success"][0]
            # very short / almost same statement → reuse verified Lean
            from .memory import _normalize_nl
            if _normalize_nl(top.natural_language) == _normalize_nl(
                structured.main_theorem or cleaned
            ) or (
                len(cleaned) < 120
                and _normalize_nl(cleaned) in _normalize_nl(top.natural_language)
            ):
                self._trace(
                    "lean_compile", ok=True, phase="memory_hit_start",
                    len=len(top.lean_code or ""),
                )
                ok, msg = self._compile(top.lean_code)
                self._trace(
                    "lean_compile", ok=ok, phase="memory_hit_result",
                    len=len(top.lean_code or ""), error="" if ok else msg,
                )
                ver = build_report(
                    compiled=ok,
                    raw_message=msg,
                    code=top.lean_code,
                    natural_language=top.natural_language,
                    lean_available=lean_available(self.lean_bin),
                    llm=None,
                )
                return FormalizationResult(
                    # Only the local Lean compiler decides formal success.  The
                    # API audit is advisory and must never override it.
                    success=ok,
                    best_code=top.lean_code,
                    structured=structured,
                    proof_audit=self._stage_proof_audit(
                        audit_source,
                        lean_code=top.lean_code,
                        compiler_result=msg,
                    ),
                    selected_candidate=1,
                    candidates=[
                        Candidate(
                            lean_code=top.lean_code,
                            compiled=ok,
                            source_stage="memory_hit",
                            error_log=[] if ok else [msg],
                        )
                    ],
                    memory_hits=[
                        {
                            "type": "success",
                            "id": top.id,
                            "nl": top.natural_language[:120],
                            "has_nl_proof": bool(
                                (top.natural_language_proof or "").strip()
                            ),
                        }
                    ],
                    repair_count=0,
                    message=ver.summary + " (memory hit, fast path)",
                    domain=domain,
                    verification=ver.to_dict(),
                    token_usage=getattr(self.llm, "usage", None).to_dict()
                    if getattr(self.llm, "usage", None)
                    else {},
                )

        simple = (
            (_is_simple_claim(cleaned) or _is_simple_claim(structured.main_theorem))
            and not _has_unresolved_lean_notation(structured.main_theorem)
        )
        if not simple and _has_unresolved_lean_notation(structured.main_theorem):
            self._trace(
                "structure_placeholder_guard",
                ok=False,
                error=(
                    "Structure response contains unresolved Lean notation; "
                    "forcing declaration generation and statement preflight."
                ),
                preview="Malformed `?` notation detected before candidate generation.",
            )
        # Respect the requested breadth and repair depth. Every candidate is
        # generated independently, then receives the same repair budget.
        n_cand = max(1, self.num_candidates)
        do_critique = True
        max_rep = max(0, self.max_repair_rounds)
        if simple:
            # Simple claims still honor the requested candidate/repair counts.
            do_critique = False
        elif len(cleaned) > 350 or difficulty in ("hard", "graduate", "classic"):
            # Keep candidate and repair breadth; only omit optional critique.
            do_critique = False
        # Single-candidate runs: skip critique to cut empty/long HTTP replies
        if n_cand <= 1:
            do_critique = False

        if simple:
            understanding = structured.main_theorem
            raw_candidates = self._generate_candidates_parallel(
                n_cand,
                lambda worker, index: worker._generate_simple_candidate(
                    structured, examples_str, repairs_str, index
                ),
            )
        else:
            # Skip understand on single-candidate runs (saves a flaky HTTP call)
            if n_cand <= 1:
                understanding = structured.main_theorem or cleaned[:400]
            else:
                understanding = self._stage_understand(structured)
            base_declaration = self._stage_statement(
                structured, examples_str, understanding
            )
            # Statement elaboration comes before expensive candidate proofs.
            # Its failure is concrete local feedback, rather than three blind
            # proof attempts against a malformed declaration.
            base_declaration = use_umbrella_mathlib_import(base_declaration)
            self._trace("statement_preflight", ok=True, len=len(base_declaration),
                        preview=("Preserved fine-grained imports."
                                 if not has_umbrella_mathlib_import(base_declaration)
                                 else "Umbrella import detected; import repair will be attempted before Lean."))
            self._trace("lean_compile", ok=True, phase="statement_start", len=len(base_declaration))
            statement_ok, statement_msg = self._compile_declaration(base_declaration)
            self._trace("lean_compile", ok=statement_ok, phase="statement_result", len=len(base_declaration),
                        error="" if statement_ok else statement_msg)
            if not statement_ok:
                statement_repair_attempts = 1
                repaired_statement = self._repair_statement_once(
                    structured, base_declaration, statement_msg
                )
                if repaired_statement and repaired_statement != base_declaration:
                    base_declaration = repaired_statement
                    self._trace(
                        "statement_repair_result", ok=True, len=len(base_declaration),
                        preview="Statement repair produced a complete declaration; rechecking locally.",
                    )
                    statement_ok, statement_msg = self._compile_declaration(base_declaration)
                    self._trace(
                        "lean_compile", ok=statement_ok, phase="statement_repair_result",
                        len=len(base_declaration), error="" if statement_ok else statement_msg,
                    )
                if statement_ok:
                    pass
                elif "timed out" in (statement_msg or "").lower():
                    self._trace("statement_preflight_timeout", ok=False, len=len(base_declaration),
                                error=statement_msg,
                                preview="Statement check was slow; continuing with proof candidates.")
                else:
                    self._trace("statement_invalid", ok=False, len=len(base_declaration), error=statement_msg,
                                preview="Statement did not compile; generating no full proofs from it.")
                    self._trace(
                        "candidate_generation_skipped", ok=False,
                        requested_candidates=n_cand, generated_candidates=0,
                        max_repair_rounds=max_rep, candidate_repairs=0,
                        statement_repair_attempts=statement_repair_attempts,
                        error="Statement preflight failed before candidate generation.",
                        preview=f"Candidate generation skipped: 0 / {n_cand} candidates available.",
                    )
                    return self._statement_failure_result(structured, base_declaration, statement_msg, domain, audit_source)
            self._local_lean_facts = query_local_lean_facts(
                structured.main_theorem, project=lean_project_dir(), timeout=20
            )
            self._trace("local_lean_query", ok=bool(self._local_lean_facts),
                        len=len(self._local_lean_facts),
                        preview="; ".join(self._local_lean_facts[:3]) or "No targeted local facts returned.")
            raw_candidates = []
            # Candidate proofs are generated by the bounded executor below.
            for i in range(0):
                self._active_candidate = i + 1
                self._candidate_llm_error = ""
                temp = min(0.55, self.temperature + 0.07 * i)
                proof_code = self._stage_proof(
                    structured, base_declaration, examples_str, repairs_str, temp
                )
                known = complete_proof_with_known_lemmas(proof_code or "")
                if known:
                    proof_code = known
                    self._trace(
                        "proof_known_completion",
                        ok=True,
                        len=len(proof_code),
                        preview=(proof_code or "")[:160],
                        source="Mathlib-lemma completion",
                    )
                if proof_code and not declaration_name_matches(
                    base_declaration, proof_code
                ):
                    self._trace(
                        "proof_claim_mismatch",
                        ok=False,
                        len=len(proof_code),
                        error=(
                            "Proof stage changed the theorem name; "
                            "discarding and trying complete-proof fallback."
                        ),
                    )
                    proof_code = ""
                if proof_code and proof_self_references(proof_code):
                    self._trace(
                        "proof_self_reference",
                        ok=False,
                        len=len(proof_code),
                        error=(
                            "Proof stage body references its own theorem; "
                            "discarding and trying complete-proof fallback."
                        ),
                    )
                    proof_code = ""
                # Proof stage often returns empty / declaration-only on some HTTP
                # backends — retry with an explicit "complete this declaration"
                # prompt before repair (repair alone also tends to return empty).
                needs_body = not (proof_code or "").strip() or not has_proof_body(
                    proof_code or ""
                )
                if (
                    needs_body
                    and not self._candidate_llm_error
                    and (base_declaration or "").strip()
                ):
                    proof_code = self._stage_complete_proof(
                        structured, base_declaration
                    )
                improved = (
                    self._stage_critique(proof_code, min(temp, 0.25))
                    if do_critique and (proof_code or "").strip()
                    else proof_code
                )
                # If still empty, keep the statement so repair can finish it.
                lean = (
                    ""
                    if self._candidate_llm_error
                    else (improved or "").strip() or (base_declaration or "").strip()
                )
                raw_candidates.append(
                    Candidate(
                        lean_code=lean,
                        statement_only=base_declaration,
                        proof=improved or "",
                        source_stage=(
                            "generation_timeout"
                            if self._candidate_llm_error else "initial"
                        ),
                        error_log=(
                            [f"candidate generation timeout: {self._candidate_llm_error}"]
                            if self._candidate_llm_error else []
                        ),
                    )
                )
                self._active_candidate = None
            raw_candidates = self._generate_candidates_parallel(
                n_cand,
                lambda worker, index: worker._generate_proof_candidate(
                    structured,
                    base_declaration,
                    examples_str,
                    repairs_str,
                    do_critique,
                    index,
                ),
            )

        # The provider can deterministically echo the first reply even when
        # temperatures differ.  Before sharing verification work, give every
        # exact duplicate one bounded chance to take an independent route.
        # If it remains identical, `_verify_candidates` still deduplicates it
        # so a repeated answer cannot consume another Lean/repair budget.
        raw_candidates = self._diversify_duplicate_candidates(
            raw_candidates, structured
        )

        verified, total_repairs = self._verify_candidates(
            raw_candidates, structured, repairs_str, max_rep, save_to_memory, domain,
        )

        # Ranking compiled candidates is deterministic. Avoid an untracked
        # extra LLM call after verification, which cannot improve correctness.
        self._trace("rank", ok=True, len=len(verified), preview="Selecting the best verified candidate.")
        ranked = rank_candidates(verified)
        best = next((c for c in ranked if c.compiled and (c.lean_code or "").strip()), None)
        if best is None and ranked:
            # Prefer any non-empty code, even if not compiled (longer first)
            nonempty = [c for c in ranked if (c.lean_code or "").strip()]
            nonempty.sort(key=lambda c: -len(c.lean_code or ""))
            best = nonempty[0] if nonempty else ranked[0]

        # Recover if "best" has empty code but another candidate has code
        if best is None or not (best.lean_code or "").strip():
            for c in ranked + verified + raw_candidates:
                if (c.lean_code or "").strip():
                    best = c
                    break

        # Final re-check of best candidate for an authoritative report
        ver_report = None
        if best is not None and (best.lean_code or "").strip():
            # A timeout stops the repair loop before code changes. Repeating
            # that exact final check only consumes another full Lean timeout.
            reuse_timeout = best.status == "compile_timeout" and bool(best.error_log)
            if reuse_timeout:
                ok = False
                # Timeout recovery may append explanatory notes after the
                # compiler message. The final report must retain the actual
                # Lean result, rather than presenting a recovery note as if it
                # were compiler output.
                msg = next(
                    (
                        item for item in reversed(best.error_log)
                        if compile_status(False, item) == "compile_timeout"
                    ),
                    best.error_log[-1],
                )
                self._trace(
                    "final_compile_skipped", ok=False,
                    reason="unchanged_compile_timeout",
                    candidate=best.candidate_index or None,
                    len=len(best.lean_code or ""), error=msg,
                    preview=(
                        "Skipped final Lean compilation: the selected candidate already "
                        "timed out with unchanged code."
                    ),
                )
            else:
                self._trace(
                    "lean_compile", ok=True, phase="final_start", round="final",
                    len=len(best.lean_code or ""),
                )
                ok, msg = self._compile(best.lean_code)
                self._trace(
                    "lean_compile", ok=ok, phase="final_result", round="final",
                    len=len(best.lean_code or ""), error="" if ok else msg,
                )
                best.compiled = ok
                best.status = "verified" if ok else compile_status(ok, msg)
                best.cache_hit = best.cache_hit or msg.startswith("[compile cache hit]")
                if not ok and msg and (not best.error_log or best.error_log[-1] != msg):
                    best.error_log.append(msg)
            ver_report = build_report(
                compiled=ok,
                raw_message=msg,
                code=best.lean_code,
                natural_language=structured.main_theorem or query,
                lean_available=lean_available(self.lean_bin),
                # Compiler diagnostics are the source of final feedback.  Do
                # not replace them with an API-written explanation.
                llm=None,
            )
        else:
            ver_report = build_report(
                compiled=False,
                raw_message="No Lean code was produced by the model.",
                code="",
                natural_language=structured.main_theorem or query,
                lean_available=lean_available(self.lean_bin),
                llm=None,
            )
            best = None

        best_code = (best.lean_code if best else "") or ""
        # Never report success without usable Lean code (and no sorry/placeholder)
        from .lean_verify import _is_placeholder_lean
        ph = _is_placeholder_lean(best_code) if best_code else "empty"
        lean_success = bool(
            best and best.compiled and best_code.strip() and not ph
        )
        # Formal success is exclusively determined by a real Lean compiler
        # checking generated code.  The optional API proof review is shown
        # separately and is never a final mathematical verdict.
        success_flag = lean_success
        if ph and best_code.strip():
            # force failure message if placeholder slipped through compile
            if best is not None:
                best.compiled = False
            ver_report = build_report(
                compiled=False,
                raw_message=ph,
                code=best_code,
                natural_language=structured.main_theorem or query,
                lean_available=lean_available(self.lean_bin),
                llm=None,
            )

        # Final diagnostic when model produced nothing usable
        if not best_code.strip():
            had_raw = any((c.lean_code or "").strip() for c in raw_candidates)
            had_verified = any((c.lean_code or "").strip() for c in verified)
            self._trace(
                "empty_result",
                ok=False,
                len=0,
                error=(
                    "Lean code was lost after stages (often empty repair reply). "
                    if (had_raw or had_verified)
                    else "Model returned empty Lean at every stage. "
                )
                + "Check stage_trace / candidate error_log.",
                n_candidates=len(ranked),
                had_raw=had_raw,
                had_verified=had_verified,
                difficulty=difficulty,
            )

        # Candidate cards, trace events, and reusable scratch files all use
        # the original generation number.  Ranking may reorder the list, so
        # reporting its list position here previously highlighted the wrong
        # candidate in the UI.
        selected_candidate = int(getattr(best, "candidate_index", 0) or 0)
        proof_audit = self._stage_proof_audit(
            audit_source,
            lean_code=best_code,
            compiler_result=(ver_report.raw_message or ver_report.summary),
        )

        result = FormalizationResult(
            success=success_flag,
            best_code=best_code,
            structured=structured,
            proof_audit=proof_audit,
            selected_candidate=selected_candidate,
            # Keep original generation order; ranking selects ``best`` but
            # must not renumber cards, trace IDs, or scratch files.
            candidates=verified,
            memory_hits=[
                {
                    "type": "success",
                    "id": e.id,
                    "nl": e.natural_language[:120],
                    "has_nl_proof": bool((e.natural_language_proof or "").strip()),
                }
                for e in hits["success"]
            ]
            + [
                {"type": "repair", "id": e.id, "nl": e.natural_language[:120]}
                for e in hits["repair"]
            ],
            repair_count=total_repairs,
            message=ver_report.summary,
            domain=domain,
            verification=ver_report.to_dict(),
            token_usage=getattr(self.llm, "usage", None).to_dict()
            if getattr(self.llm, "usage", None) else {},
            stage_trace=list(getattr(self, "_stage_trace", []) or []),
            requested_candidates=n_cand,
            generated_candidates=len(raw_candidates),
            max_repair_rounds=max_rep,
            candidate_repairs=total_repairs,
            statement_repair_attempts=statement_repair_attempts,
            llm_candidate_workers=self.max_parallel_candidates,
            lean_compile_workers=self.lean_compile_workers,
        )

        if save_to_memory and best is not None and best.compiled:
            nl_proof = ""
            remaining = (
                (getattr(self, "_deadline", None) or float("inf"))
                - _time.monotonic()
                if getattr(self, "_deadline", None) is not None
                else float("inf")
            )
            if remaining >= 30:
                nl_proof = self._synthesize_nl_proof(
                    structured.main_theorem or query, best.lean_code
                )
            else:
                self._trace(
                    "nl_proof_skipped",
                    ok=False,
                    len=0,
                    error=(
                        "Skipped NL-proof synthesis because the job deadline "
                        f"is close (remaining={max(0.0, remaining):.0f}s)."
                    ),
                )
            sketch = "\n".join(
                f"{i+1}. {s}" for i, s in enumerate(structured.proof_sketch)
            )
            if not nl_proof and sketch:
                nl_proof = "Proof.\n" + sketch
            self.memory.save_success(
                natural_language=structured.main_theorem or cleaned[:500],
                lean_code=best.lean_code,
                domain=domain,
                difficulty=difficulty,
                tags=["auto", domain],
                metadata={
                    "repairs": total_repairs,
                    "compiled": True,
                    "pipeline": "v2",
                },
                natural_language_proof=nl_proof,
                enforce_quality=True,
                allow_sorry=0,
            )

        return result

    # ------------------------------------------------------------------
    # Mock path (offline)
    # ------------------------------------------------------------------
    def _mock_formalize(
        self, cleaned: str, domain: str, save_to_memory: bool
    ) -> FormalizationResult:
        from .memory import _normalize_nl

        hits = self.memory.retrieve(cleaned, domain=domain, top_k_success=5)
        relevant = []
        qn = _normalize_nl(cleaned)
        for e in hits.get("success") or []:
            en = _normalize_nl(e.natural_language or "")
            if not qn or not en:
                continue
            if qn == en or qn in en or en in qn:
                relevant.append(e)
                continue
            q_toks, n_toks = set(qn.split()), set(en.split())
            if q_toks and len(q_toks & n_toks) / max(len(q_toks), 1) >= 0.45:
                relevant.append(e)
        if relevant:
            best_entry = relevant[0]
            code = best_entry.lean_code
            structured = StructuredMath(
                definitions=[],
                main_theorem=best_entry.natural_language,
                lemmas=[],
                proof_sketch=(
                    best_entry.natural_language_proof.split("\n")
                    if best_entry.natural_language_proof
                    else []
                ),
                domain=best_entry.domain or domain,
            )
            hits = {"success": relevant, "repair": hits.get("repair") or []}
        else:
            hits = {"success": [], "repair": hits.get("repair") or []}
        if not relevant:
            structured = StructuredMath(
                definitions=[],
                main_theorem=cleaned.strip()[:300],
                lemmas=[],
                proof_sketch=[],
                domain=domain,
            )
            ver = build_report(
                compiled=False,
                raw_message=(
                    "Mock backend is offline-only: it can replay verified memory "
                    "entries but cannot formalize new claims. Configure a real LLM "
                    "backend (OpenAI/Anthropic/HTTP)."
                ),
                code="",
                natural_language=structured.main_theorem or cleaned[:300],
                lean_available=lean_available(self.lean_bin),
                llm=None,
            )
            return FormalizationResult(
                success=False,
                best_code="",
                structured=structured,
                candidates=[],
                memory_hits=[
                    {
                        "type": "success",
                        "id": e.id,
                        "nl": e.natural_language[:120],
                        "has_nl_proof": bool(
                            (e.natural_language_proof or "").strip()
                        ),
                    }
                    for e in hits["success"]
                ],
                repair_count=0,
                message=ver.summary,
                domain=domain,
                verification=ver.to_dict(),
                token_usage=getattr(self.llm, "usage", None).to_dict()
                if getattr(self.llm, "usage", None) else {},
            )

        self._trace("lean_compile", ok=True, phase="mock_start", len=len(code or ""))
        ok, msg = self._compile(code)
        self._trace(
            "lean_compile", ok=ok, phase="mock_result", len=len(code or ""),
            error="" if ok else msg,
        )
        ver = build_report(
            compiled=ok,
            raw_message=msg,
            code=code,
            natural_language=structured.main_theorem or cleaned[:300],
            lean_available=lean_available(self.lean_bin),
            llm=None,
        )
        return FormalizationResult(
            success=ok,
            best_code=code,
            structured=structured,
            candidates=[
                Candidate(
                    lean_code=code,
                    compiled=ok,
                    source_stage="mock",
                    error_log=[] if ok else [msg],
                )
            ],
            memory_hits=[
                {
                    "type": "success",
                    "id": e.id,
                    "nl": e.natural_language[:120],
                    "has_nl_proof": bool((e.natural_language_proof or "").strip()),
                }
                for e in hits["success"]
            ],
            repair_count=0,
            message=ver.summary,
            domain=structured.domain or domain,
            verification=ver.to_dict(),
            token_usage=getattr(self.llm, "usage", None).to_dict()
            if getattr(self.llm, "usage", None) else {},
        )


    def _candidate_worker(self, candidate_index: int) -> "FormalizationPipeline":
        """Create an isolated LLM/pipeline state for one parallel candidate."""
        worker = copy.copy(self)
        worker.llm = copy.copy(self.llm)
        if hasattr(worker.llm, "reset_usage"):
            worker.llm.reset_usage()
        worker._active_candidate = candidate_index
        worker._candidate_llm_error = ""
        # Candidate trace events are intentionally written to the one live job
        # trace; CPython list append is atomic and progress callbacks already
        # synchronize job-state updates in the web app.
        worker._stage_trace = self._stage_trace
        return worker

    def _merge_candidate_usage(self, worker: "FormalizationPipeline") -> None:
        """Add a worker's independently tracked provider usage to this job."""
        target = getattr(self.llm, "usage", None)
        source = getattr(worker.llm, "usage", None)
        if target is None or source is None:
            return
        for field in ("prompt_tokens", "completion_tokens", "total_tokens", "calls"):
            try:
                setattr(target, field, int(getattr(target, field, 0)) + int(getattr(source, field, 0)))
            except (TypeError, ValueError):
                pass
        if getattr(source, "reported", False):
            target.reported = True

    def _generate_candidates_parallel(
        self,
        count: int,
        generate: Callable[["FormalizationPipeline", int], Candidate],
    ) -> list[Candidate]:
        """Run bounded candidate generation concurrently, preserving ID order."""
        workers = min(max(1, self.max_parallel_candidates), count)
        if workers == 1:
            self._trace(
                "candidate_parallel", ok=True, workers=1, requested_candidates=count,
                preview=f"Generating {count} candidates sequentially with 1 LLM worker.",
            )
        else:
            self._trace(
                "candidate_parallel", ok=True, workers=workers,
                preview=f"Generating {count} candidates with up to {workers} parallel LLM requests.",
            )
        results: dict[int, Candidate] = {}

        def run(index: int) -> tuple[int, Candidate, FormalizationPipeline]:
            worker = self._candidate_worker(index)
            try:
                return index, generate(worker, index), worker
            except Exception as ex:
                worker._trace(
                    "candidate_generated", ok=False, candidate=index, len=0,
                    error=f"Candidate generation failed: {type(ex).__name__}: {ex}",
                )
                return index, Candidate(
                    lean_code="", source_stage="generation_error",
                    error_log=[f"candidate generation error: {type(ex).__name__}: {ex}"],
                ), worker

        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="lean-candidate") as pool:
            futures = [pool.submit(run, index) for index in range(1, count + 1)]
            for future in as_completed(futures):
                index, candidate, worker = future.result()
                results[index] = candidate
                self._merge_candidate_usage(worker)
        # Preserve chronological trace order. Result objects below remain
        # ordered by candidate index; rewriting selected trace entries after
        # concurrent generation made timestamps appear to go backwards.
        return [results[index] for index in range(1, count + 1)]

    def _generate_simple_candidate(
        self, structured: StructuredMath, examples: str, repairs: str, index: int
    ) -> Candidate:
        temperature = min(0.55, max(0.05, self.temperature + 0.07 * (index - 1)))
        one_shot = self._stage_simple_oneshot(
            structured, examples, repairs, temperature=temperature, index=index
        )
        self._trace(
            "candidate_generated", ok=bool((one_shot or "").strip()), len=len(one_shot or ""),
            temperature=temperature, preview=(one_shot or "")[:160] or "(empty candidate)",
        )
        if not self._candidate_llm_error:
            known = complete_proof_with_known_lemmas(one_shot or "")
            if known:
                one_shot = known
                self._trace("oneshot_known_completion", ok=True, len=len(one_shot), preview=one_shot[:160])
            elif not (one_shot or "").strip():
                self._trace("simple_oneshot_empty", ok=False, len=0, error="Simple one-shot returned no Lean code; trying fallback.")
                one_shot = self._stage_statement(structured, examples, structured.main_theorem)
                if one_shot:
                    one_shot = self._stage_complete_proof(structured, one_shot)
            elif not has_proof_body(one_shot) or proof_self_references(one_shot):
                declaration = strip_incomplete_proof(one_shot) or one_shot
                one_shot = self._stage_complete_proof(structured, declaration)
        return Candidate(
            lean_code="" if self._candidate_llm_error else one_shot,
            statement_only=one_shot.split(":=", 1)[0].strip() if ":=" in one_shot else one_shot,
            proof=one_shot,
            source_stage="generation_timeout" if self._candidate_llm_error else "simple_oneshot",
            error_log=[f"candidate generation timeout: {self._candidate_llm_error}"] if self._candidate_llm_error else [],
        )

    def _generate_proof_candidate(
        self,
        structured: StructuredMath,
        declaration: str,
        examples: str,
        repairs: str,
        do_critique: bool,
        index: int,
    ) -> Candidate:
        temperature = min(0.55, self.temperature + 0.07 * (index - 1))
        proof_code = self._stage_proof(
            structured, declaration, examples, repairs, temperature, index=index
        )
        if not self._candidate_llm_error:
            known = complete_proof_with_known_lemmas(proof_code or "")
            if known:
                proof_code = known
                self._trace("proof_known_completion", ok=True, len=len(proof_code), preview=proof_code[:160])
            if proof_code and not declaration_name_matches(declaration, proof_code):
                self._trace("proof_claim_mismatch", ok=False, len=len(proof_code), error="Proof stage changed the theorem name.")
                proof_code = ""
            if proof_code and proof_self_references(proof_code):
                self._trace("proof_self_reference", ok=False, len=len(proof_code), error="Proof body references its own theorem.")
                proof_code = ""
            if (not (proof_code or "").strip() or not has_proof_body(proof_code)) and declaration.strip():
                proof_code = self._stage_complete_proof(structured, declaration)
            if do_critique and (proof_code or "").strip():
                proof_code = self._stage_critique(proof_code, min(temperature, 0.25))
        lean = "" if self._candidate_llm_error else (proof_code or "").strip() or declaration.strip()
        self._trace(
            "candidate_generated", ok=bool(lean), len=len(lean), temperature=temperature,
            preview=lean[:160] or "(empty candidate)",
        )
        return Candidate(
            lean_code=lean, statement_only=declaration, proof=proof_code or "",
            source_stage="generation_timeout" if self._candidate_llm_error else "initial",
            error_log=[f"candidate generation timeout: {self._candidate_llm_error}"] if self._candidate_llm_error else [],
        )

    def _diversify_duplicate_candidates(
        self, candidates: list[Candidate], structured: StructuredMath,
    ) -> list[Candidate]:
        """Retry exact duplicate generation once before verification.

        A retry is deliberately bounded to one LLM call per duplicate.  Lean
        verification remains deduplicated if the provider still returns the
        same normalized source.
        """
        seen: dict[str, int] = {}
        for index, candidate in enumerate(candidates, start=1):
            code = (candidate.lean_code or "").strip()
            code_hash = lean_code_hash(code) if code else ""
            if not code_hash:
                continue
            if code_hash not in seen:
                seen[code_hash] = index
                continue
            original = seen[code_hash]
            self._trace(
                "candidate_duplicate_retry", ok=True, candidate=index,
                duplicate_of=original, len=len(code),
                preview=(f"Candidate {index} matched candidate {original}; "
                         "requesting one independent proof route."),
            )
            replacement = self._stage_diversify_proof(candidate, structured, index)
            replacement_hash = lean_code_hash(replacement) if replacement else ""
            if replacement and replacement_hash not in seen:
                candidate.lean_code = replacement
                candidate.proof = replacement
                candidate.statement_only = strip_incomplete_proof(replacement) or candidate.statement_only
                candidate.source_stage = "diversified"
                candidate.error_log.append(
                    f"replaced duplicate of candidate {original} after one independent-generation retry"
                )
                seen[replacement_hash] = index
                self._trace(
                    "candidate_diversify_result", ok=True, candidate=index,
                    duplicate_of=original, len=len(replacement),
                    preview=f"Candidate {index} received a distinct Lean candidate.",
                )
            else:
                same_as = seen.get(replacement_hash, original) if replacement_hash else original
                self._trace(
                    "candidate_diversify_result", ok=False, candidate=index,
                    duplicate_of=same_as, len=len(replacement or ""),
                    error="Independent-generation retry was empty or matched an existing candidate; verification will be shared.",
                )
        return candidates

    def _stage_diversify_proof(
        self, candidate: Candidate, structured: StructuredMath, index: int,
    ) -> str:
        """Ask once for a non-identical full candidate and validate its shape."""
        original = (candidate.lean_code or "").strip()
        if not original:
            return ""
        prompt = DIVERSIFY_PROOF_PROMPT.format(
            code=original,
            statement=structured.main_theorem or "(not available)",
        )
        raw = self._chat(
            [
                {"role": "system", "content": SYSTEM_LEAN_ONLY},
                {"role": "user", "content": prompt},
            ],
            temperature=min(0.65, max(0.3, self.temperature + 0.12 * index)),
            stage="candidate_diversify",
        )
        extracted = _extract_lean(raw) or ""
        if extracted:
            extracted = use_umbrella_mathlib_import(extracted)
        if not extracted or not has_proof_body(extracted):
            return ""
        declaration = candidate.statement_only or strip_incomplete_proof(original)
        if declaration and not declaration_name_matches(declaration, extracted):
            self._trace(
                "candidate_diversify_reject", ok=False, candidate=index,
                error="Diversified candidate changed the theorem name.",
            )
            return ""
        if proof_self_references(extracted):
            self._trace(
                "candidate_diversify_reject", ok=False, candidate=index,
                error="Diversified candidate references its own theorem.",
            )
            return ""
        return extracted.strip()

    def _trace(self, stage: str, **info: Any) -> None:
        """Record a stage diagnostic entry (visible in UI Details + server log)."""
        if not hasattr(self, "_stage_trace") or self._stage_trace is None:
            self._stage_trace = []
        active_candidate = getattr(self, "_active_candidate", None)
        if active_candidate is not None:
            info.setdefault("candidate", active_candidate)
        trace_lock = getattr(self, "_trace_lock", None)
        if trace_lock is None:
            now = _time.monotonic()
            entry = {
                "stage": stage,
                "elapsed_seconds": round(now - getattr(self, "_trace_started_at", now), 3),
                **info,
            }
            self._stage_trace.append(entry)
        else:
            with trace_lock:
                now = _time.monotonic()
                entry = {
                    "stage": stage,
                    "elapsed_seconds": round(now - getattr(self, "_trace_started_at", now), 3),
                    **info,
                }
                self._stage_trace.append(entry)
        if self.progress_callback is not None:
            try:
                self.progress_callback(dict(entry))
            except Exception:
                # Progress reporting must never interrupt formalization.
                pass
        # Compact server-side log for terminal / CMD analysis
        preview = str(info.get("preview") or info.get("error") or "")[:120].replace("\n", " ")
        length = info.get("len", info.get("raw_len", "?"))
        print(
            f"[pipeline] {stage}: len={length} ok={info.get('ok', True)} {preview}",
            flush=True,
        )

    def _trace_entry(self, entry: dict[str, Any]) -> None:
        """Forward verifier progress events into the pipeline trace."""
        item = dict(entry or {})
        stage = str(item.pop("stage", "pipeline"))
        self._trace(stage, **item)

    def _compile(self, code: str) -> tuple[bool, str]:
        """Compile within the remaining job budget, never past a stale deadline."""
        _check_deadline(getattr(self, "_deadline", None))
        deadline = getattr(self, "_deadline", None)
        timeout = None
        if deadline is not None:
            remaining = deadline - _time.monotonic()
            if remaining <= 0:
                _check_deadline(deadline)
            timeout = max(
                1,
                int(min(remaining, getattr(self, "_lean_compile_budget", remaining)) + 0.999),
            )
        preflight = preflight_lean_code(code, project=lean_project_dir())
        self._trace(
            "import_preflight", ok=bool(preflight.get("ok", True)),
            preview=(preflight.get("error") or "Local imports validated")[:240],
            suggestions=preflight.get("suggestions", []),
        )
        if not preflight.get("ok", True):
            return False, str(preflight.get("error") or "Local import preflight failed")
        code = preflight.get("code") or code
        ok, message = compile_lean(
            code, lean_bin=self.lean_bin, timeout=timeout,
            scratch_file=self._candidate_file(0),
        )
        _check_deadline(deadline)
        return ok, message

    def _compile_declaration(self, declaration: str) -> tuple[bool, str]:
        """Elaborate a theorem signature without asking Lean to prove it.

        The statement stage deliberately returns a proof-free declaration.
        Recasting its first theorem/lemma as an axiom lets Lean validate the
        imports, notation, binders, typeclasses and target before proof
        generation begins.
        """
        source = (declaration or "").strip()
        # User-provided Lean targets sometimes include a proof.  Statement
        # preflight validates only the signature, so remove that body first.
        source = source.split(":=", 1)[0].rstrip()
        source = re.sub(
            r"(?m)^(\s*)(?:theorem|lemma)\b", r"\1axiom", source, count=1
        )
        if not re.search(r"(?m)^\s*axiom\b", source):
            return False, "Statement preflight needs a theorem or lemma declaration."
        preflight = preflight_lean_code(source, project=lean_project_dir())
        if not preflight.get("ok", True):
            return False, str(preflight.get("error") or "Local import preflight failed")
        source = preflight.get("code") or source
        deadline = getattr(self, "_deadline", None)
        # Signature checks are advisory and must never monopolize a job just
        # because loading Mathlib is cold. On timeout the caller continues to
        # candidate generation; genuine compiler errors still stop early.
        budget = min(30.0, getattr(self, "_lean_compile_budget", self.lean_compile_timeout))
        remaining = (deadline - _time.monotonic()) if deadline is not None else budget
        timeout = max(1, int(min(remaining, budget) + 0.999))
        root = getattr(self, "_scratch_dir", None)
        scratch = Path(root) / "statement_preflight.lean" if root is not None else None
        return compile_declaration(
            source, lean_bin=self.lean_bin, timeout=timeout, scratch_file=scratch,
        )

    def _statement_failure_result(
        self, structured: StructuredMath, declaration: str, error: str,
        domain: str, audit_source: str,
    ) -> FormalizationResult:
        """Return explicit statement-level failure instead of blind proof calls."""
        candidate = Candidate(
            lean_code=declaration,
            statement_only=declaration,
            source_stage="statement_preflight",
            error_log=[error],
            compiled=False,
            status="statement_invalid",
            candidate_index=1,
        )
        ver = build_report(
            compiled=False, raw_message=error, code=declaration,
            natural_language=structured.main_theorem, lean_available=lean_available(self.lean_bin), llm=None,
        )
        return FormalizationResult(
            success=False, best_code=declaration, structured=structured,
            # This is a statement diagnostic, not a generated proof candidate.
            # Keeping it out of ``candidates`` prevents the UI from implying
            # that candidate 1 was generated or repaired.
            candidates=[], selected_candidate=0, repair_count=0,
            message="Lean rejected the statement before proof generation: " + ver.summary,
            domain=domain, verification=ver.to_dict(),
            token_usage=getattr(self.llm, "usage", None).to_dict()
            if getattr(self.llm, "usage", None) else {},
            stage_trace=list(getattr(self, "_stage_trace", []) or []),
            requested_candidates=max(1, self.num_candidates),
            generated_candidates=0,
            max_repair_rounds=max(0, self.max_repair_rounds),
            candidate_repairs=0,
            statement_repair_attempts=1,
            llm_candidate_workers=self.max_parallel_candidates,
            lean_compile_workers=self.lean_compile_workers,
        )

    def _repair_statement_once(
        self, structured: StructuredMath, declaration: str, error: str,
    ) -> str:
        """Use one bounded LLM call to complete a malformed statement only."""
        if not declaration.strip() or "timed out" in (error or "").lower():
            return ""
        self._trace(
            "statement_repair", ok=True, len=len(declaration),
            preview="Requesting one bounded repair for the Lean statement only.",
        )
        try:
            raw = self._chat(
                [
                    {"role": "system", "content": SYSTEM_LEAN_ONLY},
                    {"role": "user", "content": STATEMENT_REPAIR_PROMPT.format(
                        statement=structured.main_theorem or "(not available)",
                        declaration=declaration,
                        error=(error or "")[:3000],
                        local_modules=project_llm_context(statement=structured.main_theorem or ""),
                    )},
                ],
                temperature=0.1,
                stage="statement_repair",
            )
            repaired = extract_lean_code(raw) or ""
            if not repaired or has_proof_body(repaired):
                return ""
            repaired = use_umbrella_mathlib_import(repaired)
            if has_umbrella_mathlib_import(repaired):
                return ""
            return repaired.strip()
        except Exception as ex:
            self._trace("statement_repair_result", ok=False, len=0, error=str(ex))
            return ""

    def _candidate_file(self, candidate: int) -> Optional[Path]:
        root = getattr(self, "_scratch_dir", None)
        return Path(root) / f"candidate_{candidate}.lean" if root is not None else None

    def _verify_candidates(
        self, candidates: list[Candidate], structured: StructuredMath,
        repairs: str, max_repairs: int, save_to_memory: bool, domain: str,
    ) -> tuple[list[Candidate], int]:
        """Verify reusable candidate files under one cross-job lock.

        Initial candidates are compiled in a bounded wave. Repairs are then
        processed by candidate; each recompile is still limited by the worker
        setting, while an outer lock prevents separate web jobs from writing
        the shared project-local candidate files at the same time.
        """
        verified: list[Optional[Candidate]] = [None] * len(candidates)
        total_repairs = 0
        if not self._project_writable and lean_project_dir() is not None:
            for i, candidate in enumerate(candidates, start=1):
                candidate.candidate_index = i
                candidate.status = "lean_project_not_writable"
                candidate.compiled = False
                candidate.error_log.append("Configured Lean project is not writable: cannot create FormalizerScratch.")
                verified[i - 1] = candidate
                self._trace("verify_result", ok=False, candidate=i, status=candidate.status,
                            preview=f"Candidate {i} was not compiled: Lean project is not writable.")
            return [c for c in verified if c is not None], total_repairs

        lock = self.verification_lock if self.verification_lock is not None else nullcontext()
        with lock:
            self._trace("verification_lock", ok=True, preview="Using reusable candidate files in FormalizerScratch.")
            canonical: dict[str, int] = {}
            pending: list[tuple[int, Candidate]] = []
            for number, candidate in enumerate(candidates, start=1):
                candidate.candidate_index = number
                candidate.code_hash = lean_code_hash(candidate.lean_code) if candidate.lean_code else ""
                if candidate.source_stage == "generation_timeout":
                    candidate.status = "generation_timeout"
                    verified[number - 1] = candidate
                    continue
                if candidate.code_hash and candidate.code_hash in canonical:
                    candidate.status = "duplicate"
                    candidate.duplicate_of = canonical[candidate.code_hash]
                    verified[number - 1] = candidate
                    continue
                if candidate.code_hash:
                    canonical[candidate.code_hash] = number
                pending.append((number, candidate))

            workers = min(max(1, self.lean_compile_workers), max(1, len(pending)))
            self._trace("lean_compile_wave", ok=True, workers=workers,
                        preview=f"Compiling {len(pending)} candidates with up to {workers} worker(s).")
            def compile_initial(number: int, candidate: Candidate) -> tuple[int, Candidate, tuple[bool, str]]:
                preflight = preflight_lean_code(candidate.lean_code, project=lean_project_dir())
                self._trace("import_preflight", ok=bool(preflight.get("ok", True)),
                            candidate=number,
                            preview=(preflight.get("error") or "Local imports validated")[:240],
                            suggestions=preflight.get("suggestions", []))
                if not preflight.get("ok", True):
                    message = str(preflight.get("error") or "Local import preflight failed")
                    self._trace("lean_compile", ok=False, phase="preflight", candidate=number,
                                attempt=1, len=len(candidate.lean_code or ""), error=message,
                                status="compile_error")
                    return number, candidate, (False, message)
                candidate.lean_code = preflight.get("code") or candidate.lean_code
                self._trace("lean_compile", ok=True, phase="start", candidate=number, attempt=1,
                            len=len(candidate.lean_code or ""))
                timeout = max(1, int(self._lean_compile_budget + 0.999))
                ok, message = compile_lean(
                    candidate.lean_code, lean_bin=self.lean_bin, timeout=timeout,
                    scratch_file=self._candidate_file(number),
                )
                self._trace("lean_compile", ok=ok, phase="result", candidate=number, attempt=1,
                            len=len(candidate.lean_code or ""), error="" if ok else message,
                            status=compile_status(ok, message))
                return number, candidate, (ok, message)

            initial: dict[int, tuple[Candidate, tuple[bool, str]]] = {}
            if workers > 1:
                with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="lean-compile") as pool:
                    futures = [pool.submit(compile_initial, number, candidate) for number, candidate in pending]
                    for future in as_completed(futures):
                        number, candidate, outcome = future.result()
                        initial[number] = (candidate, outcome)
            else:
                for number, candidate in pending:
                    number, candidate, outcome = compile_initial(number, candidate)
                    initial[number] = (candidate, outcome)

            def run_one(number: int, candidate: Candidate, initial_result: tuple[bool, str]) -> tuple[int, Candidate]:
                self._trace("verify_candidate", ok=True, candidate=number, len=len(candidate.lean_code or ""),
                            preview=f"Verifying candidate {number} with Lean.")
                fixed = repair_loop(
                    candidate, self.llm, natural_language=structured.main_theorem,
                    repair_examples=repairs, max_rounds=max_repairs, lean_bin=self.lean_bin,
                    progress_callback=self._trace_entry, candidate_index=number,
                    deadline_monotonic=getattr(self, "_deadline", None),
                    compile_timeout_seconds=self._lean_compile_budget,
                    scratch_file=self._candidate_file(number),
                    attempt_result_callback=lambda record: self._write_attempt_result(number, record),
                    initial_compilation=initial_result,
                )
                return number, fixed

            # LLM client state is not thread safe during repair.  The workers
            # setting therefore accelerates the initial compiler wave only;
            # repair loops stay deterministic and safe until isolated clients
            # are introduced for them.
            for number, _candidate in pending:
                candidate, initial_result = initial[number]
                number, fixed = run_one(number, candidate, initial_result)
                verified[number - 1] = fixed

            for number, candidate in enumerate(candidates, start=1):
                if candidate.status != "duplicate":
                    continue
                source = verified[candidate.duplicate_of - 1]
                if source is None:
                    continue
                candidate.lean_code = source.lean_code
                candidate.compiled = source.compiled
                candidate.code_hash = source.code_hash
                candidate.compile_attempts = source.compile_attempts
                candidate.repair_rounds = source.repair_rounds
                candidate.cache_hit = source.cache_hit
                candidate.error_log = list(source.error_log) + [
                    f"duplicate of candidate {candidate.duplicate_of}; reused its verification result"
                ]
                verified[number - 1] = candidate
                self._trace("candidate_duplicate", ok=True, candidate=number,
                            duplicate_of=candidate.duplicate_of,
                            preview=f"Candidate {number} duplicates candidate {candidate.duplicate_of}; no second Lean compilation.")

        output = [candidate for candidate in verified if candidate is not None]
        for candidate in output:
            total_repairs += candidate.repair_rounds
            self._write_compile_result(candidate.candidate_index, candidate)
            self._trace("verify_result", ok=candidate.compiled, candidate=candidate.candidate_index,
                        status=candidate.status,
                        preview=(f"Candidate {candidate.candidate_index} "
                                 f"{'verified' if candidate.compiled else candidate.status.replace('_', ' ')}"))
        return output, total_repairs

    def _write_compile_result(self, candidate: int, value: Candidate) -> None:
        """Persist a compact per-job record without replacing the source file."""
        root = getattr(self, "_scratch_dir", None)
        if root is None:
            return
        try:
            result_dir = Path(root) / "results"
            result_dir.mkdir(parents=True, exist_ok=True)
            suffix = "duplicate" if value.status == "duplicate" else f"attempt_{value.compile_attempts}"
            record = {
                "candidate": candidate, "status": value.status,
                "compiled": value.compiled, "code_hash": value.code_hash,
                "compile_attempts": value.compile_attempts,
                "repair_rounds": value.repair_rounds, "cache_hit": value.cache_hit,
                "duplicate_of": value.duplicate_of, "errors": value.error_log,
                "source": str(self._candidate_file(candidate) or ""),
                "lean_code": value.lean_code,
            }
            (result_dir / f"candidate_{candidate}_{suffix}.json").write_text(
                json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8"
            )
        except OSError as ex:
            self._trace("scratch_record", ok=False, candidate=candidate, error=str(ex))

    def _write_attempt_result(self, candidate: int, record: dict[str, Any]) -> None:
        root = getattr(self, "_scratch_dir", None)
        if root is None:
            return
        try:
            result_dir = Path(root) / "results"
            result_dir.mkdir(parents=True, exist_ok=True)
            output = dict(record)
            output["candidate"] = candidate
            (result_dir / f"candidate_{candidate}_attempt_{record.get('attempt', 0)}.json").write_text(
                json.dumps(output, indent=2, ensure_ascii=False), encoding="utf-8"
            )
        except OSError as ex:
            self._trace("scratch_record", ok=False, candidate=candidate, error=str(ex))

    def _system_with_project(
        self, system_content: str, statement: str = ""
    ) -> str:
        """Append local Lean/Mathlib revision so the model targets this compiler."""
        try:
            ctx = project_llm_context(statement=statement)
        except Exception:
            ctx = ""
        if not ctx:
            return system_content
        return f"{system_content}\n\n--- Local compiler target ---\n{ctx}"

    def _chat(
        self,
        messages,
        temperature: float = 0.2,
        max_tokens: int = 8192,
        *,
        stage: str = "chat",
    ) -> str:
        deadline = getattr(self, "_deadline", None)
        _check_deadline(deadline)
        # Inject project toolchain into system messages for Lean stages
        statement_text = ""
        for m in reversed(messages):
            if isinstance(m, dict) and m.get("role") == "user" and isinstance(
                m.get("content"), str
            ):
                statement_text = m["content"]
                break
        patched = []
        for m in messages:
            if (
                isinstance(m, dict)
                and m.get("role") == "system"
                and isinstance(m.get("content"), str)
            ):
                patched.append(
                    {
                        "role": "system",
                        "content": self._system_with_project(
                            m["content"], statement_text
                        ),
                    }
                )
            else:
                patched.append(m)
        configured_timeout = getattr(self.llm, "timeout_seconds", None)
        if configured_timeout is not None and deadline is not None:
            remaining = deadline - _time.monotonic()
            self.llm.timeout_seconds = max(
                1.0,
                min(
                    float(configured_timeout),
                    getattr(self, "_llm_call_budget", float(configured_timeout)),
                    remaining,
                ),
            )
        try:
            self._trace("llm_send", ok=True, request_stage=stage, message_count=len(patched))
            self._trace("llm_wait", ok=True, request_stage=stage)
            raw = self.llm.chat(
                patched, temperature=temperature, max_tokens=max_tokens
            )
        except Exception as ex:
            self._trace("llm_wait", ok=False, request_stage=stage, error=f"{type(ex).__name__}: {ex}")
            self._trace(stage, ok=False, error=f"{type(ex).__name__}: {ex}", len=0)
            # A per-request timeout while producing one of several candidates
            # must not discard the work of the other requested candidates.
            # A wall-clock job deadline remains terminal for the whole run.
            if (
                isinstance(ex, LLMTimeoutError)
                and getattr(self, "_active_candidate", None) is not None
                and (deadline is None or _time.monotonic() < deadline)
            ):
                self._candidate_llm_error = str(ex)
                self._trace(
                    "candidate_generated",
                    ok=False,
                    len=0,
                    error=f"Candidate generation timed out: {ex}",
                )
                return ""
            raise
        finally:
            if configured_timeout is not None:
                self.llm.timeout_seconds = configured_timeout
        text = raw if isinstance(raw, str) else ("" if raw is None else str(raw))
        preview = text.strip().replace("\n", " ")[:180]
        self._trace(
            stage,
            ok=bool(text.strip()),
            len=len(text),
            preview=preview or "(empty response from model)",
        )
        return text

    # ------------------------------------------------------------------
    # Stages
    # ------------------------------------------------------------------

    def _stage_proof_audit(
        self,
        source: str,
        *,
        lean_code: str = "",
        compiler_result: str = "",
    ) -> ProofAudit:
        """Review source proof after final Lean compilation; never repair it."""
        try:
            project_context = project_llm_context(statement=source[:1200])
            raw = self._chat(
                [
                    {"role": "system", "content": SYSTEM_MATH + "\n" + "Output JSON only."},
                    {
                        "role": "user",
                        "content": PROOF_AUDIT_PROMPT.format(
                            source=source[:12000],
                            lean_code=(lean_code or "(no Lean code selected)")[:14000],
                            compiler_result=(compiler_result or "(no compiler result)")[:5000],
                            project_context=project_context[:3000],
                        ),
                    },
                ],
                temperature=0.0,
                stage="proof_audit",
            )
        except LLMTimeoutError as ex:
            self._trace("proof_audit", ok=False, len=0, overall="inconclusive", step_count=0, error=str(ex))
            return ProofAudit(
                overall="inconclusive",
                theorem_alignment="unverified",
                summary=f"Proof audit timed out; Lean translation may still proceed. {ex}",
                steps=[],
            )
        except Exception as ex:
            self._trace("proof_audit", ok=False, len=0, overall="inconclusive", step_count=0, error=str(ex))
            return ProofAudit(
                overall="inconclusive",
                theorem_alignment="unverified",
                summary=f"Proof audit unavailable; Lean translation may still proceed. {ex}",
                steps=[],
            )
        data = self._parse_json_response(raw)
        allowed_overall = {"sound", "gap_found", "invalid", "ambiguous", "inconclusive"}
        allowed_step = {"valid", "gap", "invalid", "ambiguous", "inconclusive"}
        steps: list[ProofStep] = []
        for pos, item in enumerate(data.get("steps") or [], start=1):
            if not isinstance(item, dict):
                continue
            status = str(item.get("status") or "inconclusive").lower()
            if status not in allowed_step:
                status = "inconclusive"
            missing = item.get("missing") or []
            if isinstance(missing, str):
                missing = [missing]
            steps.append(
                ProofStep(
                    index=int(item.get("index") or pos),
                    source=str(item.get("source") or "").strip(),
                    claim=str(item.get("claim") or "").strip(),
                    reason=str(item.get("reason") or "").strip(),
                    status=status,
                    explanation=str(item.get("explanation") or "").strip(),
                    missing=[str(x).strip() for x in missing if str(x).strip()],
                    lean_anchor=f"step_{int(item.get('index') or pos)}",
                )
            )
        overall = str(data.get("overall") or "inconclusive").lower()
        if overall not in allowed_overall:
            overall = "inconclusive"
        if not steps:
            # Do not claim a proof is sound when the audit response is malformed.
            overall = "inconclusive"
        audit = ProofAudit(
            overall=overall,
            theorem_alignment=str(data.get("theorem_alignment") or "exact_target"),
            summary=str(data.get("summary") or "Proof audit did not return usable step findings.").strip(),
            steps=steps,
        )
        self._trace(
            "proof_audit",
            ok=bool(steps),
            len=len(raw or ""),
            overall=audit.overall,
            step_count=len(steps),
            preview=audit.summary[:160],
            error=None if steps else "Proof audit returned no usable steps.",
        )
        return audit

    @staticmethod
    def _parse_json_response(text: str) -> dict[str, Any]:
        """Extract the first JSON object from an LLM response."""
        raw = text or ""
        fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL)
        candidates = [fenced.group(1)] if fenced else []
        candidates.append(raw)
        import json

        for candidate in candidates:
            start = candidate.find("{")
            while start >= 0:
                try:
                    value, _ = json.JSONDecoder().raw_decode(candidate[start:])
                    if isinstance(value, dict):
                        return value
                except json.JSONDecodeError:
                    pass
                start = candidate.find("{", start + 1)
        return {}

    def _stage_simple_oneshot(
        self,
        s: StructuredMath,
        examples: str,
        repairs: str,
        *,
        temperature: float = 0.1,
        index: int = 1,
    ) -> str:
        """Single LLM call: full Lean snippet for elementary claims (fast path)."""
        for d in s.definitions:
            if isinstance(d, str) and d.startswith("[user_lean_statement]"):
                lean = d.split("]", 1)[-1].strip()
                if lean and ":=" in lean:
                    return normalize_lean_imports(lean)
                if lean:
                    prompt = (
                        "Complete this Lean 4 declaration with a Mathlib-style proof. "
                        "Output ONLY Lean code.\n\n"
                        f"```lean\n{lean}\n```\n"
                    )
                    raw = self._chat(
                        [
                            {"role": "system", "content": SYSTEM_LEAN_ONLY},
                            {"role": "user", "content": prompt},
                        ],
                        temperature=temperature,
                        stage="oneshot_user_lean",
                    )
                    extracted = _extract_lean(raw) or ""
                    self._trace(
                        "oneshot_extract",
                        ok=bool(extracted.strip()),
                        len=len(extracted or ""),
                        raw_len=len(raw or ""),
                        raw_preview=" ".join((raw or "").split())[:160]
                        or "(empty response from model)",
                        preview=(extracted or "")[:160] or "(extract failed / empty)",
                    )
                    return extracted

        sketch = (
            "\n".join(s.proof_sketch)
            if s.proof_sketch
            else "(direct Mathlib lemma if possible)"
        )
        prompt = ONESHOT_PROMPT.format(
            statement=s.main_theorem,
            sketch=sketch,
            examples=examples or "(none)",
            strategy=_candidate_strategy(index),
        )
        raw = self._chat(
            [
                {"role": "system", "content": SYSTEM_LEAN_ONLY},
                {"role": "user", "content": prompt},
            ],
            temperature=temperature,
            stage="oneshot",
        )
        extracted = _extract_lean(raw) or ""
        if extracted:
            extracted = use_umbrella_mathlib_import(extracted)
        self._trace(
            "oneshot_extract",
            ok=bool(extracted.strip()),
            len=len(extracted or ""),
            raw_len=len(raw or ""),
            raw_preview=" ".join((raw or "").split())[:160]
            or "(empty response from model)",
            preview=(extracted or "")[:160] or "(extract failed / empty)",
        )
        return extracted

    def _stage_understand(self, s: StructuredMath) -> str:
        prompt = UNDERSTAND_PROMPT.format(
            statement=s.main_theorem,
            definitions="\n".join(f"- {d}" for d in s.definitions) or "(none)",
        )
        return self._chat(
            [
                {"role": "system", "content": SYSTEM_MATH},
                {"role": "user", "content": prompt},
            ],
            temperature=0.1,
            stage="understand",
        )

    def _stage_statement(
        self, s: StructuredMath, examples: str, understanding: str
    ) -> str:
        for d in s.definitions:
            if isinstance(d, str) and d.startswith("[user_lean_statement]"):
                lean = d.split("]", 1)[-1].strip()
                if lean:
                    self._trace(
                        "statement_user_lean",
                        ok=True,
                        len=len(lean),
                        preview=lean[:160],
                    )
                    return lean
        prompt = STATEMENT_PROMPT.format(
            statement=s.main_theorem + "\n\nClarification:\n" + understanding,
            definitions="\n".join(f"- {d}" for d in s.definitions) or "(none)",
            examples=examples or "(no memory examples yet)",
        )
        raw = self._chat(
            [
                {"role": "system", "content": SYSTEM_LEAN_ONLY},
                {"role": "user", "content": prompt},
            ],
            temperature=0.1,
            stage="statement",
        )
        extracted = _extract_lean(raw) or ""
        if extracted:
            extracted = use_umbrella_mathlib_import(extracted)
        self._trace(
            "statement_extract",
            ok=bool(extracted.strip()),
            len=len(extracted or ""),
            raw_len=len(raw or ""),
            raw_preview=" ".join((raw or "").split())[:160]
            or "(empty response from model)",
            preview=(extracted or "")[:160] or "(extract failed / empty)",
            error=(
                "Raw response was non-empty but no Lean code was extracted."
                if raw.strip() and not extracted.strip()
                else None
            ),
        )
        return extracted

    def _stage_proof(
        self,
        s: StructuredMath,
        declaration: str,
        examples: str,
        repairs: str,
        temperature: float,
        *,
        index: int = 1,
    ) -> str:
        sketch = (
            "\n".join(f"{i+1}. {step}" for i, step in enumerate(s.proof_sketch))
            or "(no sketch — derive a short direct proof)"
        )
        if not (declaration or "").strip():
            self._trace(
                "proof_skip",
                ok=False,
                len=0,
                error="Empty declaration from statement stage — skipping proof LLM call",
            )
            return ""
        prompt = PROOF_PROMPT.format(
            declaration=declaration,
            sketch=sketch,
            examples=examples or "(none)",
            repairs=repairs or "(none)",
            local_context="\n".join(self._local_lean_facts) or "(No targeted facts were returned; use only established Mathlib names.)",
            strategy=_candidate_strategy(index),
        )
        raw = self._chat(
            [
                {"role": "system", "content": SYSTEM_LEAN_ONLY},
                {"role": "user", "content": prompt},
            ],
            temperature=temperature,
            stage="proof",
        )
        extracted = _extract_lean(raw) or ""
        if extracted:
            extracted = use_umbrella_mathlib_import(extracted)
        # If the model only echoed the declaration (no proof body), keep it
        # but mark the gap so repair / UI can see it.
        body_ok = bool(extracted) and has_proof_body(extracted)
        self_ref = bool(extracted) and proof_self_references(extracted)
        self._trace(
            "proof_extract",
            ok=bool(extracted.strip()) and body_ok and not self_ref,
            len=len(extracted or ""),
            raw_len=len(raw or ""),
            raw_preview=" ".join((raw or "").split())[:160]
            or "(empty response from model)",
            preview=(extracted or "")[:160] or "(extract failed / empty)",
            has_proof_body=body_ok,
            error=(
                "Proof body references its own theorem."
                if self_ref
                else (
                    "Raw response was non-empty but no Lean code was extracted."
                    if raw.strip() and not extracted.strip()
                    else None
                )
            ),
        )
        if extracted.strip() and not body_ok:
            self._trace(
                "proof_no_body",
                ok=False,
                len=len(extracted),
                error=(
                    "Proof stage returned a declaration without := / by. "
                    "Trying complete-proof fallback."
                ),
            )
        return extracted

    def _stage_complete_proof(self, s: StructuredMath, declaration: str) -> str:
        """Second-chance LLM call when the proof stage returned empty / no body."""
        if not (declaration or "").strip():
            return ""
        known = complete_proof_with_known_lemmas(
            declaration, s.main_theorem or ""
        )
        if known:
            self._trace(
                "complete_proof_known",
                ok=True,
                len=len(known),
                preview=(known or "")[:160],
                source="Mathlib-lemma completion",
            )
            return known
        sketch = (
            "\n".join(f"{i+1}. {step}" for i, step in enumerate(s.proof_sketch))
            or "(complete with Mathlib lemmas)"
        )
        prompt = COMPLETE_PROOF_PROMPT.format(
            declaration=declaration,
            statement=s.main_theorem or "",
            sketch=sketch,
        )
        raw = self._chat(
            [
                {"role": "system", "content": SYSTEM_LEAN_ONLY},
                {"role": "user", "content": prompt},
            ],
            temperature=0.15,
            stage="complete_proof",
        )
        extracted = _extract_lean(raw) or ""
        if extracted:
            extracted = use_umbrella_mathlib_import(extracted)
        body_ok = bool(extracted) and has_proof_body(extracted)
        self_ref = bool(extracted) and proof_self_references(extracted)
        self._trace(
            "complete_proof_extract",
            ok=bool(extracted.strip()) and body_ok and not self_ref,
            len=len(extracted or ""),
            raw_len=len(raw or ""),
            raw_preview=" ".join((raw or "").split())[:160]
            or "(empty response from model)",
            preview=(extracted or "")[:160] or "(extract failed / empty)",
            has_proof_body=body_ok,
            error=(
                "Complete-proof fallback references its own theorem."
                if self_ref
                else (
                    "Raw response was non-empty but no Lean code was extracted."
                    if raw.strip() and not extracted.strip()
                    else None
                )
            ),
        )
        # Prefer completed code; if still no body, keep declaration for repair
        if body_ok and not self_ref:
            return extracted
        return declaration

    def _stage_critique(self, code: str, temperature: float) -> str:
        if not (code or "").strip():
            self._trace("critique_skip", ok=False, len=0, error="Empty code — skip critique")
            return ""
        prompt = CRITIQUE_PROMPT.format(code=code)
        raw = self._chat(
            [
                {"role": "system", "content": SYSTEM_LEAN_ONLY},
                {"role": "user", "content": prompt},
            ],
            temperature=temperature,
            stage="critique",
        )
        extracted = _extract_lean(raw) or ""
        if extracted:
            extracted = use_umbrella_mathlib_import(extracted)
        self._trace(
            "critique_extract",
            ok=bool(extracted.strip()),
            len=len(extracted or ""),
            raw_len=len(raw or ""),
            raw_preview=" ".join((raw or "").split())[:160]
            or "(empty response from model)",
            preview=(extracted or "")[:160] or "(extract failed / empty)",
            error=(
                "Raw response was non-empty but no Lean code was extracted."
                if raw.strip() and not extracted.strip()
                else None
            ),
        )
        return extracted

    def _synthesize_nl_proof(self, statement: str, code: str) -> str:
        """Generate a formal NL proof aligned with the Lean code for memory."""
        try:
            prompt = NL_PROOF_FROM_LEAN_PROMPT.format(
                statement=statement[:500], code=code[:3000]
            )
            raw = self._chat(
                [
                    {"role": "system", "content": SYSTEM_MATH},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.15,
                stage="nl_proof",
            )
            text = (raw or "").strip()
            if text and not text.lower().startswith("proof"):
                text = "Proof.\n" + text
            return text[:2000]
        except Exception as ex:
            self._trace("nl_proof", ok=False, error=str(ex), len=0)
            return ""

    # ------------------------------------------------------------------
    # Formatting helpers — always triple: statement / NL proof / Lean
    # ------------------------------------------------------------------
    @staticmethod
    def _format_success_examples(
        entries: list, max_lean: int = 600, max_total: int = 1800
    ) -> str:
        if not entries:
            return ""
        blocks: list[str] = []
        total = 0
        for i, e in enumerate(entries, 1):
            lean_code = normalize_lean_imports(e.lean_code or "").strip()[:max_lean]
            block = (
                f"### Ex {i}\n"
                f"NL: {(e.natural_language or '')[:180]}\n"
                f"```lean\n{lean_code}\n```"
            )
            if total + len(block) > max_total:
                if not blocks:
                    block = block[:max_total]
                else:
                    break
            blocks.append(block)
            total += len(block)
        return "\n\n".join(blocks)

    @staticmethod
    def _format_repair_examples(
        entries: list, max_code: int = 400, max_total: int = 900
    ) -> str:
        if not entries:
            return ""
        blocks: list[str] = []
        total = 0
        for i, e in enumerate(entries, 1):
            wrong_code = normalize_lean_imports(e.wrong_code or "").strip()[:max_code]
            corrected_code = normalize_lean_imports(
                e.corrected_code or e.lean_code or ""
            ).strip()[:max_code]
            block = (
                f"### Fix {i}\n"
                f"Err: {(e.error_message or '')[:200]}\n"
                f"Wrong:\n```lean\n{wrong_code}\n```\n"
                f"Fixed:\n```lean\n{corrected_code}\n```"
            )
            if total + len(block) > max_total:
                if not blocks:
                    block = block[:max_total]
                else:
                    break
            blocks.append(block)
            total += len(block)
        return "\n\n".join(blocks)


def _extract_lean(text: str) -> Optional[str]:
    return extract_lean_code(text)
