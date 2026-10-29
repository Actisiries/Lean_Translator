from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from paper2lean.orchestrator import Paper2Lean


def test_tex_analysis(tmp_path: Path):
    source = tmp_path / "paper.tex"
    source.write_text(r"\begin{theorem}\label{main} For all n, n = n. \end{theorem}", encoding="utf-8")
    model = Paper2Lean(tmp_path / "runs").analyze(source)
    assert model.format == "tex"
    assert model.units[0].label == "main"
    assert (tmp_path / "runs" / model.id / "inventory" / "paper.json").exists()


if __name__ == "__main__":
    import unittest
    unittest.main()
