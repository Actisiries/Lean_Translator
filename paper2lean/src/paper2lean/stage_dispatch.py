from __future__ import annotations

from typing import Any

from .llm import LLMDispatcher


class StageDispatcher:
    """Central prompt routing point; deterministic stages can bypass the LLM."""
    def __init__(self, llm: LLMDispatcher):
        self.llm = llm

    def json_stage(self, stage: str, task: str, payload: dict[str, Any]) -> tuple[Any, bool]:
        return self.llm.ask(stage, task, payload, json_output=True)

    def lean_stage(self, stage: str, task: str, payload: dict[str, Any]) -> tuple[str | None, bool]:
        value, used = self.llm.ask(stage, task, payload, json_output=False)
        return value if isinstance(value, str) else None, used
