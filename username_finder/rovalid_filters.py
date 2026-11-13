"""Validated, case-insensitive predicates shared by streaming RoValid display and export."""

from dataclasses import dataclass


@dataclass(frozen=True)
class ResultFilters:
    length: str = "all"
    exact_length: int = 4
    underscore: str = "all"
    digits: str = "all"
    digit_count: str = "all"
    repetition: str = "all"
    start: str = "all"
    end: str = "all"
    prefix: str = ""
    suffix: str = ""
    text_mode: str = "contains"
    text: str = ""
    classification: str = ""

    def __post_init__(self):
        choices = {
            "length": ("all", "3", "4", "5", "6", "7+", "exact"),
            "underscore": ("all", "yes", "no"),
            "digits": ("all", "none", "some", "only"),
            "digit_count": ("all", "0", "1", "2", "3+"),
            "repetition": ("all", "none", "any", "adjacent", "abab", "identical"),
            "start": ("all", "letter", "digit"),
            "end": ("all", "letter", "digit"),
            "text_mode": ("contains", "starts", "ends"),
            # Legacy helper callers may request aggregate classes: no persisted rows match them.
            # The GUI offers only the two per-name evidence classes.
            "classification": ("", "Available", "Unknown", "Taken", "Censored", "Invalid"),
        }
        for key, values in choices.items():
            if getattr(self, key) not in values:
                raise ValueError("Unsupported result filter: " + key)
        if type(self.exact_length) is not int or not 1 <= self.exact_length <= 20:
            raise ValueError("Exact length must be 1..20")
        for key in ("prefix", "suffix", "text"):
            if not isinstance(getattr(self, key), str) or len(getattr(self, key)) > 256:
                raise ValueError("Invalid filter text")

    def matches(self, row):
        name = row[0].casefold()
        if self.classification and row[1] != self.classification:
            return False
        if self.length == "exact" and len(name) != self.exact_length:
            return False
        if self.length in ("3", "4", "5", "6") and len(name) != int(self.length):
            return False
        if self.length == "7+" and len(name) < 7:
            return False
        if self.underscore != "all" and ("_" in name) != (self.underscore == "yes"):
            return False
        count = sum(c in "0123456789" for c in name)
        if (
            (self.digits == "none" and count)
            or (self.digits == "some" and not count)
            or (self.digits == "only" and (not name or count != len(name)))
        ):
            return False
        if self.digit_count != "all":
            if self.digit_count == "3+":
                if count < 3:
                    return False
            elif count != int(self.digit_count):
                return False
        repeated = len(set(name)) < len(name)
        patterns = {
            "all": True,
            "none": not repeated,
            "any": repeated,
            "adjacent": any(a == b for a, b in zip(name, name[1:], strict=False)),
            "abab": len(name) == 4 and name[0] == name[2] and name[1] == name[3] and name[0] != name[1],
            "identical": bool(name) and len(set(name)) == 1,
        }
        if not patterns[self.repetition]:
            return False
        for key, char in ((self.start, name[:1]), (self.end, name[-1:])):
            if key == "letter" and char not in "abcdefghijklmnopqrstuvwxyz":
                return False
            if key == "digit" and char not in "0123456789":
                return False
        if not name.startswith(self.prefix.casefold()) or not name.endswith(self.suffix.casefold()):
            return False
        text = self.text.casefold()
        return {
            "contains": lambda: text in name,
            "starts": lambda: name.startswith(text),
            "ends": lambda: name.endswith(text),
        }[self.text_mode]()


def parse_filters(value=None, *, query="", classification=""):
    if value is None:
        return ResultFilters(text=query, classification=classification)
    if not isinstance(value, dict):
        raise ValueError("Filters must be an object")
    try:
        return ResultFilters(**value)
    except TypeError as exc:
        raise ValueError("Unsupported filters") from exc
