"""Compact local-Mathlib hints derived from the pinned mathlib_project."""

from __future__ import annotations

import re
from typing import Any


_MATHLIB_HINTS: list[dict[str, Any]] = [
    {
        "keys": ("lagrange", "subgroup", "cardinality", "divides", "card"),
        "imports": ("Mathlib.GroupTheory.Coset.Card",),
        "lemmas": ("Subgroup.card_subgroup_dvd_card",),
    },
    {
        "keys": ("orbit", "stabilizer", "group action", "mulaction"),
        "imports": ("Mathlib.GroupTheory.GroupAction.Quotient",),
        "lemmas": (
            "MulAction.card_orbit_mul_card_stabilizer_eq_card_group",
            "MulAction.orbitEquivQuotientStabilizer",
        ),
    },
    {
        "keys": ("contraction", "contracting", "fixed point", "banach"),
        "imports": ("Mathlib.Topology.MetricSpace.Contracting",),
        "lemmas": (
            "ContractingWith.fixedPoint",
            "ContractingWith.fixedPoint_isFixedPt",
            "ContractingWith.fixedPoint_unique",
        ),
    },
    {
        "keys": ("intermediate value", "continuous", "ivt"),
        "imports": ("Mathlib.Topology.Order.IntermediateValue",),
        "lemmas": ("intermediate_value_Icc",),
    },
    {
        "keys": ("even", "odd", "parity", "natural number"),
        "imports": ("Mathlib.Algebra.Ring.Parity",),
        "lemmas": ("Nat.even_or_odd", "Even.add"),
    },
    {
        "keys": ("gcd", "greatest common divisor", "bezout", "euclidean"),
        "imports": ("Mathlib.Data.Nat.GCD.Basic",),
        "lemmas": ("Nat.gcd_dvd_left", "Nat.gcd_dvd_right", "Nat.dvd_gcd"),
    },
]


def mathlib_hints_for_statement(statement: str) -> str:
    """Return a short list of exact Mathlib imports/lemmas for the claim."""
    if not statement:
        return ""
    lower = (statement or "").lower()
    selected = [
        h for h in _MATHLIB_HINTS if any(k in lower for k in h["keys"])
    ]
    if not selected:
        return ""
    lines = ["Local Mathlib hints (only if they fit the exact claim):"]
    for hint in selected:
        for imp in hint["imports"]:
            lines.append(f"- import {imp}")
        for lemma in hint["lemmas"]:
            lines.append(f"- {lemma}")
    return "\n".join(lines)
