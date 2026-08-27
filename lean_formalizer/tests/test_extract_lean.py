from __future__ import annotations

import sys
import os
import tempfile
import threading
import time
from unittest.mock import patch
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from lean_formalizer.lean_verify import (
    declaration_name_matches,
    extract_lean_code,
    has_proof_body,
    normalize_lean_imports,
    repair_loop,
    strip_incomplete_proof,
    proof_self_references,
    complete_proof_with_known_lemmas,
    normalized_lean_code,
    recover_import_failure,
    identifier_suggestions,
    compile_declaration,
    unresolved_lean_placeholder_reason,
    compile_lean,
    compile_status,
    has_umbrella_mathlib_import,
    import_policy_error,
)
from lean_formalizer.memory import _style_relevant, tfidf_cosine
from lean_formalizer.models import Candidate, StructuredMath
from lean_formalizer.pipeline import FormalizationPipeline
from lean_formalizer.pipeline import _has_unresolved_lean_notation
from lean_formalizer.diagnose import build_report
from lean_formalizer.llm import _content_text, probe_http_endpoint, LLMTimeoutError
from lean_formalizer.structure import _parse_json_block, decompose
from lean_formalizer.mathlib_hints import mathlib_hints_for_statement
from lean_formalizer.self_fix import search_mathlib_imports, self_fix_lean
from lean_formalizer.autoconfig import load_local_config_env


