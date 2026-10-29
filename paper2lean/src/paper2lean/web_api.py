from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .llm import LLMDispatcher
from .orchestrator import Paper2Lean


def _runner(payload: dict[str, Any], output_dir: str | Path, progress=None) -> Paper2Lean:
    llm = LLMDispatcher(
        url=payload.get("llm_http_url") or None,
        model=payload.get("llm_http_model") or None,
        api_key=payload.get("llm_http_key") or None,
    )
    return Paper2Lean(
        output_dir=output_dir,
        llm=llm,
        max_repair_rounds=int(payload.get("max_repair_rounds", 2)),
        max_parallel_workers=int(payload.get("max_parallel_workers", 1)),
        unit_time_budget_seconds=payload.get("unit_time_budget_seconds"),
        progress=progress,
    )


def analyze_request(payload: dict[str, Any], *, output_dir: str | Path = "paper_runs", progress=None) -> dict[str, Any]:
    path = payload.get("path") or payload.get("file")
    if not path:
        raise ValueError("path is required")
    model = _runner(payload, output_dir, progress=progress).analyze(path)
    result = model.to_dict()
    # The source is retained in extraction/raw_text.txt. Keep the web response
    # small and avoid echoing an entire paper into the result pane.
    result["text"] = ""
    result["files"] = {name: "" for name in result.get("files", {})}
    return result


def run_request(payload: dict[str, Any], *, output_dir: str | Path = "paper_runs", progress=None) -> dict[str, Any]:
    paper_id = payload.get("paper_id")
    if not paper_id:
        raise ValueError("paper_id is required")
    if not re.fullmatch(r"[a-f0-9]{16}", str(paper_id)):
        raise ValueError("Invalid paper_id")
    root = Path(output_dir) / paper_id / "inventory" / "paper.json"
    if not root.exists():
        raise FileNotFoundError(str(root))
    from .models import PaperModel
    model = PaperModel.from_dict(json.loads(root.read_text(encoding="utf-8")))
    return _runner(payload, output_dir, progress=progress).run(
        model,
        target=payload.get("target"),
        max_units=payload.get("max_units"),
        num_candidates=int(payload.get("num_candidates", 2)),
        resume=bool(payload.get("resume", True)),
        retry_failed=payload.get("retry_failed") is True,
        retry_confirmed=payload.get("retry_confirmed") is True,
    )


def status_request(payload: dict[str, Any], *, output_dir: str | Path = "paper_runs") -> dict[str, Any]:
    paper_id = payload.get("paper_id")
    if not paper_id:
        raise ValueError("paper_id is required")
    if not re.fullmatch(r"[a-f0-9]{16}", str(paper_id)):
        raise ValueError("Invalid paper_id")
    root = Path(output_dir) / paper_id
    report = root / "reports" / "report.json"
    events = root / "events.jsonl"
    recent = []
    if events.exists():
        for line in events.read_text(encoding="utf-8").splitlines():
            try:
                recent.append(json.loads(line))
            except json.JSONDecodeError:
                continue  # A concurrent append may not have finished its line.
    result = {
        "paper_id": paper_id,
        "report": json.loads(report.read_text(encoding="utf-8")) if report.exists() else None,
        "events": recent[-5:],
    }
    # A saved log cannot establish whether a process is still alive. Active
    # request status comes from the in-memory, read-only activity endpoint.
    result["running"] = None
    result["last_event"] = result["events"][-1] if result["events"] else None
    return result


def history_request(*, output_dir: str | Path = "paper_runs", limit: int = 30) -> dict[str, Any]:
    root = Path(output_dir)
    items = []
    if root.exists():
        for report in root.glob("*/reports/report.json"):
            try:
                value = json.loads(report.read_text(encoding="utf-8"))
                items.append({"paper_id": value.get("paper_id"), "coverage": value.get("coverage", 0), "units": value.get("units", []), "assembly": value.get("assembly", {})})
            except (OSError, ValueError):
                continue
    return {"items": items[-max(1, int(limit)):][::-1]}


def statistics_request(*, output_dir: str | Path = "paper_runs") -> dict[str, Any]:
    history = history_request(output_dir=output_dir, limit=1000)["items"]
    units = [unit for item in history for unit in item.get("units", [])]
    succeeded = sum(unit.get("status") == "succeeded" for unit in units)
    return {"runs": len(history), "units": len(units), "succeeded": succeeded, "failed": len(units) - succeeded, "coverage": succeeded / len(units) if units else 0.0}


def llm_check_request(payload: dict[str, Any]) -> dict[str, Any]:
    runner = _runner(payload, payload.get("output_dir") or "paper_runs")
    ok, message = runner.llm.probe()
    supplied_model = bool(payload.get("llm_http_model"))
    supplied_url = bool(payload.get("llm_http_url"))
    return {"connected": ok, "message": message, "model": runner.llm.model, "endpoint": runner.llm.url, "source": "webpage" if supplied_model or supplied_url else "environment"}
