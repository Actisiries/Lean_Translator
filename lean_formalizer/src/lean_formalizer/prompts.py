"""Multi-stage prompt templates for NL → Lean 4 formalization.

Design principles
-----------------
1. Statement first, then proof (never invent the claim while proving).
2. Show parallel NL statement + NL proof + Lean when examples exist.
3. Prefer Mathlib names, imports, and tactic-mode proofs.
4. Never output placeholders, refusals, or `sorry`.
5. If the full claim is too large, formalize a precise CORE theorem that still
   captures the intended mathematics — never a trivial True example.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# System roles
# ---------------------------------------------------------------------------

SYSTEM_MATH = (
    "You are an expert mathematician and Lean 4 / Mathlib formalization specialist. "
    "Be precise, conservative, and idiomatic. Prefer existing Mathlib lemmas to ad-hoc proofs. "
    "Always formalize the USER'S claim (or a clearly named core of that same claim). "
    "Never substitute a different classical theorem that merely lives in the same area "
    "(e.g. Rolle/MVT instead of intermediate-value; Fermat instead of Euclid's lemma; "
    "Banach fixed-point instead of a plain continuous fixed-point on [0,1]). "
    "Never refuse by writing placeholders, `example : True`, or `sorry`."
)

SYSTEM_LEAN_ONLY = (
    "You output only valid Lean 4 code (optionally inside a single ```lean fence). "
    "No prose outside Lean comments.\n"
    "Do NOT include reasoning, planning, or analysis; the response should start "
    "with ```lean, `import`, `theorem`, or `lemma`.\n"
    "FIDELITY (highest priority):\n"
    "- The theorem/lemma type MUST match the user's mathematical claim.\n"
    "- Do NOT replace it with a neighboring famous theorem from memory or examples.\n"
    "- Memory/examples are for STYLE and import patterns only, not for changing the claim.\n"
    "- If the statement stage already fixed a declaration, KEEP that name and type;\n"
    "  only complete or minimally fix the proof/imports.\n"
    "- The theorem name and type MUST be identical to the declaration/claim supplied;\n"
    "  never prove a neighboring theorem just because it looks similar\n"
    "  (Rolle/MVT != intermediate-value, Fermat != Euclid's lemma, etc.).\n"
    "LOCAL MATHLIB HINTS (use exact names when they fit):\n"
    "- Lagrange: `Subgroup.card_subgroup_dvd_card` in `Mathlib.GroupTheory.Coset.Card`\n"
    "- Group first isomorphism theorem: `QuotientGroup.quotientKerEquivRange φ`\n"
    "  (not `φ.quotientKerEquivRange`) in `Mathlib.GroupTheory.QuotientGroup.Basic`\n"
    "- Banach fixed point: `ContractingWith.fixedPoint` / `fixedPoint_isFixedPt` /\n"
    "  `fixedPoint_unique` in `Mathlib.Topology.MetricSpace.Contracting`\n"
    "FORBIDDEN outputs (never produce these):\n"
    "- `sorry`\n"
    "- `example : True` or `True := trivial` / `True := by trivial` as a refusal\n"
    '- Comments like "No declaration provided" or "please supply the statement"\n'
    "- Empty files or proof-free declarations when a proof is required\n"
    "- Invented lemma names that are not in Mathlib\n"
    "- Any unresolved placeholder/metavariable: a standalone `?`, `?_`, `?m_1`, "
    "or pseudo-syntax such as `? k in Finset.range n`\n"
    "REQUIRED:\n"
    "- At least one real `theorem` or `lemma` whose type matches the user's mathematics\n"
    "- Prefer a fine-grained import that contains the needed declarations (for example\n"
    "  `Mathlib.GroupTheory.QuotientGroup.Basic`); use `import Mathlib` only when the\n"
    "  local module is genuinely unknown. Every chosen import must exist in this project.\n"
    "- NEVER output the umbrella line `import Mathlib`; it is disabled by the local\n"
    "  verifier because loading the whole library exceeds the machine time budget.\n"
    "- A full proof that closes all goals (prefer `exact` / `apply` of a real library lemma)\n"
    "- Every type and binder must be concrete. Use `Nat`/`ℕ`, never `(n : ?)`.\n"
    "- For finite sums, use `(Finset.range n).sum (fun k => 2 * k + 1)` when suitable. "
    "Never use prose or pseudo-Lean such as `? k in Finset.range n`.\n"
    "If the full claim is too large, formalize a precise CORE of the SAME claim —\n"
    "still a real theorem with a real proof, never a True/sorry stub or a different theorem."
)

SYSTEM_JSON = (
    "You output only a single JSON object (optionally inside a ```json fence). "
    "No commentary."
)

# ---------------------------------------------------------------------------
# Step 2 – Structure
# ---------------------------------------------------------------------------

STRUCTURE_PROMPT = """\
Decompose the following mathematical text for formalization in Lean 4 + Mathlib.

