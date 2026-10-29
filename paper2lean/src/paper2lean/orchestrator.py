from __future__ import annotations

import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .llm import LLMDispatcher
from .models import MathUnit, PaperModel
from .runtime import BudgetExceeded, UnitBudget
from .stages import build_dependencies, build_notation, dependency_closure, ingest, link_proofs, normalize_blocks, structure_map
from .verification import axiom_audit, compile_code, exact_axiom_audit
from .verification import correspondence_audit
from .verification import failure_fingerprint, validate_mathlib_names
from .prompts import AUDIT, PROOF_CANDIDATE, REPAIR
from .ranking import rank_candidates, score_candidate
from .storage import ArtifactStore


RETRYABLE_STATUSES = {"failed", "timed_out", "blocked"}


class Paper2Lean:
    """Run the source-preserving paper workflow and persist every stage."""
    def __init__(self, output_dir: str | Path = "paper_runs", llm: Any = None, compiler: Any = None,
                 max_repair_rounds: int = 2, max_parallel_workers: int = 1,
                 unit_time_budget_seconds: float | None = None, progress=None):
        self.output_dir = Path(output_dir)
        # The web API already constructs a stage-aware LLMDispatcher. Avoid
        # wrapping it as a low-level client (which would require `.chat`).
        self.llm = llm if isinstance(llm, LLMDispatcher) else LLMDispatcher(llm)
        self.compiler = compiler
        self.max_repair_rounds = max(0, int(max_repair_rounds))
        self.max_parallel_workers = max(1, int(max_parallel_workers))
        self.unit_time_budget_seconds = float(unit_time_budget_seconds if unit_time_budget_seconds is not None
                                              else os.environ.get("PAPER2LEAN_UNIT_BUDGET_SECONDS", "600"))
        UnitBudget(self.unit_time_budget_seconds)  # Validate before starting work.
        self.progress = progress
        self._event_lock = threading.Lock()

    def _event(self, store: ArtifactStore, stage: str, status: str, **extra: Any) -> None:
        if "backend" not in extra:
            llm_stages = {"structure_map", "normalize_blocks", "notation_ledger", "proof_linking", "dependency_plan",
                          "representation_plan", "declaration", "candidate", "fidelity_audit"}
            extra["backend"] = "LLM + local" if stage in llm_stages and self.llm.available else "local"
        event = {"time": time.time(), "stage": stage, "status": status, **extra}
        if self.progress:
            self.progress(event)
        with self._event_lock:
            store.append_jsonl("events.jsonl", event)

    def analyze(self, path: str | Path, progress=None) -> PaperModel:
        def emit(stage: str, status: str, **extra: Any) -> None:
            self._event(store, stage, status, **extra)
            if progress:
                progress(stage, status, extra)

        model = ingest(path)
        root = self.output_dir / model.id
        store = ArtifactStore(root)
        if self.progress:
            self.progress({"time": time.time(), "stage": "ingest", "status": "running", "paper_id": model.id})
        # A fresh analysis owns this paper directory. Do not mix events from
        # an earlier attempt with the current run shown in the web UI.
        events_path = root / "events.jsonl"
        if events_path.exists():
            events_path.unlink()
        emit("ingest", "succeeded", source=str(path), paper_id=model.id)
        store.write_json("source/manifest.json", {"id": model.id, "name": model.name, "format": model.format, "files": model.files, "warnings": model.warnings, "page_count": model.page_count})
        store.write_text("extraction/raw_text.txt", model.text)
        store.write_json("extraction/blocks.json", [asdict(b) for b in model.blocks])
        emit("structure_map", "running", detail="LLM classifies document blocks; deterministic source spans remain authoritative.")
        store.write_json("inventory/structure_map.json", structure_map(model, self.llm))
        emit("structure_map", "succeeded", llm_used=self.llm.available)
        emit("normalize_blocks", "running", detail="Extract exact statements from source headings; preserve mathematical text.")
        normalize_blocks(model, self.llm)
        emit("normalize_blocks", "succeeded", llm_used=False, backend="local")
        emit("notation_ledger", "running", detail="Build symbol and macro context.")
        build_notation(model, self.llm)
        emit("notation_ledger", "succeeded", llm_used=self.llm.available)
        emit("proof_linking", "running", detail="Associate source proofs with claims.")
        link_proofs(model, self.llm)
        emit("proof_linking", "succeeded")
        emit("dependency_plan", "running", detail="Build evidence-backed prerequisite graph.")
        build_dependencies(model, self.llm)
        emit("dependency_plan", "succeeded", edges=len(model.edges))
        store.write_json("inventory/proof_links.json", model.proof_links)
        # Persist the final classified blocks, including names and source spans.
        store.write_json("extraction/blocks.json", [asdict(b) for b in model.blocks])
        store.write_json("inventory/paper.json", model.to_dict())
        store.write_json("inventory/notation_ledger.json", model.notation)
        store.write_json("planning/dependency_graph.json", {"edges": model.edges, "nodes": [asdict(u) for u in model.units]})
        emit("inventory", "succeeded", units=len(model.units), blocks=len(model.blocks))
        return model

    def run(self, model: PaperModel, *, target: str | None = None, max_units: int | None = None,
            num_candidates: int = 2, resume: bool = True, retry_failed: bool = False,
            retry_confirmed: bool = False) -> dict[str, Any]:
        if retry_failed and retry_confirmed is not True:
            raise ValueError("Retrying failed units requires explicit user confirmation.")
        root = self.output_dir / model.id
        store = ArtifactStore(root)
        selected = dependency_closure(model, target)
        if retry_failed:
            selected = [u for u in selected if u.status in RETRYABLE_STATUSES]
            if not selected:
                raise ValueError("No failed or timed-out units to retry.")
        if max_units is not None:
            selected = selected[:max_units]
        for unit in selected:
            unit.selected = True
            if retry_failed:
                store.write_json(f"units/{unit.id}/history/{time.time_ns()}.json", asdict(unit))
            saved = store.read_json(f"units/{unit.id}/unit.json") if resume and not retry_failed else None
            if isinstance(saved, dict) and saved.get("status") == "succeeded" and saved.get("statement") == unit.statement and saved.get("code"):
                for key, value in saved.items():
                    if hasattr(unit, key):
                        setattr(unit, key, value)
                self._event(store, "resume", "succeeded", unit=unit.id)
                continue
            budget = UnitBudget(self.unit_time_budget_seconds)
            self._event(store, "unit", "running", unit=unit.id, label=unit.label,
                        detail=f"Time budget: {self.unit_time_budget_seconds:g}s")
            try:
                self._formalize_unit(model, unit, store, root, num_candidates, budget)
            except BudgetExceeded as exc:
                unit.status = "timed_out"
                unit.issues.append(str(exc))
                unit.compiler = {"compiled": False, "message": str(exc)}
            except Exception as exc:
                unit.status = "failed"
                unit.issues.append(str(exc))
                unit.compiler = {"compiled": False, "message": str(exc)}
            store.write_json(f"units/{unit.id}/unit.json", asdict(unit))
            self._event(store, "unit", unit.status, unit=unit.id, label=unit.label,
                        detail=unit.compiler.get("message", "")[:500])
        store.write_json("inventory/paper.json", model.to_dict())
        assembly = self._assemble(model, store)
        report = {
            "paper_id": model.id,
            "units": [{"id": u.id, "label": u.label, "status": u.status, "compiler": u.compiler, "review": u.review} for u in model.units],
            "llm_available": self.llm.available,
            "coverage": sum(u.status == "succeeded" for u in selected) / len(selected) if selected else 0.0,
            "assembly": assembly,
            "unit_time_budget_seconds": self.unit_time_budget_seconds,
            "retryable_units": [u.id for u in model.units if u.status in RETRYABLE_STATUSES],
            "counts": {"total": len(model.units), "compiled": sum(u.status == "succeeded" for u in model.units),
                       "failed": sum(u.status in RETRYABLE_STATUSES for u in model.units),
                       "pending": sum(u.status == "pending" for u in model.units)},
            "retried_units": [u.id for u in selected] if retry_failed else [],
        }
        # Retry coverage must describe the accumulated paper, not only this pass.
        if retry_failed:
            attempted = [u for u in model.units if u.status != "pending"]
            report["coverage"] = sum(u.status == "succeeded" for u in attempted) / len(attempted) if attempted else 0.0
        store.write_json("reports/report.json", report)
        store.write_text("reports/report.md", self._markdown(report))
        self._event(store, "report", "succeeded", coverage=report["coverage"])
        return report

    def _formalize_unit(self, model: PaperModel, unit: MathUnit, store: ArtifactStore,
                        root: Path, num_candidates: int, budget: UnitBudget) -> None:
        previous_failure = ({"code": unit.code, "compiler": dict(unit.compiler), "attempts": list(unit.attempts)}
                            if unit.status in RETRYABLE_STATUSES else None)
        unit.attempts = []
        unit.review = {}
        unit.code = ""
        payload = {"unit": asdict(unit), "notation": model.notation, "dependencies": [asdict(u) for u in model.units if u.id in unit.dependencies]}
        if previous_failure:
            payload["previous_failure"] = previous_failure
            payload["retry_policy"] = "Use the previous compiler errors to change the failed approach. Do not reproduce a failed candidate unchanged; preserve the source claim."
        payload["num_candidates"] = max(1, int(num_candidates))
        self._event(store, "representation_plan", "running", unit=unit.id, backend="LLM")
        plan, used = self.llm.ask("representation_plan", "You are a Lean 4 representation planner. Preserve the exact claim. Return JSON with imports and mathlib_names (a list of exact qualified Mathlib declarations you intend to use).", payload, budget=budget)
        unit.representation = plan if isinstance(plan, dict) else {}
        store.write_json(f"units/{unit.id}/representation.json", unit.representation)
        self._event(store, "representation_plan", "succeeded", unit=unit.id, llm_used=used)
        self._event(store, "mathlib_names", "running", unit=unit.id, backend="local Lean")
        name_check = validate_mathlib_names(unit.representation,
            compiler=lambda code, path: self._compile(code, path, budget=budget),
            scratch_file=root / "units" / unit.id / "MathlibNames.lean")
        unit.review["mathlib_names"] = name_check
        payload["representation"] = unit.representation
        payload["mathlib_name_check"] = name_check
        payload["name_policy"] = "Do not use names listed as missing; use confirmed names or a direct proof."
        store.write_json(f"units/{unit.id}/mathlib_names.json", name_check)
        self._event(store, "mathlib_names", name_check["status"], unit=unit.id, backend="local Lean",
                    detail=("Missing: " + ", ".join(name_check.get("missing", []))) if name_check.get("missing") else name_check["status"])
        self._event(store, "declaration", "running", unit=unit.id, detail="Generate a Lean signature from the fixed source claim.")
        declaration, used_decl = self.llm.ask("declaration", "You are an expert Lean 4 declaration writer. Output only a theorem declaration without proof.", payload, json_output=False, budget=budget)
        unit.declaration = declaration if isinstance(declaration, str) else ""
        payload["declaration"] = unit.declaration
        store.write_text(f"units/{unit.id}/declaration.lean", unit.declaration)
        self._event(store, "declaration", "succeeded", unit=unit.id, llm_used=used_decl)
        candidate_count = max(1, int(payload.get("num_candidates", 2)))
        candidates: list[dict[str, Any]] = []

        def attempt_candidate(candidate_index: int) -> dict[str, Any] | None:
            try:
                return generate_candidate(candidate_index)
            except BudgetExceeded as exc:
                self._event(store, "candidate", "timed_out", unit=unit.id, candidate=candidate_index + 1, detail=str(exc))
                return None

        def generate_candidate(candidate_index: int) -> dict[str, Any] | None:
            budget.remaining()
            strategy = ("Use the shortest confirmed Mathlib theorem." if candidate_index == 0 else "Use an independent direct tactic or construction route.")
            candidate_payload = {**payload, "candidate_index": candidate_index + 1, "strategy": strategy}
            self._event(store, "candidate", "running", unit=unit.id, candidate=candidate_index + 1, detail=strategy)
            code, used_code = self.llm.ask("proof_candidate", PROOF_CANDIDATE, candidate_payload, json_output=False, budget=budget)
            if not isinstance(code, str) or not code.strip():
                self._event(store, "candidate", "failed", unit=unit.id, candidate=candidate_index + 1, error=self.llm.last_error or "empty response")
                return None
            candidate_code = code.strip()
            self._event(store, "verification", "running", unit=unit.id, candidate=candidate_index + 1, backend="local Lean")
            ok, diagnostic = self._compile(candidate_code, root / "units" / unit.id / f"Candidate_{candidate_index}.lean", budget=budget)
            self._event(store, "verification", "succeeded" if ok else "failed", unit=unit.id, candidate=candidate_index + 1, backend="local Lean", detail=diagnostic[:500])
            store.write_text(f"units/{unit.id}/attempts/candidate_{candidate_index}.lean", candidate_code)
            attempt = {"candidate": candidate_index, "round": 0, "compiled": ok, "diagnostic": diagnostic, "llm_used": used_code}
            history = [{**attempt, "code": candidate_code}]
            history_path = f"units/{unit.id}/attempts/candidate_{candidate_index}_history.json"
            store.write_json(history_path, history)
            seen_code = {candidate_code}
            repair_round = 0
            failures = {failure_fingerprint(diagnostic)} if not ok else set()
            while not ok and repair_round < self.max_repair_rounds and self.llm.available:
                try:
                    budget.remaining()
                except BudgetExceeded:
                    break
                repair_round += 1
                repair_payload = {**candidate_payload, "current_code": candidate_code, "compiler_diagnostic": diagnostic,
                                  "repair_round": repair_round, "repair_history": list(history),
                                  "repair_policy": "Address the exact compiler errors. Do not return any previous failed code or repeat a failed approach."}
                self._event(store, "repair", "running", unit=unit.id, candidate=candidate_index + 1, backend="LLM", round=repair_round)
                try:
                    repaired, used_repair = self.llm.ask("repair", REPAIR, repair_payload, json_output=False, budget=budget)
                except BudgetExceeded:
                    break
                if not isinstance(repaired, str) or not repaired.strip() or repaired.strip() in seen_code:
                    self._event(store, "repair", "stopped", unit=unit.id, candidate=candidate_index + 1,
                                detail="Repair returned empty or previously attempted code.")
                    break
                candidate_code = repaired.strip()
                seen_code.add(candidate_code)
                ok, diagnostic = self._compile(candidate_code, root / "units" / unit.id / f"Candidate_{candidate_index}_repair_{repair_round}.lean", budget=budget)
                self._event(store, "repair", "succeeded" if ok else "retry", unit=unit.id, candidate=candidate_index + 1, round=repair_round, backend="LLM + local Lean", detail=diagnostic[:500])
                store.write_text(f"units/{unit.id}/attempts/candidate_{candidate_index}_repair_{repair_round}.lean", candidate_code)
                attempt = {"candidate": candidate_index, "round": repair_round, "compiled": ok, "diagnostic": diagnostic, "llm_used": used_repair}
                history.append({**attempt, "code": candidate_code})
                store.write_json(history_path, history)
                fingerprint = failure_fingerprint(diagnostic)
                if not ok and fingerprint in failures:
                    self._event(store, "repair", "stopped", unit=unit.id, candidate=candidate_index + 1,
                                detail="Repeated identical failure; stopping this candidate's repairs.")
                    break
                failures.add(fingerprint)
            self._event(store, "candidate", "succeeded" if ok else "failed", unit=unit.id,
                        candidate=candidate_index + 1, detail=diagnostic[:500])
            _, metadata = score_candidate(candidate_code, ok, diagnostic)
            return {"code": candidate_code, **metadata, "attempt": {**attempt, "history": history}}

        workers = min(candidate_count, self.max_parallel_workers)
        if workers == 1:
            candidates = [item for item in (attempt_candidate(i) for i in range(candidate_count)) if item]
        else:
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="paper2lean-candidate") as executor:
                futures = [executor.submit(attempt_candidate, i) for i in range(candidate_count)]
                candidates = [item for future in as_completed(futures) for item in [future.result()] if item]
        unit.attempts.extend(item.pop("attempt") for item in candidates if item.get("attempt"))
        ranked = rank_candidates(candidates)
        self._event(store, "candidate_ranking", "succeeded", unit=unit.id, candidates=len(ranked), detail="Select highest-scoring compiled candidate.")
        if ranked:
            best = ranked[0]
            unit.code = best["code"]
            ok = bool(best["compiled"])
            diagnostic = str(best.get("diagnostic", ""))
            unit.review["candidates"] = [{k: v for k, v in item.items() if k != "code"} for item in ranked]
            store.write_json(f"units/{unit.id}/candidates.json", [{k: v for k, v in item.items() if k != "code"} for item in ranked])
        else:
            code, ok, diagnostic = "", False, "No proof candidate was returned by the LLM."
            budget.remaining()
        if ranked:
            unit.compiler = {"compiled": ok, "message": diagnostic}
            if not ok:
                budget.remaining()
            unit.review["trust"] = axiom_audit(unit.code)
            if ok:
                self._event(store, "trust_audit", "running", unit=unit.id, backend="local Lean")
                unit.review["axioms"] = exact_axiom_audit(unit.code, compiler=lambda source: self._compile(source, root / "units" / unit.id / "AxiomAudit.lean", budget=budget))
            unit.review["correspondence"] = correspondence_audit(unit.statement, unit.code)
            if ok and self.llm.available:
                audit_payload = {
                    "source_statement": unit.statement,
                    "source_proof": unit.proof,
                    "lean_code": unit.code,
                    "compiler_result": unit.compiler,
                }
                self._event(store, "fidelity_audit", "running", unit=unit.id, backend="LLM")
                try:
                    audit, used_audit = self.llm.ask("fidelity_audit", AUDIT, audit_payload, budget=budget)
                except BudgetExceeded:
                    audit, used_audit = {"status": "budget_exhausted", "message": "Proof compiled; fidelity audit unfinished."}, False
                if isinstance(audit, dict):
                    unit.review["fidelity"] = audit
                    unit.review["fidelity"]["llm_used"] = used_audit
            unit.status = "succeeded" if ok else "failed"
            if not ok:
                unit.issues.append(diagnostic[:1000])
        else:
            unit.status = "failed"
            unit.compiler = {"compiled": False, "message": self.llm.last_error or diagnostic}

    def _compile(self, code: str, scratch_file: Path, *, budget: UnitBudget | None = None) -> tuple[bool, str]:
        if budget:
            budget.remaining()
        if callable(self.compiler):
            result = self.compiler(code, scratch_file=scratch_file)
            if budget:
                budget.remaining()
            return result
        return compile_code(code, scratch_file=scratch_file, budget=budget)

    def _assemble(self, model: PaperModel, store: ArtifactStore) -> dict[str, Any]:
        accepted = [u for u in model.units if u.status == "succeeded" and u.code.strip()]
        root = store.path("assembly", "PaperFormalization")
        root.mkdir(parents=True, exist_ok=True)
        imports: list[str] = []
        modules: list[str] = []
        for index, unit in enumerate(accepted, 1):
            filename = f"Result_{index}_{re.sub(r'[^A-Za-z0-9_]', '_', unit.label)[:40]}.lean"
            (root / filename).write_text(unit.code + "\n", encoding="utf-8")
            imports.append(f"import PaperFormalization.{filename[:-5]}")
            modules.append(unit.code.strip())
        (root / "Main.lean").write_text("\n\n".join(modules) + ("\n" if modules else ""), encoding="utf-8")
        main = root / "Main.lean"
        self._event(store, "assembly", "running", backend="local Lean")
        build_ok, build_message = self._compile(main.read_text(encoding="utf-8"), main) if accepted else (False, "No accepted units to assemble.")
        result = {"accepted_units": [u.id for u in accepted], "main": str(main), "status": "built" if build_ok else "build_failed", "compiled": build_ok, "message": build_message}
        store.write_json("assembly/build.json", result)
        self._event(store, "assembly", "succeeded" if build_ok else "failed", accepted=len(accepted))
        return result

    @staticmethod
    def _markdown(report: dict[str, Any]) -> str:
        lines = [f"# Paper2Lean report: {report['paper_id']}", "", f"- Coverage: {report['coverage']:.1%}", "", "## Units", ""]
        lines.extend(f"- **{u['label']}** — `{u['status']}`" for u in report["units"])
        return "\n".join(lines) + "\n"
