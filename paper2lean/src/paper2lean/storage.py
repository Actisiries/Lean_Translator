from __future__ import annotations

import json
import os
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any


class ArtifactStore:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def path(self, *parts: str) -> Path:
        p = self.root.joinpath(*parts)
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    def write_json(self, name: str, value: Any) -> Path:
        p = self.path(name)
        if is_dataclass(value):
            value = asdict(value)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(json.dumps(value, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
        os.replace(tmp, p)
        return p

    def read_json(self, name: str, default: Any = None) -> Any:
        p = self.root / name
        if not p.exists():
            return default
        return json.loads(p.read_text(encoding="utf-8"))

    def write_text(self, name: str, value: str) -> Path:
        p = self.path(name)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(value, encoding="utf-8")
        os.replace(tmp, p)
        return p

    def append_jsonl(self, name: str, value: Any) -> Path:
        p = self.path(name)
        with p.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(value, ensure_ascii=False, default=str) + "\n")
        return p