class ExtractLeanTest(unittest.TestCase):
    def test_identifier_suggestions_accepts_list_index_and_scans_declarations(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "Demo.lean").write_text(
                "theorem intermediate_value_Icc : True := by trivial\n",
                encoding="utf-8",
            )
            with patch(
                "lean_formalizer.lean_verify._build_project_index",
                return_value={"root": str(root), "declarations": []},
            ):
                hints = identifier_suggestions(
                    "error: unknown identifier 'intermediate_value_Iccx'"
                )
        self.assertIn("intermediate_value_Icc", hints)

    def test_structure_placeholder_guard_detects_malformed_pseudo_sum(self) -> None:
        self.assertTrue(_has_unresolved_lean_notation(
            "The natural-number sum ? k in Finset.range n, (2 * k + 1) equals n ^ 2."
        ))
        self.assertTrue(_has_unresolved_lean_notation("For n : ?, the result holds."))
        self.assertFalse(_has_unresolved_lean_notation(
            "The sum from k = 0 through n − 1 of 2k + 1 equals n squared."
        ))

    def test_invented_axiom_is_rejected_as_placeholder(self) -> None:
        from lean_formalizer.lean_verify import _is_placeholder_lean

        code = "axiom fabricated : True\ntheorem demo : True := fabricated"
        self.assertIn("invented `axiom`", _is_placeholder_lean(code))

    def test_unresolved_question_mark_notation_is_rejected_before_lean(self) -> None:
        from lean_formalizer.lean_verify import _is_placeholder_lean

        self.assertIn(
            "placeholder",
            _is_placeholder_lean(
                "theorem odd_sum (n : ?) : ? k in Finset.range n, (2*k+1) = n^2 := by simp"
            ).lower(),
        )

    def test_declaration_holes_fail_before_lake_compilation(self) -> None:
        declaration = "import Mathlib\n\naxiom malformed (G : Type*) : G ?_"
        with patch("lean_formalizer.lean_verify._compile_in_lake_project") as compile_lake:
            ok, message = compile_declaration(declaration)
        self.assertFalse(ok)
        self.assertIn("placeholder", message.lower())
        compile_lake.assert_not_called()

    def test_unicode_logical_symbols_are_not_hole_syntax(self) -> None:
        self.assertEqual(
            unresolved_lean_placeholder_reason("axiom demo : P ∧ ∃ x, Q x"), ""
        )

    def test_umbrella_import_is_rejected_before_lean(self) -> None:
        code = "import Mathlib\n\nexample : 1 + 1 = 2 := by decide"
        self.assertTrue(has_umbrella_mathlib_import(code))
        self.assertIn("umbrella import", import_policy_error(code).lower())
        with patch("lean_formalizer.lean_verify._compile_in_lake_project") as compile_lake:
            ok, message = compile_lean(code, timeout=1)
        self.assertFalse(ok)
        self.assertEqual(compile_status(ok, message), "import_timeout_risk")
        compile_lake.assert_not_called()

    def test_fine_grained_import_is_not_rejected(self) -> None:
        code = "import Mathlib.Data.Nat.Basic\n\nexample : 1 + 1 = 2 := by decide"
        self.assertFalse(has_umbrella_mathlib_import(code))
        self.assertEqual(import_policy_error(code), "")

    def test_valid_sum_notation_is_not_rejected_as_question_mark_placeholder(self) -> None:
        from lean_formalizer.lean_verify import _is_placeholder_lean

        code = "theorem odd_sum (n : Nat) : (∑ k in Finset.range n, (2*k+1)) = n^2 := by simp"
        self.assertNotIn("placeholder", _is_placeholder_lean(code).lower())

    def test_prose_before_closed_fence(self) -> None:
        raw = (
            "Here is the Lean code:\n"
            "```lean\n"
            "import Mathlib.Data.Set.Basic\n"
            "theorem demo : True := by trivial\n"
            "```\n"
        )
        self.assertIn("theorem demo", extract_lean_code(raw) or "")

    def test_core_nat_identity_drops_umbrella_mathlib_import(self) -> None:
        code = (
            "import Mathlib\n\n"
            "theorem nat_add_zero (n : Nat) : n + 0 = n := Nat.add_zero n"
        )
        normalized = normalize_lean_imports(code)
        self.assertNotIn("import Mathlib", normalized)
        self.assertIn("Nat.add_zero n", normalized)

    def test_unclosed_fence(self) -> None:
        raw = (
            "Sure, here it is:\n"
            "```lean\n"
            "theorem demo : True := by trivial"
        )
        self.assertEqual(
            extract_lean_code(raw), "theorem demo : True := by trivial"
        )

    def test_thinking_tags_are_removed(self) -> None:
        raw = (
            "<think>I should use Rolle.</think>\n"
            "```lean\n"
            "theorem demo : True := by trivial\n"
            "```"
        )
        self.assertNotIn("Rolle", extract_lean_code(raw) or "")

    def test_pure_prose_returns_none(self) -> None:
        self.assertIsNone(extract_lean_code("I cannot produce Lean code."))

    def test_proof_body_detection(self) -> None:
        self.assertFalse(has_proof_body("theorem demo : 1 = 1 :="))
        self.assertFalse(has_proof_body("theorem demo : 1 = 1 := by"))
        self.assertFalse(has_proof_body("theorem demo : 1 = 1 := ..."))
        self.assertTrue(
            has_proof_body("theorem demo : 1 = 1 := by omega")
        )
        self.assertEqual(
            strip_incomplete_proof("theorem demo : 1 = 1 := ..."),
            "theorem demo : 1 = 1",
        )
        self.assertTrue(
            proof_self_references("theorem demo : 1 = 1 := demo")
        )
        self.assertFalse(
            proof_self_references(
                "lemma demo_aux : 1 = 1 := rfl\n"
                "theorem demo : 1 = 1 := demo_aux"
            )
        )

    def test_declaration_name_guard(self) -> None:
        old = "theorem demo : 1 = 1"
        self.assertTrue(
            declaration_name_matches(
                old, "theorem demo : 1 = 1 := by omega"
            )
        )
        self.assertFalse(
            declaration_name_matches(
                old, "theorem rolle : 1 = 1 := by omega"
            )
        )

    def test_intermediate_value_import_alias(self) -> None:
        self.assertIn(
            "import Mathlib.Topology.Order.IntermediateValue",
            normalize_lean_imports(
                "import Mathlib.Topology.Algebra.Order.IntermediateValue"
            ),
        )

    def test_contracting_fixed_point_completion(self) -> None:
        decl = (
            "import Mathlib.Topology.MetricSpace.Contracting\n\n"
            "theorem contractive_mapping_unique_fixed_point "
            "{α : Type*} [MetricSpace α] [CompleteSpace α] [Nonempty α] "
            "{T : α → α} {K : NNReal} (hT : ContractingWith K T) : "
            "∃! x : α, T x = x"
        )
        out = complete_proof_with_known_lemmas(decl)
        self.assertIn("ContractingWith.fixedPoint T hT", out)
        self.assertNotIn("ContractingWith.fixedPoint f", out)
        self.assertTrue(has_proof_body(out))

    def test_contracting_completion_replaces_invalid_proof(self) -> None:
        code = (
            "import Mathlib.Topology.MetricSpace.Contracting\n\n"
            "theorem banach_fixed_point "
            "{α : Type*} [MetricSpace α] [CompleteSpace α] [Nonempty α] "
            "{T : α → α} {K : NNReal} (hK : K < 1) "
            "(hT : ContractingWith K T) : ∃! x : α, T x = x := by\n"
            "  refine ⟨hT.fixedPoint hK, hT.fixedPoint_isFixedPt hK, ?_⟩\n"
            "  intro y hy\n"
            "  exact hT.fixedPoint_unique hK hy"
        )
        out = complete_proof_with_known_lemmas(code)
        self.assertEqual(out.count(":= by"), 1)
        self.assertNotIn("fixedPoint hK", out)
        self.assertIn("ContractingWith.fixedPoint T hT", out)

    def test_compact_image_completion_uses_continuous_on(self) -> None:
        code = (
            "import Mathlib.Topology.Basic\n\n"
            "theorem compact_image_wrapper {α β : Type*} "
            "[TopologicalSpace α] [TopologicalSpace β] "
            "(K : Set α) (f : α → β) (hK : IsCompact K) "
            "(hf : Continuous f) : IsCompact (f '' K) := by\n"
            "  exact hK.image hf"
        )
        out = complete_proof_with_known_lemmas(code)
        self.assertIn("Mathlib.Topology.Compactness.Compact", out)
        self.assertIn("hK.image_of_continuousOn hf.continuousOn", out)
        self.assertEqual(out.count(":= by"), 1)

    def test_lagrange_completion(self) -> None:
        decl = (
            "import Mathlib.GroupTheory.Subgroup.Basic\n\n"
            "theorem lagrange_card_dvd_card {G : Type*} [Group G] [Fintype G] "
            "(H : Subgroup G) : Fintype.card H ∣ Fintype.card G"
        )
        out = complete_proof_with_known_lemmas(decl)
        self.assertIn("Subgroup.card_subgroup_dvd_card", out)
        self.assertIn("Mathlib.GroupTheory.Coset.Card", out)
        self.assertIn("[Fintype H]", out)
        self.assertTrue(has_proof_body(out))

    def test_orbit_stabilizer_completion(self) -> None:
        decl = (
            "import Mathlib\n\n"
            "theorem orbit_stabilizer_cardinal {G : Type u} {X : Type v} "
            "[Group G] [MulAction G X] [Nonempty X] (x : X) : "
            "Cardinal.mk (MulAction.orbit G x) = "
            "Cardinal.mk (G ⧸ MulAction.stabilizer G x)"
        )
        out = complete_proof_with_known_lemmas(decl)
        self.assertIn("MulAction.orbitEquivQuotientStabilizer", out)
        self.assertIn("Mathlib.GroupTheory.GroupAction.Quotient", out)
        self.assertIn("Mathlib.SetTheory.Cardinal.Finite", out)
        self.assertIn("Nat.card (MulAction.orbit G x)", out)
        self.assertNotIn("Cardinal.mk", out)
        self.assertTrue(has_proof_body(out))

    def test_orbit_stabilizer_nat_card_conjunction(self) -> None:
        decl = (
            "import Mathlib\n\n"
            "variable {G X : Type*} [Group G] [MulAction G X]\n"
            "theorem orbit_equiv_quotient_stabilizer_card (x : X) : "
            "Nonempty (MulAction.orbit G x ≃ (G ⧸ MulAction.stabilizer G x)) ∧ "
            "Nat.card (MulAction.orbit G x) = "
            "Nat.card (G ⧸ MulAction.stabilizer G x)"
        )
        out = complete_proof_with_known_lemmas(decl)
        self.assertIn("Nat.card_congr", out)
        self.assertNotIn("Nonempty", out)
        self.assertTrue(has_proof_body(out))

    def test_orbit_stabilizer_equivalence(self) -> None:
        decl = (
            "import Mathlib.GroupTheory.GroupAction.Quotient\n\n"
            "noncomputable theorem orbit_stabilizer_bijection "
            "{G X : Type*} [Group G] [MulAction G X] (x : X) : "
            "MulAction.orbit G x ≃ G ⧸ MulAction.stabilizer G x"
        )
        out = complete_proof_with_known_lemmas(decl)
        self.assertIn("noncomputable def orbit_stabilizer_bijection", out)
        self.assertIn("MulAction.orbitEquivQuotientStabilizer G x", out)
        self.assertTrue(has_proof_body(out))

    def test_orbit_stabilizer_explicit_type_binders(self) -> None:
        decl = (
            "import Mathlib.GroupTheory.GroupAction.Quotient\n\n"
            "theorem orbit_stabilizer (G : Type*) [Group G] "
            "(X : Type*) [MulAction G X] (x : X) : "
            "MulAction.orbit G x ≃ G ⧸ MulAction.stabilizer G x"
        )
        out = complete_proof_with_known_lemmas(decl)
        self.assertIn("MulAction.orbitEquivQuotientStabilizer G x", out)
        self.assertNotIn("MulAction.orbitEquivQuotientStabilizer G G", out)
        self.assertTrue(has_proof_body(out))


