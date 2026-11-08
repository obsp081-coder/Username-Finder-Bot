import csv
import sqlite3
import ssl

import pytest
from test_overnight import execute
from test_overnight import rigs as rigs

from username_finder import roblox_protocol, x_protocol
from username_finder.result_search import ResultFilter, ResultSearch


@pytest.mark.parametrize("platform", ["Roblox", "X"])
@pytest.mark.parametrize(
    "kind",
    [
        "tls",
        "read_timeout",
        "malformed",
        "contradictory",
        "challenge",
        "retry_after_invalid",
        "429_unknown_end",
        "401",
        "403",
    ],
)
def test_unrecoverable_responses_never_enter_transport_retry_loop(rigs, platform, kind):
    rig = rigs(platform)
    protocol = roblox_protocol if platform == "Roblox" else x_protocol
    if kind == "tls":
        response = ssl.SSLCertVerificationError("credential-like text is not a diagnostic")
    elif kind == "read_timeout":
        # A response began; even a network error cannot be treated as a pre-response retry.
        response = protocol.Response(
            200, b"partial", {"content-type": "application/json"}, error="timeout", complete=False
        )
    elif kind in ("malformed", "contradictory"):
        body = (
            b"{"
            if kind == "malformed"
            else (b'{"code":0,"message":7}' if platform == "Roblox" else b'{"valid":true,"reason":"taken"}')
        )
        response = protocol.Response(200, body, {"content-type": "application/json"})
    elif kind == "challenge":
        response = protocol.Response(403, b"challenge", {"challenge-indicator": "present"})
        if platform == "Roblox":
            response.challenge = True
    else:
        status = int(kind) if kind.isdecimal() else 429
        response = protocol.Response(
            status, b"", {"retry-after": "nonsense"} if kind == "retry_after_invalid" else {}
        )
    execute(rig, [response])
    assert len(rig.calls) == 1
    assert rig.repo.results()[0][0]["classification"] == "Unknown"
    assert rig.night.get(rig.identity)["attempts"] == 0
    assert rig.night.get(rig.identity)["state"] == "Review"
    assert not rig.night.startup_candidates(rig.repo)
    assert "credential-like" not in str(rig.night.get(rig.identity))


@pytest.mark.parametrize("platform", ["Roblox", "X"])
def test_error_after_committed_response_keeps_original_evidence(rigs, platform):
    rig = rigs(platform)

    def updated():
        raise RuntimeError("failure after result commit")

    # The final notification can fail as well; the database must still contain one known observation.
    with pytest.raises(RuntimeError):
        execute(rig, updated=updated)
    assert len(rig.calls) == 1
    assert rig.repo.results()[0][0]["classification"] == "Available"
    assert len(rig.repo.history("NightOne")) == 1
    assert rig.night.get(rig.identity)["state"] == "Review"


@pytest.mark.parametrize("platform", ["Roblox", "X"])
def test_failure_during_pacing_never_reissues_previous_result(rigs, platform):
    rig = rigs(platform)

    def broken_wait(_):
        raise OSError("wait subsystem unavailable")

    execute(rig, wait=broken_wait)
    assert len(rig.calls) == 1 and rig.repo.results()[1] == 1
    assert rig.repo.results()[0][0]["classification"] == "Available"
    assert rig.night.get(rig.identity)["state"] == "Review"


def test_transaction_body_failure_is_not_retried_or_partly_committed(rigs):
    rig = rigs("Roblox")
    entered = []
    with pytest.raises(sqlite3.OperationalError, match="I/O"):
        with rig.store.transaction() as db:
            entered.append(1)
            db.execute("UPDATE roblox_runs SET error='not committed' WHERE id=?", (rig.identity,))
            raise sqlite3.OperationalError("simulated disk I/O error")
    assert entered == [1]
    assert rig.repo.progress(rig.identity)["error"] == ""
    assert not rig.store.db.in_transaction


def test_commit_failure_rolls_back_without_replaying_transaction(rigs):
    rig = rigs("Roblox")
    rig.store.db.close()

    class FailedCommit(sqlite3.Connection):
        def commit(self):
            raise sqlite3.OperationalError("simulated commit I/O failure")

    rig.store.db = sqlite3.connect(rig.store.path, factory=FailedCommit)
    rig.store.db.row_factory = sqlite3.Row
    entered = []
    with pytest.raises(sqlite3.OperationalError, match="commit I/O"):
        with rig.store.transaction() as db:
            entered.append(1)
            db.execute("UPDATE roblox_runs SET error='not committed' WHERE id=?", (rig.identity,))
    assert entered == [1] and not rig.store.db.in_transaction
    assert rig.repo.progress(rig.identity)["error"] == ""


def test_csv_write_failure_preserves_destination_and_database(rigs, tmp_path, monkeypatch):
    rig = rigs("Roblox")
    execute(rig)
    destination = tmp_path / "existing.csv"
    destination.write_text("original", encoding="utf-8")
    original = csv.DictWriter.writerow
    written = []

    def failing(writer, row):
        written.append(1)
        if len(written) == 3:
            raise OSError("simulated disk-full export")
        return original(writer, row)

    monkeypatch.setattr(csv.DictWriter, "writerow", failing)
    with pytest.raises(OSError):
        ResultSearch(rig.store.path).export(destination, "Roblox", ResultFilter())
    assert destination.read_text() == "original"
    assert not list(tmp_path.glob("*.tmp"))
    assert rig.repo.results()[1] == 2
    assert rig.repo.progress(rig.identity)["state"] == "Completed"
