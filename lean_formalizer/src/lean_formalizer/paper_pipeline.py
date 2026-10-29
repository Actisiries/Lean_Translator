"""Document-level orchestration around the legacy single-claim pipeline.

Short text continues to use :class:`FormalizationPipeline` directly. PDF/TeX
inputs use this module: analyze the paper, build a source-backed dependency
plan, execute ready theorem units in order, and persist a resumable report.
"""
from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import re
import time
from typing import Any, Callable, Optional

from .document_extract import extract_document, plan_dependencies
from .document_models import MathUnit, PaperDocument
from .latex_extract import MAX_CONTEXT_CHARS
from .models import FormalizationResult
from .pipeline import FormalizationPipeline


class PaperPipeline:
    def __init__(self, *, llm=None, memory_dir: str | Path = "./memory",
                 output_dir: str | Path = "./paper_runs", num_candidates: int = 2,
                 max_repair_rounds: int = 2, progress_callback: Optional[Callable[[dict[str, Any]], None]] = None,
                 verification_lock: Any = None, temperature: float = 0.2,
                 max_parallel_candidates: int | None = None,
                 lean_compile_workers: int | None = None,
                 lean_compile_timeout: float | None = None):
        self.llm = llm
        self.memory_dir = Path(memory_dir)
        self.output_dir = Path(output_dir)
        self.num_candidates = num_candidates
        self.max_repair_rounds = max_repair_rounds
        self.progress_callback = progress_callback
        self.verification_lock = verification_lock
        self.pipeline_options = dict(
            temperature=temperature, max_parallel_candidates=max_parallel_candidates,
            lean_compile_workers=lean_compile_workers, lean_compile_timeout=lean_compile_timeout,
        )

    @staticmethod
    def paper_id(path: str | Path) -> str:
        p = Path(path)
        digest = hashlib.sha256(p.read_bytes()).hexdigest()[:16]
        return f"paper_{digest}"

    def _root(self, doc: PaperDocument) -> Path:
        root = self.output_dir / doc.id
        root.mkdir(parents=True, exist_ok=True)
        return root

    def analyze(self, path: str | Path, *, project_root: str | Path | None = None,
                target: str | None = None, scope: str = "all-results",
                max_pages: int | None = None, source_name: str | None = None) -> PaperDocument:
        self._event("paper_ingest", "Reading source and building a source-linked inventory.")
        doc = extract_document(path, project_root=project_root, max_pages=max_pages)
        if source_name:
            doc.name = source_name
        plan_dependencies(doc, target=target, scope=scope)
        root = self._root(doc)
        self._write_json(root / "source_manifest.json", {
            "paper_id": doc.id, "name": doc.name, "format": doc.format,
            "files": doc.files, "warnings": doc.warnings,
            "page_count": doc.page_count, "extraction_complete": doc.extraction_complete,
        })
        self._write_json(root / "claim_inventory.json", doc.to_dict())
        self._write_json(root / "dependency_graph.json", {"nodes": [asdict(u) for u in doc.units], "edges": doc.edges})
        return doc

    def load(self, paper_id: str) -> PaperDocument:
        path = self.output_dir / paper_id / "claim_inventory.json"
        if not path.exists():
            raise FileNotFoundError(f"Paper plan not found: {paper_id}")
        return PaperDocument.from_dict(json.loads(path.read_text(encoding="utf-8")))

    def run(self, doc: PaperDocument, *, target: str | None = None,
            scope: str = "all-results", deadline_seconds: float | None = None,
            resume: bool = True, save_to_memory: bool = True,
            target_statement: str | None = None, domain: str = "general") -> dict[str, Any]:
        root = self._root(doc)
        if target or not any(u.selected for u in doc.units):
            plan_dependencies(doc, target=target, scope=scope)
        started = time.monotonic()
        deadline = started + deadline_seconds if deadline_seconds else None
        by_id = {u.id: u for u in doc.units}
        selected_theorems = [u for u in doc.units if u.selected and u.kind == "theorem"]
        if not selected_theorems:
            raise ValueError("No theorem targets were identified in this paper. Provide TeX with theorem environments or a PDF with visible theorem/lemma headings.")
        if target_statement and len(selected_theorems) != 1:
            raise ValueError("A target statement requires exactly one selected theorem. Select a theorem label or clear the target statement for a whole-paper run.")
        # Load completed work once, before any save can overwrite sibling
        # artifacts. Failures and exhausted budgets are retried on a new run.
        for unit in doc.units:
            if not unit.selected:
                continue
            unit.status = "pending"
            unit.message = ""
            if resume and not target_statement:
                self._load_unit_artifact(root, unit)
        terminal = {"succeeded", "skipped", "blocked", "failed", "budget_exhausted"}
        changed = True
        while changed:
            changed = False
            for unit in doc.units:
                if not unit.selected or unit.status in terminal:
                    continue
                if deadline and time.monotonic() >= deadline:
                    unit.status = "budget_exhausted"
                    unit.message = "Paper deadline reached before this unit ran."
                    changed = True
                    continue
                deps = [by_id[d] for d in unit.dependencies if d in by_id]
                if any(d.status not in {"succeeded", "skipped"} for d in deps):
                    if any(d.status in {"failed", "blocked"} for d in deps):
                        unit.status = "blocked"
                        unit.message = "Blocked by an unsuccessful dependency: " + ", ".join(d.label for d in deps if d.status not in {"succeeded", "skipped"})
                        changed = True
                    continue
                if unit.kind == "definition":
                    # Definitions are included in theorem context first. They
                    # become formal units when a target statement is selected.
                    unit.status = "skipped"
                    unit.message = "Definition retained as source context; no standalone proof target requested."
                    changed = True
                    continue
                unit.status = "running"
                self._save(doc, root)
                self._event("paper_unit_start", f"Formalizing {unit.label} ({unit.id}).", unit=unit.id)
                context = self._context(doc, unit, by_id)
                if len(context) > MAX_CONTEXT_CHARS:
                    unit.status = "blocked"
                    unit.message = f"Required context is {len(context):,} characters; split the target or select fewer dependencies."
                    changed = True
                    self._save(doc, root)
                    continue
                pipeline = None
                try:
                    pipeline = FormalizationPipeline(
                        llm=self.llm, memory_dir=self.memory_dir,
                        num_candidates=self.num_candidates,
                        max_repair_rounds=self.max_repair_rounds,
                        require_exact_target=True,
                        verification_lock=self.verification_lock,
                        progress_callback=(lambda event, unit_id=unit.id: self.progress_callback(dict(event, unit=unit_id))) if self.progress_callback else None,
                        **self.pipeline_options,
                    )
                    result = pipeline.formalize(
                        text=context, target_statement=target_statement or unit.statement,
                        domain=domain, save_to_memory=save_to_memory,
                        deadline_monotonic=deadline,
                    )
                    self._record(unit, result)
                except Exception as exc:
                    unit.status = "failed"
                    unit.message = str(exc)
                    unit.result = {
                        "success": False, "message": str(exc),
                        "error_type": type(exc).__name__,
                        "stage_trace": list(getattr(pipeline, "_stage_trace", [])),
                        "token_usage": self.llm.usage.to_dict() if getattr(self.llm, "usage", None) else {},
                    }
                self._event("paper_unit_end", unit.message, unit=unit.id, status=unit.status, ok=unit.status == "succeeded")
                self._save(doc, root)
                changed = True
        for unit in doc.units:
            if unit.selected and unit.status not in terminal:
                unit.status = "blocked"
                unit.message = "Unresolved dependency cycle or missing prerequisite: " + ", ".join(unit.dependencies)
        self._save(doc, root)
        report = self.report(doc, started=started, deadline_seconds=deadline_seconds)
        self._write_json(root / "report.json", report)
        (root / "report.md").write_text(self._markdown(report), encoding="utf-8")
        self._assemble(doc, root)
        return report

    def _context(self, doc: PaperDocument, unit: MathUnit, by_id: dict[str, MathUnit]) -> str:
        pieces = ["PAPER CONTEXT (source-backed; do not change the target):"]
        if doc.macros:
            pieces.append("Macro and theorem-environment definitions:\n" + "\n".join(doc.macros))
        pieces.append("Selected theorem (formalize this exact claim):\n" + unit.statement)
        if unit.unresolved:
            pieces.append("Unresolved references (not verified prerequisites; do not invent their contents):\n" + "\n".join(unit.unresolved))
        for dep_id in unit.dependencies:
            dep = by_id[dep_id]
            if dep.code:
                pieces.append(f"Verified dependency {dep.label}:\n{dep.code}")
            else:
                pieces.append(f"Required source dependency {dep.label}:\n{dep.statement}")
        for block_id in unit.context:
            block = next((b for b in doc.blocks if b.id == block_id), None)
            if block and block.text.strip():
                pieces.append(f"Source context ({block.kind}):\n{block.text}")
        pieces.append("Source proof for this target:\n" + (unit.proof or "(not provided)"))
        return "\n\n".join(pieces)

    def _record(self, unit: MathUnit, result: FormalizationResult) -> None:
        unit.result = result.to_dict()
        unit.compiler = result.verification
        unit.review = {
            "statement": "unchecked",
            "proof": (result.proof_audit.to_dict() if hasattr(result.proof_audit, "to_dict") else asdict(result.proof_audit)) if result.proof_audit else {},
        }
        unit.attempts.append({
            "timestamp": result.timestamp, "success": result.success,
            "message": result.message, "token_usage": result.token_usage,
            "selected_candidate": result.selected_candidate,
        })
        if result.success and result.best_code.strip():
            unit.status = "succeeded"
            unit.code = result.best_code
            unit.signature = self._signature(result.best_code)
            unit.message = result.message
        else:
            unit.status = "failed"
            unit.message = result.message or "No verified Lean result."

    @staticmethod
    def _signature(code: str) -> str:
        lines = []
        for line in code.splitlines():
            if ":=" in line:
                lines.append(line.split(":=", 1)[0].rstrip())
            elif re.match(r"\s*(theorem|lemma|def|structure|class|inductive)\b", line):
                lines.append(line.rstrip())
        return "\n".join(lines)

    def _load_unit_artifact(self, root: Path, unit: MathUnit) -> bool:
        path = root / "units" / f"{unit.id}.json"
        if not path.exists():
            return False
        try:
            saved = json.loads(path.read_text(encoding="utf-8"))
            if (saved.get("status") in {"succeeded", "skipped"}
                    and saved.get("statement") == unit.statement
                    and saved.get("proof") == unit.proof):
                # Never restore selection/dependency planning from an older scope.
                for key in ("status", "message", "code", "signature", "compiler", "review", "attempts", "result"):
                    if key in saved:
                        setattr(unit, key, saved[key])
                return True
        except (OSError, ValueError):
            return False
        return False

    def _save(self, doc: PaperDocument, root: Path) -> None:
        self._write_json(root / "claim_inventory.json", doc.to_dict())
        units = root / "units"
        units.mkdir(exist_ok=True)
        for unit in doc.units:
            if unit.selected:
                self._write_json(units / f"{unit.id}.json", asdict(unit))
        self._write_json(root / "dependency_graph.json", {"nodes": [asdict(u) for u in doc.units], "edges": doc.edges})

    def report(self, doc: PaperDocument, *, started: float | None = None, deadline_seconds: float | None = None) -> dict[str, Any]:
        selected = [u for u in doc.units if u.selected]
        theorem_selected = [u for u in selected if u.kind == "theorem"]
        theorem_succeeded = sum(u.status == "succeeded" for u in theorem_selected)
        success = bool(theorem_selected) and theorem_succeeded == len(theorem_selected)
        failures = [f"{u.label}: {u.message}" for u in theorem_selected if u.status != "succeeded"]
        message = f"{theorem_succeeded}/{len(theorem_selected)} selected theorems compiled."
        if failures:
            message += " " + " | ".join(failures)
        results = [u.result for u in selected if u.result]
        usage = {field: sum(int(r.get('token_usage', {}).get(field, 0) or 0) for r in results)
                 for field in ('prompt_tokens', 'completion_tokens', 'total_tokens', 'calls')}
        usage['reported'] = any(r.get('token_usage', {}).get('reported', False) for r in results)
        trace = [dict(entry, unit=u.id) for u in selected for entry in u.result.get("stage_trace", [])]
        # The UI shows one unit at a time through its existing code panel. The
        # full report retains every unit's candidates/code and compiler result.
        displayed = next((u for u in theorem_selected if u.status == "succeeded"),
                         next((u for u in theorem_selected if u.result), None))
        displayed_result = displayed.result if displayed else {}
        return {
            "success": success, "message": message,
            "artifacts": str((self.output_dir / doc.id).resolve()),
            "best_code": displayed_result.get("best_code", ""),
            "displayed_unit": displayed.id if displayed else "",
            "candidates": displayed_result.get("candidates", []),
            "structured": displayed_result.get("structured"),
            "proof_audit": displayed_result.get("proof_audit"),
            "stage_trace": trace,
            "token_usage": usage,
            "quota_exceeded": any(r.get("quota_exceeded") for r in results),
            "timed_out": any(u.status == "budget_exhausted" for u in selected),
            "verification": {
                "compiled": success, "summary": message,
                "raw_message": "\n".join(f"{u.label}: {u.compiler.get('raw_message') or u.message}" for u in theorem_selected),
            },
            "source": {"name": doc.name, "format": doc.format, "warnings": doc.warnings},
            "paper_id": doc.id, "name": doc.name, "format": doc.format,
            "page_count": doc.page_count, "extraction_complete": doc.extraction_complete,
            "warnings": doc.warnings, "units_total": len(doc.units),
            "units_selected": len(selected),
            "theorem_units_selected": len(theorem_selected),
            "status_counts": {s: sum(u.status == s for u in doc.units) for s in sorted({u.status for u in doc.units})},
            "selected_units": [{"id": u.id, "label": u.label, "kind": u.kind, "status": u.status,
                                "dependencies": u.dependencies, "message": u.message,
                                "unresolved": u.unresolved,
                                "source_block": u.block_id, "compiler": u.compiler,
                                "review": u.review, "code": u.code,
                                "result": u.result} for u in selected],
            "elapsed_seconds": round(max(0.0, time.monotonic() - (started or time.monotonic())), 3),
            "deadline_seconds": deadline_seconds,
            "coverage": (theorem_succeeded / len(theorem_selected) if theorem_selected else 0.0),
        }

    def _assemble(self, doc: PaperDocument, root: Path) -> None:
        accepted = [u for u in doc.units if u.status == "succeeded" and u.code.strip()]
        project = root / "PaperFormalization"
        project.mkdir(exist_ok=True)
        for old in project.glob("Result_*.lean"):
            old.unlink()
        imports = []
        for index, unit in enumerate(accepted, 1):
            name = f"Result_{index}_{re.sub(r'[^A-Za-z0-9_]', '_', unit.label)[:48]}"
            (project / f"{name}.lean").write_text(unit.code, encoding="utf-8")
            imports.append(f"import PaperFormalization.{name}")
        (project / "Main.lean").write_text("\n".join(imports) + ("\n" if imports else ""), encoding="utf-8")

    @staticmethod
    def _write_json(path: Path, value: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(value, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)

    def _event(self, stage: str, message: str, **extra: Any) -> None:
        if self.progress_callback:
            self.progress_callback({"stage": stage, "ok": True, "message": message, **extra})

    @staticmethod
    def _markdown(report: dict[str, Any]) -> str:
        lines = [f"# Paper formalization report: {report['name']}", "", f"- Format: `{report['format']}`", f"- Selected units: {report['units_selected']}", f"- Coverage: {report['coverage']:.1%}", "", "## Units", ""]
        for unit in report["selected_units"]:
            lines.append(f"- **{unit['label']}** — `{unit['status']}` — {unit['message']}")
        if report["warnings"]:
            lines += ["", "## Warnings", ""] + [f"- {w}" for w in report["warnings"]]
        return "\n".join(lines) + "\n"
