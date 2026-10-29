from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from lean_formalizer.latex_extract import MAX_CONTEXT_CHARS, load_latex, parse_latex
from lean_formalizer.cli import build_parser, cmd_formalize
from lean_formalizer.extract import load_text_or_pdf
from lean_formalizer.llm import MockLLM
from lean_formalizer.models import FormalizationResult
from lean_formalizer.pipeline import FormalizationPipeline
from lean_formalizer.structure import decompose
from lean_formalizer import webapp


class LatexExtractionTest(unittest.TestCase):
    def test_math_macros_comments_custom_theorems_and_lines(self):
        source = r"""\documentclass{article}
\newcommand{\R}{\mathbb{R}}
\newtheorem{result}[theorem]{Theorem}
\begin{document}
% ignored \begin{theorem} garbage
Assume $x \in \R$. The rate is $5\%$.
\begin{result}[A title]\label{thm:main}
\[
\frac{1}{1+\frac{1}{x}} \leq x^2
\]
\end{result}
\begin{proof}
\begin{enumerate}\item Use $x^2$.\end{enumerate}
\end{proof}
\end{document}
"""
        text, metadata = parse_latex(source, "paper.TeX").select()
        self.assertIn(r"\newcommand{\R}{\mathbb{R}}", text)
        self.assertIn(r"\frac{1}{1+\frac{1}{x}}", text)
        self.assertIn(r"5\%", text)
        self.assertNotIn("ignored", text)
        self.assertNotIn("documentclass", text)
        self.assertIn(r"\begin{enumerate}", text)
        self.assertEqual(metadata["selected_label"], "thm:main")
        self.assertEqual(metadata["blocks"][0]["start_line"], 7)
        self.assertEqual(metadata["blocks"][0]["end_line"], 11)
        self.assertTrue(metadata["warnings"])

    def test_multiple_theorems_require_selection_keep_dependencies_and_proof(self):
        source = r"""\begin{document}
Let $n \in \mathbb{N}$.
\begin{definition}\label{def:a}A nested $\frac{n}{2}$ definition.\end{definition}
\begin{lemma}\label{lem:a}Useful result.\end{lemma}
\begin{proof}Other proof should be excluded.\end{proof}
\begin{theorem}\label{thm:main}By \ref{lem:a} and \eqref{eq:later}, $n=n$.\end{theorem}
\begin{proof}This is the selected proof.\end{proof}
\begin{equation}\label{eq:later} n+0=n \end{equation}
\end{document}"""
        doc = parse_latex(source)
        with self.assertRaisesRegex(ValueError, "Select one"):
            doc.select()
        text, metadata = doc.select("thm:main")
        for expected in ("Let $n", "nested", "Useful result", "n+0=n", "selected proof"):
            self.assertIn(expected, text)
        self.assertNotIn("Other proof", text)
        self.assertEqual(metadata["warnings"], [])
        self.assertEqual([b["included"] for b in metadata["blocks"]], [True, True, False, True, True, True])
        with self.assertRaisesRegex(ValueError, "not found"):
            doc.select("missing")

    def test_late_theorem_is_selected_without_truncation(self):
        source = "Unrelated introduction. " * 400 + r"\begin{theorem}\label{late}$n=n$.\end{theorem}"
        text, _ = parse_latex(source).select("late")
        self.assertIn(r"\label{late}", text)
        self.assertGreater(len(text), 6000)

    def test_large_previous_theorem_is_excluded_but_context_is_never_cut(self):
        source = (r"\begin{theorem}\label{old}" + "Old claim. " * 6000 + r"\end{theorem}"
                  + r"\begin{theorem}\label{new}$n=n$.\end{theorem}")
        text, _ = parse_latex(source).select("new")
        self.assertNotIn("Old claim", text)
        self.assertIn("$n=n$", text)
        with self.assertRaisesRegex(ValueError, "No content was truncated"):
            parse_latex("x" * (MAX_CONTEXT_CHARS + 1)).select()

    def test_standalone_warnings_do_not_read_included_files(self):
        text, metadata = parse_latex(
            r"\input{secret.tex}\begin{theorem}By \ref{missing} and $\unknown{x}$, $x=x$.\end{theorem}"
        ).select()
        self.assertIn(r"\unknown{x}", text)
        warnings = " ".join(metadata["warnings"])
        self.assertIn("Included files are not loaded", warnings)
        self.assertIn("missing", warnings)
        self.assertIn(r"\unknown", warnings)

    def test_starred_environments_and_duplicate_labels(self):
        text, _ = parse_latex(r"\begin{theorem*}$x=x$.\end{theorem*}").select()
        self.assertIn("Selected theorem", text)
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            parse_latex(r"\begin{theorem}\label{x}A.\end{theorem}\begin{lemma}\label{x}B.\end{lemma}")

    def test_math_lines_are_not_cleaned_as_pdf_page_numbers(self):
        source = "\\begin{theorem}\n\\[\n42\n\\]\n\\end{theorem}"
        text, _ = parse_latex(source).select()
        self.assertIn("\n42\n", text)

    def test_prose_reference_retains_forward_dependency(self):
        source = (r"Assume the identity in \eqref{eq:later}."
                  r"\begin{theorem}\label{main}$n=n$.\end{theorem}"
                  r"\begin{equation}\label{eq:later}n+0=n\end{equation}")
        text, metadata = parse_latex(source).select("main")
        self.assertIn("n+0=n", text)
        self.assertFalse(metadata["warnings"])

    def test_malformed_source_is_reported(self):
        for source in (r"\begin{theorem}No end", r"\begin{theorem}Bad \frac{1}{2\end{theorem}"):
            with self.subTest(source=source), self.assertRaisesRegex(ValueError, "Malformed"):
                parse_latex(source)

    def test_bibliography_and_comments_are_excluded(self):
        text, _ = parse_latex(
            "A claim. % hidden\n" + r"\begin{thebibliography}{9}\bibitem{x}Not mathematics.\end{thebibliography}"
        ).select()
        self.assertIn("A claim.", text)
        self.assertNotIn("hidden", text)
        self.assertNotIn("Not mathematics", text)

    def test_utf8_bom_uppercase_extension_and_encoding_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "paper.TeX"
            path.write_text(r"\begin{theorem}$n=n$.\end{theorem}", encoding="utf-8-sig")
            text, _ = load_latex(path).select()
            self.assertIn("$n=n$", text)
            path.write_bytes(b"\xff\xfe\x00")
            with self.assertRaisesRegex(ValueError, "UTF-8"):
                load_latex(path)
            with self.assertRaisesRegex(ValueError, "Expected a .tex"):
                load_latex(Path(tmp) / "bad.txt")


