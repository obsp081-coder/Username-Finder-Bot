"""X-only generation, local preparation and preliminary evidence adaptation."""

from __future__ import annotations

import hashlib
import json
import math
import secrets
import string
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime

from . import x_protocol as protocol
from .generation_ranges import allocations, interleave
from .roblox import input_spec as input_spec
from .roblox import input_values  # Platform-independent UTF-8 TXT/CSV reader.

INTERVALS = (30.0, 15.0, 10.0, 5.0, 4.0, 3.0, 2.0, 1.5, 1.0, 0.5, 0.0)
WARNING = (
    "\u2066X\u2069 تجريبي • نقطة التحقق غير رسمية.\n"
    "\u2066Available\u2069 إشارة أولية لا تضمن التسجيل؛ قد تختلف واجهة تغيير الاسم.\n"
    "تعارض \u2066hzwu9\u2069 لم يُحل. طول 15 مقبول محليًا مع اختلاف التوثيق؛ صحة الصيغة لا تثبت الإتاحة."
)
LABELS = {
    "Available": "متاح أوليًا — غير مؤكد (Available)",
    "Taken": "مستخدم أوليًا (Taken)",
    "Reserved": "مرفوض من المنصة (Reserved)",
    "Invalid": "صيغة غير صالحة (Invalid)",
    "Unknown": "غير محسوم (Unknown)",
}


def now():
    return datetime.now(UTC).isoformat()


def interval(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value not in INTERVALS:
        raise ValueError("اختر أحد فواصل X المتاحة من القائمة؛ الافتراضي 5 ثوانٍ.")
    return float(value)


def restriction_wait(headers, checked_at):
    """Read server minimum-wait evidence; it never authorizes clearing a restriction."""
    dates, sources, invalid = [], [], []
    try:
        observed = datetime.fromisoformat(checked_at)
        if observed.tzinfo is None:
            raise ValueError
    except (ValueError, TypeError):
        observed = None
    if "retry-after" in headers:
        value = headers["retry-after"]
        try:
            if isinstance(value, str) and value.strip().isascii() and value.strip().isdecimal():
                if observed is None:
                    raise ValueError
                date = observed + timedelta(seconds=int(value.strip()))
            else:
                date = parsedate_to_datetime(value)
                if date.tzinfo is None:
                    raise ValueError
            dates.append(date.astimezone(UTC))
            sources.append("retry-after")
        except (ValueError, TypeError, OverflowError, AttributeError):
            invalid.append("retry-after")
    for prefix in ("x-rate-limit-", "x-ratelimit-"):
        if str(headers.get(prefix + "remaining", "")).strip() != "0":
            continue
        key = prefix + "reset"
        try:
            value = headers.get(key, "")
            if not isinstance(value, str) or not value.strip().isascii() or not value.strip().isdecimal():
                raise ValueError
            dates.append(datetime.fromtimestamp(int(value.strip()), UTC))
            sources.append(key)
        except (ValueError, OverflowError, OSError):
            invalid.append(key)
    return {
        "minimum_retry_at": max(dates).isoformat() if dates else None,
        "wait_sources": sources,
        "invalid_wait_headers": invalid,
        "expiry_known": False,
    }


def generation_spec(length, quantity):
    if type(length) is not int or not 5 <= length <= 15:
        raise ValueError("طول اسم X من 5 إلى 15 محرفًا.")
    total = 37**length
    if type(quantity) is not int or not 1 <= quantity <= total:
        raise ValueError("كمية التوليد تتجاوز المساحة الفريدة أو ليست موجبة.")
    multiplier = secrets.randbelow(total - 1) + 1
    while math.gcd(multiplier, total) != 1:
        multiplier = multiplier % (total - 1) + 1
    return dict(length=length, quantity=quantity, multiplier=multiplier, offset=secrets.randbelow(total))


def generation_range_spec(minimum, maximum, quantity):
    counts = allocations(minimum, maximum, quantity, lower=5, upper=15, capacity=lambda length: 37**length)
    if minimum == maximum:
        return generation_spec(minimum, quantity)
    return {
        "minimum": minimum,
        "maximum": maximum,
        "quantity": quantity,
        "length_specs": [generation_spec(length, count) for length, count in counts.items()],
    }


def _generated_names(config):
    alphabet = string.ascii_lowercase + string.digits + "_"
    for index in range(config["quantity"]):
        rank = (config["multiplier"] * index + config["offset"]) % (37 ** config["length"])
        chars = []
        for _ in range(config["length"]):
            rank, digit = divmod(rank, 37)
            chars.append(alphabet[digit])
        yield "".join(reversed(chars))


def candidates(spec):
    if "generation" not in spec:
        yield from input_values(spec)
        return
    config = spec["generation"]
    if "length_specs" in config:
        yield from interleave(_generated_names(part) for part in config["length_specs"])
    else:
        yield from _generated_names(config)


def result_for(username, response, *, checked_at=None, latency=None):
    classification, reason, parsed = protocol.parse(response, username)
    raw = response.body.decode("utf-8", errors="replace")
    sanitized = protocol.redact(parsed) if parsed is not None else None
    safe_body = (
        json.dumps(sanitized, ensure_ascii=False)
        if parsed is not None and sanitized != parsed
        else protocol.redact(raw)
    )
    return dict(
        username=username,
        checked_at=checked_at or now(),
        classification=classification,
        reason=reason,
        api_reason=parsed.get("reason") if isinstance(parsed, dict) else None,
        parsed_fields=sanitized,
        http_status=response.status,
        headers=protocol.redact(response.headers),
        response_data=safe_body,
        raw_sha256=hashlib.sha256(response.body).hexdigest(),
        raw_complete=response.complete,
        error=response.error,
        latency_seconds=latency,
        redaction_applied=safe_body != raw,
        preliminary=True,
        independently_verified=False,
        warning=WARNING,
        syntax=protocol.validate_syntax(username),
        parameters={"username": username},
        endpoint=protocol.ENDPOINT,
    )
