import base64
import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace

from scripts.generate_image import (
    ImageClient,
    ImageSettings,
    build_prompt,
    generate_image,
    load_description,
    load_settings,
)


class FakeImagesAPI:
    def __init__(self) -> None:
        self.arguments: dict[str, object] = {}

    def generate(self, **kwargs: object) -> SimpleNamespace:
        self.arguments = kwargs
        encoded = base64.b64encode(b"fake-png-bytes").decode("ascii")
        return SimpleNamespace(
            data=[SimpleNamespace(b64_json=encoded, revised_prompt="A revised portrait prompt.")]
        )


class FakeImageAPIClient:
    def __init__(self) -> None:
        self.images = FakeImagesAPI()


class GenerateImageTests(unittest.TestCase):
    def test_generates_image_and_metadata_from_description_json(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            description_path = root / "description.json"
            description_path.write_text(
                json.dumps(
                    {
                        "format_version": 1,
                        "status": "completed",
                        "book_id": "red-rising-test",
                        "book_title": "Red Rising",
                        "character": "Darrow when he was a Red",
                        "physical_description": "Tall, pale, rusty red hair, red eyes, and scarred hands.",
                    }
                ),
                encoding="utf-8",
            )
            guidelines_path = root / "guidelines.md"
            guidelines_path.write_text("Keep the portrait grounded and documentary-like.", encoding="utf-8")
            fake_client = FakeImageAPIClient()
            result = generate_image(
                description_path,
                root / "images",
                ImageSettings("gpt-image-2", "test-key"),
                guidelines_path,
                ImageClient("gpt-image-2", "test-key", client=fake_client),
            )

            self.assertEqual(result.image_path.read_bytes(), b"fake-png-bytes")
            metadata = json.loads(result.metadata_path.read_text(encoding="utf-8"))
            self.assertEqual(metadata["character"], "Darrow when he was a Red")
            self.assertEqual(metadata["model"], "gpt-image-2")
            self.assertIn("rusty red hair", metadata["prompt"])
            self.assertIn("documentary-like", metadata["prompt"])
            self.assertEqual(fake_client.images.arguments["size"], "1024x1536")
            self.assertEqual(fake_client.images.arguments["quality"], "high")
            self.assertEqual(fake_client.images.arguments["output_format"], "png")

    def test_plain_text_description_is_supported(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "character.txt"
            path.write_text("Dark hair and brown eyes.", encoding="utf-8")
            description = load_description(path)
            self.assertEqual(description.character, "character")
            self.assertEqual(description.physical_description, "Dark hair and brown eyes.")

    def test_incomplete_description_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "description.json"
            path.write_text(
                json.dumps(
                    {
                        "format_version": 1,
                        "status": "no_evidence",
                        "character": "Darrow",
                        "physical_description": None,
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "completed description"):
                load_description(path)

    def test_image_config_requires_image_model(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            config_path = Path(temporary_directory) / "models.toml"
            config_path.write_text(
                '[image_generation]\nprovider="openai"\nmodel="gpt-5.6-luna"\napi_key_env="OPENAI_API_KEY"\n',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "GPT Image"):
                load_settings(config_path, Path(temporary_directory) / "missing.env")

    def test_prompt_keeps_physical_description_as_source_of_truth(self) -> None:
        description = load_description_from_values()
        prompt = build_prompt(description)
        self.assertIn("preserve every supported physical trait", prompt)
        self.assertIn(description.physical_description, prompt)
        self.assertIn("no text, captions, logos, watermark", prompt)


def load_description_from_values():
    from scripts.generate_image import CharacterDescription

    return CharacterDescription(
        character="Darrow as a Red",
        physical_description="Rusty red hair and red eyes.",
    )


if __name__ == "__main__":
    unittest.main()