Domain hint: {domain}

Text:
\"\"\"
{text}
\"\"\"

Return a JSON object with exactly these keys:
{{
  "definitions": ["precise ingredients needed in Lean, one idea each"],
  "main_theorem": "ONE precise claim with quantifiers, hypotheses, and conclusion in words",
  "lemmas": ["useful intermediate claims, if any"],
  "proof_sketch": ["atomic formalizable step 1", "step 2", "..."],
  "domain": "number_theory | algebra | analysis | topology | logic | set_theory | combinatorics | geometry | probability | general",
  "difficulty": "easy | medium | hard | graduate"
}}

Rules:
- main_theorem: ONE precise claim with types/quantifiers in words.
- This is a mathematical PLAN, not Lean code. Do not use partial Lean syntax,
  notation fragments, or metavariables such as `?`, `?_`, `(n : ?)`, or
  `? k in Finset.range n`. Describe a finite sum in words instead.
- proof_sketch: 3–8 atomic steps (each a few tactics or one library lemma).
- Famous theorems: put the standard name in main_theorem and sketch the usual textbook argument.
- If the claim is very large, still name the full theorem, but make proof_sketch focus on a
  CORE formalizable path (existence, uniqueness, or a standard special case).
- Prefer wording aligned with Mathlib concepts when applicable
  (MetricSpace, CompleteSpace, ContractingWith, Subalgebra, ContinuousMap, Ideal, …).

Respond with ONLY the JSON object.
"""

# ---------------------------------------------------------------------------
# Proof review (after final Lean compilation)
# ---------------------------------------------------------------------------

PROOF_AUDIT_PROMPT = r"""\
Review the submitted HUMAN mathematical proof against the final Lean
formalization and its Lean compiler result. Your job is to identify whether
the source-proof steps are represented, unsupported, or contradicted by the
formalization. Do not silently replace the submitted proof with another proof.

Submitted theorem and proof:
{source}

Final Lean code selected by the translator:
```lean
{lean_code}
```

Lean compiler result:
{compiler_result}

Local Lean project context:
{project_context}

Return exactly one JSON object:
{{
  "overall": "sound | gap_found | invalid | ambiguous | inconclusive",
  "theorem_alignment": "exact_target",
  "summary": "short, precise assessment",
  "steps": [
    {{
      "index": 1,
      "source": "quoted or closely paraphrased submitted step",
      "claim": "what this step concludes",
      "reason": "stated justification",
      "status": "valid | gap | invalid | ambiguous | inconclusive",
      "explanation": "why this step does or does not follow",
      "missing": ["specific lemma, definition, or hypothesis needed"]
    }}
  ]
}}

Rules:
- Audit every meaningful submitted proof step in its original order.
- Use the final Lean code and compiler result as evidence. If compilation
  failed, say which source step is not yet formalized; do not call the source
  proof false merely because generated Lean code failed to compile.
- If compilation succeeded, compare the Lean theorem/proof to the submitted
  theorem/proof and flag missing hypotheses or a changed theorem target.
- Mark `gap` for an unstated lemma, missing assumption, or unjustified inference.
- Mark `invalid` only if the assertion does not follow or is false under the stated hypotheses.
- Do not call a step valid merely because a different proof of the theorem exists.
- Do not invent assumptions, axioms, or a weaker theorem to make it work.
- The response is a review, not Lean code or a repaired proof. Do not claim a
  step is formally verified unless the supplied Lean compiler result succeeded
  and the supplied Lean code actually represents that step.
