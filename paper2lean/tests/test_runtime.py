import io
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from paper2lean.llm import LLMDispatcher
from paper2lean.orchestrator import Paper2Lean
from paper2lean.runtime import ActivityRegistry, BudgetExceeded, UnitBudget
from paper2lean.verification import audit_code, compile_code, failure_fingerprint, validate_mathlib_names
from paper2lean import webapp


class Client:
    def __init__(self, mode="ok"):
        self.mode = mode
        self.stages = []
        self.payloads = []

    def chat(self, messages, **kwargs):
        content = messages[-1]["content"]
        stage = content.split("STAGE: ")[1].splitlines()[0]
        self.stages.append(stage)
        self.payloads.append(json.loads(content.split("INPUT:\n")[1]))
        if stage == "representation_plan":
            if self.mode == "slow":
                time.sleep(.025)
            return '{"imports": ["Mathlib"], "mathlib_names": ["Nat.missing"]}'
        if stage == "declaration":
            return "theorem demo : 1 = 1"
        if stage in {"proof_candidate", "repair"}:
            return "theorem demo : 1 = 1 := by rfl" + ("\n-- changed" if stage == "repair" else "")
        return "{}"


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)

    def runner(self, client, compiler, **options):
        source = self.root / "paper.tex"
        source.write_text(r"\begin{theorem}\label{one} 1=1.\end{theorem}" +
                          r"\begin{theorem}\label{two} 2=2.\end{theorem}", encoding="utf-8")
        runner = Paper2Lean(self.root / "runs", llm=client, compiler=compiler, **options)
        return runner, runner.analyze(source)

    def test_repeated_diagnostic_stops_repair(self):
        client = Client()
        def compiler(code, scratch_file=None):
            if "#check" in code:
                return False, "error: Unknown constant `Nat.missing`"
            return False, f"{scratch_file}:9:2: error: Unknown constant `Nat.missing`"
        runner, model = self.runner(client, compiler, max_repair_rounds=8)
        report = runner.run(model, target=model.units[0].id, num_candidates=1)
        self.assertEqual(client.stages.count("repair"), 1)
        self.assertEqual(report["units"][0]["status"], "failed")
        declaration_payload = client.payloads[client.stages.index("declaration")]
        self.assertEqual(declaration_payload["mathlib_name_check"]["missing"], ["Nat.missing"])

    def test_budget_saves_timeout_and_continues(self):
        runner, model = self.runner(Client("slow"), lambda code, scratch_file=None: (True, "compiled"), unit_time_budget_seconds=.005)
        report = runner.run(model, num_candidates=2)
        self.assertEqual([item["status"] for item in report["units"]], ["timed_out", "timed_out"])
        self.assertTrue((self.root / "runs" / model.id / "reports/report.json").exists())

    def test_parallel_candidates_share_budget(self):
        runner, model = self.runner(Client(), lambda code, scratch_file=None: (True, "compiled"), max_parallel_workers=2)
        report = runner.run(model, target=model.units[0].id, num_candidates=2)
        self.assertEqual(report["coverage"], 1)
        self.assertEqual(len(model.units[0].attempts), 2)

    def test_legitimate_question_mark_tactics(self):
        for tactic in ("exact?", "apply?", "simp?"):
            self.assertTrue(audit_code(f"theorem x : True := by {tactic}")["trusted"])
        for hole in ("?_", "?m.123", "..."):
            self.assertFalse(audit_code(f"theorem x : True := {hole}")["trusted"])

    def test_failure_fingerprint_ignores_paths(self):
        self.assertEqual(failure_fingerprint("C:/one.lean:1:2: error: bad"),
                         failure_fingerprint("C:/two.lean:9:3: error: bad"))

    def test_missing_compiler_not_reported_as_missing_name(self):
        check = validate_mathlib_names({"mathlib_names": ["Nat.add_comm"]},
            compiler=lambda source, path: (False, "Lean compiler unavailable"), scratch_file=self.root / "check.lean")
        self.assertEqual(check["status"], "unavailable")
        self.assertEqual(check["missing"], [])

    def test_compiler_decodes_utf8_independently_of_windows_locale(self):
        with patch.dict("os.environ", {"LEAN_PROJECT": "", "LEAN_PATH": sys.executable}):
            ok, output = compile_code("import sys\nsys.stdout.buffer.write(bytes([226, 132, 149]))",
                                      scratch_file=self.root / "unicode.lean")
        self.assertTrue(ok)
        self.assertEqual(output, "\u2115")

    def test_compiler_timeout_kills_windows_process_tree(self):
        import subprocess
        process = unittest.mock.MagicMock()
        process.pid = 1234
        process.communicate.side_effect = [subprocess.TimeoutExpired("lean", 1), ("", "")]
        with patch("paper2lean.verification.subprocess.Popen", return_value=process), patch("paper2lean.verification.subprocess.run") as kill:
            ok, output = compile_code("theorem demo : True := by trivial", scratch_file=self.root / "timeout.lean", timeout=1)
        self.assertFalse(ok)
        self.assertIn("timed out", output)
        if sys.platform == "win32":
            self.assertEqual(kill.call_args.args[0], ["taskkill", "/PID", "1234", "/T", "/F"])

    def test_activity_is_bounded_and_conflicts_are_rejected(self):
        registry = ActivityRegistry()
        registry.start("a", "analysis")
        registry.claim("a", "paper")
        registry.start("b", "formalization")
        with self.assertRaises(ValueError):
            registry.claim("b", "paper")
        for n in range(10):
            registry.event("a", {"n": n})
        self.assertEqual([e["n"] for e in registry.snapshot("a")["events"]], list(range(5, 10)))
        self.assertTrue(registry.snapshot("a")["running"])
        registry.finish("a")
        registry.claim("b", "paper")
        self.assertFalse(registry.snapshot("a")["running"])

    def test_failed_activity_releases_paper_and_preserves_reason(self):
        registry = ActivityRegistry()
        registry.start("failed", "analysis")
        registry.claim("failed", "paper")
        registry.finish("failed", "Endpoint disconnected")
        state = registry.snapshot("failed")
        self.assertEqual(state["status"], "failed")
        self.assertEqual(state["error"], "Endpoint disconnected")
        self.assertFalse(state["running"])
        registry.start("retry", "analysis")
        registry.claim("retry", "paper")

    def test_http_failure_always_returns_pair(self):
        dispatcher = LLMDispatcher(url="http://test/v1")
        error = urllib.error.HTTPError(dispatcher.url, 524, "timeout", {}, io.BytesIO(b"upstream timeout"))
        with patch.dict("os.environ", {"PAPER2LEAN_LLM_RETRIES": "1"}), patch("urllib.request.urlopen", side_effect=error):
            value, used = dispatcher.ask("proof_candidate", "Lean", {}, json_output=False)
        self.assertEqual(value, "")
        self.assertTrue(used)
        self.assertIn("524", dispatcher.last_error)

    def test_http_budget_caps_timeout_and_retries(self):
        dispatcher = LLMDispatcher(url="http://test/v1")
        seen = []
        clock = [0.0]
        def stalled(request, timeout):
            seen.append(timeout)
            clock[0] += timeout + .005
            raise TimeoutError("timeout")
        with patch("urllib.request.urlopen", side_effect=stalled), patch("paper2lean.runtime.time.monotonic", side_effect=lambda: clock[0]):
            with self.assertRaises(BudgetExceeded):
                dispatcher.ask("declaration", "Lean", {}, budget=UnitBudget(.02))
        self.assertEqual(len(seen), 1)
        self.assertLessEqual(seen[0], .021)

    def test_expired_budget_never_calls_endpoint(self):
        with patch("paper2lean.runtime.time.monotonic", return_value=0):
            budget = UnitBudget(1)
        with patch("urllib.request.urlopen") as endpoint:
            with patch("paper2lean.runtime.time.monotonic", return_value=2):
                with self.assertRaises(BudgetExceeded):
                    LLMDispatcher(url="http://test/v1").ask("declaration", "Lean", {}, budget=budget)
            endpoint.assert_not_called()

    def test_monitor_is_read_only_while_request_is_synchronous(self):
        entered, release = threading.Event(), threading.Event()
        def analyze(payload, *, output_dir, progress):
            progress({"time": time.time(), "stage": "structure_map", "status": "running"})
            entered.set()
            release.wait(5)
            return {"id": "a" * 16, "units": [], "text": ""}
        server = ThreadingHTTPServer(("127.0.0.1", 0), webapp.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        url = f"http://127.0.0.1:{server.server_port}"
        result = []
        def send():
            req = urllib.request.Request(url + "/api/analyze", data=json.dumps({"request_id": "test-monitor"}).encode(), headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req) as response:
                result.append((response.status, json.load(response)))
        try:
            with patch.object(webapp, "ACTIVITY", ActivityRegistry()), patch.object(webapp, "analyze_request", analyze):
                worker = threading.Thread(target=send)
                worker.start()
                self.assertTrue(entered.wait(2))
                with urllib.request.urlopen(url + "/api/activity?request_id=test-monitor") as response:
                    state = json.load(response)
                self.assertTrue(state["running"])
                self.assertEqual(result, [])
                release.set()
                worker.join(3)
                self.assertEqual(result[0][0], 200)
                self.assertEqual(webapp.ACTIVITY.snapshot("test-monitor")["status"], "completed")
        finally:
            release.set()
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
