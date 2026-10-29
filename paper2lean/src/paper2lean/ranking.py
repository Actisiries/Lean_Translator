from __future__ import annotations

from typing import Any

from .verification import audit_code


def score_candidate(code: str, compiled: bool, diagnostic: str = "") -> tuple[int, dict[str, Any]]:
    audit = audit_code(code)
    score = 0
    if compiled:
        score += 100
    if audit["trusted"]:
        score += 20
    if "error" not in diagnostic.lower():
        score += 2
    score -= max(0, len(code or "") // 5000)
    return score, {"score": score, "compiled": compiled, "audit": audit, "diagnostic": diagnostic}


def rank_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(candidates, key=lambda item: int(item.get("score", -1)), reverse=True)
