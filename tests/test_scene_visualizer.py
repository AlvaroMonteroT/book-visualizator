import unittest

from scripts.find_character import Paragraph
from scripts.scene_visualizer import find_passage


class SceneVisualizerTests(unittest.TestCase):
    def test_matches_photographed_text_and_returns_nearby_context(self) -> None:
        paragraphs = [
            Paragraph(1, 1, "The rain fell over the silent city."),
            Paragraph(1, 2, "Mara opened the red door and stepped into the hall."),
            Paragraph(1, 3, "A brass lantern burned beside the stairs."),
        ]
        match = find_passage(paragraphs, "Mara opened the red door and stepped into the hall.")
        self.assertEqual(match.paragraph.reference, (1, 2))
        self.assertEqual([paragraph.reference for paragraph in match.context], [(1, 1), (1, 2), (1, 3)])
        self.assertEqual(match.confidence, 1.0)

    def test_rejects_unmatched_short_text(self) -> None:
        with self.assertRaises(ValueError):
            find_passage([Paragraph(1, 1, "A long paragraph with enough words.")], "hello")

    def test_rejects_a_weak_match_instead_of_using_an_unrelated_paragraph(self) -> None:
        paragraphs = [
            Paragraph(73, 57, "R"),
            Paragraph(73, 58, "The mansion rises above symmetrical gardens and a long pool."),
        ]
        with self.assertRaises(ValueError):
            find_passage(paragraphs, "A completely different photographed passage with enough words to test matching.")


if __name__ == "__main__":
    unittest.main()