class _ProseLLM:
    def chat(self, messages, temperature=0.2, max_tokens=4096):
        return "I cannot fix this because the statement is incomplete."


class RepairSafetyTest(unittest.TestCase):
    def test_import_failure_is_repaired_before_llm_round(self) -> None:
        code = "import Mathlib.GroupTheory.QuotientGroup\nimport Mathlib\ntheorem demo : True := by trivial"
        fixed, notes = recover_import_failure(
            code, "error: object file 'x' of module Mathlib.GroupTheory.QuotientGroup does not exist"
        )
        self.assertNotIn("import Mathlib.GroupTheory.QuotientGroup", fixed)
        self.assertIn("import Mathlib", fixed)
        self.assertTrue(notes)
    def test_compile_timeout_without_enabled_repairs_does_not_request_repair(self) -> None:
        events: list[dict] = []
        candidate = Candidate(lean_code="theorem demo : 1 = 1 := rfl")
        with patch(
            "lean_formalizer.lean_verify.compile_lean",
            return_value=(False, "Lean compilation timed out after 1s"),
        ):
            fixed = repair_loop(candidate, _ProseLLM(), max_rounds=0, progress_callback=events.append)
        self.assertEqual(fixed.status, "compile_timeout")
        self.assertEqual(fixed.compile_attempts, 1)
        self.assertEqual(fixed.repair_rounds, 0)
        self.assertFalse(any(e.get("stage") == "llm_send" for e in events))

    def test_compile_timeout_uses_one_simplification_recovery(self) -> None:
        class _SimplifyingLLM:
            timeout_seconds = 30

            def chat(self, *_args, **_kwargs):
                return "```lean\ntheorem demo : 1 = 1 := rfl\n```"

        events: list[dict] = []
        candidate = Candidate(lean_code="theorem demo : 1 = 1 := by exact 1")
        with patch(
            "lean_formalizer.lean_verify.compile_lean",
            side_effect=[
                (False, "Lean compilation timed out after 120s"),
                (True, "OK"),
            ],
        ) as compile_mock:
            fixed = repair_loop(
                candidate, _SimplifyingLLM(), max_rounds=2,
                progress_callback=events.append,
            )
        self.assertTrue(fixed.compiled)
        self.assertEqual(fixed.compile_attempts, 2)
        self.assertEqual(fixed.repair_rounds, 1)
        self.assertEqual(compile_mock.call_count, 2)
        self.assertTrue(any(e.get("stage") == "timeout_recovery" for e in events))

    def test_unchanged_repair_stops_without_recompile(self) -> None:
        class _SameLean:
            def chat(self, *_args, **_kwargs):
                return "```lean\ntheorem demo : 1 = 1 := by exact 1\n```"

        candidate = Candidate(lean_code="theorem demo : 1 = 1 := by exact 1")
        with patch(
            "lean_formalizer.lean_verify.compile_lean",
            return_value=(False, "synthetic test error"),
        ) as compile_mock:
            fixed = repair_loop(candidate, _SameLean(), max_rounds=2)
        self.assertEqual(fixed.status, "repair_noop")
        self.assertEqual(compile_mock.call_count, 1)
        self.assertEqual(fixed.repair_rounds, 1)

    def test_normalized_code_ignores_trailing_whitespace(self) -> None:
        self.assertEqual(
            normalized_lean_code("theorem demo : True := by trivial  \n"),
            normalized_lean_code("theorem demo : True := by trivial\n\n"),
        )
    def test_two_repairs_have_three_compile_attempts_not_three_repair_rounds(self) -> None:
        events: list[dict] = []
        candidate = Candidate(lean_code="theorem demo : 1 = 1 := by exact 1")
        with patch("lean_formalizer.lean_verify.compile_lean", return_value=(False, "synthetic test error")):
            repair_loop(
                candidate,
                _ProseLLM(),
                max_rounds=2,
                progress_callback=events.append,
            )
        attempts = [e["attempt"] for e in events if e.get("stage") == "lean_compile" and e.get("phase") == "start"]
        repairs = [e["round"] for e in events if e.get("request_stage") == "repair" and e.get("stage") == "llm_send"]
        self.assertEqual(attempts, [1, 2, 3])
        self.assertEqual(repairs, [1, 2])

    def test_repair_prose_does_not_replace_code(self) -> None:
        declaration = "theorem demo : 1 = 1 := by"
        candidate = Candidate(
            lean_code=declaration,
            source_stage="initial",
        )
        fixed = repair_loop(
            candidate,
            _ProseLLM(),
            natural_language="",
            max_rounds=1,
        )
        self.assertEqual(fixed.lean_code, declaration)
        self.assertTrue(
            any("keeping previous Lean code" in e for e in fixed.error_log)
        )

    def test_repair_reports_llm_and_compile_stages(self) -> None:
        events: list[dict] = []
        candidate = Candidate(lean_code="theorem demo : 1 = 1 := by exact 1")
        with patch("lean_formalizer.lean_verify.compile_lean", return_value=(False, "synthetic test error")):
            repair_loop(
                candidate,
                _ProseLLM(),
                max_rounds=1,
                progress_callback=events.append,
            )
        stages = [e.get("stage") for e in events]
        self.assertIn("lean_compile", stages)
        self.assertIn("llm_send", stages)
        self.assertIn("llm_wait", stages)


class PipelineProgressTest(unittest.TestCase):
    def test_structure_reports_llm_stages(self) -> None:
        class _StructureLLM:
            def chat(self, *_args, **_kwargs):
                return '{"main_theorem":"Every number equals itself","difficulty":"easy"}'

        events: list[dict] = []
        result = decompose("Every number equals itself.", _StructureLLM(), progress_callback=events.append)
        self.assertEqual(result.main_theorem, "Every number equals itself.")
        stages = [e.get("stage") for e in events]
        self.assertEqual(stages, ["llm_send", "llm_wait", "llm_response"])

    def test_duplicate_reuses_repaired_code(self) -> None:
        class _LLM:
            timeout_seconds = 30
            def reset_usage(self): pass
            def chat(self, *_args, **_kwargs):
                return "theorem demo : True := by trivial"

        pipe = FormalizationPipeline(
            llm=_LLM(), memory_dir=".", num_candidates=2, max_repair_rounds=1,
        )
        structured = StructuredMath(main_theorem="A true claim.", difficulty="easy")

        def fake_repair(candidate, _llm, **_kwargs):
            candidate.lean_code = "theorem demo : True := by trivial"
            candidate.compiled = True
            candidate.status = "verified"
            return candidate

        with patch("lean_formalizer.pipeline.decompose", return_value=structured), \
             patch.object(pipe.memory, "retrieve", return_value={"success": [], "repair": [], "examples": []}), \
             patch("lean_formalizer.pipeline.repair_loop", side_effect=fake_repair), \
             patch("lean_formalizer.pipeline.compile_lean", return_value=(True, "OK")):
            result = pipe.formalize(text="A true claim.", save_to_memory=False)
        self.assertEqual(result.candidates[1].status, "duplicate")
        self.assertEqual(result.candidates[1].lean_code, result.candidates[0].lean_code)

    def test_duplicate_gets_one_distinct_generation_retry(self) -> None:
        class _LLM:
            timeout_seconds = 30

            def __init__(self):
                self.calls = 0

            def reset_usage(self): pass

            def chat(self, *_args, **_kwargs):
                self.calls += 1
                return "theorem demo : True := by decide"

        pipe = FormalizationPipeline(llm=_LLM(), memory_dir=".")
        pipe._stage_trace = []
        pipe._trace_started_at = time.monotonic()
        structured = StructuredMath(main_theorem="A true claim.", difficulty="easy")
        first = Candidate(
            lean_code="theorem demo : True := by trivial",
            statement_only="theorem demo : True",
        )
        second = Candidate(
            lean_code="theorem demo : True := by trivial",
            statement_only="theorem demo : True",
        )
        result = pipe._diversify_duplicate_candidates([first, second], structured)
        self.assertNotEqual(result[0].lean_code, result[1].lean_code)
        self.assertEqual(result[1].source_stage, "diversified")
        self.assertIn("candidate_duplicate_retry", [e["stage"] for e in pipe._stage_trace])


