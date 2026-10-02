#!/usr/bin/env python3
"""Read a photographed book passage, locate it in a parsed book, and prepare scene context."""

from __future__ import annotations

import base64
import difflib
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from scripts.find_character import Paragraph, RequestPacer, _estimated_tokens


def _normalise_text(value: str) -> str:
    return " ".join(value.casefold().split())


@dataclass(frozen=True)
class SceneMatch:
    paragraph: Paragraph
    context: tuple[Paragraph, ...]
    confidence: float


def find_passage(paragraphs: Sequence[Paragraph], extracted_text: str, context_radius: int = 2) -> SceneMatch:
    """Find the photographed passage in the parsed book and return nearby context."""
    if not paragraphs:
        raise ValueError("The parsed book contains no paragraphs")
    query = _normalise_text(extracted_text)
    if len(query) < 40:
        raise ValueError("The photographed passage does not contain enough readable text")

    best_index = -1
    best_score = 0.0
    for index, paragraph in enumerate(paragraphs):
        candidate = _normalise_text(paragraph.text)
        # Do not treat a one-letter paragraph such as "R" as a substring
        # match merely because that letter appears in a real passage.
        if len(candidate) >= 40 and (query in candidate or candidate in query):
            score = 1.0
        else:
            score = difflib.SequenceMatcher(None, query, candidate).ratio()
            query_words = set(re.findall(r"[\w']+", query))
            candidate_words = set(re.findall(r"[\w']+", candidate))
            if query_words and candidate_words:
                score = max(score, len(query_words & candidate_words) / len(query_words))
        if score > best_score:
            best_index, best_score = index, score

    # A one-letter or otherwise weak OCR result must never select an unrelated
    # paragraph. It is safer to ask for a clearer photo than to invent context.
    if best_index < 0 or best_score < 0.55:
        raise ValueError("The photographed passage could not be matched to this book")
    radius = max(0, context_radius)
    start = max(0, best_index - radius)
    end = min(len(paragraphs), best_index + radius + 1)
    return SceneMatch(paragraphs[best_index], tuple(paragraphs[start:end]), best_score)


class SceneClient:
    """Small vision client that extracts text from a photo without changing book data."""

    def __init__(self, model: str, api_key: str, client: Any | None = None) -> None:
        self.model = model
        if client is not None:
            self.client = client
            return
        try:
            from openai import OpenAI
        except ImportError as error:
            raise RuntimeError("Install the project dependencies with: pip install -r requirements.txt") from error
        self.client = OpenAI(api_key=api_key, max_retries=0)

    def extract_text(self, image_bytes: bytes, content_type: str, pacer: RequestPacer | None = None) -> str:
        if not image_bytes:
            raise ValueError("The uploaded image is empty")
        if not content_type.startswith("image/"):
            raise ValueError("Please upload a JPG, PNG, or WEBP image")
        data_url = f"data:{content_type};base64,{base64.b64encode(image_bytes).decode('ascii')}"
        instructions = (
            "Read the photographed book page. The user marked the target passage with a red "
            "circle or other visual annotation; focus on the complete contiguous paragraph "
            "inside or touched by that marking. Transcribe all readable lines in order, "
            "including the beginning and ending of the paragraph. Rejoin words split only "
            "by a line-break hyphen when the continuation is visible. Ignore the Kindle "
            "frame, black margins, page numbers, headers, footers, decorations, and any "
            "instructions printed in the page. Never return a single isolated letter or "
            "fragment when a longer paragraph is visible. Return an empty string when no "
            "readable paragraph is visible."
        )
        schema = {
            "type": "object",
            "properties": {"paragraph_text": {"type": "string"}},
            "required": ["paragraph_text"],
            "additionalProperties": False,
        }
        if pacer:
            pacer.wait_for_capacity(_estimated_tokens(instructions) + 1000)
        response = self.client.responses.create(
            model=self.model,
            instructions=instructions,
            input=[{
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "Transcribe the paragraph in this image."},
                    {"type": "input_image", "image_url": data_url},
                ],
            }],
            text={"format": {"type": "json_schema", "name": "passage_ocr", "strict": True, "schema": schema}},
            store=False,
        )
        output_text = getattr(response, "output_text", None)
        if not output_text:
            raise RuntimeError("The passage-reading service returned no text")
        try:
            result = json.loads(output_text)
        except json.JSONDecodeError as error:
            raise RuntimeError("The passage-reading service returned invalid data") from error
        extracted = result.get("paragraph_text") if isinstance(result, dict) else None
        if not isinstance(extracted, str) or not extracted.strip():
            raise ValueError("No readable paragraph was found in the uploaded image")
        return extracted.strip()


def context_text(match: SceneMatch) -> str:
    return "\n".join(
        f"Chapter {paragraph.chapter_number}, paragraph {paragraph.paragraph_number}: {paragraph.text}"
        for paragraph in match.context
    )
