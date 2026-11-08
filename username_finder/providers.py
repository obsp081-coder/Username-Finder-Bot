from __future__ import annotations

import asyncio
import hashlib
import time
from typing import Protocol

from .domain import RULES_VERSION, Classification, Evidence, syntax_error


class ProviderFailure(Exception):
    def __init__(self, code: str, retry_after: float = 0, retryable: bool = True):
        super().__init__(code)
        self.code = code
        self.retry_after = max(0, retry_after)
        self.retryable = retryable


class Verifier(Protocol):
    name: str
    version: str
    mode: str
    safe_concurrency: int
    min_interval: float

    async def verify(self, username: str) -> Evidence: ...


class MockVerifier:
    """Deterministic local simulation; never makes network requests."""

    name = "local-mock"
    version = "1"
    mode = "simulated"
    safe_concurrency = 1000
    min_interval = 0.0

    def __init__(self, delay: float = 0.005):
        self.delay = delay

    async def verify(self, username: str) -> Evidence:
        started = time.perf_counter()
        await asyncio.sleep(self.delay)
        statuses = list(Classification)
        index = int.from_bytes(hashlib.sha256(username.encode("ascii")).digest()[:2], "big") % len(statuses)
        status = statuses[index]
        kind = {
            Classification.AVAILABLE: "explicitly_available",
            Classification.TAKEN: "explicitly_taken",
            Classification.RESERVED: "explicitly_reserved",
            Classification.INVALID: "explicitly_invalid",
            Classification.UNKNOWN: "simulated_ambiguous",
        }[status]
        return Evidence(
            status,
            kind,
            self.name,
            self.version,
            self.mode,
            trustworthy=status != Classification.UNKNOWN,
            latency_ms=(time.perf_counter() - started) * 1000,
        )


class DisabledSnapchatVerifier:
    name = "snapchat-disabled"
    version = "unvalidated"
    mode = "real"
    safe_concurrency = 1
    min_interval = 1.0

    async def verify(self, username: str) -> Evidence:
        raise ProviderFailure("LIVE_VERIFICATION_NOT_VALIDATED", retryable=False)


def local_invalid(mode: str) -> Evidence:
    return Evidence(Classification.INVALID, "syntax_invalid", "local-syntax", RULES_VERSION, mode, True)


def classify_observation(kind: str, *, validated: bool = False) -> Classification:
    """A transport status or generic rejection cannot prove availability."""
    if kind == "syntax_invalid":
        return Classification.INVALID
    if not validated:
        return Classification.UNKNOWN
    return {
        "explicitly_available": Classification.AVAILABLE,
        "explicitly_taken": Classification.TAKEN,
        "account_exists": Classification.TAKEN,
        "explicitly_reserved": Classification.RESERVED,
        "explicitly_invalid": Classification.INVALID,
    }.get(kind, Classification.UNKNOWN)


async def verify_locally_first(provider: Verifier, username: str) -> Evidence:
    if syntax_error(username):
        return local_invalid(provider.mode)
    return await provider.verify(username)
