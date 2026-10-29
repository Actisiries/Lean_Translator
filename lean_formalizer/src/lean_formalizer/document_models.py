"""Source-backed records shared by paper extraction, planning, and execution."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class SourceSpan:
    file: str
    start_line: int
    end_line: int
    page: int = 0
    bbox: list[float] = field(default_factory=list)


@dataclass
class DocumentBlock:
    id: str
    kind: str
    text: str
    start: int
    end: int
    spans: list[SourceSpan] = field(default_factory=list)
    labels: list[str] = field(default_factory=list)
    section: str = ""


@dataclass
class MathUnit:
    id: str
    kind: str
    label: str
    statement: str
    block_id: str
    proof: str = ""
    context: list[str] = field(default_factory=list)
    dependencies: list[str] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)
    selected: bool = False
    status: str = "pending"
    message: str = ""
    signature: str = ""
    code: str = ""
    imports: list[str] = field(default_factory=list)
    review: dict[str, Any] = field(default_factory=dict)
    compiler: dict[str, Any] = field(default_factory=dict)
    attempts: list[dict[str, Any]] = field(default_factory=list)
    result: dict[str, Any] = field(default_factory=dict)


@dataclass
class PaperDocument:
    id: str
    name: str
    format: str
    text: str
    blocks: list[DocumentBlock]
    units: list[MathUnit]
    files: dict[str, str] = field(default_factory=dict)
    macros: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    edges: list[dict[str, str]] = field(default_factory=list)
    page_count: int = 0
    extraction_complete: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict) -> "PaperDocument":
        d = dict(value)
        d["blocks"] = [DocumentBlock(**{**b, "spans": [SourceSpan(**s) for s in b["spans"]]}) for b in d["blocks"]]
        d["units"] = [MathUnit(**u) for u in d["units"]]
        return cls(**d)