class StyleRelevanceTest(unittest.TestCase):
    def test_off_topic_classics_are_not_style_relevant(self) -> None:
        query = (
            "Let G be a finite abelian group and g in G with order(g) = n = a*b "
            "where gcd(a,b)=1. Then there exist x,y with g = x*y, order(x)=a, "
            "order(y)=b."
        )
        self.assertFalse(
            _style_relevant(
                query,
                "Mean value theorem: there exists c with (f b - f a) = f' c (b - a).",
            )
        )
        self.assertFalse(
            _style_relevant(
                query,
                "Intermediate value theorem: a continuous function attains every value.",
            )
        )

    def test_same_topic_is_style_relevant(self) -> None:
        self.assertTrue(
            _style_relevant(
                "The Euclidean algorithm computes the gcd of two integers.",
                "gcd(a, b) = gcd(b, a % b) for integers (Euclidean algorithm step).",
            )
        )


class MemoryRankingTest(unittest.TestCase):
    def test_tfidf_cosine_uses_standard_library_only(self) -> None:
        scores = tfidf_cosine(
            "finite group order",
            ["finite group order divides", "continuous real function"],
        )
        self.assertEqual(len(scores), 2)
        self.assertGreater(scores[0], scores[1])


class CandidateBudgetTest(unittest.TestCase):
    def test_default_candidate_generation_is_sequential(self) -> None:
        pipe = FormalizationPipeline(llm=object(), memory_dir=".")
        self.assertEqual(pipe.max_parallel_candidates, 1)

    def test_final_timeout_is_reused_without_second_lean_compile(self) -> None:
        class _LLM:
            timeout_seconds = 30

            def reset_usage(self):
                pass

            def chat(self, messages, temperature=0.2, max_tokens=4096):
                return "theorem demo (n : Nat) : n + 0 = n := Nat.add_zero n"

        pipe = FormalizationPipeline(
            llm=_LLM(), memory_dir=".", num_candidates=1,
            max_repair_rounds=3,
        )
        structured = StructuredMath(
            main_theorem="For every natural number n, n + 0 = n.",
            difficulty="easy", domain="general",
        )
        timeout_message = "Lean compilation timed out after 120s"
        with patch("lean_formalizer.pipeline.decompose", return_value=structured), \
             patch.object(pipe.memory, "retrieve", return_value={"success": [], "repair": [], "examples": []}), \
             patch("lean_formalizer.pipeline.compile_lean", return_value=(False, timeout_message)) as compile_mock:
            result = pipe.formalize(
                text="For every natural number n, n + 0 = n.", save_to_memory=False
            )

        self.assertEqual(compile_mock.call_count, 1)
        self.assertEqual(result.candidates[0].status, "compile_timeout")
        self.assertIn(timeout_message, result.verification["raw_message"])
        self.assertTrue(any(
            event.get("stage") == "final_compile_skipped"
            and event.get("reason") == "unchanged_compile_timeout"
            for event in result.stage_trace
        ))

    def test_selected_candidate_uses_original_generation_id_after_ranking(self) -> None:
        class _LLM:
            timeout_seconds = 30

            def reset_usage(self):
                pass

            def chat(self, messages, temperature=0.2, max_tokens=4096):
                if temperature < 0.25:
                    return "theorem candidate_one : 1 = 1 := rfl"
                return "theorem candidate_two : 2 = 2 := rfl"

        pipe = FormalizationPipeline(
            llm=_LLM(), memory_dir=".", num_candidates=2,
            max_repair_rounds=0,
        )
        structured = StructuredMath(
            main_theorem="A small arithmetic identity.", difficulty="easy", domain="general",
        )

        def fake_repair(candidate, _llm, **_kwargs):
            candidate.compiled = candidate.candidate_index == 2
            candidate.status = "verified" if candidate.compiled else "compile_error"
            return candidate

        with patch("lean_formalizer.pipeline.decompose", return_value=structured), \
             patch.object(pipe.memory, "retrieve", return_value={"success": [], "repair": [], "examples": []}), \
             patch("lean_formalizer.pipeline.repair_loop", side_effect=fake_repair), \
             patch("lean_formalizer.pipeline.compile_lean", return_value=(True, "OK")):
            result = pipe.formalize(text="A small arithmetic identity.", save_to_memory=False)

        self.assertEqual(result.selected_candidate, 2)
        self.assertEqual([c.candidate_index for c in result.candidates], [1, 2])

    def test_job_budget_caps_each_llm_call(self) -> None:
        class _LLM:
            timeout_seconds = 600

            def reset_usage(self):
                pass

            def chat(self, messages, temperature=0.2, max_tokens=4096):
                return "theorem demo (n : Nat) : n + 0 = n := Nat.add_zero n"

        pipe = FormalizationPipeline(
            llm=_LLM(), memory_dir=".", num_candidates=3,
            max_repair_rounds=2,
        )
        structured = StructuredMath(
            main_theorem="For every natural number n, n + 0 = n.",
            difficulty="easy", domain="general",
        )
        def fake_repair(candidate, llm, **kwargs):
            candidate.compiled = True
            return candidate
        with patch("lean_formalizer.pipeline._time.monotonic", return_value=100.0), \
             patch("lean_formalizer.pipeline.decompose", return_value=structured), \
             patch.object(pipe.memory, "retrieve", return_value={"success": [], "repair": [], "examples": []}), \
             patch("lean_formalizer.pipeline.repair_loop", side_effect=fake_repair), \
             patch("lean_formalizer.pipeline.compile_lean", return_value=(True, "OK")):
            pipe.formalize(
                text="For every natural number n, n + 0 = n.",
                deadline_monotonic=760.0, save_to_memory=False,
            )
        self.assertLess(pipe._llm_call_budget, 600)
        self.assertEqual(pipe.llm.call_timeout_seconds, pipe._llm_call_budget)

    def test_candidate_generation_is_bounded_and_parallel(self) -> None:
        class _LLM:
            timeout_seconds = 30

            def __init__(self):
                self.stats = {"active": 0, "peak": 0}
                self.lock = threading.Lock()

            def reset_usage(self):
                pass

            def chat(self, messages, temperature=0.2, max_tokens=4096):
                with self.lock:
                    self.stats["active"] += 1
                    self.stats["peak"] = max(self.stats["peak"], self.stats["active"])
                try:
                    time.sleep(0.04)
                    return "theorem demo (n : Nat) : n + 0 = n := Nat.add_zero n"
                finally:
                    with self.lock:
                        self.stats["active"] -= 1

        llm = _LLM()
        pipe = FormalizationPipeline(
            llm=llm,
            memory_dir=".",
            num_candidates=3,
            max_repair_rounds=0,
            max_parallel_candidates=2,
        )
        structured = StructuredMath(
            main_theorem="For every natural number n, n + 0 = n.",
            difficulty="easy",
            domain="general",
        )

        def fake_repair(candidate, llm, **kwargs):
            candidate.compiled = True
            return candidate

        with patch("lean_formalizer.pipeline.decompose", return_value=structured), \
             patch.object(pipe.memory, "retrieve", return_value={"success": [], "repair": [], "examples": []}), \
             patch("lean_formalizer.pipeline.repair_loop", side_effect=fake_repair), \
             patch("lean_formalizer.pipeline.compile_lean", return_value=(True, "OK")):
            result = pipe.formalize(
                text="For every natural number n, n + 0 = n.", save_to_memory=False
            )

        self.assertEqual(len(result.candidates), 3)
        self.assertEqual(llm.stats["peak"], 2)
        elapsed = [float(entry["elapsed_seconds"]) for entry in result.stage_trace]
        self.assertEqual(elapsed, sorted(elapsed))

    def test_candidate_timeout_does_not_abort_remaining_candidates(self) -> None:
        class _LLM:
            timeout_seconds = 30

            def reset_usage(self):
                pass

            def chat(self, messages, temperature=0.2, max_tokens=4096):
                if temperature < 0.25:
                    raise LLMTimeoutError("synthetic candidate timeout")
                return "theorem demo (n : Nat) : n + 0 = n := Nat.add_zero n"

        pipe = FormalizationPipeline(
            llm=_LLM(),
            memory_dir=".",
            num_candidates=3,
            max_repair_rounds=0,
        )
        structured = StructuredMath(
            main_theorem="For every natural number n, n + 0 = n.",
            difficulty="easy",
            domain="general",
        )

        def fake_repair(candidate, llm, **kwargs):
            candidate.compiled = bool(candidate.lean_code)
            return candidate

        with patch("lean_formalizer.pipeline.decompose", return_value=structured), \
             patch.object(pipe.memory, "retrieve", return_value={"success": [], "repair": [], "examples": []}), \
             patch("lean_formalizer.pipeline.repair_loop", side_effect=fake_repair), \
             patch("lean_formalizer.pipeline.compile_lean", return_value=(True, "OK")):
            result = pipe.formalize(
                text="For every natural number n, n + 0 = n.", save_to_memory=False
            )

        self.assertEqual(len(result.candidates), 3)
        timed_out = [c for c in result.candidates if c.source_stage == "generation_timeout"]
        self.assertEqual(len(timed_out), 1)
        self.assertFalse(timed_out[0].compiled)
        self.assertEqual(sum(c.compiled for c in result.candidates), 2)

    def test_requested_candidates_share_repair_budget(self) -> None:
        class _LLM:
            timeout_seconds = 30

            def reset_usage(self):
                pass

            def chat(self, messages, temperature=0.2, max_tokens=4096):
                return "theorem demo (n : Nat) : n + 0 = n := Nat.add_zero n"

        pipe = FormalizationPipeline(
            llm=_LLM(),
            memory_dir=".",
            num_candidates=3,
            max_repair_rounds=4,
        )
        repair_calls: list[dict] = []

        def fake_repair(candidate, llm, **kwargs):
            repair_calls.append(kwargs)
            candidate.compiled = True
            return candidate

        structured = StructuredMath(
            main_theorem="For every natural number n, n + 0 = n.",
            difficulty="easy",
            domain="general",
        )
        with patch("lean_formalizer.pipeline.decompose", return_value=structured), \
             patch.object(pipe.memory, "retrieve", return_value={"success": [], "repair": [], "examples": []}), \
             patch("lean_formalizer.pipeline.repair_loop", side_effect=fake_repair), \
             patch("lean_formalizer.pipeline.compile_lean", return_value=(True, "OK")):
            result = pipe.formalize(text="For every natural number n, n + 0 = n.", save_to_memory=False)

        # Identical generated candidates are intentionally verified once and
        # marked as duplicates, so they cannot waste the full repair budget.
        self.assertEqual(len(repair_calls), 1)
        self.assertEqual([call["candidate_index"] for call in repair_calls], [1])
        self.assertEqual([call["max_rounds"] for call in repair_calls], [4])
        self.assertEqual(len(result.candidates), 3)
        self.assertEqual([c.status for c in result.candidates[1:]], ["duplicate", "duplicate"])
        generated = [
            entry for entry in result.stage_trace
            if entry.get("stage") == "candidate_generated"
        ]
        self.assertEqual([entry["candidate"] for entry in generated], [1, 2, 3])

    def test_web_request_preserves_zero_repair_rounds(self) -> None:
        from lean_formalizer.webapp import _request_int

        self.assertEqual(_request_int({"num_candidates": "3"}, "num_candidates", 2, 1), 3)
        self.assertEqual(_request_int({"max_repair_rounds": "0"}, "max_repair_rounds", 3, 0), 0)