"""

# ---------------------------------------------------------------------------
# Step 4a – Understand
# ---------------------------------------------------------------------------

UNDERSTAND_PROMPT = """\
Rephrase the claim below in precise English as a guide for Lean 4 + Mathlib formalization.

Statement:
{statement}

Definitions / context:
{definitions}

In under 220 words, cover:
1. Objects and types (spaces, rings, functions, …) and quantifiers (∀ / ∃).
2. Hypotheses that become typeclasses or explicit assumptions
   (CompleteSpace, Nonempty, Field, CompactSpace, IsAlgebraHom, …).
3. Exact conclusion (existence, uniqueness, equality, density, inequality, …).
4. Closest Mathlib strategy:
   - reuse a named library theorem if one exists (give likely name/module if known);
   - otherwise outline constructions (sequences, filters, ideals, lattices, …).
5. If the full claim is very large, name a CORE sub-claim that still captures the intent
   and is realistic to formalize in one snippet — still a real theorem, never a placeholder.

Plain text only. Do not write Lean yet.
Never say you cannot formalize it. Never suggest example : True or sorry.
"""

# ---------------------------------------------------------------------------
# Step 4b – Statement only
# ---------------------------------------------------------------------------

STATEMENT_PROMPT = """\
Formalize the claim as a Lean 4 theorem/lemma *declaration only* (signature + type; no proof).

Hard requirements:
- One real `theorem` or `lemma` matching the claim (NEVER `example : True` as a refusal).
- Mathlib typeclass style (`[MetricSpace α]`, `[Ring R]`, `[CompactSpace K]`, …).
- Use one or more fine-grained imports that exist in the configured local
  Mathlib project. Never output the umbrella line `import Mathlib`, because it
  is disabled by the local verifier and can exceed the compile budget. If the
  required module is uncertain, omit imports rather than inventing a module;
  the local import-repair stage will add a confirmed module.
- Mathlib Unicode when natural (ℕ, ℝ, ≤, ∃, →, …).
- Do NOT write `:=`, `by`, or `sorry`.
- Do NOT write placeholders or comments asking for a statement.
- Never emit a standalone `?`, `?_`, `(x : ?)`, or pseudo-sum notation such
  as `? k in Finset.range n`. Every binder type must be concrete.
- For a finite sum, prefer `(Finset.range n).sum (fun k => expression)`;
  it is valid Lean and does not depend on transmitting the `∑` glyph.
- For Banach fixed-point claims, use `ContractingWith K f` with `K : NNReal`;
  do NOT expand the contraction hypothesis into raw `dist` inequalities.

If the informal claim is huge (full Stone–Weierstrass, full Galois correspondence, …):
- Put the famous name in a short `/- ... -/` comment.
- Formalize the strongest precise CORE statement that is still faithful
  (e.g. density for a standard subalgebra instance; fixed point for ContractingWith),
  not a trivial True example.

Natural-language statement:
{statement}

Relevant definitions:
{definitions}

Parallel examples (NL + NL proof + Lean). Imitate *style, imports, and typeclass
patterns ONLY; never copy a theorem that differs from this claim:
{examples}

Output: Lean snippet with imports + theorem/lemma signature only.
"""

STATEMENT_REPAIR_PROMPT = """\
Repair this incomplete or malformed Lean 4 theorem declaration. Output ONLY a
complete declaration with imports and the SAME theorem name and mathematical
type. Do not add a proof body. Close every binder, implication, conjunction,
existential, `let`, and structure. The declaration must end at a complete type,
not halfway through a proposition. Never use `sorry`, `axiom`, `?`, or the
umbrella import `import Mathlib`; use fine-grained imports from the local module
index.

Natural-language target:
{statement}

Current declaration:
```lean
{declaration}
```

Compiler diagnostic:
```
{error}
```

Local module index:
{local_modules}

Output ONLY the repaired Lean declaration.
"""

# ---------------------------------------------------------------------------
# Step 4c – Proof
# ---------------------------------------------------------------------------

PROOF_PROMPT = """\
Complete this Lean 4 declaration with a full proof. Keep the SAME name and type.

