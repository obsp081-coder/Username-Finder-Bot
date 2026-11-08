"""Memory-only result shaping, safe diagnostics and explicit local export."""

from __future__ import annotations

import csv
import json
import re
from pathlib import Path

from .osintgram_api import error_message
from .osintgram_display import MISSING as MISSING
from .osintgram_display import display_value as display_value

PRIVATE_KEY = re.compile(r"password|passwd|token|cookie|session|authorization|api[_-]?key|secret", re.I)


def safe_result(value, depth=0):
    """Redact credential-shaped fields; never echo arbitrary backend error text."""
    if depth > 80:
        return "[nesting limit]"
    if isinstance(value, dict):
        return {
            key: "[redacted]"
            if PRIVATE_KEY.search(key)
            else error_message(item)
            if key.lower() in ("error", "error_message", "detail")
            else safe_result(item, depth + 1)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [safe_result(item, depth + 1) for item in value]
    return value


def datasets(value):
    """Root plus nested lists, with bounded traversal; JSON retains all other fields."""
    found = [("النتيجة", value)]

    def visit(item, path, depth):
        if depth > 5 or len(found) >= 40:
            return
        if isinstance(item, dict):
            for key, child in list(item.items())[:100]:
                label = f"{path}.{key}" if path else key
                if isinstance(child, list):
                    found.append((label, child))
                elif isinstance(child, dict):
                    visit(child, label, depth + 1)
                if len(found) >= 40:
                    break

    visit(value, "", 0)
    return found


def table_data(value, bounded=True):
    """Return headers and raw rows; only the visible projection is bounded."""
    if isinstance(value, dict):
        items = list(value.items())
        if bounded:
            items = items[:300]
        return ["الحقل", "القيمة"], [list(pair) for pair in items]
    if isinstance(value, list):
        sample = value[:300] if bounded else value
        if all(isinstance(item, dict) for item in sample) and sample:
            keys = list(dict.fromkeys(key for item in sample for key in item))
            if bounded:
                keys = keys[:32]
            return keys, [[item.get(key, MISSING) for key in keys] for item in sample]
        return ["القيمة"], [[item] for item in sample]
    return ["القيمة"], [[value]]


def csv_compatible(value):
    def scalar(item):
        return item is None or isinstance(item, (str, int, float, bool))

    if isinstance(value, dict):
        return bool(value) and all(scalar(item) for item in value.values())
    if not isinstance(value, list) or not value:
        return False
    if all(scalar(item) for item in value):
        return True
    return (
        all(isinstance(item, dict) for item in value)
        and bool(value[0])
        and all(set(item) == set(value[0]) and all(scalar(v) for v in item.values()) for item in value)
    )


def json_preview(value):
    """Bound even nested/large text before formatting on the GUI thread."""
    remaining = [2000]

    def clip(item, depth=0):
        remaining[0] -= 1
        if remaining[0] < 0 or depth > 8:
            return "[preview limit; export JSON for complete retained data]"
        if isinstance(item, dict):
            result = {k: clip(v, depth + 1) for k, v in list(item.items())[:40]}
            if len(item) > 40:
                result["[preview]"] = "additional fields available in JSON export"
            return result
        if isinstance(item, list):
            result = [clip(v, depth + 1) for v in item[:40]]
            if len(item) > 40:
                result.append("[additional items available in JSON export]")
            return result
        return item[:4000] + "…" if isinstance(item, str) and len(item) > 4000 else item

    text = json.dumps(clip(value), ensure_ascii=False, indent=2)
    return text[:65536] + ("\n[preview truncated]" if len(text) > 65536 else "")


def export_json(path, document):
    Path(path).write_text(
        json.dumps(document, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8"
    )


def export_csv(path, value):
    if not csv_compatible(value):
        raise ValueError("CSV requires a flat, consistent dataset")
    headers, rows = table_data(value, bounded=False)
    with Path(path).open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream)
        # JSON-encoded cells preserve null/empty text/numeric IDs and prevent spreadsheet formulas.
        writer.writerow([json.dumps(header, ensure_ascii=False) for header in headers])
        writer.writerows(
            [[json.dumps(cell, ensure_ascii=False, allow_nan=False) for cell in row] for row in rows]
        )
