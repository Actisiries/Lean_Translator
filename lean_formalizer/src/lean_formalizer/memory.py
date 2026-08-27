"""Memory bank: translator successes vs curated example library.

- success.jsonl  : only formalizations verified by *this* translator (tag auto)
- examples.jsonl : curated NL–Lean pairs for few-shot retrieval (not claimed as
                   translator outputs)
- repairs.jsonl  : failed→fixed pairs from the repair loop
"""

from __future__ import annotations

import json
import math
import re
import uuid
from collections import Counter
from pathlib import Path
from typing import Any, Optional

from .models import MemoryEntry
from .lean_verify import normalize_lean_imports


_TOKEN_RE = re.compile(r"[a-zA-Z0-9_]+|[^\s]")
_SORRY_RE = re.compile(r"\bsorry\b")
_LEAN_CODE_RE = re.compile(
    r"\b(theorem|lemma|example|def|instance)\b", re.IGNORECASE
)
_REAL_LEAN_CODE_RE = re.compile(r"\b(theorem|lemma|def|instance)\b", re.IGNORECASE)
_TRIVIAL_TRUE_RE = re.compile(
    r"\b(?:theorem|lemma|example)\b[^=]*:\s*True\b(?=[^:=]*(?::=|\bby\b|$))",
    re.IGNORECASE,
)
_MEMORY_STOPWORDS = frozenset(
    """
    a an the and or of in on with for every any exists there is are be to
    by via if then let such that this these those its it as from at between
    we can has have show shows prove proof theorem lemma example follows
    following using used use one two three when where which whose not no
    also both each all some so b c d e f g h i j k l m n p q r s t u v w x y z
    """.split()
)


def _tokenize(text: str) -> list[str]:
    return [t.lower() for t in _TOKEN_RE.findall(text or "")]


def _normalize_nl(text: str) -> str:
    t = (text or "").strip().lower()
    t = re.sub(r"\s+", " ", t)
    t = re.sub(r"[^\w\s]", "", t)
    return t


def _style_relevant(query: str, entry_nl: str) -> bool:
    """Keep only memory entries that share real mathematical vocabulary."""
    qn = _normalize_nl(query or "")
    en = _normalize_nl(entry_nl or "")
    if not qn or not en:
        return False
    if qn == en or qn in en or en in qn:
        return True
    def _words(tokens: list[str]) -> set[str]:
        return {
            t
            for t in tokens
            if re.fullmatch(r"[a-zA-Z0-9_]+", t) and t not in _MEMORY_STOPWORDS
        }

    qt = _words(_tokenize(query))
    et = _words(_tokenize(entry_nl))
    common = qt & et
    if common & {
        "abelian",
        "bezout",
        "gcd",
        "order",
        "cyclic",
        "coprime",
        "divisor",
        "subgroup",
        "eigenvalue",
        "homeomorphism",
        "isomorphism",
    }:
        return True
    return len(common) >= 2


