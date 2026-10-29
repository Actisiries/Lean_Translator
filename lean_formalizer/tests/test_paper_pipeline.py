"""File ingestion and paper job regressions; LLM/Lean are isolated substitutes."""
from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from lean_formalizer.document_models import DocumentBlock, MathUnit, PaperDocument, SourceSpan
from lean_formalizer.document_extract import extract_document, extract_pdf, plan_dependencies, _glyph_warning
from lean_formalizer.file_support import missing_parser_message
from lean_formalizer.models import FormalizationResult, Candidate
from lean_formalizer.llm import MockLLM
from lean_formalizer.paper_pipeline import PaperPipeline
from lean_formalizer.pipeline import FormalizationPipeline
from lean_formalizer.lean_verify import _noncacheable_compiler_failure, declaration_preflight_source
from lean_formalizer import webapp


def write_pdf(path: Path):
    # Minimal digital PDF with a real text stream, readable by either parser.
    stream = b"BT /F1 12 Tf 72 720 Td (Theorem 1. For every natural number n, n + 0 = n.) Tj 0 -20 Td (Proof. By the definition of addition.) Tj ET"
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>",
               b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
               b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
               b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
               b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream"]
    data = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for i, obj in enumerate(objects, 1):
        offsets.append(len(data))
        data.extend(str(i).encode() + b" 0 obj\n" + obj + b"\nendobj\n")
    xref = len(data)
    data.extend(b"xref\n0 6\n0000000000 65535 f \n")
    for offset in offsets[1:]:
        data.extend(f"{offset:010d} 00000 n \n".encode())
    data.extend(f"trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    path.write_bytes(data)


def paper():
    blocks = [DocumentBlock("b1", "theorem", "First claim", 0, 10, labels=["first"]),
              DocumentBlock("b2", "theorem", "Second claim", 11, 22, labels=["second"])]
    units = [MathUnit("u1", "theorem", "first", "First claim", "b1", selected=True),
             MathUnit("u2", "theorem", "second", "Second claim", "b2", selected=True)]
    return PaperDocument("paper-test", "paper.tex", "latex", "First claim Second claim", blocks, units)


class PaperRunTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.pipeline = PaperPipeline(output_dir=self.tmp.name)
        # Fails quickly instead of hanging the test if the infinite loop returns.
        self.saves = 0
        original = self.pipeline._save
        def bounded_save(*args):
            self.saves += 1
            if self.saves > 15:
                raise AssertionError("Paper repeatedly scheduled a terminal unit")
            return original(*args)
        self.pipeline._save = bounded_save

    def test_failed_unit_finishes_once_with_and_without_resume(self):
        for resume in (True, False):
            with self.subTest(resume=resume), patch("lean_formalizer.paper_pipeline.FormalizationPipeline") as factory:
                self.saves = 0
                factory.return_value.formalize.return_value = FormalizationResult(
                    success=False, best_code="theorem broken : False := by trivial",
                    message="Lean rejected this proof", verification={"raw_message": "unsolved goals"})
                report = self.pipeline.run(paper(), resume=resume, save_to_memory=False)
                self.assertEqual(factory.return_value.formalize.call_count, 2)
                self.assertEqual(report["status_counts"], {"failed": 2})
                self.assertIn("Lean rejected this proof", report["message"])
                self.assertIn("unsolved goals", report["verification"]["raw_message"])
                self.assertTrue(report["best_code"])

    def test_expired_deadline_terminates_without_model_calls(self):
        with patch("lean_formalizer.paper_pipeline.FormalizationPipeline") as factory:
            report = self.pipeline.run(paper(), deadline_seconds=-1)
        factory.assert_not_called()
        self.assertEqual(report["status_counts"], {"budget_exhausted": 2})

    def test_model_exception_retains_trace_and_monitored_unit(self):
        events = []
        self.pipeline.progress_callback = events.append
        with patch('lean_formalizer.paper_pipeline.FormalizationPipeline') as factory:
            factory.return_value._stage_trace = [{'stage': 'llm_wait', 'ok': False, 'error': 'connection refused'}]
            factory.return_value.formalize.side_effect = RuntimeError('connection refused')
            report = self.pipeline.run(paper(), resume=False, save_to_memory=False)
            callback = factory.call_args.kwargs['progress_callback']
            callback({'stage': 'test_event'})
        self.assertEqual(report['status_counts'], {'failed': 2})
        self.assertEqual(len(report['stage_trace']), 2)
        self.assertEqual(report['stage_trace'][0]['error'], 'connection refused')
        self.assertEqual(report['selected_units'][0]['result']['error_type'], 'RuntimeError')
        self.assertEqual(events[-1]['unit'], 'u2')
        self.assertEqual(len([e for e in events if e['stage'] == 'paper_unit_end' and not e['ok']]), 2)

    def test_resume_reuses_success_but_retries_failure_once(self):
        result = FormalizationResult(success=True, best_code="theorem demo : True := by trivial")
        with patch("lean_formalizer.paper_pipeline.FormalizationPipeline") as factory:
            factory.return_value.formalize.side_effect = [result, FormalizationResult(success=False, message="retry me")]
            self.pipeline.run(paper())
        self.saves = 0
        with patch("lean_formalizer.paper_pipeline.FormalizationPipeline") as factory:
            factory.return_value.formalize.return_value = result
            report = self.pipeline.run(paper())
        self.assertEqual(factory.return_value.formalize.call_count, 1)
        self.assertEqual(report["coverage"], 1.0)
        self.assertEqual(len(report["selected_units"]), 2)

    def test_cycle_is_reported_as_blocked(self):
        doc = paper()
        doc.units[0].dependencies = ["u2"]
        doc.units[1].dependencies = ["u1"]
        with patch("lean_formalizer.paper_pipeline.FormalizationPipeline") as factory:
            report = self.pipeline.run(doc)
        factory.assert_not_called()
        self.assertEqual(report["status_counts"], {"blocked": 2})

    def test_success_preserves_code_candidates_trace_and_artifacts(self):
        code = "theorem demo : True := by trivial"
        with patch("lean_formalizer.paper_pipeline.FormalizationPipeline") as factory:
            factory.return_value.formalize.return_value = FormalizationResult(
                success=True, best_code=code, candidates=[Candidate(code, compiled=True)],
                stage_trace=[{"stage": "lean_compile", "ok": True}])
            report = self.pipeline.run(paper())
        self.assertTrue(report["success"])
        self.assertEqual(report["best_code"], code)
        self.assertEqual(len(report["stage_trace"]), 2)
        self.assertTrue(report["candidates"])
        saved = json.loads((Path(report["artifacts"]) / "report.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["selected_units"][0]["code"], code)

    def test_paper_usage_includes_all_units(self):
        with patch('lean_formalizer.paper_pipeline.FormalizationPipeline') as factory:
            factory.return_value.formalize.side_effect = [
                FormalizationResult(success=False, token_usage={'total_tokens': 10, 'calls': 2, 'reported': True}),
                FormalizationResult(success=False, token_usage={'total_tokens': 20, 'calls': 3, 'reported': True}),
            ]
            report = self.pipeline.run(paper(), resume=False, save_to_memory=False)
        self.assertEqual(report['token_usage']['total_tokens'], 30)
        self.assertEqual(report['token_usage']['calls'], 5)

    def test_context_contains_target_macros_and_proof(self):
        doc = paper()
        doc.macros = [r"\newcommand{\N}{\mathbb{N}}"]
        doc.units[0].proof = "Source argument"
        text = self.pipeline._context(doc, doc.units[0], {u.id: u for u in doc.units})
        self.assertIn("First claim", text)
        self.assertIn(doc.macros[0], text)
        self.assertIn("Source argument", text)

    def test_paper_requires_full_target_without_changing_text_default(self):
        with patch('lean_formalizer.paper_pipeline.FormalizationPipeline') as factory:
            factory.return_value.formalize.return_value = FormalizationResult(success=False, message='missing definitions')
            self.pipeline.run(paper(), resume=False, save_to_memory=False)
        self.assertTrue(factory.call_args.kwargs['require_exact_target'])
        pipe = FormalizationPipeline(llm=MockLLM(), memory_dir=self.tmp.name, require_exact_target=True)
        with patch('lean_formalizer.pipeline.project_llm_context', return_value=''):
            prompt = pipe._system_with_project('A system role allowing a core')
        self.assertIn('overrides any permission to formalize a core', prompt)
        self.assertIn('entire selected result', prompt)
        self.assertFalse(FormalizationPipeline(llm=MockLLM(), memory_dir=self.tmp.name).require_exact_target)

    def test_git_environment_failure_is_not_reused_as_lean_error(self):
        self.assertTrue(_noncacheable_compiler_failure('fatal: detected dubious ownership in repository'))
        self.assertTrue(_noncacheable_compiler_failure("error: external command 'git' exited with code 128"))
        self.assertFalse(_noncacheable_compiler_failure('unsolved goals: x = y'))

    def test_statement_preflight_preserves_definitions_and_supporting_proofs(self):
        code = '''namespace Paper
def Predicate (n : Nat) : Prop := n = n
private lemma helper (n : Nat) : Predicate n := by rfl
/- theorem misleading : False := by sorry /-
theorem nested_comment : False := by sorry -/ -/
theorem target (n : Nat := 0) : Predicate n := by exact helper n
end Paper'''
        source = declaration_preflight_source(code)
        self.assertIn('def Predicate (n : Nat) : Prop := n = n', source)
        self.assertIn('private lemma helper (n : Nat) : Predicate n := by rfl', source)
        self.assertIn('axiom target (n : Nat := 0) : Predicate n', source)
        self.assertNotIn('by exact helper n', source)
        self.assertTrue(source.endswith('end Paper'))
        self.assertIn('axiom target : (let x := 0; x = x)',
                      declaration_preflight_source('theorem target : (let x := 0; x = x)'))


class FileInputTests(unittest.TestCase):
    def test_pdf_boundaries_citations_and_section_assumptions(self):
        source = """1. Introduction
Some introduction.
2. sdf-absorbing rings
All rings in this section are Marot rings.
Definition 1. An SAF-ring is a ring with property SAF.
Theorem 2. A characteristic-two ring is SAF iff it is SP.
Remark 3. This remark is not the proof of Theorem 2.
Proposition4. Finite products preserve SAF.
Proof. By [6,
Theorem 2.5] and Theorem 2.
Proof of (i). First argument.
4 AUTHOR RUNNING HEADER
1 n i i S
Proof of (ii). Second argument.
Example 10. An example, using [2,
Corollary 2.7] gives the next result.
Corollary 13. The next result.
Proof. Use [7,
Theorem 3.2], then Proposition4.
3. Strong rings
New section assumptions.
Theorem 19. A further claim.
Proof. Its argument.
References
[7] Bibliography entry.
"""
        lines = [(line, SourceSpan('sample.pdf', 0, 0, 1)) for line in source.splitlines()]
        with patch('lean_formalizer.document_extract._pdf_lines', return_value=(lines, 1, [])):
            doc = extract_pdf(Path(__file__))
        self.assertEqual([u.label for u in doc.units], ['Definition 1', 'Theorem 2', 'Proposition 4', 'Corollary 13', 'Theorem 19'])
        by_label = {u.label: u for u in doc.units}
        self.assertEqual(by_label['Theorem 2'].proof, '')
        self.assertIn('Second argument', by_label['Proposition 4'].proof)
        self.assertIn('AUTHOR RUNNING HEADER', by_label['Proposition 4'].proof)
        self.assertNotIn('Example 10', by_label['Proposition 4'].proof)
        self.assertNotIn('Bibliography', by_label['Theorem 19'].proof)
        contexts = '\n'.join(b.text for b in doc.blocks if b.id in by_label['Theorem 2'].context)
        self.assertIn('Marot rings', contexts)
        plan_dependencies(doc)
        self.assertIn(by_label['Theorem 2'].id, by_label['Proposition 4'].dependencies)
        self.assertIn(by_label['Proposition 4'].id, by_label['Corollary 13'].dependencies)
        self.assertTrue(any('External citation [7, Theorem 3.2]' == r for r in by_label['Corollary 13'].unresolved))
        self.assertTrue(_glyph_warning('(cid:81)', 3))

    def test_pdf_real_extraction_and_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "paper.pdf"
            write_pdf(path)
            for fallback in (False, True):
                with self.subTest(fallback=fallback), patch.dict(sys.modules, {"pdfplumber": None} if fallback else {}):
                    doc = extract_document(path)
                    self.assertEqual(len(doc.units), 1)
                    self.assertIn("n + 0 = n", doc.units[0].statement)
                    self.assertIn("addition", doc.units[0].proof)
                    self.assertEqual(doc.blocks[0].spans[0].page, 1)

    def test_tex_real_extraction(self):
        doc = extract_document(Path(__file__).resolve().parents[1] / "examples" / "identity.tex")
        self.assertEqual(len(doc.units), 1)
        self.assertEqual(doc.units[0].label, "thm:add-zero")
        self.assertTrue(doc.macros)
        self.assertIn("addition", doc.units[0].proof)

    def test_web_both_file_types_complete_and_show_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            pdf = Path(tmp) / "paper.pdf"
            write_pdf(pdf)
            tex = Path(__file__).resolve().parents[1] / "examples" / "identity.tex"
            for path in (pdf, tex):
                with self.subTest(path=path), patch("lean_formalizer.webapp._build_llm_from_request", return_value=MockLLM()), \
                     patch("lean_formalizer.paper_pipeline.FormalizationPipeline") as factory:
                    code = "theorem add_zero (n : Nat) : n + 0 = n := Nat.add_zero n"
                    factory.return_value.formalize.return_value = FormalizationResult(success=True, best_code=code)
                    report = webapp._run_pipeline(
                        {"paper_output": str(Path(tmp) / "runs"), "_upload_name": path.name,
                         "num_candidates": "3", "lean_compile_timeout": "45", "save_to_memory": "false"},
                        None, str(path) if path.suffix == ".pdf" else None,
                        tex_path=str(path) if path.suffix == ".tex" else None)
                    self.assertTrue(report["success"])
                    self.assertEqual(report["best_code"], code)
                    self.assertEqual(report["paper_report"]["name"], path.name)
                    self.assertIn("n", factory.return_value.formalize.call_args.kwargs["text"])
                    self.assertEqual(factory.call_args.kwargs["num_candidates"], 3)

    def test_missing_parser_error_identifies_server_and_fix(self):
        for kind in ("tex", "pdf"):
            message = missing_parser_message(kind)
            self.assertIn(sys.executable, message)
            self.assertIn("--target", message)
            self.assertIn("restart", message)


if __name__ == "__main__":
    unittest.main()
