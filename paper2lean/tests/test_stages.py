from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from paper2lean.stages import dependency_closure, ingest, normalize_blocks


def test_tex_normalization_and_dependency_order(tmp_path: Path):
    source = tmp_path / "paper.tex"
    source.write_text(
        r"""\begin{definition}\label{def:a} A definition. \end{definition}
\begin{theorem}\label{thm:a} First result. \end{theorem}
\begin{theorem}\label{thm:b} By thm:a, second result. \end{theorem}""",
        encoding="utf-8",
    )
    model = normalize_blocks(ingest(source))
    model.units[1].dependencies.append(model.units[0].id)
    ordered = dependency_closure(model, "thm:b")
    assert ordered[-1].label == "thm:b"
