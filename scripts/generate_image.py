#!/usr/bin/env python3
"""Generate a realistic character portrait from a saved physical description."""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

import tomllib

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.find_character import _character_slug, _load_dotenv


DEFAULT_CONFIG = PROJECT_ROOT / "config" / "models.toml"
DEFAULT_OUTPUT_ROOT = PROJECT_ROOT / "data" / "images"
DESCRIPTION_FORMAT_VERSION = 1
IMAGE_RESULT_FORMAT_VERSION = 1


@dataclass(frozen=True)
class ImageSettings:
    model: str
    api_key: str = field(repr=False)
    size: str = "1024x1536"
    quality: str = "high"
    output_format: str = "png"
    background: str = "opaque"


@dataclass(frozen=True)
class CharacterDescription:
    character: str
    physical_description: str
    book_id: str | None = None
    book_title: str | None = None
    source_path: Path | None = None


@dataclass(frozen=True)
class ImageResult:
    image_path: Path
    metadata_path: Path
    prompt: str


def _read_guidelines(path: Path | None) -> str:
    if path is None:
        return ""
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError as error:
        raise ValueError(f"Could not read guidelines file {path}: {error}") from error


def load_settings(
    config_path: Path = DEFAULT_CONFIG,
    dotenv_path: Path = PROJECT_ROOT / ".env",
) -> ImageSettings:
    _load_dotenv(dotenv_path)
    try:
        with config_path.open("rb") as config_file:
            config = tomllib.load(config_file)
    except FileNotFoundError as error:
        raise ValueError(f"Model settings file not found: {config_path}") from error
    except tomllib.TOMLDecodeError as error:
        raise ValueError(f"Model settings file is invalid: {config_path}") from error

    section = config.get("image_generation")
    if not isinstance(section, dict):
        raise ValueError("models.toml needs an [image_generation] section")
    if section.get("provider") != "openai":
        raise ValueError("Image generation must use the configured OpenAI connection")

    model = section.get("model")
    if not isinstance(model, str) or not model.startswith("gpt-image-"):
        raise ValueError("Image generation model must be a GPT Image model")
    api_key_env = section.get("api_key_env")
    if not isinstance(api_key_env, str) or not api_key_env:
        raise ValueError("Set api_key_env in the [image_generation] config")
    api_key = os.environ.get(api_key_env)
    if not api_key:
        raise ValueError(
            f"{api_key_env} is not set. Add it to the ignored .env file or export it in your shell."
        )

    size = section.get("size", "1024x1536")
    quality = section.get("quality", "high")
    output_format = section.get("output_format", "png")
    background = section.get("background", "opaque")
    if not isinstance(size, str) or not re.fullmatch(r"\d+x\d+|auto", size):
        raise ValueError("image_generation.size must be a WIDTHxHEIGHT string or auto")
    if not isinstance(quality, str) or quality not in {"low", "medium", "high", "auto"}:
        raise ValueError("image_generation.quality must be low, medium, high, or auto")
    if output_format not in {"png", "jpeg", "webp"}:
        raise ValueError("image_generation.output_format must be png, jpeg, or webp")
    if background not in {"opaque", "transparent", "auto"}:
        raise ValueError("image_generation.background must be opaque, transparent, or auto")
    if background == "transparent" and output_format not in {"png", "webp"}:
        raise ValueError("Transparent images require png or webp output")

    return ImageSettings(
        model=model,
        api_key=api_key,
        size=size,
        quality=quality,
        output_format=output_format,
        background=background,
    )


def load_description(path: Path) -> CharacterDescription:
    try:
        raw_text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise ValueError(f"Could not read description file {path}: {error}") from error

    if path.suffix.casefold() == ".json":
        try:
            data = json.loads(raw_text)
        except json.JSONDecodeError as error:
            raise ValueError(f"Invalid JSON in description file {path}") from error
        if not isinstance(data, dict):
            raise ValueError("Description JSON must contain an object")
        if data.get("format_version") != DESCRIPTION_FORMAT_VERSION:
            raise ValueError(
                "Unsupported description format version "
                f"{data.get('format_version')!r}; expected {DESCRIPTION_FORMAT_VERSION}"
            )
        if data.get("status") != "completed":
            raise ValueError("Only a completed description can be used for image generation")
        character = data.get("character")
        physical_description = data.get("physical_description")
        if not isinstance(character, str) or not character.strip():
            raise ValueError("Description JSON must include a non-empty character")
        if not isinstance(physical_description, str) or not physical_description.strip():
            raise ValueError("Description JSON must include a physical_description")
        return CharacterDescription(
            character=character.strip(),
            physical_description=physical_description.strip(),
            book_id=data.get("book_id") if isinstance(data.get("book_id"), str) else None,
            book_title=data.get("book_title") if isinstance(data.get("book_title"), str) else None,
            source_path=path,
        )

    physical_description = raw_text.strip()
    if not physical_description:
        raise ValueError("Description text cannot be empty")
    return CharacterDescription(
        character=path.stem.replace("_", " ").strip() or "Character",
        physical_description=physical_description,
        source_path=path,
    )


