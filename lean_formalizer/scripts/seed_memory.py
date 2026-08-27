#!/usr/bin/env python3
"""Seed the memory bank with a few high-quality examples."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from lean_formalizer.memory import MemoryBank


EXAMPLES = [
    {
        "natural_language": "Every natural number is either even or odd.",
        "lean_code": """import Mathlib.Algebra.Ring.Parity
theorem nat_even_or_odd (n : ℕ) : Even n ∨ Odd n := Nat.even_or_odd n""",
        "domain": "number_theory",
        "difficulty": "easy",
        "tags": ["parity", "nat"],
    },
    {
        "natural_language": "The sum of two even natural numbers is even.",
        "lean_code": """import Mathlib.Algebra.Ring.Parity
theorem even_add_even {m n : ℕ} (hm : Even m) (hn : Even n) : Even (m + n) :=
  hm.add hn""",
        "domain": "number_theory",
        "difficulty": "easy",
        "tags": ["parity", "nat"],
    },
    {
        "natural_language": "Zero is even.",
        "lean_code": """import Mathlib.Algebra.Ring.Parity
theorem even_zero : Even (0 : ℕ) := Even.zero""",
        "domain": "number_theory",
        "difficulty": "easy",
        "tags": ["parity"],
    },
    {
        "natural_language": "If n is even then n + 1 is odd.",
        "lean_code": """import Mathlib.Algebra.Ring.Parity
theorem even_add_one_odd {n : ℕ} (h : Even n) : Odd (n + 1) := h.add_one""",
        "domain": "number_theory",
        "difficulty": "easy",
        "tags": ["parity"],
    },
]


def main() -> None:
    bank = MemoryBank(ROOT / "memory")
    for ex in EXAMPLES:
        bank.save_success(**ex)
    print("Seeded", len(EXAMPLES), "success examples.")
    print(bank.stats())


if __name__ == "__main__":
    main()