class ProofAuditTest(unittest.TestCase):
    class _AuditLLM:
        timeout_seconds = 30

        def __init__(self, response: str):
            self.response = response

        def chat(self, *_args, **_kwargs):
            return self.response

    def _audit(self, response: str):
        pipe = FormalizationPipeline(
            llm=self._AuditLLM(response), memory_dir=".", num_candidates=1,
            max_repair_rounds=0,
        )
        pipe._stage_trace = []
        pipe._deadline = None
        pipe._llm_call_budget = 30
        return pipe._stage_proof_audit(
            "Theorem T. Proof: submitted step.",
            lean_code="theorem t : True := by trivial",
            compiler_result="Lean compilation succeeded.",
        ), pipe

    def test_sound_audit_preserves_step_order(self) -> None:
        response = (
            '{"overall":"sound","theorem_alignment":"exact_target",'
            '"summary":"All steps follow.","steps":['
            '{"index":1,"source":"Apply lemma.","claim":"P","reason":"lemma",'
            '"status":"valid","explanation":"The lemma applies.","missing":[]},'
            '{"index":2,"source":"Conclude.","claim":"Q","reason":"transitivity",'
            '"status":"valid","explanation":"Follows from P.","missing":[]}'
            ']}'
        )
        audit, pipe = self._audit(response)
        self.assertEqual(audit.overall, "sound")
        self.assertEqual([step.index for step in audit.steps], [1, 2])
        self.assertEqual([step.lean_anchor for step in audit.steps], ["step_1", "step_2"])
        self.assertTrue(any(e.get("stage") == "proof_audit" and e.get("ok") for e in pipe._stage_trace))

    def test_gap_and_invalid_findings_are_kept(self) -> None:
        response = (
            '{"overall":"gap_found","theorem_alignment":"exact_target",'
            '"summary":"Cancellation is not justified.","steps":['
            '{"index":1,"source":"Cancel factors.","claim":"a=b",'
            '"reason":"cancellation","status":"gap",'
            '"explanation":"Nonzero hypotheses are missing.",'
            '"missing":["p != 0"]},'
            '{"index":2,"source":"Therefore.","claim":"False",'
            '"reason":"none","status":"invalid",'
            '"explanation":"The conclusion does not follow.","missing":[]}'
            ']}'
        )
        audit, _ = self._audit(response)
        self.assertEqual(audit.overall, "gap_found")
        self.assertEqual([step.status for step in audit.steps], ["gap", "invalid"])
        self.assertEqual(audit.steps[0].missing, ["p != 0"])

    def test_malformed_audit_is_inconclusive(self) -> None:
        audit, _ = self._audit("```lean\ntheorem not_json : True := by trivial\n```")
        self.assertEqual(audit.overall, "inconclusive")
        self.assertEqual(audit.steps, [])

    def test_audit_cannot_override_compiler_success(self) -> None:
        """The API review is advisory; real Lean compilation decides success."""
        pipe = FormalizationPipeline(memory_dir=".")
        source = Path(__file__).parents[1] / "src" / "lean_formalizer" / "pipeline.py"
        text = source.read_text(encoding="utf-8")
        success_block = text[text.index("lean_success = bool("):text.index("# Final diagnostic when model produced nothing usable")]
        self.assertIn("success_flag = lean_success", success_block)
        self.assertNotIn("success_flag = lean_success and", success_block)

    def test_review_prompt_contains_final_lean_and_compiler_result(self) -> None:
        class _CaptureLLM:
            timeout_seconds = 30

            def __init__(self):
                self.messages = []

            def chat(self, messages, *_args, **_kwargs):
                self.messages = messages
                return '{"overall":"inconclusive","summary":"Needs review.","steps":[]}'

        llm = _CaptureLLM()
        pipe = FormalizationPipeline(llm=llm, memory_dir=".")
        pipe._stage_trace = []
        pipe._deadline = None
        pipe._llm_call_budget = 30
        pipe._stage_proof_audit(
            "Theorem T. Proof: apply h.",
            lean_code="theorem t : True := by trivial",
            compiler_result="error: unsolved goals",
        )
        prompt = llm.messages[-1]["content"]
        self.assertIn("Final Lean code selected by the translator", prompt)
        self.assertIn("theorem t : True := by trivial", prompt)
        self.assertIn("error: unsolved goals", prompt)
        self.assertIn("Local Lean project context", prompt)


