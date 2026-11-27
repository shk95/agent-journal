import re
import unicodedata
from functools import lru_cache

from markdown_it import MarkdownIt
from wcwidth import iter_graphemes, strip_sequences, wcswidth


CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
MARKDOWN = MarkdownIt("commonmark", {"html": False})
UNTITLED_NOTE = "Untitled note"
WRAP_MARK = "↳ "


@lru_cache(maxsize=1024)
def cell_width(cluster):
    return max(0, wcswidth(cluster))


def display_text(text):
    if "\x1b" in text or "\x9b" in text or "\x9d" in text:
        text = strip_sequences(text)
    return unicodedata.normalize("NFC", CONTROL.sub("", text))


def display_clusters(line):
    for cluster in iter_graphemes(display_text(line)):
        if cluster == "\t":
            yield cluster, 0
        else:
            yield cluster, cell_width(cluster)


def fit(text, width):
    if width <= 0:
        return ""
    if text.isascii() and text.isprintable():
        return text[:width]
    out = []
    used = 0
    line = text.partition("\n")[0]
    for cluster, cells in display_clusters(line):
        if cluster == "\t":
            cells = 4 - used % 4
            cluster = " " * cells
        if used + cells > width:
            break
        out.append(cluster)
        used += cells
    return "".join(out)


def lines(text):
    start = 0
    while (end := text.find("\n", start)) >= 0:
        yield text[start:end]
        start = end + 1
    yield text[start:]


def wrap(text, width, *, mark=False):
    gutter = len(WRAP_MARK) if mark and width > len(WRAP_MARK) else 0
    width = max(width - gutter, 1)
    for line in lines(text):
        if not (line.isascii() and line.isprintable()):
            line = display_text(line)
        for start, end in wrapped_spans(line, width):
            prefix = (WRAP_MARK if start else " " * gutter) if gutter else ""
            yield prefix + fit(line[start:end], width)


def wrapped_spans(line, width):
    width = max(width, 1)
    if not line:
        yield 0, 0
        return
    start = 0
    if line.isascii() and line.isprintable():
        for start in range(0, len(line), width):
            yield start, min(start + width, len(line))
        return
    while start < len(line):
        used = 0
        end = start
        for cluster in iter_graphemes(line, start=start):
            cells = 4 - used % 4 if cluster == "\t" else cell_width(cluster)
            if used + cells > width and end > start:
                break
            end += len(cluster)
            used += cells
        yield start, end
        start = end


def heading_text(token):
    return "".join(
        " " if part.type in ("softbreak", "hardbreak") else part.content
        for part in token.children if part.type in (
            "text", "code_inline", "image", "softbreak", "hardbreak"
        )
    ).strip()


@lru_cache(maxsize=64)
def markdown_heading(source):
    tokens = MARKDOWN.parse(source)
    for i, token in enumerate(tokens):
        if token.type == "heading_open" and token.level == 0:
            if title := heading_text(tokens[i + 1]):
                return title[:240]
    return ""


def note_title(note):
    if "body" in note:
        return markdown_heading(note["body"]) or UNTITLED_NOTE
    return note.get("title") or UNTITLED_NOTE


def markdown_with_title(body, title):
    title = title.strip()
    if title and markdown_heading(body) != title:
        heading = re.sub(r"([\\\x60*_\[\]#!<>&])", r"\\\1", title)
        return f"# {heading}\n\n{body}" if body else f"# {heading}"
    return body


def markdown_preview(source, omit_leading_title=False):
    out = []
    lists = []
    quote = 0
    marker = ""
    tokens = MARKDOWN.parse(source)
    if omit_leading_title and tokens and tokens[0].type == "heading_open":
        if 0 < len(heading_text(tokens[1])) <= 240:
            tokens = tokens[3:]
    for token in tokens:
        match token.type:
            case "blockquote_open":
                quote += 1
            case "blockquote_close":
                quote -= 1
            case "bullet_list_open":
                lists.append(None)
            case "ordered_list_open":
                lists.append(int(token.attrGet("start") or 1))
            case "bullet_list_close" | "ordered_list_close":
                lists.pop()
            case "list_item_open":
                number = lists[-1]
                marker = "• " if number is None else f"{number}. "
                if number is not None:
                    lists[-1] += 1
            case "heading_open":
                if out and out[-1]:
                    out.append("")
            case "inline":
                parts = []
                links = []
                for child in token.children or ():
                    match child.type:
                        case "strong_open" | "strong_close":
                            parts.append("*")
                        case "em_open" | "em_close":
                            parts.append("_")
                        case "link_open":
                            links.append(child.attrGet("href"))
                        case "link_close":
                            parts.append(f" <{links.pop()}>")
                        case "hardbreak" | "softbreak":
                            parts.append("\n")
                        case _:
                            parts.append(child.content)
                out.append(
                    "│ " * quote
                    + "  " * max(0, len(lists) - 1)
                    + marker
                    + "".join(parts)
                )
                marker = ""
            case "paragraph_close" | "heading_close":
                if not lists and not quote:
                    out.append("")
            case "fence" | "code_block":
                out.extend("│ " + line for line in lines(token.content.rstrip("\n")))
                out.append("")
            case "hr":
                out.append("────────")
    return "\n".join(out).rstrip("\n")
