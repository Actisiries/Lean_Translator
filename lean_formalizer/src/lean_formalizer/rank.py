"""Step 7 – Ranking & selection among successfully compiled candidates."""

from __future__ import annotations

import json
import re
from typing import Optional

from .llm import LLMClient
from .models import Candidate
from .prompts import RANK_PROMPT


def rank_candidates(
    candidates: list[Candidate],
    llm: Optional[LLMClient] = None,
) -> list[Candidate]:
    """
    Rank compiled candidates. Prefer:
      1. compiled successfully
      2. fewer lines / more idiomatic (LLM assist when available)
      3. no `sorry`
    """
    compiled = [c for c in candidates if c.compiled]
    if not compiled:
        # Prefer non-empty, then longer snippets (empty repair must not win)
        return sorted(
            candidates,
            key=lambda c: (
                not (c.lean_code or "").strip(),
                -len(c.lean_code or ""),
            ),
        )

    # Heuristic base scores
    for c in compiled:
        score = 0.0
        lines = c.lean_code.count("\n") + 1
        score -= lines * 0.1                     # prefer shorter
        if "sorry" in c.lean_code:
            score -= 50
        if "by" in c.lean_code:
            score += 5                           # tactic style
        if c.lean_code.count("sorry") == 0:
            score += 10
        # Prefer codes that look like they import Mathlib
        if "Mathlib" in c.lean_code or "import" in c.lean_code:
            score += 3
        c.rank_score = score

    # Optional LLM re-ranking
    if llm is not None and len(compiled) > 1:
        order = _llm_rank(compiled, llm)
        if order:
            for i, idx in enumerate(order):
                if 0 <= idx < len(compiled):
                    compiled[idx].rank_score += (len(order) - i) * 10

    compiled.sort(key=lambda c: -c.rank_score)
    # Append non-compiled at the end
    rest = [c for c in candidates if not c.compiled]
    return compiled + rest


def _llm_rank(candidates: list[Candidate], llm: LLMClient) -> list[int]:
    blocks = []
    for i, c in enumerate(candidates):
        blocks.append(f"Candidate {i}:\n```lean\n{c.lean_code}\n```")
    prompt = RANK_PROMPT.format(candidates="\n\n".join(blocks))
    try:
        reply = llm.chat(
            [
                {"role": "system", "content": "You rank Lean code quality."},
                {"role": "user", "content": prompt},
            ],
            temperature=0.0,
        )
        m = re.search(r"\[[\d,\s]+\]", reply)
        if m:
            return list(json.loads(m.group(0)))
    except Exception:
        pass
    return list(range(len(candidates)))
