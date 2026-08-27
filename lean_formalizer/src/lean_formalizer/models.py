"""Shared data models for the formalization pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Optional
import json
from datetime import datetime, timezone


@dataclass
class StructuredMath:
    """Result of Step 2 – structure & decomposition."""

    definitions: list[str] = field(default_factory=list)
    main_theorem: str = ""
    lemmas: list[str] = field(default_factory=list)
    proof_sketch: list[str] = field(default_factory=list)
    domain: str = "general"
    difficulty: str = "medium"
    raw_text: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "StructuredMath":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class Candidate:
    """One generated Lean candidate before / after verification."""

    lean_code: str
    statement_only: str = ""
    proof: str = ""
    source_stage: str = ""          # e.g. "initial", "repair_1"
    error_log: list[str] = field(default_factory=list)
    compiled: bool = False
    # Compiler-derived lifecycle information. ``compiled`` remains for
    # compatibility with existing callers and saved results.
    status: str = "pending"
    code_hash: str = ""
    compile_attempts: int = 0
    repair_rounds: int = 0
    cache_hit: bool = False
    duplicate_of: int = 0
    candidate_index: int = 0
    rank_score: float = 0.0
    notes: str = ""


@dataclass
class ProofStep:
    """One source-proof step and its mathematical audit finding."""

    index: int
    source: str
    claim: str = ""
    reason: str = ""
    status: str = "inconclusive"  # valid | gap | invalid | ambiguous | inconclusive
    explanation: str = ""
    missing: list[str] = field(default_factory=list)
    lean_anchor: str = ""


@dataclass
class ProofAudit:
    """Review of the submitted human proof against the final Lean result."""

    overall: str = "inconclusive"  # sound | gap_found | invalid | ambiguous | inconclusive
    theorem_alignment: str = "unverified"
    summary: str = ""
    steps: list[ProofStep] = field(default_factory=list)


@dataclass
class FormalizationResult:
    """Final output of the whole pipeline."""

    success: bool
    best_code: str = ""
    structured: Optional[StructuredMath] = None
    proof_audit: Optional[ProofAudit] = None
    # Original 1-based generation ID selected for final checking. Zero means
    # that no candidate code was selected; ranking never renumbers it.
    selected_candidate: int = 0
    candidates: list[Candidate] = field(default_factory=list)
    memory_hits: list[dict[str, Any]] = field(default_factory=list)
    repair_count: int = 0
    message: str = ""
    domain: str = "general"
    # Compilation + compiler-derived formalization feedback for the user
    verification: dict[str, Any] = field(default_factory=dict)
    token_usage: dict[str, Any] = field(default_factory=dict)
    quota_exceeded: bool = False
    # Per-stage diagnostics (lengths, previews, errors) for empty-code debugging
    stage_trace: list[dict[str, Any]] = field(default_factory=list)
    # Run configuration and actual work counts.  Keeping these separate makes
    # it clear that a worker count is concurrency, not the requested breadth.
    requested_candidates: int = 0
    generated_candidates: int = 0
    max_repair_rounds: int = 0
    candidate_repairs: int = 0
    statement_repair_attempts: int = 0
    llm_candidate_workers: int = 0
    lean_compile_workers: int = 0
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        return d


@dataclass
class MemoryEntry:
    """One item stored in the long-term memory bank.

    Fields for NL↔FL alignment during translation:
      - natural_language: theorem/lemma statement in natural language
      - natural_language_proof: formal-style prose proof aligned with lean_code
      - lean_code: Lean 4 formalization (statement + proof)
    """

    id: str
    natural_language: str
    lean_code: str
    natural_language_proof: str = ""     # formal NL proof corresponding to lean_code
    domain: str = "general"
    difficulty: str = "medium"          # easy / medium / hard / classic / graduate
    success: bool = True
    # For negative / repair examples:
    wrong_code: str = ""
    error_message: str = ""
    corrected_code: str = ""
    tags: list[str] = field(default_factory=list)
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "MemoryEntry":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, ensure_ascii=False)
