"""Source-based segmentation. TeX labels identify references, never printed numbers.

This is a structural reader, not a TeX interpreter. Unsupported counter macros
and missing included files are reported instead of being silently expanded.
"""
from __future__ import annotations

import hashlib
import re

from .models import SourceBlock, SourceSpan

CLAIMS = {"theorem", "lemma", "proposition", "corollary", "claim"}
KINDS = CLAIMS | {"definition", "assumption", "proof", "example", "remark"}
REFERENCES = re.compile(r"\\(?:eqref|ref|autoref|cref|Cref)\s*\{([^}]+)\}")


def source_id(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def mask_comments(text):
    """Keep offsets and line breaks, including escaped percent signs."""
    chars = list(text)
    for match in re.finditer(r"%", text):
        if chars[match.start()] == " ":
            continue
        n = match.start() - 1
        while n >= 0 and text[n] == "\\":
            n -= 1
        if (match.start() - n - 1) % 2 == 0:
            end = text.find("\n", match.start())
            end = len(text) if end < 0 else end
            chars[match.start():end] = " " * (end - match.start())
    return "".join(chars)


def group(text, position, opening="{", closing="}"):
    """Read nested arguments, including braces inside optional titles."""
    position = skip_space(text, position)
    if position >= len(text) or text[position] != opening:
        return None, position
    start, depth, braces = position + 1, 1, 0
    position += 1
    while position < len(text):
        char = text[position]
        if char == "\\":
            position += 2
            continue
        if opening == "[":
            if char == "{":
                braces += 1
            elif char == "}":
                braces -= 1
        if not braces:
            if char == opening:
                depth += 1
            elif char == closing:
                depth -= 1
                if depth == 0:
                    return text[start:position], position + 1
        position += 1
    return None, start - 1


def skip_space(text, position):
    while position < len(text) and text[position].isspace():
        position += 1
    return position


def display(kind, number="", title="", caption=""):
    name = caption or kind.capitalize()
    return name + (" " + number if number else "") + (" (" + title + ")" if title else "")


def statement_text(block):
    value = mask_comments(block.text).strip()
    if value.startswith("\\begin"):
        env, end = group(value, len("\\begin"))
        if env:
            _, after_title = group(value, end, "[", "]")
            value = value[after_title:]
            value = re.sub(r"\\end\s*\{" + re.escape(env) + r"\}\s*$", "", value)
    elif block.display_name:
        match = pdf_heading(value.splitlines()[0])
        if match:
            value = value[match["end"]:]
    return re.sub(r"\\label\s*\{[^}]+\}", "", value).strip()


def tex_blocks(source, text):
    masked = text
    # Literal examples must not create claims or change counters.
    for pattern in (r"\\begin\{(?:verbatim\*?|lstlisting|minted|comment)\}.*?\\end\{(?:verbatim\*?|lstlisting|minted|comment)\}",
                    r"\\verb\*?([^\w\s]).*?\1"):
        masked = re.sub(pattern, lambda m: re.sub(r"[^\n]", " ", m[0]), masked, flags=re.S)
    masked = mask_comments(masked)
    digest = source_id(text)
    blocks, warnings, counters, reset, stack = [], [], {}, {}, []
    environments = {kind: (kind, kind.capitalize(), kind, "") for kind in KINDS - {"proof"}}
    environments["proof"] = ("proof", "Proof", "", "")
    current_section, cursor, skip, active = "", 0, 0, None
    complete = True
    unknown_numbering = set()

    def emit(start, end, kind="prose", **metadata):
        if not masked[start:end].strip():
            return
        blocks.append(SourceBlock(source_id(f"{digest}:{start}:{kind}"), kind, text[start:end],
            spans=[SourceSpan(source, text.count("\n", 0, start)+1, text.count("\n", 0, max(start, end-1))+1)],
            labels=re.findall(r"\\label\s*\{([^}]+)\}", masked[start:end]), section=current_section,
            structure_locked=True, **metadata))

    def reset_children(parent, visited=None):
        visited = set() if visited is None else visited
        if parent in visited:
            return
        visited.add(parent)
        for child in [c for c, owner in reset.items() if owner == parent and c != parent]:
            counters[child] = 0
            reset_children(child, visited)

    def formatted(counter, visited=None):
        visited = set() if visited is None else visited
        if counter in visited or counter in unknown_numbering:
            return ""
        visited.add(counter)
        parent = reset.get(counter)
        prefix = formatted(parent, visited) if parent else ""
        if parent and not prefix:
            return ""
        return (prefix + "." if prefix else "") + str(counters.get(counter, 0))

    reset.update(subsection="section", subsubsection="subsection")
    commands = re.compile(r"\\(newtheorem|begin|end|chapter|section|subsection|subsubsection|setcounter|addtocounter|numberwithin|counterwithin|newcommand|renewcommand|providecommand|input|include)\b(?![A-Za-z])")
    # A star is not a word boundary; consume it after matching the command.
    for token in commands.finditer(masked):
        if token.start() < skip:
            continue
        command, end = token[1], token.end()
        if end < len(masked) and masked[end] == "*":
            command += "*"
            end += 1
        if command in {"begin", "end"}:
            env, after = group(masked, end)
            if not env:
                continue
            skip = after
            if command == "begin":
                stack.append(env)
                base = env.rstrip("*")
                spec = environments.get(env) or environments.get(base)
                if spec:
                    kind, caption, counter, _ = spec
                    title, skip = group(masked, after, "[", "]")
                    if counter and not env.endswith("*"):
                        counters[counter] = counters.get(counter, 0) + 1
                        reset_children(counter)
                    number = formatted(counter) if counter and not env.endswith("*") else ""
                    if active is None:
                        emit(cursor, token.start())
                        active = (token.start(), len(stack), kind, number, title or "", caption)
            else:
                if not stack or stack[-1] != env:
                    warnings.append(f"Unbalanced TeX environment {env} near line {text.count(chr(10), 0, token.start())+1}.")
                    complete = False
                    continue
                if active and active[1] == len(stack):
                    start, _, kind, number, title, caption = active
                    emit(start, after, kind, number=number, title=title, display_name=display(kind, number, title, caption))
                    cursor, active = after, None
                stack.pop()
            continue
        if command in {"input", "include"}:
            name, skip = group(masked, end)
            warnings.append(f"Included source {name or '(unbraced path)'} was not expanded; supply a combined TeX file.")
            complete = False
            continue
        if active:
            continue
        if command.startswith("newtheorem"):
            env, end = group(masked, end)
            shared, end = group(masked, end, "[", "]")
            caption, end = group(masked, end)
            parent, end = group(masked, end, "[", "]")
            if env and caption:
                kind = next((k for k in sorted(KINDS) if re.search(r"\b" + k + r"\b", caption, re.I)), "prose")
                counter = environments.get(shared, (None, None, shared, None))[2] if shared else env
                if command.endswith("*"):
                    counter = ""
                environments[env] = (kind, caption, counter, parent or "")
                if counter and parent:
                    reset[counter] = parent
                counters.setdefault(counter, 0)
            skip = end
        elif command.rstrip("*") in {"chapter", "section", "subsection", "subsubsection"}:
            _, end = group(masked, end, "[", "]")
            title, end = group(masked, end)
            if title is not None:
                emit(cursor, token.start())
                cursor = token.start()
                level = command.rstrip("*")
                if level == "chapter":
                    reset["section"] = "chapter"
                if not command.endswith("*"):
                    counters[level] = counters.get(level, 0) + 1
                    reset_children(level)
                current_section = level + ":" + str(token.start()) + ":" + title
            skip = end
        elif command in {"setcounter", "addtocounter", "numberwithin", "counterwithin"}:
            name, end = group(masked, end)
            value, end = group(masked, end)
            if name and value:
                if command in {"numberwithin", "counterwithin"}:
                    reset[name] = value
                elif re.fullmatch(r"-?\d+", value.strip()):
                    counters[name] = int(value) + (counters.get(name, 0) if command == "addtocounter" else 0)
                    reset_children(name)
                else:
                    unknown_numbering.add(name)
                    warnings.append(f"Counter {name} uses a nonliteral value; printed number needs review.")
            skip = end
        elif command.rstrip("*") in {"newcommand", "renewcommand", "providecommand"}:
            name, end = group(masked, end)
            if name is None:
                macro = re.match(r"\s*(\\[A-Za-z]+)", masked[end:])
                if macro:
                    name, end = macro[1], end + macro.end()
            if name and name.startswith("\\the"):
                unknown_numbering.add(name[4:])
                warnings.append(f"Custom numbering {name} needs review; printed number omitted.")
            _, end = group(masked, end, "[", "]")
            _, end = group(masked, end, "[", "]")
            _, skip = group(masked, end)
    if active:
        start, _, kind, number, title, caption = active
        emit(start, len(text), kind, number=number, title=title, display_name=display(kind, number, title, caption),
             issues=["Unclosed environment; extraction requires source review."])
        cursor = len(text)
    if stack:
        warnings.append("Unclosed TeX environments: " + ", ".join(stack))
        complete = False
    emit(cursor, len(text))
    return blocks, warnings, complete


_PDF_KIND = re.compile(r"^\s*(theorem|lemma|proposition|corollary|claim|definition|assumption|proof|example|remark)\b", re.I)


def pdf_heading(line):
    match = _PDF_KIND.match(line)
    if not match:
        return None
    kind, end = match[1].lower(), match.end()
    rest = line[end:]
    number = re.match(r"\s+(\d+(?:\.\d+)*[A-Za-z]?|[A-Z])(?=\s|[.:]|$)", rest)
    # An unnumbered heading needs punctuation, so 'Theorem applies...' stays prose.
    explicit_proof = kind == "proof" and re.match(r"\s+(?:of|for)\s+(?:Theorem|Lemma|Proposition|Corollary|Claim)\b", rest, re.I)
    if not number and not explicit_proof and not re.match(r"\s*[.:]", rest) and not (kind == "proof" and not rest.strip()):
        return None
    if number:
        end += number.end()
    punctuation = re.match(r"\s*[.:]?\s*", line[end:])
    end += punctuation.end()
    title = ""
    if kind != "proof" and line[end:].startswith("("):
        title, after = group(line, end, "(", ")")
        if title is not None:
            end = after + len(re.match(r"[.:]?\s*", line[after:])[0])
    return {"kind": kind, "number": number[1] if number else "", "title": title or "", "end": end}


def pdf_blocks(source, text):
    """Preserve heading text and page-local spans, including cross-page proofs."""
    blocks, current, spans = [], [], []
    kind, number, title, start = "prose", "", "", 0

    def flush():
        if current and "\n".join(current).strip():
            value = "\n".join(current).strip()
            blocks.append(SourceBlock(source_id(f"{source_id(text)}:{start}:{kind}"), kind, value,
                spans=list(spans), labels=[display(kind, number)] if number else [],
                number=number, title=title, display_name=display(kind, number, title) if kind != "prose" else "",
                structure_locked=kind != "prose", confidence=.72 if kind != "prose" else .9,
                issues=["PDF text segmentation; verify mathematical symbols and reading order."] if kind != "prose" else []))
        current.clear()
        spans.clear()

    offset = 0
    for page, page_text in enumerate(text.split("\f"), 1):
        for line_no, line in enumerate(page_text.splitlines(), 1):
            if not line.strip() and not current:
                offset += len(line) + 1
                continue
            heading = pdf_heading(line)
            if heading:
                flush()
                kind, number, title, start = heading["kind"], heading["number"], heading["title"], offset
            elif re.match(r"^\s*(?:References|Bibliography|Acknowledg(?:e)?ments|\d+(?:\.\d+)*\s+[A-Z])\s*", line):
                flush()
                kind, number, title, start = "prose", "", "", offset
            if not current:
                start = offset
            current.append(line)
            if not spans or spans[-1].page != page:
                spans.append(SourceSpan(source, line_no, line_no, page))
            else:
                spans[-1].end_line = line_no
            # A visible proof terminator ends ownership before following prose.
            if kind == "proof" and re.search(r"[□∎]\s*$", line):
                flush()
                kind, number, title = "prose", "", ""
            offset += len(line) + 1
    flush()
    return blocks
