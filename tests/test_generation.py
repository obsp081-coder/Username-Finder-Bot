import itertools
import unittest

from username_finder.domain import DIGITS, LETTERS, Filters, normalize, syntax_error
from username_finder.generation import GeneratorSpace
from username_finder.providers import classify_observation


class GenerationTests(unittest.TestCase):
    def test_syntax_and_normalization(self):
        for name in ["abc", "a_b", "ab9", "a.b", "a-b"]:
            self.assertIsNone(syntax_error(name))
        for name in ["ar", "1abc", "abc_", "a_.b", "a@b", "a ب", "a" * 16]:
            self.assertIsNotNone(syntax_error(name))
        self.assertEqual(normalize(" @AbC00 \r\n"), "@abc00")

    def test_exact_counts_and_large_rank(self):
        for n in (3, 4, 15):
            f = Filters(n, n, specials="_.-")
            space = GeneratorSpace(f)
            expected = 26 * 36 ** (n - 1) + 26 * (n - 2) * 3 * 36 ** (n - 2)
            self.assertEqual(space.exact_count, expected)
            for rank in (0, expected // 2, expected - 1):
                self.assertTrue(f.matches(space.unrank(rank)))
        self.assertGreater(expected, 2**63)

    def test_reference_enumeration_with_filters(self):
        variants = [
            Filters(3, 3, prefix="a", specials="_.-"),
            Filters(3, 3, prefix="a", required="b"),
            Filters(3, 3, prefix="a", repeat_target="a", repeat_count=2),
            Filters(3, 3, prefix="a", repeat_target="a", adjacency="separated"),
            Filters(3, 3, prefix="a", repeat_target="a", adjacency="adjacent"),
            Filters(3, 3, prefix="a", repeat_target="any", repeat_exact=False),
            Filters(3, 3, letters=False),
            Filters(3, 3, prefix="ab", suffix="bc"),
        ]
        for f in variants:
            with self.subTest(f=f):
                reference = {
                    a + b + c
                    for a, b, c in itertools.product(LETTERS, LETTERS + DIGITS + "_.-", LETTERS + DIGITS)
                    if f.matches(a + b + c)
                }
                actual = list(GeneratorSpace(f).iter_from())
                self.assertEqual(set(actual), reference)
                self.assertEqual(len(actual), len(reference))

    def test_repetition_one_target_and_conflicts(self):
        self.assertTrue(Filters(3, 15, repeat_target="any").matches("aabb"))
        self.assertFalse(Filters(3, 15, repeat_target="a", adjacency="separated").matches("aabb"))
        self.assertEqual(GeneratorSpace(Filters(3, 3, prefix="1")).total, 0)
        self.assertEqual(GeneratorSpace(Filters(3, 3, letters=False, digits=False)).total, 0)
        self.assertEqual(GeneratorSpace(Filters(3, 3, prefix="ab", suffix="cd")).total, 0)

    def test_ambiguous_observations(self):
        for kind in ["404", "403", "429", "timeout", "network_error", "unavailable_unspecified"]:
            self.assertEqual(classify_observation(kind, validated=True), "Unknown")
        self.assertEqual(classify_observation("explicitly_available"), "Unknown")
        self.assertEqual(classify_observation("explicitly_available", validated=True), "Available")

    def test_extremely_rare_any_repeat_is_generated_directly(self):
        filters = Filters(15, 15, repeat_target="any", repeat_count=15)
        space = GeneratorSpace(filters)
        self.assertEqual(set(space.iter_from()), {letter * 15 for letter in LETTERS})
        self.assertEqual(space.total, 26)
        self.assertIsNone(space.exact_count)
