from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class SourceSpan:
    source: str
    start_line: int = 0
    end_line: int = 0
    page: int = 0
    bbox: list[float] = field(default_factory=list)


@dataclass
class SourceBlock:
    id: str
    kind: str
    text: str
    spans: list[SourceSpan] = field(default_factory=list)
    section: str = ""
    labels: list[str] = field(default_factory=list)
    confidence: float = 1.0
    issues: list[str] = field(default_factory=list)
    display_name: str = ""
    number: str = ""
    title: str = ""
    structure_locked: bool = False


@dataclass
class MathUnit:
    id: str
    kind: str
    label: str
    statement: str
    source_block_ids: list[str] = field(default_factory=list)
    proof_block_ids: list[str] = field(default_factory=list)
    proof: str = ""
    assumptions: list[str] = field(default_factory=list)
    dependencies: list[str] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)
    selected: bool = False
    status: str = "pending"
    representation: dict[str, Any] = field(default_factory=dict)
    declaration: str = ""
    code: str = ""
    compiler: dict[str, Any] = field(default_factory=dict)
    review: dict[str, Any] = field(default_factory=dict)
    attempts: list[dict[str, Any]] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)
    display_name: str = ""
    number: str = ""
    title: str = ""
    proof_status: str = "missing"
    proof_evidence: list[str] = field(default_factory=list)


@dataclass
class PaperModel:
    id: str
    name: str
    format: str
    text: str = ""
    files: dict[str, str] = field(default_factory=dict)
    blocks: list[SourceBlock] = field(default_factory=list)
    units: list[MathUnit] = field(default_factory=list)
    notation: dict[str, Any] = field(default_factory=dict)
    proof_links: list[dict[str, Any]] = field(default_factory=list)
    edges: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    page_count: int = 0
    extraction_complete: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "PaperModel":
        d = dict(value)
        d["blocks"] = [SourceBlock(**{**b, "spans": [SourceSpan(**s) for s in b.get("spans", [])]}) for b in d.get("blocks", [])]
        d["units"] = [MathUnit(**u) for u in d.get("units", [])]
        return cls(**d)


@dataclass
class StageResult:
    stage: str
    status: str
    output: Any = None
    message: str = ""
    artifact: str = ""
    llm_used: bool = False
    issues: list[str] = field(default_factory=list)