class CompilerFeedbackSourceTest(unittest.TestCase):
    def test_build_report_does_not_request_api_failure_explanation(self) -> None:
        class _LLM:
            def chat(self, *_args, **_kwargs):
                raise AssertionError("Compiler report must not call the API")

        report = build_report(
            compiled=False,
            raw_message="unknown identifier `missingLemma`",
            code="theorem demo : True := by exact missingLemma",
            natural_language="A test claim.",
            lean_available=True,
            llm=_LLM(),
        )
        self.assertIn("Unknown name", report.summary)
        self.assertIn("Lean cannot find", report.math_feedback)


class DiagnosticSummaryTest(unittest.TestCase):
    def test_no_code_uses_raw_reason(self) -> None:
        report = build_report(
            compiled=False,
            raw_message="Mock backend cannot formalize new claims.",
            code="",
            natural_language="test claim",
            lean_available=True,
        )
        self.assertIn("Mock backend", report.summary)


class LLMResponseShapeTest(unittest.TestCase):
    def test_content_text_extracts_strings_and_blocks(self) -> None:
        self.assertEqual(_content_text("abc"), "abc")
        self.assertEqual(
            _content_text(
                [{"type": "text", "text": "hello"}, {"type": "output_text", "text": " world"}]
            ),
            "hello\n world",
        )
        self.assertEqual(_content_text(None), "")

    def test_structure_json_parser_handles_prose_and_unclosed_fence(self) -> None:
        raw = (
            "Here is the structure.\n```json\n"
            '{"main_theorem": "The CRT claim", "difficulty": "medium"}'
        )
        self.assertEqual(
            _parse_json_block(raw),
            {"main_theorem": "The CRT claim", "difficulty": "medium"},
        )


class MathlibHintTest(unittest.TestCase):
    def test_statement_matched_hints(self) -> None:
        lagrange = mathlib_hints_for_statement(
            "Let G be a finite group and H a subgroup. |H| divides |G|."
        )
        self.assertIn("Subgroup.card_subgroup_dvd_card", lagrange)
        self.assertIn("Mathlib.GroupTheory.Coset.Card", lagrange)
        banach = mathlib_hints_for_statement(
            "A contraction on a complete metric space has a unique fixed point."
        )
        self.assertIn("ContractingWith.fixedPoint", banach)
        self.assertEqual(mathlib_hints_for_statement("2 + 2 = 4"), "")


class SelfFixTest(unittest.TestCase):
    def test_noncomputable_theorem_becomes_def_for_equiv(self) -> None:
        code = (
            "noncomputable theorem orbit_equiv {G X : Type*} "
            "[Group G] [MulAction G X] (x : X) : "
            "MulAction.orbit G x ≃ G ⧸ MulAction.stabilizer G x"
        )
        fixed, notes = self_fix_lean(code)
        self.assertIn("noncomputable def orbit_equiv", fixed)
        self.assertTrue(notes)


