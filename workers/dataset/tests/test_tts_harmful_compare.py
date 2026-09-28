from __future__ import annotations

import unittest

from speechcraft_dataset.tts_harmful_compare import compare_tts_harmful


class TtsHarmfulCompareTests(unittest.TestCase):
    def test_punctuation_and_case_are_neutral(self) -> None:
        result = compare_tts_harmful("Hello, world.", "hello world")
        self.assertFalse(result.harmful_mismatch)

    def test_empty_strings(self) -> None:
        result = compare_tts_harmful("", "")
        self.assertFalse(result.harmful_mismatch)

    def test_identical_text(self) -> None:
        result = compare_tts_harmful("hello world", "hello world")
        self.assertFalse(result.harmful_mismatch)

    def test_content_word_replacement_is_harmful(self) -> None:
        result = compare_tts_harmful("I thought so", "I though so")
        self.assertTrue(result.harmful_mismatch)
        self.assertTrue(any(diff.category == "content_word" for diff in result.diffs))

    def test_word_order_change_is_harmful(self) -> None:
        result = compare_tts_harmful("the cat sat", "sat the cat")
        self.assertTrue(result.harmful_mismatch)
        self.assertTrue(any(diff.category == "word_order" for diff in result.diffs))

    def test_stutter_is_harmful(self) -> None:
        result = compare_tts_harmful("He-He had a plan.", "he had a plan")
        self.assertTrue(result.harmful_mismatch)

    def test_contraction_variant_is_harmful(self) -> None:
        result = compare_tts_harmful("I am going to leave", "I am gonna leave")
        self.assertTrue(result.harmful_mismatch)

    def test_ok_vs_okay_is_harmful(self) -> None:
        result = compare_tts_harmful("that's ok", "that's okay")
        self.assertTrue(result.harmful_mismatch)

    def test_filler_difference_is_harmful(self) -> None:
        result = compare_tts_harmful("hello world", "hello um world")
        self.assertTrue(result.harmful_mismatch)

    def test_number_in_text_is_harmful(self) -> None:
        result = compare_tts_harmful("about 12 hours", "about twelve hours")
        self.assertTrue(result.harmful_mismatch)


if __name__ == "__main__":
    unittest.main()
