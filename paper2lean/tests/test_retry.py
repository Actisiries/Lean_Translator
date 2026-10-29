import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from test_runtime import Client
from paper2lean.models import MathUnit, PaperModel
from paper2lean.orchestrator import Paper2Lean
from paper2lean.web_api import run_request


class RetryTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)

    def model(self):
        return PaperModel("a" * 16, "paper", "tex", units=[
            MathUnit("good", "theorem", "Good", "1=1", status="succeeded", code="theorem good : True := by trivial"),
            MathUnit("bad", "theorem", "Bad", "1=1", status="failed", code="old failed code",
                     compiler={"compiled": False, "message": "error: unknown old name"}),
            MathUnit("later", "theorem", "Later", "1=1")])

    def test_retry_requires_confirmation_before_any_work(self):
        client = Client()
        runner = Paper2Lean(self.root, llm=client, compiler=lambda *a, **kw: (True, "compiled"))
        with self.assertRaisesRegex(ValueError, "confirmation"):
            runner.run(self.model(), retry_failed=True)
        self.assertEqual(client.stages, [])

    def test_retry_only_failures_preserves_success_and_provides_previous_errors(self):
        client = Client()
        model = self.model()
        model.units.append(MathUnit("timeout", "theorem", "Timed", "1=1", status="timed_out"))
        good = model.units[0].code
        runner = Paper2Lean(self.root, llm=client, compiler=lambda *a, **kw: (True, "compiled"))
        report = runner.run(model, retry_failed=True, retry_confirmed=True, num_candidates=1)
        self.assertEqual(report["retried_units"], ["bad", "timeout"])
        self.assertEqual(report["retryable_units"], [])
        self.assertEqual(model.units[0].code, good)
        self.assertEqual(model.units[2].status, "pending")
        self.assertEqual(report["counts"], {"total": 4, "compiled": 3, "failed": 0, "pending": 1})
        plan = client.payloads[client.stages.index("representation_plan")]
        self.assertEqual(plan["previous_failure"]["compiler"]["message"], "error: unknown old name")
        snapshot = next((self.root / model.id / "units/bad/history").glob("*.json"))
        self.assertEqual(json.loads(snapshot.read_text())["code"], "old failed code")

    def test_repair_history_contains_each_code_and_error_and_rejects_cycle(self):
        class CyclingClient(Client):
            def chat(self, messages, **kwargs):
                response = super().chat(messages, **kwargs)
                if self.stages[-1] == "repair" and self.stages.count("repair") == 2:
                    return "theorem demo : 1 = 1 := by rfl"
                return response
        client = CyclingClient()
        calls = []
        def compiler(code, scratch_file=None):
            if "#check" in code:
                return True, "names checked"
            if scratch_file.name.startswith("Candidate_"):
                calls.append(code)
            return False, "error: second failure" if "-- changed" in code else "error: first failure"
        model = self.model()
        runner = Paper2Lean(self.root, llm=client, compiler=compiler, max_repair_rounds=5)
        runner.run(model, target="bad", num_candidates=1)
        repairs = [p for stage, p in zip(client.stages, client.payloads) if stage == "repair"]
        self.assertEqual(len(repairs), 2)
        self.assertEqual([h["diagnostic"] for h in repairs[1]["repair_history"]],
                         ["error: first failure", "error: second failure"])
        self.assertEqual(len(calls), 2)
        history = model.units[1].attempts[0]["history"]
        self.assertEqual(len(history), 2)
        saved = self.root / model.id / "units/bad/attempts/candidate_0_history.json"
        self.assertEqual(json.loads(saved.read_text()), history)

    def test_api_requires_actual_boolean_permission(self):
        model = self.model()
        path = self.root / model.id / "inventory/paper.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(model.to_dict()))
        runner = Paper2Lean(self.root, llm=Client(), compiler=lambda *a, **kw: (True, "compiled"))
        with patch("paper2lean.web_api._runner", return_value=runner):
            with self.assertRaisesRegex(ValueError, "confirmation"):
                run_request({"paper_id": model.id, "retry_failed": True, "retry_confirmed": "true"}, output_dir=self.root)
            report = run_request({"paper_id": model.id, "retry_failed": True, "retry_confirmed": True, "num_candidates": 1}, output_dir=self.root)
        self.assertEqual(report["retried_units"], ["bad"])

    def test_retry_rejects_empty_selection(self):
        model = self.model()
        model.units[1].status = "succeeded"
        runner = Paper2Lean(self.root, llm=Client())
        with self.assertRaisesRegex(ValueError, "No failed"):
            runner.run(model, retry_failed=True, retry_confirmed=True)