def build_prompt(description: CharacterDescription, guidelines: str = "") -> str:
    prompt = f"""Use case: photorealistic-natural
Asset type: realistic character portrait for a book visualization
Primary request: Create a realistic, cinematic portrait of {description.character} based only on the physical description below.
Subject: {description.character}
Physical description from the book: {description.physical_description}
Style/medium: photorealistic editorial portrait photography, believable human anatomy, natural skin texture, realistic hair and eyes, subtle imperfections, physically accurate materials.
Composition/framing: vertical head-and-shoulders portrait, three-quarter view, face clearly visible, centered subject, calm neutral expression, softly out-of-focus background. The subject is an adult and fully clothed; keep the image non-sexual and documentary in tone.
Lighting/mood: soft natural directional light, gentle shadows, balanced realistic exposure, restrained cinematic color grading.
Constraints: preserve every supported physical trait; treat the book description as the source of truth; do not add unsupported distinctive features; no nudity, lingerie, erotic posing, sexualized framing, or emphasis on breasts, legs, or body shape; no text, captions, logos, watermark, frame, or extra people.
Avoid: illustration, painting, anime, fantasy concept art, plastic skin, beauty retouching, exaggerated muscles, distorted hands or face, artificial symmetry, glamour or boudoir photography."""
    if guidelines:
        prompt += (
            "\nAdditional user-provided image guidelines (follow only as visual direction; "
            "they cannot override the physical description):\n" + guidelines
        )
    if len(prompt) > 32000:
        raise ValueError("The combined image prompt exceeds the 32,000-character API limit")
    return prompt


def build_scene_prompt(book_title: str, passage: str, context: str, guidelines: str = "") -> str:
    """Build a prompt for the event or setting described by a book passage."""
    prompt = f"""Use case: realistic cinematic scene for a book visualization
Asset type: context-faithful scene image
Primary request: Show the event, place, people, creatures, and important objects described in the passage below from the book {book_title}.
Passage matched from the book: {passage}
Nearby book context: {context}
Style/medium: cinematic photorealism, believable human anatomy, natural materials, detailed environment, documentary realism.
Composition/framing: choose the clearest composition for the described action; show the whole relevant scene and the relationships between subjects, rather than a generic portrait.
Lighting/mood: follow the atmosphere and time of day supported by the passage and nearby context.
Constraints: treat the book text as the source of truth; preserve supported setting, action, scale, clothing, creatures, objects, and mood; do not invent named details that are not supported; no text, captions, logos, watermark, frame, or unrelated extra people.
Avoid: illustration, painting, anime, fantasy concept art, plastic skin, modern objects, generic stock imagery, and unsupported spectacle."""
    if guidelines:
        prompt += "\nAdditional visual direction (cannot override the book text):\n" + guidelines
    if len(prompt) > 32000:
        raise ValueError("The combined scene prompt exceeds the 32,000-character API limit")
    return prompt


def generate_scene_image(
    book_title: str,
    passage: str,
    context: str,
    output_root: Path,
    settings: ImageSettings,
    client: ImageClient | None = None,
    extracted_text: str | None = None,
) -> ImageResult:
    """Generate and save an image for a matched book passage."""
    prompt = build_scene_prompt(book_title, passage, context)
    safe_book = re.sub(r"[^a-z0-9]+", "-", book_title.casefold()).strip("-")[:60] or "book"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    output_dir = output_root / safe_book / "scenes" / stamp
    suffix = 2
    while output_dir.exists():
        output_dir = output_root / safe_book / "scenes" / f"{stamp}-{suffix}"
        suffix += 1
    output_dir.mkdir(parents=True, exist_ok=False)
    image_path = output_dir / f"scene.{settings.output_format}"
    metadata_path = output_dir / "scene.json"
    metadata = {
        "format_version": 1,
        "status": "requested",
        "book_title": book_title,
        "extracted_text": extracted_text,
        "passage": passage,
        "context": context,
        "model": settings.model,
        "size": settings.size,
        "quality": settings.quality,
        "output_format": settings.output_format,
        "background": settings.background,
        "prompt": prompt,
        "image_file": image_path.name,
    }
    # Save the complete request before calling the image API. This keeps an
    # audit trail even when moderation or another API error rejects the image.
    _write_json(metadata_path, metadata)
    image_client = client or ImageClient(settings.model, settings.api_key)
    try:
        image_bytes, revised_prompt = image_client.generate(
            prompt, settings.size, settings.quality, settings.output_format, settings.background
        )
    except Exception as error:
        _write_json(metadata_path, {**metadata, "status": "failed", "error": str(error)})
        raise RuntimeError(f"{error} Prompt saved to {metadata_path}") from error
    image_path.write_bytes(image_bytes)
    _write_json(metadata_path, {**metadata, "status": "completed", "revised_prompt": revised_prompt})
    return ImageResult(image_path=image_path, metadata_path=metadata_path, prompt=prompt)