Rules: no sorry; no unresolved `?`/`?_` holes; do not switch theorems; keep the
exact name/type from the declaration; prefer Mathlib `exact`/`apply`; use `by`
tactics. Do not turn a finite sum into pseudo-syntax such as `? k in ...`.

Independent-candidate instruction: {strategy}

```lean
{declaration}
```

Sketch: {sketch}
Examples (style only; ignore any that prove a different claim): {examples}
Local Lean facts checked in the configured project (use only when they fit):
{local_context}

Output ONLY a ```lean block: imports + same statement + proof body.
"""

DIVERSIFY_PROOF_PROMPT = """\
The candidate below is byte-for-byte identical to an earlier candidate. Produce
ONE independent Lean 4 proof candidate for the same theorem. Keep the exact
theorem name and type, but use a materially different proof route when possible
(for example, a confirmed library theorem instead of a manual construction, or
different confirmed lemmas/tactics). Do not merely reformat or echo the code.

No `sorry`, no unresolved `?`/`?_` holes, no invented APIs, and no prose.

Candidate to replace:
```lean
{code}
```

The source theorem in words: {statement}

Output ONLY a complete ```lean block.
"""

# ---------------------------------------------------------------------------
# Fast path – elementary / single-shot
# ---------------------------------------------------------------------------

ONESHOT_PROMPT = """\
Formalize the following mathematical claim as ONE complete Lean 4 + Mathlib snippet
(imports + theorem/lemma + full proof).

Hard rules:
- Real `theorem` or `lemma` matching the claim (NEVER refuse with `example : True`).
- No `sorry`, no placeholders, no "declaration not provided".
- No standalone `?`, `?_`, `(n : ?)`, or pseudo-Lean such as
  `? k in Finset.range n`. A `?` character is not a missing Lean symbol/type.
- For finite sums, prefer `(Finset.range n).sum (fun k => expression)` over
  handwritten Unicode sum notation when it fits the claim.
- Prefer `exact` / `apply` of an existing Mathlib lemma when the claim is standard.
- Tactic proofs must close all goals.
- Famous claims: use standard Mathlib shapes when known
  (ContractingWith K f with K : NNReal for Banach; Nat.even_or_odd for parity;
   Subalgebra / ContinuousMap material for Stone–Weierstrass cores; etc.).
- For Banach fixed-point claims, keep the contraction hypothesis as
  `ContractingWith K f`; do NOT expand it into raw `dist` inequalities.
- If the FULL claim is too large for one snippet, formalize a precise CORE theorem
  that still captures the intended mathematics (state the core clearly in the theorem
  name/type). Never answer with a trivial True example with sorry.

Claim:
{statement}

Independent-candidate instruction: {strategy}

Sketch (optional):
{sketch}

Examples (style only; never let them replace the claim below):
{examples}

Output ONLY the final Lean code block (imports + real theorem/lemma + proof,
no reasoning/commentary, no sorry).
"""

# ---------------------------------------------------------------------------
# Step 4d – Self-critique
# ---------------------------------------------------------------------------

CRITIQUE_PROMPT = """\
Improve the following Lean 4 code for Mathlib idiomaticity and correctness.

Checklist:
- Must remain a real formalization of a mathematical claim
  (never `example : True` with sorry, never "no declaration provided").
- Remove unused imports; keep required Mathlib imports.
- Prefer `exact` / `apply` of library lemmas over verbose re-proofs.
- Fix obvious typeclass or coercion issues.
- Keep the same mathematical meaning.
- Keep the same theorem name and type supplied by the user; do not replace the claim.
- Single coherent snippet pasteable into a Mathlib project.

Code:
```lean
{code}
```

Return the full improved Lean code only (no sorry or unresolved `?`/`?_` placeholders).
"""

# ---------------------------------------------------------------------------
# Complete a bare declaration (proof stage empty / no body)
# ---------------------------------------------------------------------------

COMPLETE_PROOF_PROMPT = """\
This declaration has no proof. Add `:= by ...` (or a term).
Keep the EXACT same name and type. Do NOT substitute a neighboring theorem.
No sorry, empty reply, or unresolved `?`/`?_` placeholder.