class ConfigAndCompilerTest(unittest.TestCase):
    def test_unwritable_lake_scratch_is_a_compiler_failure(self) -> None:
        from lean_formalizer.lean_verify import _compile_in_lake_project

        project = Path(r"C:\Users\Anson Kam\Lean_Work\mathlib_project")
        if not project.is_dir():
            self.skipTest("local Mathlib project unavailable")
        with patch("pathlib.Path.mkdir", side_effect=PermissionError("denied")):
            ok, message = _compile_in_lake_project(
                "theorem demo : True := by trivial", project, timeout=1
            )
        self.assertFalse(ok)
        self.assertIn("Cannot create Lean compiler scratch directory", message)

    def test_legacy_powershell_env_file_is_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "local_config.env"
            cfg.write_text(
                '$env:LLM_BACKEND = "http"\n'
                r'$env:LEAN_PROJECT = "C:\Lean Work\mathlib_project"' + "\n",
                encoding="utf-8",
            )
            values = load_local_config_env(cfg)
        self.assertEqual(values.get("LLM_BACKEND"), "http")
        self.assertEqual(
            values.get("LEAN_PROJECT"),
            r"C:\Lean Work\mathlib_project",
        )

    def test_lake_project_does_not_use_synthetic_checker(self) -> None:
        # The standalone `lean` shim can be absent while `lake env lean` is
        # still available through elan.  A malformed theorem must therefore
        # fail real compilation rather than pass the synthetic fallback.
        project = Path(r"C:\Users\Anson Kam\Lean_Work\mathlib_project")
        if not project.is_dir():
            self.skipTest("local Mathlib project unavailable")
        old_project = os.environ.get("LEAN_PROJECT")
        old_path = os.environ.get("PATH", "")
        old_elan = os.environ.get("ELAN_HOME")
        try:
            os.environ["LEAN_PROJECT"] = str(project)
            os.environ["PATH"] = ""
            os.environ["ELAN_HOME"] = str(Path.home() / ".elan")
            from lean_formalizer.lean_verify import compile_lean

            ok, message = compile_lean("theorem malformed : True := by exact 1")
        finally:
            os.environ["PATH"] = old_path
            if old_project is None:
                os.environ.pop("LEAN_PROJECT", None)
            else:
                os.environ["LEAN_PROJECT"] = old_project
            if old_elan is None:
                os.environ.pop("ELAN_HOME", None)
            else:
                os.environ["ELAN_HOME"] = old_elan
        self.assertFalse(ok)
        self.assertNotIn("synthetic-ok", message)

    def test_core_nat_identity_uses_standalone_lean(self) -> None:
        from lean_formalizer.lean_verify import compile_lean

        project = Path(r"C:\Users\Anson Kam\Lean_Work\mathlib_project")
        if not project.is_dir():
            self.skipTest("local Mathlib project unavailable")
        with patch.dict(os.environ, {"LEAN_PROJECT": str(project)}, clear=False), \
             patch("lean_formalizer.lean_verify._compile_in_lake_project") as lake:
            ok, message = compile_lean(
                "import Mathlib\n\n"
                "theorem nat_add_zero (n : Nat) : n + 0 = n := Nat.add_zero n",
                timeout=30,
            )
        self.assertTrue(ok, message)
        lake.assert_not_called()

    def test_colliding_mathlib_declaration_is_renamed(self) -> None:
        from lean_formalizer.lean_verify import _rename_colliding_declaration

        code = (
            "import Mathlib\n"
            "theorem LinearMap.injective_iff_surjective {K V : Type*} "
            "[Field K] [AddCommGroup V] [Module K V] [FiniteDimensional K V] "
            "(f : V →ₗ[K] V) : Function.Injective f ↔ Function.Surjective f := by\n"
            "  exact FiniteDimensional.injective_iff_surjective f"
        )
        out, notes = _rename_colliding_declaration(
            code,
            "error: `LinearMap.injective_iff_surjective` has already been declared",
        )
        self.assertIn("theorem LinearMap.injective_iff_surjective_formalized", out)
        self.assertIn("FiniteDimensional.injective_iff_surjective f", out)
        self.assertTrue(notes)


class DoctorBackendTest(unittest.TestCase):
    def test_doctor_uses_configured_backend(self) -> None:
        # Import lazily: webapp keeps server-global state and is otherwise not
        # needed by the unit tests.
        from lean_formalizer.webapp import Handler

        seen: list[str] = []

        class _ReplyLLM:
            def chat(self, *_args, **_kwargs):
                return "pong"

        def fake_llm(backend=None):
            seen.append(backend)
            return _ReplyLLM()

        class _Handler:
            path = "/api/doctor"

        with patch.dict(
            os.environ,
            {
                "LLM_BACKEND": "http",
                "LLM_HTTP_URL": "http://127.0.0.1:15721/v1",
            },
            clear=False,
        ), \
             patch("lean_formalizer.webapp.probe_http_endpoint", return_value=(True, "Connected.")), \
             patch("lean_formalizer.webapp.get_llm", side_effect=fake_llm), \
             patch("lean_formalizer.webapp._json_response") as response:
            Handler.do_GET(_Handler())

        self.assertEqual(seen, ["http"])
        self.assertTrue(response.call_args.args[2]["llm_ok"])

    def test_mathlib_import_search(self) -> None:
        hits = search_mathlib_imports(
            "MulAction.orbitEquivQuotientStabilizer",
            Path(r"C:\Users\Anson Kam\Lean_Work\mathlib_project"),
        )
        self.assertIn("Mathlib.GroupTheory.GroupAction.Quotient", hits)

    def test_unknown_identifier_adds_import(self) -> None:
        code = (
            "theorem orbit_stabilizer_cardinal {G X : Type*} "
            "[Group G] [MulAction G X] (x : X) : "
            "MulAction.orbit G x ≃ G ⧸ MulAction.stabilizer G x := "
            "MulAction.orbitEquivQuotientStabilizer G x"
        )
        fixed, notes = self_fix_lean(
            code,
            error="unknown constant `MulAction.orbitEquivQuotientStabilizer`",
            project=Path(r"C:\Users\Anson Kam\Lean_Work\mathlib_project"),
        )
        self.assertIn("import Mathlib.GroupTheory.GroupAction.Quotient", fixed)
        self.assertTrue(notes)


class BackendSelectionTest(unittest.TestCase):
    def test_http_without_endpoint_falls_back_to_mock(self) -> None:
        from lean_formalizer.webapp import _configured_backend

        with patch.dict(os.environ, {"LLM_BACKEND": "http"}, clear=True):
            self.assertEqual(_configured_backend({"llm_backend": "http"}), "mock")

    def test_http_with_endpoint_is_selected(self) -> None:
        from lean_formalizer.webapp import _configured_backend

        self.assertEqual(
            _configured_backend(
                {
                    "llm_backend": "http",
                    "llm_http_url": "http://127.0.0.1:15721/v1",
                }
            ),
            "http",
        )

    def test_request_can_clear_server_http_endpoint(self) -> None:
        from lean_formalizer.webapp import _configured_backend

        with patch.dict(
            os.environ,
            {"LLM_BACKEND": "http", "LLM_HTTP_URL": "http://server/v1"},
            clear=True,
        ):
            self.assertEqual(
                _configured_backend({"llm_backend": "http", "llm_http_url": ""}),
                "mock",
            )

    def test_unreachable_http_endpoint_uses_mock_status(self) -> None:
        from lean_formalizer.webapp import _backend_status

        status = _backend_status(
            {"llm_backend": "http", "llm_http_url": "http://127.0.0.1:1/v1"}
        )
        self.assertEqual(status["backend"], "mock")
        self.assertFalse(status["http_available"])

    def test_reachable_user_http_endpoint_uses_http_status(self) -> None:
        from lean_formalizer.webapp import _backend_status

        with patch(
            "lean_formalizer.webapp.probe_http_endpoint",
            return_value=(True, "Connected."),
        ) as probe:
            status = _backend_status(
                {
                    "llm_backend": "http",
                    "llm_http_url": "http://127.0.0.1:15721/v1",
                    "llm_http_key": "test-key",
                    "llm_http_model": "test-model",
                }
            )

        self.assertEqual(status["backend"], "http")
        self.assertTrue(status["http_available"])
        self.assertEqual(status["model"], "test-model")
        self.assertEqual(probe.call_args.args, ("http://127.0.0.1:15721/v1", "test-key"))


