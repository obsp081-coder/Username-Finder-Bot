from hypothesis import given, settings
from hypothesis import strategies as st

from username_finder.domain import Filters, syntax_error
from username_finder.generation import GeneratorSpace


@settings(max_examples=70, deadline=None)
@given(st.integers(3, 15), st.sampled_from(["", "_", ".-", "_.-"]), st.booleans(), st.booleans())
def test_rank_boundaries_obey_every_filter(length, specials, letters, digits):
    filters = Filters(length, length, letters=letters, digits=digits, specials=specials)
    space = GeneratorSpace(filters)
    if space.total:
        for rank in {0, space.total // 2, space.total - 1}:
            name = space.unrank(rank)
            assert syntax_error(name) is None
            assert filters.matches(name)


@settings(max_examples=60, deadline=None)
@given(
    st.integers(0, 1295), st.sampled_from(["a", "b", "z"]), st.sampled_from(["any", "adjacent", "separated"])
)
def test_specific_repeat_count_matches_reference(rank, target, adjacency):
    filters = Filters(3, 3, prefix="a", repeat_target=target, repeat_exact=False, adjacency=adjacency)
    space = GeneratorSpace(filters)
    if space.total:
        assert filters.matches(space.unrank(rank % space.total))
