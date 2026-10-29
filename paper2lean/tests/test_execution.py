from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from paper2lean.orchestrator import Paper2Lean


class FakeLLM:
    def __init__(self):
        self.calls = []

    def chat(self, messages, temperature=0.2, max_tokens=4096):
        user = messages[-1]["content"]
        self.calls.append(user)
        if "STAGE: representation_plan" in user:
            return '{"imports": [], "objects": [], "typeclasses": []}'
        if "STAGE: declaration" in user:
            return "theorem main : 1 = 1"
        if "STAGE: proof_candidate" in user:
            return "theorem main : 1 = 1 := by rfl"
        return '{"blocks": [], "symbols": [], "links": []}'


def test_run_uses_compiler_and_writes_events(tmp_path: Path):
    source = tmp_path / "paper.tex"
    source.write_text(r"\begin{theorem}\label{main} 1=1. \end{theorem}", encoding="utf-8")
    llm = FakeLLM()
    compiler = lambda code, scratch_file=None: (True, "ok")
    runner = Paper2Lean(tmp_path / "runs", llm=llm, compiler=compiler)
    model = runner.analyze(source)
    report = runner.run(model, max_units=1)
    assert report["coverage"] == 1.0
    assert model.units[0].status == "succeeded"
    assert (tmp_path / "runs" / model.id / "events.jsonl").exists()
    assert (tmp_path / "runs" / model.id / "assembly" / "PaperFormalization" / "Main.lean").exists()


if __name__ == "__main__":
    import unittest
    unittest.main()