class HttpEndpointProbeTest(unittest.TestCase):
    def test_probe_does_not_post_chat_completion(self) -> None:
        class _Response:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def getcode(self):
                return self.status

        with patch("lean_formalizer.llm.urllib.request.urlopen", return_value=_Response()) as open_url:
            ok, _ = probe_http_endpoint("http://127.0.0.1:15721/v1", "token")

        self.assertTrue(ok)
        request = open_url.call_args.args[0]
        self.assertEqual(request.get_method(), "GET")
        self.assertTrue(request.full_url.endswith("/v1/models"))
        self.assertEqual(request.get_header("Authorization"), "Bearer token")


class HttpEndpointFieldTest(unittest.TestCase):
    def test_endpoint_field_does_not_rewrite_completion_suffix(self) -> None:
        static_dir = Path(__file__).parents[1] / "src" / "lean_formalizer" / "static"
        app = (static_dir / "app.js").read_text(encoding="utf-8")
        html = (static_dir / "index.html").read_text(encoding="utf-8")
        self.assertNotIn("function normalizeHttpUrl", app)
        self.assertNotIn("normalizeHttpUrl(", app)
        self.assertIn("payload.llm_http_url = url", app)
        self.assertNotIn("/chat/completions auto-added", html)


class LeanFeedbackPanelTest(unittest.TestCase):
    def test_compiler_feedback_panel_uses_live_and_final_diagnostics(self) -> None:
        static_dir = Path(__file__).parents[1] / "src" / "lean_formalizer" / "static"
        app = (static_dir / "app.js").read_text(encoding="utf-8")
        html = (static_dir / "index.html").read_text(encoding="utf-8")
        css = (static_dir / "style.css").read_text(encoding="utf-8")

        self.assertIn('id="lean-feedback"', html)
        self.assertIn('id="lean-feedback-status"', html)
        self.assertIn('id="out-lean-feedback"', html)
        self.assertIn("function renderLiveLeanFeedback(entries)", app)
        self.assertIn('entry?.stage === "lean_compile"', app)
        self.assertIn('/start$/.test(compilation.phase)', app)
        self.assertIn("function finalLeanFeedback(data)", app)
        self.assertIn("verification.raw_message", app)
        self.assertIn("failedCandidate?.error_log", app)
        self.assertIn("function progressEventLabel(entry = {})", app)
        self.assertIn("repair round", app)
        self.assertIn("attempt ${entry.attempt}", app)
        self.assertIn("function clearPreviousResult()", app)
        self.assertIn("clearPreviousResult();", app)
        self.assertIn("function renderProofAudit(audit)", app)
        self.assertIn('id="proof-audit"', html)
        self.assertIn("selectedCandidate", app)
        self.assertIn("final result: candidate", app)
        self.assertIn("Final: candidate", app)
        self.assertIn("function renderCandidateOverview", app)
        self.assertIn("candidate-card", app)
        self.assertIn("showCandidateDetails", app)
        self.assertIn('id="candidate-overview"', html)
        self.assertIn("grid-template-columns: repeat(3", css)
        self.assertIn("Candidate rejected; repair or another candidate may still run", app)
        self.assertIn("job.current_event", app)
        self.assertIn("while (true)", app)
        self.assertIn(".lean-feedback.compiling", css)
        self.assertIn(".lean-feedback.compiled", css)
        self.assertIn(".lean-feedback.failed", css)


class JobLifecycleTest(unittest.TestCase):
    def test_job_stays_running_until_pipeline_returns(self) -> None:
        from lean_formalizer import webapp

        entered = threading.Event()
        release = threading.Event()

        def slow_pipeline(_data, text=None, pdf_path=None, progress_callback=None):
            if progress_callback:
                progress_callback({
                    "stage": "lean_compile",
                    "ok": True,
                    "phase": "start",
                    "candidate": 2,
                    "round": 1,
                })
            entered.set()
            self.assertTrue(release.wait(2))
            return {"success": True, "best_code": "theorem done : True := by trivial"}

        with patch("lean_formalizer.webapp._backend_status", return_value={"backend": "http"}), \
             patch("lean_formalizer.webapp._model_for_backend", return_value="test-model"), \
             patch("lean_formalizer.webapp._job_timeout_seconds", return_value=0.01), \
             patch("lean_formalizer.webapp._llm_timeout_seconds", return_value=0.01), \
             patch("lean_formalizer.webapp._run_pipeline", side_effect=slow_pipeline):
            job_id = webapp._start_job({}, "A test claim.", None)
            self.assertTrue(entered.wait(1))
            time.sleep(0.08)
            with webapp._jobs_lock:
                job = dict(webapp._jobs[job_id])
            self.assertEqual(job["status"], "running")
            self.assertIsNone(job["result"])
            self.assertEqual(job["current_event"]["candidate"], 2)
            release.set()
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                with webapp._jobs_lock:
                    job = dict(webapp._jobs[job_id])
                if job["status"] == "done":
                    break
                time.sleep(0.01)
            self.assertEqual(job["status"], "done")
            self.assertTrue(job["result"]["success"])

        with webapp._jobs_lock:
            webapp._jobs.pop(job_id, None)

    def test_llm_timeout_is_capped_by_job_limit(self) -> None:
        from lean_formalizer.webapp import _llm_timeout_seconds

        self.assertEqual(_llm_timeout_seconds({"llm_timeout": "120"}, 30), 30)


class OfflineMemoryTest(unittest.TestCase):
    def test_nat_add_zero_memory_example_compiles(self) -> None:
        from lean_formalizer.lean_verify import compile_lean

        code = "theorem nat_add_zero (n : Nat) : n + 0 = n := Nat.add_zero n"
        with patch.dict(
            os.environ,
            {"LEAN_PROJECT": r"C:\Users\Anson Kam\Lean_Work\mathlib_project"},
            clear=False,
        ):
            ok, message = compile_lean(code)
        self.assertTrue(ok, message)


if __name__ == "__main__":
    unittest.main()