class MemoryBank:
    def __init__(self, directory: str | Path = "./memory"):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.success_path = self.dir / "success.jsonl"
        self.examples_path = self.dir / "examples.jsonl"
        self.repair_path = self.dir / "repairs.jsonl"
        self._success: list[MemoryEntry] = []
        self._examples: list[MemoryEntry] = []
        self._repairs: list[MemoryEntry] = []
        self._load()

    def _load(self) -> None:
        self._success = self._read_jsonl(self.success_path)
        self._examples = self._read_jsonl(self.examples_path)
        self._repairs = self._read_jsonl(self.repair_path)
        self._success = [
            e for e in self._success
            if self.is_high_quality(e.natural_language, e.lean_code)[0]
        ]
        self._examples = [
            e for e in self._examples
            if self.is_high_quality(e.natural_language, e.lean_code)[0]
        ]

    @staticmethod
    def _read_jsonl(path: Path) -> list[MemoryEntry]:
        if not path.exists():
            return []
        entries: list[MemoryEntry] = []
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = MemoryEntry.from_dict(json.loads(line))
                except Exception:
                    continue
                entry.lean_code = normalize_lean_imports(entry.lean_code)
                entry.wrong_code = normalize_lean_imports(entry.wrong_code)
                entry.corrected_code = normalize_lean_imports(entry.corrected_code)
                entries.append(entry)
        return entries

    def _append(self, path: Path, entry: MemoryEntry) -> None:
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry.to_dict(), ensure_ascii=False) + "\n")

    def _rewrite_success(self) -> None:
        with self.success_path.open("w", encoding="utf-8") as f:
            for e in self._success:
                f.write(json.dumps(e.to_dict(), ensure_ascii=False) + "\n")

    @staticmethod
    def is_high_quality(
        natural_language: str,
        lean_code: str,
        *,
        max_sorry: int = 0,
    ) -> tuple[bool, str]:
        nl = (natural_language or "").strip()
        code = (lean_code or "").strip()
        if len(nl) < 8:
            return False, "statement too short"
        if len(code) < 10:
            return False, "lean code too short"
        if not _REAL_LEAN_CODE_RE.search(code):
            return False, "no theorem/lemma/def/instance found"
        sorry_count = len(_SORRY_RE.findall(code))
        if sorry_count > max_sorry:
            return False, f"too many sorry ({sorry_count})"
        if re.search(r"True\s*:=\s*(by\s+)?trivial", code, re.IGNORECASE):
            return False, "placeholder trivial theorem"
        if "True := True" in code:
            return False, "placeholder trivial theorem"
        if _TRIVIAL_TRUE_RE.search(code):
            return False, "placeholder theorem proving True"
        if re.search(r"\btheorem\s+placeholder\b", code, re.IGNORECASE):
            return False, "placeholder-named theorem"
        return True, "ok"

    def find_duplicate(self, natural_language: str) -> Optional[MemoryEntry]:
        key = _normalize_nl(natural_language)
        if not key:
            return None
        for e in self._success:
            if _normalize_nl(e.natural_language) == key:
                return e
        return None

    def save_success(
        self,
        natural_language: str,
        lean_code: str,
        domain: str = "general",
        difficulty: str = "medium",
        tags: Optional[list[str]] = None,
        metadata: Optional[dict[str, Any]] = None,
        natural_language_proof: str = "",
        *,
        enforce_quality: bool = True,
        allow_sorry: int = 0,
        replace_duplicate: bool = True,
    ) -> Optional[MemoryEntry]:
        """Save a *translator-verified* formalization only."""
        lean_code = normalize_lean_imports(lean_code)
        if enforce_quality:
            ok, reason = self.is_high_quality(
                natural_language, lean_code, max_sorry=allow_sorry
            )
            if not ok:
                return None

        tags = list(tags or [])
        if "auto" not in tags:
            tags.append("auto")
        meta = dict(metadata or {})
        meta.setdefault("role", "translator_success")
        meta.setdefault("source", "translator")

        dup = self.find_duplicate(natural_language)
        if dup is not None:
            if not replace_duplicate:
                return dup
            new_proof = (natural_language_proof or "").strip()
            old_proof = (dup.natural_language_proof or "").strip()
            if len(new_proof) >= len(old_proof):
                dup.natural_language_proof = new_proof or old_proof
            if len(lean_code) >= len(dup.lean_code):
                dup.lean_code = lean_code
            if domain and domain != "general":
                dup.domain = domain
            if difficulty:
                dup.difficulty = difficulty
            dup.tags = sorted(set((dup.tags or []) + tags))
            m = dict(dup.metadata or {})
            m.update(meta)
            dup.metadata = m
            self._rewrite_success()
            return dup

        entry = MemoryEntry(
            id=str(uuid.uuid4()),
            natural_language=natural_language.strip(),
            lean_code=lean_code.strip(),
            natural_language_proof=(natural_language_proof or "").strip(),
            domain=domain or "general",
            difficulty=difficulty or "medium",
            success=True,
            tags=tags,
            metadata=meta,
        )
        self._success.append(entry)
        self._append(self.success_path, entry)
        return entry

    def save_repair(
        self,
        natural_language: str,
        wrong_code: str,
        error_message: str,
        corrected_code: str,
        domain: str = "general",
        tags: Optional[list[str]] = None,
    ) -> Optional[MemoryEntry]:
        if not (wrong_code and corrected_code and error_message):
            return None
        wrong_code = normalize_lean_imports(wrong_code)
        corrected_code = normalize_lean_imports(corrected_code)
        if wrong_code.strip() == corrected_code.strip():
            return None
        entry = MemoryEntry(
            id=str(uuid.uuid4()),
            natural_language=natural_language,
            lean_code=corrected_code,
            domain=domain,
            success=False,
            wrong_code=wrong_code,
            error_message=error_message[:2000],
            corrected_code=corrected_code,
            tags=list(tags or ["repair"]),
        )
        self._repairs.append(entry)
        self._append(self.repair_path, entry)
        return entry

    def retrieve(
        self,
        query: str,
        domain: Optional[str] = None,
        top_k_success: int = 5,
        top_k_repair: int = 2,
        *,
        include_examples: bool = True,
        strict_style: bool = False,
    ) -> dict[str, list[MemoryEntry]]:
        """Retrieve few-shot material: examples + translator successes, and repairs."""
        pool: list[MemoryEntry] = list(self._success)
        if include_examples:
            pool = list(self._examples) + pool
        # Require a modest TF-IDF floor so garbage queries do not pick random theorems
        ranked = self._rank(query, pool, domain=domain, min_score=0.05)
        repairs = self._rank(
            query, self._repairs, domain=domain, min_score=0.04
        )
        if strict_style:
            ranked = [
                e for e in ranked if _style_relevant(query, e.natural_language)
            ]
            repairs = [
                e for e in repairs if _style_relevant(query, e.natural_language)
            ]
        return {
            "success": ranked[:top_k_success],
            "repair": repairs[:top_k_repair],
            "examples": ranked[:top_k_success],
        }

    def _rank(
        self,
        query: str,
        entries: list[MemoryEntry],
        domain: Optional[str] = None,
        *,
        min_score: float = 0.0,
    ) -> list[MemoryEntry]:
        if not entries:
            return []
        docs = [
            " ".join(
                [
                    e.natural_language or "",
                    e.natural_language_proof or "",
                    e.lean_code or "",
                    e.domain or "",
                    " ".join(e.tags or []),
                ]
            )
            for e in entries
        ]
        scores = tfidf_cosine(query, docs)
        if domain and domain != "general":
            for i, e in enumerate(entries):
                if e.domain == domain:
                    scores[i] += 0.08
                if domain in (e.tags or []):
                    scores[i] += 0.03
        for i, e in enumerate(entries):
            if (e.natural_language_proof or "").strip():
                scores[i] += 0.02
            # slight preference for real translator successes when scores close
            if "auto" in (e.tags or []):
                scores[i] += 0.01
            # exact / near-exact NL match boost
            if _normalize_nl(query) and _normalize_nl(query) == _normalize_nl(
                e.natural_language or ""
            ):
                scores[i] += 0.5
            elif _normalize_nl(query) and _normalize_nl(query) in _normalize_nl(
                e.natural_language or ""
            ):
                scores[i] += 0.15
        order = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)
        ranked = []
        for i in order:
            if float(scores[i]) < min_score:
                continue
            ranked.append(entries[i])
        return ranked

    def stats(self) -> dict[str, Any]:
        with_proof = sum(
            1 for e in self._success if (e.natural_language_proof or "").strip()
        )
        ex_proof = sum(
            1 for e in self._examples if (e.natural_language_proof or "").strip()
        )

        def _domains(entries: list[MemoryEntry]) -> dict[str, int]:
            c: Counter[str] = Counter((e.domain or "general") for e in entries)
            return dict(c.most_common())

        def _difficulty(entries: list[MemoryEntry]) -> dict[str, int]:
            c: Counter[str] = Counter((e.difficulty or "unknown") for e in entries)
            return dict(c.most_common())

        return {
            "translator_success_count": len(self._success),
            "example_count": len(self._examples),
            "repair_count": len(self._repairs),
            "with_nl_proof": with_proof,
            "examples_with_nl_proof": ex_proof,
            "success_count": len(self._success),
            "success_by_domain": _domains(self._success),
            "examples_by_domain": _domains(self._examples),
            "success_by_difficulty": _difficulty(self._success),
            "examples_by_difficulty": _difficulty(self._examples),
            "recent_successes": [
                {
                    "nl": (e.natural_language or "")[:120],
                    "domain": e.domain,
                    "difficulty": e.difficulty,
                    "created_at": getattr(e, "created_at", "") or (e.metadata or {}).get("created_at", ""),
                }
                for e in list(reversed(self._success))[:15]
            ],
            "recent_examples": [
                {
                    "nl": (e.natural_language or "")[:120],
                    "domain": e.domain,
                    "difficulty": e.difficulty,
                }
                for e in self._examples[:10]
            ],
        }


