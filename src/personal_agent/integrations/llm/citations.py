"""Render provider citation annotations as readable links without altering history."""

import re
from typing import Any
from urllib.parse import urlsplit

_OPAQUE_CITATION = re.compile(r"cite[^]*")


def _public_link(value: object) -> str | None:
    if not isinstance(value, str) or not value or any(c.isspace() or ord(c) < 32 for c in value):
        return None
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme.lower() not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
        ):
            return None
    except ValueError:
        return None
    return value


def render_cited_text(output: list[dict[str, Any]]) -> str:
    """Keep claims intact and replace opaque citation tokens with numbered sources.

    Citation offsets are local to each output-text part. An annotation may cover a
    citation token or the cited claim itself: insert after its range, then remove
    provider tokens, rather than blindly replacing text inside the range.
    """
    sources: dict[str, tuple[int, str]] = {}
    parts: list[str] = []
    for item in output:
        if item.get("type") != "message":
            continue
        for part in item.get("content", []):
            if part.get("type") != "output_text" or not isinstance(part.get("text"), str):
                continue
            original = part["text"]
            tokens = list(_OPAQUE_CITATION.finditer(original))
            insertions: dict[int, list[int]] = {}
            for annotation in part.get("annotations", []):
                if annotation.get("type") != "url_citation":
                    continue
                url = _public_link(annotation.get("url"))
                if url is None:
                    continue
                if url not in sources:
                    title = annotation.get("title")
                    title = " ".join(title.split())[:200] if isinstance(title, str) else ""
                    sources[url] = (len(sources) + 1, title or "Source")
                number = sources[url][0]
                start, end = annotation.get("start_index"), annotation.get("end_index")
                if (
                    not isinstance(start, int)
                    or isinstance(start, bool)
                    or not isinstance(end, int)
                    or isinstance(end, bool)
                    or not 0 <= start <= end <= len(original)
                ):
                    # Missing/invalid offsets still yield a linked source; never cut a claim.
                    end = len(original)
                else:
                    for token in tokens:
                        if start < token.end() and end > token.start():
                            end = max(end, token.end())
                numbers = insertions.setdefault(end, [])
                if number not in numbers:
                    numbers.append(number)
            rendered: list[str] = []
            previous = 0
            for offset, numbers in sorted(insertions.items()):
                rendered.append(original[previous:offset])
                rendered.append("".join(f"[{number}]" for number in numbers))
                previous = offset
            rendered.append(original[previous:])
            parts.append(_OPAQUE_CITATION.sub("", "".join(rendered)))
    text = "\n".join(parts)
    if sources:
        links = "\n".join(f"[{number}] {title} — {url}" for url, (number, title) in sources.items())
        text += "\n\nSources:\n" + links
    return text