class LatexIntegrationTest(unittest.TestCase):
    def test_cli_has_real_tex_option_and_exclusive_sources(self):
        parser = build_parser()
        args = parser.parse_args(["formalize", "--tex", "paper.TeX", "--theorem-label", "main"])
        self.assertEqual(args.tex, "paper.TeX")
        self.assertIsNone(args.text)
        for argv in (["--tex", "a.tex", "--text", "other"], ["--te", "a.tex"]):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                parser.parse_args(["formalize", *argv])
        with patch("lean_formalizer.cli.get_llm"), patch("lean_formalizer.cli.PaperPipeline") as pipeline:
            pipeline.return_value.analyze.return_value.id = "paper-test"
            pipeline.return_value.run.return_value = {
                "name": "paper.TeX", "format": "latex", "units_selected": 1,
                "units_total": 1, "coverage": 0.0, "status_counts": {"failed": 1},
            }
            with contextlib.redirect_stdout(io.StringIO()):
                cmd_formalize(args)
            self.assertEqual(pipeline.return_value.analyze.call_args.args[0], "paper.TeX")
            self.assertEqual(pipeline.return_value.analyze.call_args.kwargs["target"], "main")

    def test_text_pdf_and_tex_loader_compatibility(self):
        self.assertEqual(load_text_or_pdf(text="  A claim.  "), "A claim.")
        self.assertEqual(load_text_or_pdf(), "")
        with patch("lean_formalizer.extract.extract_from_pdf", return_value="PDF claim.") as pdf:
            self.assertEqual(load_text_or_pdf(pdf_path="a.pdf", max_pages=2), "PDF claim.")
            pdf.assert_called_once_with("a.pdf", max_pages=2)
        with self.assertRaisesRegex(ValueError, "exactly one"):
            load_text_or_pdf(text="A", tex_path="a.tex")
        with self.assertRaisesRegex(ValueError, "requires a TeX"):
            load_text_or_pdf(text="A", theorem_label="x")

    def test_pipeline_preserves_extraction_and_reports_errors_without_llm(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "paper.tex"
            path.write_text(r"\newcommand{\R}{\mathbb{R}}\begin{theorem}\label{main}$x\in\R$.\end{theorem}", encoding="utf-8")
            pipe = FormalizationPipeline(llm=MockLLM(), memory_dir=Path(tmp) / "memory")
            with patch.object(pipe, "_formalize_loaded", return_value=FormalizationResult(success=False)) as loaded:
                result = pipe.formalize(tex_path=path, theorem_label="main", save_to_memory=False)
                self.assertIn(r"\mathbb{R}", loaded.call_args.kwargs["text"])
                self.assertEqual(result.source["selected_label"], "main")
                self.assertTrue(result.source["warnings"])
                loaded.reset_mock()
                result = pipe.formalize(tex_path=path, theorem_label="missing")
                self.assertFalse(result.success)
                self.assertIn("not found", result.message)
                loaded.assert_not_called()
                result = pipe.formalize(text="x" * (MAX_CONTEXT_CHARS + 1))
                self.assertIn("No content was truncated", result.message)
                loaded.assert_not_called()

    def test_decomposition_sees_late_claim_and_retains_long_statement(self):
        claim = "Assume " + "a meaningful hypothesis, " * 30 + "then the conclusion holds."
        class CaptureLLM:
            def chat(self, messages, **kwargs):
                self.prompt = messages[-1]["content"]
                return json.dumps({"main_theorem": claim})
        llm = CaptureLLM()
        result = decompose("x" * 6000 + "LATE_THEOREM", llm)
        self.assertIn("LATE_THEOREM", llm.prompt)
        self.assertEqual(result.main_theorem, claim)
        with self.assertRaisesRegex(ValueError, "No content was truncated"):
            decompose("x" * (MAX_CONTEXT_CHARS + 1), llm)

    def test_tex_reaches_offline_pipeline_with_full_source_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "paper.tex"
            source = r"\begin{theorem}\label{main}For $n\in\mathbb{N}$, $n+0=n$.\end{theorem}"
            path.write_text(source, encoding="utf-8")
            pipe = FormalizationPipeline(llm=MockLLM(), memory_dir=Path(tmp) / "memory")
            with patch("lean_formalizer.pipeline.lean_available", return_value=False), \
                 patch("lean_formalizer.pipeline.lean_project_dir", return_value=None), \
                 patch("lean_formalizer.pipeline.default_scratch_root", return_value=None):
                result = pipe.formalize(tex_path=path, save_to_memory=False)
            self.assertEqual(result.source["selected_label"], "main")
            self.assertIn(source, result.structured.raw_text)
            self.assertFalse(result.success)
            self.assertIn("Mock backend", result.verification["raw_message"])


def multipart(files, fields=None):
    boundary = "test-formalizer"
    parts = []
    for name, value in (fields or {}).items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
    for field, filename, content in files:
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; filename="{filename}"; name="{field}"\r\nContent-Type: application/octet-stream\r\n\r\n'.encode() + content + b"\r\n")
    return f'multipart/form-data; boundary="{boundary}"', b"".join(parts) + f"--{boundary}--\r\n".encode()


class LatexUploadTest(unittest.TestCase):
    def test_tex_and_pdf_bytes_survive_multipart(self):
        for field, filename, content in (
            ("file", "paper.TeX", b"\\begin{theorem}$x=x$.\\end{theorem}\n"),
            ("pdf", "paper.pdf", b"%PDF-1.7\r\nbinary\x00\xff\r\n"),
        ):
            ctype, body = multipart([(field, filename, content)], {"theorem_label": "main"})
            data, path = webapp._parse_multipart(ctype, body)
            try:
                self.assertEqual(Path(path).read_bytes(), content)
                self.assertEqual(Path(path).suffix, Path(filename).suffix.lower())
                self.assertEqual(data["theorem_label"], "main")
                self.assertEqual(data["_upload_name"], filename)
            finally:
                Path(path).unlink()

    def test_invalid_uploads_do_not_create_temporary_files(self):
        invalid = [
            [("file", "bad.txt", b"not tex")],
            [("file", "bad.tex", b"\xff")],
            [("file", "fake.pdf", b"not pdf")],
            [("file", "empty.tex", b"")],
            [("file", "a.tex", b"x"), ("file", "b.tex", b"y")],
        ]
        with patch("lean_formalizer.webapp.tempfile.NamedTemporaryFile") as temporary:
            for files in invalid:
                with self.subTest(files=files), self.assertRaises(ValueError):
                    webapp._parse_multipart(*multipart(files))
            with self.assertRaises(ValueError):
                webapp._parse_multipart("multipart/form-data", b"bad")
            temporary.assert_not_called()

    def test_handler_dispatches_tex_without_stale_text(self):
        ctype, body = multipart([("file", "a.tex", b"A claim.")], {"theorem_label": "main"})
        class Request:
            headers = {"Content-Type": ctype, "Content-Length": str(len(body))}
            rfile = io.BytesIO(body)
        with patch("lean_formalizer.webapp._start_job", return_value="job") as start, \
             patch("lean_formalizer.webapp._backend_status", return_value={"backend": "mock"}), \
             patch("lean_formalizer.webapp._json_response") as respond:
            webapp.Handler._handle_formalize_upload_async(Request())
            kwargs = start.call_args.kwargs
            try:
                self.assertIsNone(kwargs["pdf_path"])
                self.assertIsNone(kwargs["text"])
                self.assertTrue(kwargs["tex_path"].endswith(".tex"))
                self.assertEqual(respond.call_args.args[1], 200)
            finally:
                Path(kwargs["tex_path"]).unlink()

    def test_upload_cleanup_on_dispatch_error(self):
        ctype, body = multipart([("file", "a.tex", b"A claim.")])
        class Request:
            headers = {"Content-Type": ctype, "Content-Length": str(len(body))}
            rfile = io.BytesIO(body)
        paths = []
        def fail(_data, **kwargs):
            paths.append(kwargs["tex_path"])
            raise ValueError("cannot start")
        with patch("lean_formalizer.webapp._start_job", side_effect=fail), \
             patch("lean_formalizer.webapp._backend_status", return_value={"backend": "mock"}), \
             patch("lean_formalizer.webapp._json_response") as respond:
            webapp.Handler._handle_formalize_upload_async(Request())
            self.assertEqual(respond.call_args.args[1], 400)
        self.assertTrue(paths)
        self.assertFalse(Path(paths[0]).exists())

    def test_worker_cleans_tex_after_failure(self):
        with tempfile.NamedTemporaryFile(suffix=".tex", delete=False) as stream:
            path = stream.name
        finished = threading.Event()
        original_unlink = webapp.os.unlink
        def unlink(value):
            original_unlink(value)
            finished.set()
        with patch("lean_formalizer.webapp._backend_status", return_value={"backend": "mock"}), \
             patch("lean_formalizer.webapp._run_pipeline", side_effect=ValueError("bad source")), \
             patch("lean_formalizer.webapp.os.unlink", side_effect=unlink), \
             patch("lean_formalizer.webapp.traceback.print_exc"):
            job_id = webapp._start_job({}, None, None, tex_path=path)
            self.assertTrue(finished.wait(2))
        self.assertFalse(Path(path).exists())
        with webapp._jobs_lock:
            self.assertEqual(webapp._jobs.pop(job_id)["status"], "error")

    def test_target_statement_does_not_replace_uploaded_tex(self):
        with patch("lean_formalizer.webapp._build_llm_from_request"), \
             patch("lean_formalizer.webapp.PaperPipeline") as pipeline:
            pipeline.return_value.run.return_value = {"coverage": 0.0}
            webapp._run_pipeline({"target_statement": "My target", "theorem_label": "main", "_upload_name": "paper.tex"},
                                 text=None, pdf_path=None, tex_path="temp.tex")
            analyzed = pipeline.return_value.analyze.call_args
            self.assertEqual(analyzed.args[0], "temp.tex")
            self.assertEqual(analyzed.kwargs["source_name"], "paper.tex")
            self.assertEqual(analyzed.kwargs["target"], "main")
            self.assertEqual(pipeline.return_value.run.call_args.kwargs["target_statement"], "My target")


if __name__ == "__main__":
    unittest.main()