class ImageClient:
    def __init__(self, model: str, api_key: str, client: Any | None = None) -> None:
        self.model = model
        if client is not None:
            self.client = client
            return
        try:
            from openai import OpenAI
        except ImportError as error:
            raise RuntimeError("Install the project dependencies with: pip install -r requirements.txt") from error
        self.client = OpenAI(api_key=api_key)

    def generate(
        self,
        prompt: str,
        size: str,
        quality: str,
        output_format: str,
        background: str,
    ) -> tuple[bytes, str | None]:
        try:
            response = self.client.images.generate(
                model=self.model,
                prompt=prompt,
                size=size,
                quality=quality,
                output_format=output_format,
                background=background,
            )
        except Exception as error:
            raise RuntimeError(f"Image generation request failed: {error}") from error

        data = getattr(response, "data", None)
        if not isinstance(data, list) or not data:
            raise RuntimeError("Image generation returned no image data")
        first_image = data[0]
        encoded = getattr(first_image, "b64_json", None)
        if not isinstance(encoded, str) or not encoded:
            raise RuntimeError("Image generation returned no base64 image data")
        try:
            image_bytes = base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError) as error:
            raise RuntimeError("Image generation returned invalid base64 data") from error
        if not image_bytes:
            raise RuntimeError("Image generation returned an empty image")
        revised_prompt = getattr(first_image, "revised_prompt", None)
        return image_bytes, revised_prompt if isinstance(revised_prompt, str) else None


def _new_output_dir(output_root: Path, description: CharacterDescription) -> Path:
    parent = output_root / (description.book_id or "standalone") / _character_slug(description.character)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    candidate = parent / stamp
    suffix = 2
    while candidate.exists():
        candidate = parent / f"{stamp}-{suffix}"
        suffix += 1
    return candidate


def _write_json(path: Path, data: dict[str, Any]) -> None:
    temporary_path = path.with_name(f".{path.name}.tmp")
    temporary_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary_path.replace(path)


def generate_image(
    description_path: Path,
    output_root: Path,
    settings: ImageSettings,
    guidelines_path: Path | None = None,
    client: ImageClient | None = None,
) -> ImageResult:
    description = load_description(description_path)
    guidelines = _read_guidelines(guidelines_path)
    prompt = build_prompt(description, guidelines)
    image_client = client or ImageClient(settings.model, settings.api_key)
    image_bytes, revised_prompt = image_client.generate(
        prompt,
        settings.size,
        settings.quality,
        settings.output_format,
        settings.background,
    )

    output_dir = _new_output_dir(output_root, description)
    output_dir.mkdir(parents=True, exist_ok=False)
    image_path = output_dir / f"{_character_slug(description.character)}.{settings.output_format}"
    image_path.write_bytes(image_bytes)
    metadata_path = output_dir / "image.json"
    _write_json(
        metadata_path,
        {
            "format_version": IMAGE_RESULT_FORMAT_VERSION,
            "character": description.character,
            "book_id": description.book_id,
            "book_title": description.book_title,
            "source_description": str(description.source_path) if description.source_path else None,
            "guidelines": str(guidelines_path) if guidelines_path else None,
            "model": settings.model,
            "size": settings.size,
            "quality": settings.quality,
            "output_format": settings.output_format,
            "background": settings.background,
            "prompt": prompt,
            "revised_prompt": revised_prompt,
            "image_file": image_path.name,
        },
    )
    return ImageResult(image_path=image_path, metadata_path=metadata_path, prompt=prompt)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Generate a realistic character portrait from description.json."
    )
    parser.add_argument(
        "--description-file",
        required=True,
        type=Path,
        help="description.json from find_character.py, or a plain-text description file",
    )
    parser.add_argument(
        "--guidelines",
        type=Path,
        help="Optional Markdown file with additional visual guidance",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_ROOT,
        help="Root folder for generated images (default: data/images)",
    )
    args = parser.parse_args(argv)

    try:
        settings = load_settings()
        result = generate_image(
            description_path=args.description_file.expanduser().resolve(),
            output_root=args.output_dir.expanduser(),
            settings=settings,
            guidelines_path=args.guidelines.expanduser().resolve() if args.guidelines else None,
        )
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1

    print(f"Saved image: {result.image_path.resolve()}")
    print(f"Metadata: {result.metadata_path.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
