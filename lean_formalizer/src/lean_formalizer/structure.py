"""Step 2 – Structure & Decomposition via LLM."""

from __future__ import annotations

import json
import re
import time
from typing import Any, Callable, Optional

from .llm import LLMClient, LLMTimeoutError
from .models import StructuredMath
from .prompts import STRUCTURE_PROMPT, SYSTEM_JSON, SYSTEM_MATH


def decompose(
    cleaned_text: str,
    llm: LLMClient,
    domain: str = "general",
    progress_callback: Optional[Callable[[dict[str, Any]], None]] = None,
    deadline_monotonic: Optional[float] = None,
) -> StructuredMath:
    """Extract definitions, main theorem, lemmas, proof sketch, domain, difficulty."""
    prompt = STRUCTURE_PROMPT.format(text=cleaned_text[:6000], domain=domain)
    messages = [
        {"role": "system", "content": SYSTEM_MATH + " " + SYSTEM_JSON},
        {"role": "user", "content": prompt},
    ]
    if progress_callback:
        progress_callback({"stage": "llm_send", "ok": True, "request_stage": "structure"})
        progress_callback({"stage": "llm_wait", "ok": True, "request_stage": "structure"})
    configured_timeout = getattr(llm, "timeout_seconds", None)
    if deadline_monotonic is not None:
        remaining = deadline_monotonic - time.monotonic()
        if remaining <= 0:
            raise LLMTimeoutError("Formalization stopped: exceeded job time limit.")
        if configured_timeout is not None:
            llm.timeout_seconds = max(
                1.0,
                min(
                    float(configured_timeout),
                    float(getattr(llm, "call_timeout_seconds", configured_timeout)),
                    remaining,
                ),
            )
    try:
        response = llm.chat(messages, temperature=0.1, max_tokens=8192)
    except Exception as ex:
        if progress_callback:
            progress_callback({
                "stage": "llm_wait", "ok": False, "request_stage": "structure",
                "error": f"{type(ex).__name__}: {ex}",
            })
        raise
    finally:
        if configured_timeout is not None:
            llm.timeout_seconds = configured_timeout
    if progress_callback:
        progress_callback({
            "stage": "llm_response", "ok": bool(str(response or "").strip()),
            "request_stage": "structure", "len": len(str(response or "")),
        })
    data = _parse_json_block(response)
    dom = str(data.get("domain") or domain or "general").strip() or "general"
    diff = str(data.get("difficulty") or "medium").strip().lower() or "medium"
    if diff not in {"easy", "medium", "hard", "graduate", "classic"}:
        diff = "medium"
    proof_sketch = data.get("proof_sketch") or []
    if isinstance(proof_sketch, str):
        proof_sketch = [s.strip() for s in proof_sketch.splitlines() if s.strip()]
    fallback_main = re.split(r"(?<=[.!?])\s+(?=[A-Z])", cleaned_text.strip())
    main_theorem = str(
        data.get("main_theorem") or (fallback_main[0] if fallback_main else cleaned_text)
    ).strip()[:400]
    if len(main_theorem) < 30 and len(cleaned_text.strip()) > len(main_theorem):
        main_theorem = cleaned_text.strip()[:400]
    return StructuredMath(
        definitions=list(data.get("definitions") or []),
        main_theorem=main_theorem,
        lemmas=list(data.get("lemmas") or []),
        proof_sketch=list(proof_sketch),
        domain=dom,
        difficulty=diff,
        raw_text=cleaned_text,
    )


def _parse_json_block(text: str) -> dict[str, Any]:
    text = text or ""
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    decoder = json.JSONDecoder()
    start = 0
    while True:
        idx = text.find("{", start)
        if idx == -1:
            break
        try:
            obj, _ = decoder.raw_decode(text[idx:])
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            pass
        start = idx + 1
    return {}