def tfidf_cosine(query: str, documents: list[str]) -> list[float]:
    """Return TF-IDF cosine scores without requiring NumPy at runtime."""
    if not documents:
        return []

    tokenized_docs = [_tokenize(d) for d in documents]
    tokenized_q = _tokenize(query)
    vocab = sorted({t for doc in tokenized_docs + [tokenized_q] for t in doc})
    if not vocab:
        return [0.0] * len(documents)

    N = len(documents)
    df: Counter[str] = Counter()
    for doc in tokenized_docs:
        for t in set(doc):
            df[t] += 1

    def vectorize(tokens: list[str]) -> dict[str, float]:
        tf = Counter(tokens)
        v: dict[str, float] = {}
        for t, c in tf.items():
            idf = math.log((N + 1) / (df.get(t, 0) + 1)) + 1.0
            v[t] = (c / max(len(tokens), 1)) * idf
        return v

    qv = vectorize(tokenized_q)
    qnorm = math.sqrt(sum(value * value for value in qv.values())) + 1e-12
    scores: list[float] = []
    for i, doc in enumerate(tokenized_docs):
        dv = vectorize(doc)
        dnorm = math.sqrt(sum(value * value for value in dv.values())) + 1e-12
        dot = sum(value * dv.get(token, 0.0) for token, value in qv.items())
        scores.append(dot / (qnorm * dnorm))
    return scores
