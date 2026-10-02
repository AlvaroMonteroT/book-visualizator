#!/usr/bin/env python3
"""Retrieve relevant book passages for grounded question answering."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Sequence

from scripts.find_character import Paragraph


@dataclass(frozen=True)
class BookContext:
    passages: tuple[Paragraph, ...]


def _words(value: str) -> set[str]:
    return {word for word in re.findall(r"[a-zA-Z][a-zA-Z’'-]{2,}", value.casefold())}


def retrieve_context(paragraphs: Sequence[Paragraph], question: str, limit: int = 12) -> BookContext:
    """Return the most relevant paragraphs plus a little surrounding context."""
    query_words = _words(question)
    if len(query_words) < 1:
        raise ValueError("Ask a question about the book first")
    scored: list[tuple[float, int]] = []
    for index, paragraph in enumerate(paragraphs):
        paragraph_words = _words(paragraph.text)
        overlap = len(query_words & paragraph_words)
        if overlap:
            score = overlap / max(1, len(query_words))
            scored.append((score, index))
    scored.sort(key=lambda item: (-item[0], item[1]))
    if not scored:
        # The answer model can still explain that the book contains no evidence,
        # but it should receive a bounded sample rather than the whole book.
        return BookContext(tuple(paragraphs[:limit]))
    selected: set[int] = set()
    for _, index in scored[: max(1, limit // 3)]:
        selected.update(range(max(0, index - 1), min(len(paragraphs), index + 2)))
        if len(selected) >= limit:
            break
    chosen = sorted(selected)[:limit]
    return BookContext(tuple(paragraphs[index] for index in chosen))


def context_payload(context: BookContext) -> list[dict[str, object]]:
    return [paragraph.as_record() for paragraph in context.passages]

