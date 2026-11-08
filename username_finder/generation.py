from __future__ import annotations

from dataclasses import replace
from functools import cache

from .domain import DIGITS, LETTERS, SPECIALS, Filters


class LengthSpace:
    """Count/rank directly using small DP states, never materializing the space."""

    def __init__(self, filters: Filters, length: int):
        self.filters = filters
        self.length = length
        body = (LETTERS if filters.letters else "") + (DIGITS if filters.digits else "")
        self.options: list[str] = []
        for pos in range(length):
            choices = LETTERS if pos == 0 else body + (filters.specials if pos < length - 1 else "")
            fixed = []
            if pos < len(filters.prefix):
                fixed.append(filters.prefix[pos])
            suffix_pos = pos - (length - len(filters.suffix))
            if suffix_pos >= 0:
                fixed.append(filters.suffix[suffix_pos])
            if fixed:
                choices = "".join(c for c in choices if all(c == item for item in fixed))
            if len(filters.prefix) > length or len(filters.suffix) > length:
                choices = ""
            self.options.append(choices)
        self.target = filters.repeat_target if filters.repeat_target != "any" else ""
        self.count = cache(self._count)

    def advance(self, state: tuple, char: str) -> tuple | None:
        pos, special, match, occurrences, run, max_run = state
        f = self.filters
        special += char in SPECIALS
        if special > 1:
            return None
        if f.required and match < len(f.required):
            text = f.required[:match] + char
            match = max(k for k in range(len(f.required) + 1) if text.endswith(f.required[:k]))
        if self.target:
            if char == self.target:
                occurrences += 1
                run += 1
                max_run = max(max_run, run)
                if f.repeat_exact and occurrences > f.repeat_count:
                    return None
                if f.adjacency == "separated" and run > 1:
                    return None
            else:
                run = 0
            if occurrences + self.length - pos - 1 < f.repeat_count:
                return None
            occurrences = min(occurrences, f.repeat_count + 1)
            run = min(run, f.repeat_count)
            max_run = min(max_run, f.repeat_count)
        return pos + 1, special, match, occurrences, run, max_run

    def _count(self, state: tuple) -> int:
        pos, _, match, occurrences, _, max_run = state
        f = self.filters
        if pos == self.length:
            if match != len(f.required):
                return 0
            if self.target:
                if occurrences < f.repeat_count or (f.repeat_exact and occurrences != f.repeat_count):
                    return 0
                if f.adjacency == "adjacent" and max_run < f.repeat_count:
                    return 0
            return 1
        total = 0
        for char in self.options[pos]:
            next_state = self.advance(state, char)
            if next_state is not None:
                total += self.count(next_state)
        return total

    @property
    def total(self) -> int:
        return self.count((0, 0, 0, 0, 0, 0))

    def unrank(self, rank: int) -> str:
        if not 0 <= rank < self.total:
            raise IndexError(rank)
        state = (0, 0, 0, 0, 0, 0)
        result = []
        for choices in self.options:
            for char in choices:
                next_state = self.advance(state, char)
                size = 0 if next_state is None else self.count(next_state)
                if rank < size:
                    result.append(char)
                    state = next_state
                    break
                rank -= size
        return "".join(result)


class GeneratorSpace:
    def __init__(self, filters: Filters):
        filters.validate()
        self.filters = filters
        if filters.repeat_target == "any":
            # Enumerate constrained symbol spaces directly; never scan the astronomical
            # unconstrained universe looking for a rare repetition pattern.
            self.spaces = [
                LengthSpace(replace(filters, repeat_target=target), n)
                for target in LETTERS + DIGITS
                for n in range(filters.min_length, filters.max_length + 1)
            ]
        else:
            self.spaces = [LengthSpace(filters, n) for n in range(filters.min_length, filters.max_length + 1)]
        self.total = sum(space.total for space in self.spaces)

    @property
    def exact_count(self) -> int | None:
        return None if self.filters.repeat_target == "any" else self.total

    def unrank(self, rank: int) -> str:
        if rank < 0:
            raise IndexError(rank)
        for space in self.spaces:
            if rank < space.total:
                return space.unrank(rank)
            rank -= space.total
        raise IndexError(rank)

    def iter_from(self, rank: int = 0):
        for space in self.spaces:
            if rank >= space.total:
                rank -= space.total
                continue
            while rank < space.total:
                name = space.unrank(rank)
                rank += 1
                if self.filters.repeat_target == "any":
                    # A deterministic owner removes overlaps without an unbounded seen set.
                    owner = next(
                        target
                        for target in LETTERS + DIGITS
                        if target in name and replace(self.filters, repeat_target=target).matches(name)
                    )
                    if space.filters.repeat_target != owner:
                        continue
                yield name
            rank = 0

    def describe(self) -> str:
        if self.total == 0:
            return "لا توجد احتمالات؛ راجع الطول والبداية والنهاية والأنواع وشرط التكرار."
        if self.exact_count is None:
            return f"الحد الأعلى لاتحاد شروط أي رمز مكرر: {self.total:,}؛ العدد الفريد النهائي غير محسوب."
        return f"عدد الاحتمالات الدقيق: {self.total:,}"