```lean
{declaration}
```

Claim: {statement}
Sketch: {sketch}

Output ONLY the final ```lean block with imports + same statement + proof.
No reasoning/commentary before or after.
"""

IMPORT_REPAIR_PROMPT = """\
Replace only the import policy of this Lean 4 candidate while preserving the
EXACT theorem name, type, and proof. The local verifier rejects the umbrella
line `import Mathlib` because it loads the entire library and times out.

Use one or more fine-grained imports that exist in the configured local Mathlib
project. Do not guess a module path: use the local module index below. If the
proof uses only Lean core, remove the Mathlib import. Do not change the theorem
statement or proof unless a namespace qualification is required by the chosen
import. No `import Mathlib`, no `sorry`, no placeholders, and no prose.

Local module index:
{local_modules}

Candidate:
```lean
{code}
```

Output ONLY the complete Lean code block.
"""

# ---------------------------------------------------------------------------
# Step 6 – Repair
# ---------------------------------------------------------------------------

REPAIR_PROMPT = """\
Fix this Lean 4 so it compiles. Keep the SAME theorem name and type.
Never switch to a different mathematical theorem, even if a hint/example is similar.
No sorry, empty reply, or unresolved `?`/`?_` placeholder. Replace malformed
pseudo-sums such as `? k in Finset.range n` with valid Lean, preferably
`(Finset.range n).sum (fun k => ...)`.

Code:
```lean
{code}
```

Error:
```
{error}
```

Hints: {repairs} (style-only; ignore claims that differ from this one)

If the error is "no proof body", add `:= by ...` using Mathlib lemmas.
Output ONLY the final ```lean block: imports + statement + proof.
No reasoning/commentary before or after.
"""

TIMEOUT_RECOVERY_PROMPT = """\
Lean spent the entire typechecking allowance on this code and returned no
diagnostic. Produce a LOWER-ELABORATION replacement that keeps the EXACT same
theorem name and type. This is a timeout recovery, not permission to weaken or
replace the mathematical claim.

Prefer one exact, stable Mathlib theorem or a small chain of explicitly typed
`have` statements. Avoid broad search or expensive automation (`aesop`,
`exact?`, `simp?`), large implicit inference chains, and re-proving library
theorems. Never use the umbrella `import Mathlib`; use the smallest confirmed
local Mathlib import(s) required by the code. No `sorry`, axioms, empty replies,
or prose.

Code that timed out:
```lean
{code}
```

Compiler result:
```
{error}
```

Output ONLY the complete ```lean block: imports + unchanged statement +
simplified proof.
"""

# ---------------------------------------------------------------------------
# Step 7 – Rank
# ---------------------------------------------------------------------------

RANK_PROMPT = """\
Several Lean 4 candidates are available. Rank best → worst by:
1. Real formalization of the intended claim (not a placeholder)
2. No `sorry` / no trivial `True` examples
3. Correct Mathlib style and lemma use
4. Readable tactic proof
5. Shortness when quality is equal

Candidates:
{candidates}

Reply with ONLY a JSON array of 0-based indices in preferred order, e.g. [2, 0, 1].
"""

# ---------------------------------------------------------------------------
# Optional: NL proof from Lean (memory)
# ---------------------------------------------------------------------------

NL_PROOF_FROM_LEAN_PROMPT = """\
Write a short formal-style natural-language proof that mirrors the following Lean 4 code.
Use the same logical order as the tactics / lemma applications.
Do not mention Lean tactic names unless needed for clarity.
Start with "Proof." and stay under 250 words.

Theorem statement (NL):
{statement}

Lean code:
```lean
{code}
```

Natural-language proof:
"""

# ---------------------------------------------------------------------------
# Memory quality gate
# ---------------------------------------------------------------------------

MEMORY_QUALITY_PROMPT = """\
Judge whether this formalization is high enough quality to store as a training example.

Statement:
{statement}

Lean:
```lean
{code}
```

Reject if: contains sorry; is example : True / trivial True; missing the claim;
invented axioms; clearly wrong mathematics; or "no declaration provided".

Answer with JSON only:
{{
  "accept": true/false,
  "reason": "short reason"
}}
"""
