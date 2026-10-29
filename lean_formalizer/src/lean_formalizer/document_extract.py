"""Whole-document extraction and conservative mathematical inventory.

No LLM context limit is applied during ingestion. Original source and exact
locations survive segmentation, including cross-page PDF proof blocks.
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

from .document_models import DocumentBlock, MathUnit, PaperDocument, SourceSpan
from .latex_extract import MAX_TEX_BYTES, _references, parse_latex

MAX_DOCUMENT_BYTES = 25 * 1024 * 1024
MAX_PAGES = 200
_HEAD = re.compile(
    r"(?im)^[ \t]*(theorem|lemma|proposition|corollary|claim|conjecture|"
    r"definition|assumption|notation|remark|example|proof)"
    r"(?:[ \t]*(\d+(?:[.\-]\d+)*)(?=[ \t]*[.:]|[ \t]*$)"
    r"|(?=[ \t]*[.:]|[ \t]+\(|[ \t]+of\b))"
)
_SECTION = re.compile(
    r"\\(?P<tex>section|subsection|subsubsection)\*?"
    r"(?:\[[^\]]*\])?\{(?P<title>[^{}]+)\}"
    r"|^[ \t]*(?P<num>\d+(?:\.\d+)*)(?:\.[ \t]+|[ \t]{2,})(?P<plain>[A-Za-z][^\n]{2,90})$",
    re.M,
)


def _mask_citations(text: str) -> str:
    # Preserve offsets/newlines, including references wrapped across PDF rows.
    return re.sub(r"\[[^\[\]]*\]", lambda m: re.sub(r"[^\n]", " ", m.group()), text)


def _glyph_warning(text: str, page: int) -> list[str]:
    if re.search(r"\(cid:\d+\)|\ufffd", text):
        return [f"Page {page}: undecoded PDF glyphs; mathematical symbols require source review."]
    return []


def _id(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _mask_comments(text: str) -> str:
    # Preserve offsets; a percent preceded by an odd number of slashes is literal.
    return re.sub(r"(?m)(?<!\\)((?:\\\\)*)%[^\n]*", lambda m: m.group(1) + " " * (len(m.group()) - len(m.group(1))), text)


def _tex_project(path: Path, project_root: Path | None) -> tuple[str, list[tuple[int, int, str, str]], dict[str, str]]:
    root = (project_root or path.parent).resolve()
    files: dict[str, str] = {}
    segments: list[tuple[int, int, str, str]] = []
    pieces: list[str] = []
    total = 0
    offset = 0

    def expand(current: Path, stack: tuple[Path, ...]):
        nonlocal total, offset
        current = current.resolve()
        if not current.is_relative_to(root):
            raise ValueError(f"TeX include escapes project root: {current.name}")
        if current in stack:
            raise ValueError("Cyclic TeX include: " + " -> ".join(p.name for p in (*stack, current)))
        if len(stack) > 30:
            raise ValueError("TeX include nesting exceeds 30 files.")
        raw = current.read_bytes()
        total += len(raw)
        if total > MAX_TEX_BYTES:
            raise ValueError("Expanded TeX project exceeds 5 MiB.")
        try:
            content = raw.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ValueError(f"TeX file must use UTF-8: {current.name}") from exc
        name = current.relative_to(root).as_posix()
        files[name] = content
        masked = _mask_comments(content)
        cursor = 0

        def append(start: int, end: int):
            nonlocal offset
            text = content[start:end]
            if text:
                # Prefix keeps original line counts for source mapping.
                segments.append((offset, offset + len(text), name, content[:start]))
                pieces.append(text)
                offset += len(text)

        for m in re.finditer(r"\\(?:input|include|subfile)\s*\{([^{}]+)\}", masked):
            append(cursor, m.start())
            target = Path(m.group(1))
            if target.is_absolute() or "\\" in m.group(1) or any(c in m.group(1) for c in "#$~"):
                raise ValueError("Only static project-local TeX includes are supported.")
            if not target.suffix:
                target = target.with_suffix(".tex")
            expand(current.parent / target, (*stack, current))
            cursor = m.end()
        append(cursor, len(content))

    expand(path, ())
    return "".join(pieces), segments, files


def extract_tex(path: Path, project_root: Path | None = None) -> PaperDocument:
    text, segments, files = _tex_project(path, project_root)
    parsed = parse_latex(text, path.name)

    def spans(start: int, end: int) -> list[SourceSpan]:
        result = []
        for a, b, name, prefix in segments:
            if a < end and b > start:
                left, right = max(start, a), min(end, b)
                first = prefix.count("\n") + text[a:left].count("\n") + 1
                last = prefix.count("\n") + text[a:max(left, right - 1)].count("\n") + 1
                result.append(SourceSpan(name, first, last))
        return result

    blocks = []
    cursor = parsed.body_start
    for b in parsed.blocks:
        if b.start > cursor and parsed.source[cursor:b.start].strip():
            blocks.append(DocumentBlock(_id(f"{cursor}:prose"), "prose", parsed.source[cursor:b.start], cursor, b.start, spans(cursor, b.start)))
        env = re.match(r"\\begin\s*\{([^}]+)\}", b.text)
        name = env.group(1).rstrip("*") if env else ""
        kind = b.kind
        if kind == "context":
            kind = "definition" if name in {"definition", "notation"} else "assumption" if name == "assumption" else "context"
            if name not in {"equation", "align", "gather", "remark", "example", "assumption", "definition", "notation"}:
                kind = "definition"  # Custom context environments declared by newtheorem.
        blocks.append(DocumentBlock(_id(f"{b.start}:{kind}"), kind, b.text, b.start, b.end, spans(b.start, b.end), b.labels))
        cursor = b.end
    if cursor < parsed.body_end and parsed.source[cursor:parsed.body_end].strip():
        blocks.append(DocumentBlock(_id(f"{cursor}:prose"), "prose", parsed.source[cursor:parsed.body_end], cursor, parsed.body_end, spans(cursor, parsed.body_end)))
    doc = PaperDocument(_id(text), path.name, "latex", parsed.source, blocks, [], files, parsed.macros, parsed.warnings)
    _inventory(doc)
    return doc


def _pdf_lines(path: Path) -> tuple[list[tuple[str, SourceSpan]], int, list[str]]:
    try:
        import pdfplumber
    except ImportError:
        pdfplumber = None
    lines: list[tuple[str, SourceSpan]] = []
    warnings = []
    if pdfplumber:
        with pdfplumber.open(path) as pdf:
            if len(pdf.pages) > MAX_PAGES:
                raise ValueError(f"PDF exceeds the {MAX_PAGES}-page ingestion limit.")
            for page_no, page in enumerate(pdf.pages, 1):
                words = page.extract_words() or []
                warnings.extend(_glyph_warning(" ".join(w["text"] for w in words), page_no))
                midpoint = page.width / 2
                left = [w for w in words if w["x1"] < midpoint - 8]
                right = [w for w in words if w["x0"] > midpoint + 8]
                crossing = [w for w in words if w not in left and w not in right]
                two_columns = len(left) > 30 and len(right) > 30 and len(crossing) < len(words) * .025
                groups = [left, right] if two_columns else [words]
                if two_columns:
                    warnings.append(f"Page {page_no}: two-column reading order inferred; review equation/proof ordering.")
                    # Crossing items (titles/equations) cannot be safely placed.
                    if crossing:
                        groups = [words]
                        warnings.append(f"Page {page_no}: spanning items make column order ambiguous; review required.")
                for group in groups:
                    rows: list[list[dict]] = []
                    for w in sorted(group, key=lambda w: (w["top"], w["x0"])):
                        if rows and abs(rows[-1][0]["top"] - w["top"]) < 3:
                            rows[-1].append(w)
                        else:
                            rows.append([w])
                    for row in rows:
                        row.sort(key=lambda w: w["x0"])
                        value = " ".join(w["text"] for w in row)
                        bbox = [min(w["x0"] for w in row), min(w["top"] for w in row), max(w["x1"] for w in row), max(w["bottom"] for w in row)]
                        lines.append((value, SourceSpan(path.name, 0, 0, page_no, bbox)))
                if not words:
                    warnings.append(f"Page {page_no}: no extractable text; OCR is required.")
            return lines, len(pdf.pages), warnings
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        from .file_support import missing_parser_message
        raise ValueError(missing_parser_message("pdf")) from exc
    reader = PdfReader(path)
    if len(reader.pages) > MAX_PAGES:
        raise ValueError(f"PDF exceeds the {MAX_PAGES}-page ingestion limit.")
    warnings.append("PDF fallback extraction has no bounding boxes; review reading order and equations.")
    for number, page in enumerate(reader.pages, 1):
        value = page.extract_text() or ""
        warnings.extend(_glyph_warning(value, number))
        if not value.strip():
            warnings.append(f"Page {number}: no extractable text; OCR is required.")
        lines.extend((line, SourceSpan(path.name, 0, 0, number)) for line in value.splitlines())
    return lines, len(reader.pages), warnings


def extract_pdf(path: Path, max_pages: int | None = None) -> PaperDocument:
    lines, page_count, warnings = _pdf_lines(path)
    complete = not any("OCR is required" in w for w in warnings)
    if max_pages is not None:
        if max_pages < 1:
            raise ValueError("max_pages must be positive.")
        if max_pages < page_count:
            complete = False
            warnings.append(f"Only the first {max_pages} of {page_count} pages were requested; paper coverage is incomplete.")
            lines = [(line, span) for line, span in lines if span.page <= max_pages]
    # Keep all content except unambiguous lone page numbers. Equation-only
    # numeric lines with a bbox away from page edges are deliberately retained.
    lines = [(line, span) for line, span in lines if not (line.strip().isdigit() and span.bbox and span.bbox[1] > 730)]
    offsets = []
    parts = []
    cursor = 0
    for line_no, (value, span) in enumerate(lines, 1):
        span.start_line = span.end_line = line_no
        offsets.append((cursor, cursor + len(value) + 1, span))
        parts.append(value + "\n")
        cursor += len(value) + 1
    text = "".join(parts)
    if not text.strip():
        raise ValueError("No mathematical text could be extracted from the PDF; provide TeX or an OCR transcript.")
    masked = _mask_citations(text)
    boundaries = list(_HEAD.finditer(masked))
    blocks = []
    cuts = [(0, "prose", "")]
    for m in boundaries:
        kind = m.group(1).lower()
        kind = "theorem" if kind in {"theorem", "lemma", "proposition", "corollary", "claim", "conjecture"} else kind
        cuts.append((m.start(), kind, f"{m.group(1).title()} {m.group(2)}" if m.group(2) else ""))
    cuts += [(m.start(), "prose", "") for m in _SECTION.finditer(masked)]
    cuts += [(m.start(), "references", "") for m in re.finditer(r"(?im)^[ \t]*references[ \t]*$", masked)]
    cuts = sorted(set(cuts))
    # Subproof headings belong to the same source proof (e.g. Proof of (i)).
    merged = []
    for cut in cuts:
        if merged and cut[1] == merged[-1][1] == "proof":
            continue
        if merged and cut[0] == merged[-1][0]:
            merged[-1] = cut
        else:
            merged.append(cut)
    cuts = merged
    for i, (start, kind, label) in enumerate(cuts):
        end = cuts[i + 1][0] if i + 1 < len(cuts) else len(text)
        spans = [s for a, b, s in offsets if a < end and b > start]
        blocks.append(DocumentBlock(_id(f"{start}:{kind}"), kind, text[start:end], start, end, spans, [label] if label else []))
    warnings.append("PDF inventory uses visible theorem/proof headings; unnumbered prose claims and equation fidelity require review.")
    doc = PaperDocument(_id(path.read_bytes().hex()), path.name, "pdf", text, blocks, [], warnings=warnings,
                        page_count=page_count, extraction_complete=complete)
    _inventory(doc)
    return doc


def _inventory(doc: PaperDocument) -> None:
    sections = list(_SECTION.finditer(doc.text))
    def section_at(pos):
        stack = []
        for m in sections:
            if m.start() > pos:
                break
            level = {"section": 1, "subsection": 2, "subsubsection": 3}.get(m.group("tex"), len((m.group("num") or "1").split(".")))
            stack = [(n, t) for n, t in stack if n < level]
            title = m.group("title") or m.group("plain") or m.group(0)
            stack.append((level, title))
        return " / ".join(t for _, t in stack)
    for block in doc.blocks:
        block.section = section_at(block.start)
        if block.kind not in {"theorem", "definition"}:
            continue
        unit = MathUnit("u_" + block.id, block.kind, block.labels[0] if block.labels else "u_" + block.id,
                        block.text, block.id)
        # Retain prose/scoped assumptions from ancestor/current sections. Raw
        # introduction in other sections is kept in the inventory, not every prompt.
        for b in doc.blocks:
            if b.start >= block.start:
                break
            if b.kind in {"prose", "assumption", "notation", "context"} and (not b.section or block.section.startswith(b.section)):
                unit.context.append(b.id)
        following = [b for b in doc.blocks if b.start >= block.end]
        if following and following[0].kind == "proof":
            unit.proof = following[0].text
        elif following and following[0].kind == "prose" and len(following) > 1 and following[1].kind == "proof":
            unit.context.append(following[0].id)
            unit.proof = following[1].text
        doc.units.append(unit)
    if not doc.units:
        doc.warnings.append("No theorem or definition blocks were identified. Paper needs a source-backed inventory before execution.")


def extract_document(path: str | Path, project_root: str | Path | None = None,
                     max_pages: int | None = None) -> PaperDocument:
    path = Path(path).resolve()
    if path.stat().st_size > MAX_DOCUMENT_BYTES:
        raise ValueError("Paper exceeds the 25 MiB ingestion limit.")
    if path.suffix.lower() == ".tex":
        return extract_tex(path, Path(project_root).resolve() if project_root else None)
    if path.suffix.lower() == ".pdf":
        return extract_pdf(path, max_pages)
    raise ValueError("Paper input must be .pdf or .tex.")


def plan_dependencies(doc: PaperDocument, target: str | None = None, scope: str = "all-results") -> None:
    if scope not in {"all-results", "main-results"}:
        raise ValueError("Paper scope must be all-results or main-results.")
    # Replanning is used by resume/run; rebuild derived selections and edges.
    doc.edges.clear()
    for unit in doc.units:
        unit.selected = False
        unit.dependencies.clear()
        unit.unresolved.clear()
    by_label = {}
    block_unit = {u.block_id: u for u in doc.units}
    for b in doc.blocks:
        for label in b.labels:
            if label in by_label:
                raise ValueError(f"Duplicate paper label: {label}")
            by_label[label] = b
    for unit in doc.units:
        block = next(b for b in doc.blocks if b.id == unit.block_id)
        refs = _references(unit.statement + "\n" + unit.proof + "\n" + "\n".join(b.text for b in doc.blocks if b.id in unit.context))
        if doc.format == "pdf":
            source = unit.statement + "\n" + unit.proof
            # Bibliographic theorem numbers belong to other papers, even if
            # the same number happens to exist in this paper's inventory.
            unit.unresolved += ["External citation " + " ".join(m.group().split())
                                for m in re.finditer(r"\[\s*\d+[^\[\]]*\]", source + '\n' + '\n'.join(
                                    b.text for b in doc.blocks if b.id in unit.context))]
            unit.unresolved = list(dict.fromkeys(unit.unresolved))
            refs += [f"{m.group(1).title()} {m.group(2)}" for m in re.finditer(
                r"(?i)\b(Theorem|Lemma|Proposition|Corollary|Definition)\s*(\d+(?:\.\d+)*)", _mask_citations(source))
                if f"{m.group(1).title()} {m.group(2)}" != unit.label]
        for ref in set(refs):
            referred = by_label.get(ref)
            if not referred:
                unit.unresolved.append(ref)
            elif referred.id in block_unit:
                dep = block_unit[referred.id]
                if dep.id not in unit.dependencies:
                    unit.dependencies.append(dep.id)
                    doc.edges.append({"from": unit.id, "to": dep.id, "kind": "requires_result", "evidence": ref})
            elif referred.id not in unit.context:
                unit.context.append(referred.id)
        for previous in doc.units:
            pb = next(b for b in doc.blocks if b.id == previous.block_id)
            if previous.kind == "definition" and pb.start < block.start and (not pb.section or block.section.startswith(pb.section)):
                if previous.id not in unit.dependencies:
                    unit.dependencies.append(previous.id)
                    doc.edges.append({"from": unit.id, "to": previous.id, "kind": "uses_definition", "evidence": "preceding scoped definition"})
    targets = [u for u in doc.units if u.kind == "theorem"]
    if target:
        targets = [u for u in doc.units if target in {u.label, u.id}]
        if not targets:
            raise ValueError(f"Paper target {target!r} was not found. Available: " + ", ".join(u.label for u in doc.units))
    elif scope == "main-results" and len(targets) > 1:
        targets = [u for u in targets if re.search(r"(?i)main|principal", u.label + " " + u.statement[:150])]
        if not targets:
            doc.warnings.append("Main result is ambiguous; all theorem candidates were selected. Use a theorem label to narrow the run.")
            targets = [u for u in doc.units if u.kind == "theorem"]
    if not target and scope == "all-results":
        targets = list(doc.units)
    by_id = {u.id: u for u in doc.units}
    def select(unit):
        if unit.selected:
            return
        unit.selected = True
        for dep in unit.dependencies:
            select(by_id[dep])
    for unit in targets:
        select(unit)
