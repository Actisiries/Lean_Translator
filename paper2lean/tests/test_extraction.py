"""Regressions for source names, statement fidelity and exclusive proof ownership."""
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from paper2lean.extraction import mask_comments
from paper2lean.llm import LLMDispatcher
from paper2lean.models import PaperModel
from paper2lean.orchestrator import Paper2Lean
from paper2lean.stages import (ingest, inventory, link_proofs, normalize_blocks,
                              structure_map, _pdf_blocks)
from paper2lean.web_api import analyze_request


class ExtractionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def paper(self, text, name="paper.tex"):
        path = self.root / name
        path.write_text(text, encoding="utf-8")
        return ingest(path)

    def test_shared_counters_include_nonclaim_environments(self):
        m = self.paper(r"""\newtheorem{lem}{Lemma}[section]
\newtheorem{thm}[lem]{Theorem}
\newtheorem{note}[lem]{Remark}
\newtheorem{problem}[lem]{Problem}
\newtheorem*{fact}{Proposition}
\section{One}
\begin{thm}[Main {\cite[Theorem 1]{book}}]\label{old:7.3}A.\end{thm}
\begin{note}Note.\end{note}\begin{problem}Question.\end{problem}
\begin{lem}\label{stale}B.\end{lem}
\begin{fact}[Unnumbered]C.\end{fact}
\section*{Interlude}\begin{lem}D.\end{lem}
\section{Two}\begin{thm}E.\end{thm}""")
        self.assertEqual([u.number for u in m.units], ["1.1", "1.4", "", "1.5", "2.1"])
        self.assertEqual(m.units[0].display_name, r"Theorem 1.1 (Main {\cite[Theorem 1]{book}})")
        self.assertEqual(m.units[0].statement, "A.")
        self.assertEqual(m.units[0].label, "old:7.3")
        self.assertEqual(PaperModel.from_dict(m.to_dict()).to_dict(), m.to_dict())

    def test_subsections_and_counter_changes(self):
        m = self.paper(r"""\newtheorem{lemma}{Lemma}[subsection]
\setcounter{section}{2}\section{Three}\subsection{First}
\begin{lemma}A\end{lemma}\addtocounter{lemma}{2}\begin{lemma}B\end{lemma}
\subsection{Second}\begin{lemma}C\end{lemma}""")
        self.assertEqual([u.number for u in m.units], ["3.1.1", "3.1.4", "3.2.1"])

    def test_chained_shared_counters_and_chapters(self):
        m = self.paper(r"""\newtheorem{lem}{Lemma}[section]\newtheorem{thm}[lem]{Theorem}
\newtheorem{cor}[thm]{Corollary}\chapter{One}\section{Setup}
\begin{lem}A\end{lem}\begin{thm}B\end{thm}\begin{cor}C\end{cor}
\chapter{Two}\section{Setup}\begin{cor}D\end{cor}""")
        self.assertEqual([u.number for u in m.units], ["1.1.1", "1.1.2", "1.1.3", "2.1.1"])

    def test_custom_numbering_is_reported_instead_of_invented(self):
        m = self.paper(r"\newtheorem{lemma}{Lemma}[section]\renewcommand{\thelemma}{\Alph{lemma}}\section{A}\begin{lemma}A\end{lemma}")
        self.assertEqual(m.units[0].number, "")
        self.assertTrue(any("Custom numbering" in w for w in m.warnings))

    def test_comments_literals_and_macro_bodies_do_not_create_claims(self):
        m = self.paper(r"""% \newtheorem{theorem}{Lemma}
\newcommand{\fake}{\begin{theorem}Fake macro\end{theorem}}
\begin{verbatim}% \begin{theorem}Fake literal\end{theorem}\end{verbatim}
\verb|\begin{theorem}Inline fake\end{theorem}|
\begin{theorem}\label{real}A \% literal. % comment \end{theorem}
\begin{align}x &= x.\end{align}\end{theorem}""")
        self.assertEqual(len(m.units), 1)
        self.assertEqual(m.units[0].display_name, "Theorem 1")
        self.assertIn(r"\begin{align}x &= x.\end{align}", m.units[0].statement)
        self.assertNotIn("comment", m.units[0].statement)
        self.assertIn(r"\% literal", m.units[0].statement)
        self.assertTrue(m.extraction_complete)
        self.assertEqual(mask_comments("a \\% b % c\n"), "a \\% b    \n")

    def test_normalization_cannot_rewrite_mathematics(self):
        m = self.paper(r"\begin{theorem}[Name]\label{x}For all $x$, $x=x$.\end{theorem}")
        class Rewrite:
            available = True
            def ask(self, *args):
                raise AssertionError("Statement normalization must not request model rewrites")
        normalize_blocks(m, Rewrite())
        self.assertEqual(m.units[0].statement, "For all $x$, $x=x$.")

    def test_structure_model_cannot_reclassify_source_claims(self):
        m = self.paper(r"\begin{theorem}A\end{theorem}\begin{proof}Proof A\end{proof}")
        class Rewrite:
            available = True
            def ask(self, *args):
                return {"blocks": [{"block_id": b.id, "kind": "prose"} for b in m.blocks]}, True
        structure_map(m, Rewrite())
        link_proofs(m)
        self.assertEqual(len(m.units), 1)
        self.assertIn("Proof A", m.units[0].proof)

    def test_advisory_pdf_classification_refreshes_inventory(self):
        m = PaperModel("paper", "p.pdf", "pdf", text="A result.", blocks=_pdf_blocks("p.pdf", "A result."))
        class Classify:
            available = True
            def ask(self, *args):
                return {"blocks": [{"block_id": m.blocks[0].id, "kind": "theorem"}]}, True
        structure_map(m, Classify())
        self.assertEqual(len(m.units), 1)
        self.assertEqual(m.units[0].statement, "A result.")

    def test_delayed_titled_proof_and_adjacent_proof_have_distinct_owners(self):
        m = self.paper(r"""\begin{theorem}\label{a}A\end{theorem}
\begin{theorem}\label{b}B\end{theorem}\begin{proof}B proof\end{proof}
\begin{proof}[
Proof of Theorem~\ref{a} {\cite[2]{book}}]
A proof using Theorem~\ref{b}.\end{proof}""")
        link_proofs(m)
        self.assertIn("A proof", m.units[0].proof)
        self.assertIn("B proof", m.units[1].proof)
        self.assertNotEqual(m.units[0].proof_block_ids, m.units[1].proof_block_ids)
        self.assertEqual(m.units[0].proof_evidence, ["explicit proof title"])

    def test_unnamed_proof_not_attached_across_claims_sections_or_prose(self):
        m = self.paper(r"""\begin{theorem}A\end{theorem}\begin{theorem}B\end{theorem}
\begin{proof}B proof\end{proof}
\begin{theorem}C\end{theorem}\section{Other}\begin{proof}Unrelated\end{proof}
\begin{theorem}D\end{theorem}A substantial discussion.\begin{proof}Another proof\end{proof}""")
        link_proofs(m)
        self.assertEqual([bool(u.proof) for u in m.units], [False, True, False, False])

    def test_numbered_titles_and_unknown_explicit_owner(self):
        m = self.paper(r"""\newtheorem{theorem}{Theorem}[section]\section{One}
\begin{theorem}A\end{theorem}\begin{theorem}B\end{theorem}
\begin{proof}[Proof of Theorem 1.1.]A proof\end{proof}
\begin{theorem}C\end{theorem}\begin{proof}[Proof of Theorem~\ref{unknown}]Unknown\end{proof}""")
        link_proofs(m)
        self.assertIn("A proof", m.units[0].proof)
        self.assertFalse(m.units[1].proof)
        self.assertFalse(m.units[2].proof)

    def test_model_cannot_steal_or_clear_owned_proofs(self):
        m = self.paper(r"\begin{theorem}A\end{theorem}\begin{theorem}B\end{theorem}\begin{proof}B proof\end{proof}")
        bid = next(b.id for b in m.blocks if b.kind == "proof")
        class Duplicate:
            available = True
            def ask(self, *args):
                return {"links": [{"claim_id": m.units[0].id, "proof_block_ids": [bid], "evidence": ["model"]},
                                  {"claim_id": m.units[1].id, "proof_block_ids": []}]}, True
        link_proofs(m, Duplicate())
        self.assertFalse(m.units[0].proof)
        self.assertIn("B proof", m.units[1].proof)

    def test_model_only_ownership_remains_ambiguous(self):
        m = self.paper(r"\begin{theorem}A\end{theorem}\begin{theorem}B\end{theorem}\section{New}\begin{proof}Unknown\end{proof}")
        bid = next(b.id for b in m.blocks if b.kind == "proof")
        class Duplicate:
            available = True
            def ask(self, *args):
                return {"links": [{"claim_id": u.id, "proof_block_ids": [bid], "evidence": ["guess"]} for u in m.units]}, True
        link_proofs(m, Duplicate())
        self.assertTrue(all(u.proof_status == "ambiguous" and not u.proof for u in m.units))

    def test_missing_includes_and_unclosed_environments_are_flagged(self):
        for text in (r"\input{missing}\begin{theorem}A\end{theorem}", r"\begin{theorem}Unclosed"):
            m = self.paper(text)
            self.assertFalse(m.extraction_complete)
            self.assertTrue(m.warnings)

    def test_pdf_does_not_swallow_first_statement_word_and_preserves_pages(self):
        blocks = _pdf_blocks("p.pdf", "Theorem A (Main). A statement\nProof. First line\f\nContinued. ∎\nLemma 2.1. More.")
        m = inventory(PaperModel("paper", "p.pdf", "pdf", blocks=blocks))
        link_proofs(m)
        self.assertEqual(m.units[0].display_name, "Theorem A (Main)")
        self.assertEqual(m.units[0].statement, "A statement")
        self.assertEqual(m.units[1].statement, "More.")
        proof = next(b for b in blocks if b.kind == "proof")
        self.assertEqual([s.page for s in proof.spans], [1, 2])
        self.assertEqual(proof.spans[1].start_line, 1)  # Includes the page's leading blank line.
        self.assertEqual(proof.spans[1].end_line, 2)
        self.assertIn("Continued.", m.units[0].proof)
        self.assertEqual(_pdf_blocks("p.pdf", "Theorem applies to every field.")[0].kind, "prose")

    def test_pdf_blank_page_is_not_silently_complete(self):
        module = types.ModuleType("pdfplumber")
        pages = [types.SimpleNamespace(extract_text=lambda: "Theorem 1. A.\nProof. Done. ∎"),
                 types.SimpleNamespace(extract_text=lambda: "")]
        class PDF:
            def __enter__(self):
                return types.SimpleNamespace(pages=pages)
            def __exit__(self, *args):
                pass
        module.open = lambda path: PDF()
        path = self.root / "p.pdf"
        path.touch()
        with patch.dict(sys.modules, {"pdfplumber": module}):
            m = ingest(path)
        self.assertFalse(m.extraction_complete)
        self.assertEqual(m.page_count, 2)
        self.assertTrue(any("OCR" in w for w in m.warnings))

    def test_pdf_section_separates_claim_from_unnamed_proof(self):
        blocks = _pdf_blocks("p.pdf", "Theorem 1. A statement.\n2 Another section\nProof. Unrelated.")
        m = inventory(PaperModel("paper", "p.pdf", "pdf", blocks=blocks))
        link_proofs(m)
        self.assertEqual(m.units[0].statement, "A statement.")
        self.assertFalse(m.units[0].proof)

    def test_pdf_explicit_delayed_proof_title(self):
        blocks = _pdf_blocks("p.pdf", "Theorem 1. A.\nLemma 2. B.\nProof. B proof. ∎\nProof of Theorem 1.\nA proof. ∎")
        m = inventory(PaperModel("paper", "p.pdf", "pdf", blocks=blocks))
        link_proofs(m)
        self.assertIn("A proof", m.units[0].proof)
        self.assertIn("B proof", m.units[1].proof)

    def test_same_source_uploaded_under_different_names_has_stable_ids(self):
        text = r"\begin{theorem}A\end{theorem}\begin{proof}Done\end{proof}"
        a, b = self.paper(text), self.paper(text, "upload-uuid.tex")
        self.assertEqual([u.id for u in a.units], [u.id for u in b.units])

    def test_lxcpp_api_and_persisted_proofs(self):
        source = Path(__file__).resolve().parents[2] / "paper_runs/515994671117f606/LX-CPP.tex"
        runner = Paper2Lean(self.root / "runs", llm=LLMDispatcher(url="", api_key=""))
        with patch("paper2lean.web_api._runner", return_value=runner):
            result = analyze_request({"path": str(source)})
        self.assertEqual([u["number"] for u in result["units"]], ["1.1", "2.1", "2.2", "2.3", "2.4", "2.5", "3.1", "3.2", "3.4", "3.5", "4.1", "4.2"])
        units = {u["label"]: u for u in result["units"]}
        self.assertEqual(units["prop:3.4"]["display_name"], "Proposition 3.1")
        self.assertIn("Proof of Proposition", units["prop:3.4"]["proof"])
        self.assertEqual(units["lem:2.1"]["proof_status"], "cited_result")
        ids = [bid for u in result["units"] for bid in u["proof_block_ids"]]
        self.assertEqual(len(ids), 9)
        self.assertEqual(len(set(ids)), 9)
        persisted = json.loads((self.root / "runs" / result["id"] / "inventory/paper.json").read_text(encoding="utf-8"))
        self.assertEqual(persisted["units"], result["units"])
        self.assertEqual(result["text"], "")


if __name__ == "__main__":
    unittest.main()
