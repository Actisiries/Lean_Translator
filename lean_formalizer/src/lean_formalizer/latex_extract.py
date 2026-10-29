"""Read standalone LaTeX as data, preserving math and source locations.

This is deliberately not a TeX interpreter: macros are retained for the LLM,
and included files are reported rather than opened or executed.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
import re

MAX_TEX_BYTES = 5 * 1024 * 1024
MAX_CONTEXT_CHARS = 48_000
_THEOREMS = {"theorem", "lemma", "proposition", "corollary", "claim", "conjecture"}
_CONTEXT = {"definition", "assumption", "notation", "remark", "example", "equation", "align", "gather"}
_SKIP = {"thebibliography", "bibliography", "comment", "verbatim", "Verbatim", "lstlisting", "minted"}
_MACRO_DEFS = {"newcommand", "renewcommand", "providecommand", "DeclareMathOperator"}


def validate_context(text: str) -> None:
    """Bound model input without cutting a formula, hypothesis, or proof."""
    if len(text) > MAX_CONTEXT_CHARS:
        raise ValueError(
            f"Mathematical context has {len(text):,} characters; the limit is "
            f"{MAX_CONTEXT_CHARS:,} (approximately 12,000 tokens for English text). "
            "Select a theorem with --theorem-label or provide a smaller standalone excerpt. "
            "No content was truncated."
        )


@dataclass
class LatexBlock:
    kind: str
    text: str
    start: int
    end: int
    start_line: int
    end_line: int
    labels: list[str] = field(default_factory=list)


@dataclass
class LatexDocument:
    source_name: str
    body: str
    blocks: list[LatexBlock]
    macros: list[str]
    warnings: list[str]
    # Spans are character offsets into body, used to retain surrounding prose.
    body_start: int = 0
    body_end: int = 0
    source: str = ""

    def select(self, theorem_label: str | None = None) -> tuple[str, dict]:
        theorems = [b for b in self.blocks if b.kind == "theorem"]
        selected = None
        if theorem_label:
            matches = [b for b in theorems if theorem_label in b.labels]
            if len(matches) != 1:
                available = ", ".join(label for b in theorems for label in b.labels) or "(none)"
                raise ValueError(f"Theorem label {theorem_label!r} is not unique or was not found. Available: {available}")
            selected = matches[0]
        elif len(theorems) > 1:
            available = ", ".join(label for b in theorems for label in b.labels) or "(unlabelled)"
            raise ValueError(
                f"Found {len(theorems)} theorem-like environments. Select one with "
                f"--theorem-label (web: Theorem label). Labels: {available}. "
                "Add a label or provide an excerpt if the desired theorem is unlabelled."
            )
        elif theorems:
            selected = theorems[0]

        kept: list[LatexBlock] = []
        if selected:
            # All preceding prose/definitions may carry implicit assumptions.
            # Remove other theorem/proof blocks, then add explicitly referenced
            # blocks (including forward references) and their transitive context.
            kept = [b for b in self.blocks if b.kind == "context" and b.start < selected.start]
            kept.append(selected)
            next_blocks = [b for b in self.blocks if b.start >= selected.end]
            if next_blocks and next_blocks[0].kind == "proof":
                kept.append(next_blocks[0])
            labels = {label: b for b in self.blocks for label in b.labels}
            referenced = set(_references(self.source[self.body_start:selected.start]))
            while True:
                refs = referenced | set(_references("\n".join(b.text for b in kept)))
                extra = [labels[r] for r in refs if r in labels and labels[r] not in kept]
                if not extra:
                    break
                kept.extend(extra)
            # Keep non-environment prose before the target as context too.
            chunks: list[str] = []
            cursor = self.body_start
            for b in sorted(self.blocks, key=lambda b: b.start):
                if b.start >= selected.start:
                    break
                chunks.append(self.source[cursor:b.start])
                if b in kept:
                    chunks.append(b.text)
                cursor = b.end
            chunks.append(self.source[cursor:selected.start])
            dependencies = [b for b in kept if b.start >= selected.end and b.kind != "proof"]
            chunks.extend("Referenced context:\n" + b.text for b in sorted(dependencies, key=lambda b: b.start))
            chunks.append("Selected theorem (formalize this claim):\n" + selected.text)
            chunks.extend(b.text for b in kept if b.kind == "proof")
            body = "\n\n".join(c for c in chunks if c.strip())
        else:
            kept = list(self.blocks)
            body = self.body

        prefix = "Macro and theorem-environment definitions (preserve their meaning):\n" + "\n".join(self.macros) if self.macros else ""
        text = "\n\n".join(p for p in (prefix, body.strip()) if p)
        if not body.strip():
            raise ValueError("No mathematical content found in the TeX file.")
        validate_context(text)
        warnings = list(self.warnings)
        retained_labels = {label for b in kept for label in b.labels}
        for ref in sorted(set(_references(text)) - retained_labels):
            warnings.append(f"Reference {ref!r} has no retained labelled mathematical block; supply its context.")
        return text, {
            "format": "latex", "name": self.source_name,
            "selected_label": theorem_label or (selected.labels[0] if selected and selected.labels else ""),
            "warnings": list(dict.fromkeys(warnings)),
            "macros": self.macros,
            "blocks": [
                {k: v for k, v in asdict(b).items() if k not in {"text", "start", "end"}}
                | {"included": b in kept} for b in self.blocks
            ],
        }


def _references(text: str) -> list[str]:
    return [label.strip() for group in re.findall(r"\\(?:ref|eqref|autoref|cref|Cref)\s*\{([^{}]+)\}", text)
            for label in group.split(",")]


def load_latex(path: str | Path, source_name: str | None = None) -> LatexDocument:
    path = Path(path)
    if path.suffix.lower() != ".tex":
        raise ValueError("Expected a .tex file.")
    with path.open("rb") as stream:
        raw = stream.read(MAX_TEX_BYTES + 1)
    if len(raw) > MAX_TEX_BYTES:
        raise ValueError("TeX file exceeds the 5 MiB size limit.")
    try:
        source = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("TeX file must use UTF-8 encoding; please save it as UTF-8.") from exc
    return parse_latex(source, source_name or str(path))


def parse_latex(source: str, source_name: str = "<latex>") -> LatexDocument:
    try:
        from pylatexenc.latexwalker import LatexWalker, LatexWalkerParseError, get_default_latex_context_db
        from pylatexenc.macrospec import MacroSpec, EnvironmentSpec
        from pylatexenc.latex2text import get_default_latex_context_db as text_context_db
    except ImportError as exc:
        from .file_support import missing_parser_message
        raise ValueError(missing_parser_message("tex")) from exc

    if len(source.encode("utf-8")) > MAX_TEX_BYTES:
        raise ValueError("TeX source exceeds the 5 MiB size limit.")
    context = get_default_latex_context_db()
    context.add_context_category("formalizer", macros=[
        MacroSpec("newtheorem", "*{[{["),
        MacroSpec("newcommand", "*{[[{"), MacroSpec("renewcommand", "*{[[{"),
        MacroSpec("providecommand", "*{[[{"), MacroSpec("DeclareMathOperator", "*{{"),
    ], prepend=True)

    def walk(nodes):
        for node in nodes or []:
            yield node
            if getattr(node, "environmentname", "") not in _SKIP:
                yield from walk(getattr(node, "nodelist", None))

    def parse():
        try:
            return LatexWalker(source, latex_context=context, tolerant_parsing=False).get_latex_nodes()[0]
        except (LatexWalkerParseError, RecursionError) as exc:
            raise ValueError(f"Malformed or excessively nested LaTeX: {exc}") from exc

    def arguments(node):
        return getattr(getattr(node, "nodeargd", None), "argnlist", []) or []

    def group_text(node):
        if node is None:
            return ""
        value = source[node.pos:node.pos + node.len]
        return value[1:-1] if value[:1] in {"{", "["} else value

    nodes = parse()
    kinds = {name: "theorem" for name in _THEOREMS}
    kinds.update({name: "context" for name in _CONTEXT})
    kinds.update({name + "*": kind for name, kind in list(kinds.items())})
    macros: list[str] = []
    custom_macros: set[str] = set()
    for node in walk(nodes):
        name = getattr(node, "macroname", "")
        args = arguments(node)
        if name == "newtheorem" and len(args) > 1:
            env = group_text(args[1]).strip()
            title = group_text(args[3]).strip().lower() if len(args) > 3 else ""
            kinds[env] = "context" if title in _CONTEXT else "theorem"
            kinds[env + "*"] = kinds[env]
            macros.append(source[node.pos:node.pos + node.len])
        elif name in _MACRO_DEFS and len(args) > 1:
            custom_macros.add(group_text(args[1]).lstrip("\\"))
            macros.append(source[node.pos:node.pos + node.len])
    context.add_context_category("formalizer-environments", environments=[
        EnvironmentSpec(name, "[", is_math_mode=name.rstrip("*") in {"equation", "align", "gather"})
        for name in set(kinds) | {"proof"}
    ], prepend=True)
    nodes = parse()
    # Mask comments and discarded environments without moving source offsets.
    chars = list(source)
    for node in walk(nodes):
        if type(node).__name__ == "LatexCommentNode" or getattr(node, "environmentname", "") in _SKIP:
            for i in range(node.pos, node.pos + node.len):
                if chars[i] not in "\r\n":
                    chars[i] = " "
    cleaned_source = "".join(chars)
    all_nodes = list(walk(nodes))
    documents = [n for n in all_nodes if getattr(n, "environmentname", "") == "document"]
    if len(documents) > 1:
        raise ValueError("Expected a single LaTeX document environment.")
    body_start, body_end = 0, len(source)
    if documents:
        doc = documents[0]
        begin = re.match(r"\\begin\s*\{document\}", source[doc.pos:])
        body_start = doc.pos + begin.end()
        body_end = doc.pos + doc.len - len(r"\end{document}")
        # End commands may contain whitespace before their argument.
        end = re.search(r"\\end\s*\{document\}\s*$", source[doc.pos:doc.pos + doc.len])
        if end:
            body_end = doc.pos + end.start()

    blocks: list[LatexBlock] = []
    last_end = -1
    for node in all_nodes:
        env = getattr(node, "environmentname", "")
        if env not in kinds and env != "proof":
            continue
        if node.pos < body_start or node.pos >= body_end or node.pos < last_end:
            continue  # Keep nested environments inside their owning block.
        end = node.pos + node.len
        text = cleaned_source[node.pos:end]
        labels = re.findall(r"\\label\s*\{([^{}]+)\}", text)
        blocks.append(LatexBlock(
            kinds.get(env, "proof"), text, node.pos, end,
            source.count("\n", 0, node.pos) + 1,
            source.count("\n", 0, end - 1) + 1, labels,
        ))
        last_end = end

    warnings: list[str] = []
    if macros:
        warnings.append("Macro definitions are preserved for the model; TeX macro expansion is not performed.")
    unknown = set()
    # The walker intentionally omits many argument-free math commands from its
    # syntax database; the text database supplies the standard symbol names.
    symbol_context = text_context_db()
    for node in all_nodes:
        name = getattr(node, "macroname", "")
        if name in {"input", "include", "subfile"}:
            warnings.append("Included files are not loaded; supply a standalone file with the required definitions and proofs.")
        if name in {"def", "gdef", "edef", "xdef", "let"}:
            warnings.append("Low-level TeX macro definitions are not interpreted; rewrite them with newcommand or provide explicit notation.")
        if (name and context.get_macro_spec(name) is None
                and symbol_context.get_macro_spec(name) is None and name not in custom_macros):
            unknown.add(name)
    if unknown:
        warnings.append("Commands without parser definitions are preserved and may need context: " + ", ".join("\\" + n for n in sorted(unknown)))
    labels = [label for b in blocks for label in b.labels]
    if len(labels) != len(set(labels)):
        raise ValueError("Duplicate LaTeX labels found; labels must be unique.")
    return LatexDocument(source_name, cleaned_source[body_start:body_end], blocks,
                         macros, list(dict.fromkeys(warnings)), body_start, body_end, cleaned_source)
