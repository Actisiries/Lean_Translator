from __future__ import annotations

from typing import Any


STAGES = (
    "ingest", "structure_map", "normalize_blocks", "notation_ledger",
    "proof_linking", "dependency_plan", "representation_plan",
    "declaration", "proof_candidate", "repair", "fidelity_audit",
    "trust_audit", "assembly", "report",
)


def valid_stage(value: str) -> bool:
    return value in STAGES


def stage_record(stage: str, status: str, **extra: Any) -> dict[str, Any]:
    if not valid_stage(stage):
        raise ValueError(f"Unknown Paper2Lean stage: {stage}")
    return {"stage": stage, "status": status, **extra}
