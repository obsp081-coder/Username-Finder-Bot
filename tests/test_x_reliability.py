"""Conservative claimability policy and non-destructive historical reassessment."""

import csv
import hashlib
import json
import subprocess
import sys
import textwrap

import pytest
from test_x import Clock, response
from test_x import repo as repo
from test_x_ui import window as window

from username_finder import x
from username_finder.overnight import OvernightStore
from username_finder.result_search import ResultFilter, ResultSearch
from username_finder.storage import Store
from username_finder.x_runner import XRunner
from username_finder.x_storage import XStore


def legacy_positive(repo, name="hzwu9"):
    """Simulate a preserved pre-policy observation, never a newly verified positive."""
    identity = repo.prepare(x.input_spec(name), skip=False)
    evidence = x.result_for(name, response(True, "available", msg="Available!", desc="Available!"))
    evidence.update(
        classification="Available", reason="historical_pair_available_unverified_current_semantics"
    )
    repo.finish(repo.claim(identity), evidence)
    repo.end(identity, "Completed")
    return identity, evidence


@pytest.mark.parametrize("name", ["hzwu9", "abcde", "_name", "a1_b2"])
def test_all_positive_pairs_are_unknown_without_name_specific_guesses(name):
    sample = response(True, "available", msg="Available!", desc="Available!")
    result = x.result_for(name, sample)
    assert result["classification"] == "Unknown"
    assert result["reason"] == "api_available_does_not_establish_claimability"
    assert result["api_reason"] == "available" and result["parsed_fields"]["valid"] is True
    assert result["http_status"] == 200 and result["response_data"] == sample.body.decode()
    assert result["raw_sha256"] == hashlib.sha256(sample.body).hexdigest()
    assert result["preliminary"] and not result["independently_verified"]


@pytest.mark.parametrize(
    "valid,reason,name,expected",
    [
        (False, "taken", "named", "Taken"),
        (False, "is_banned_word", "admin", "Reserved"),
        (False, "contains_banned_word", "xadmin", "Reserved"),
        (False, "improper_format", "bad.name", "Invalid"),
        (False, "improper_format", "named", "Unknown"),
        (False, "unavailable", "named", "Unknown"),
        (False, "available", "named", "Unknown"),
        (True, "taken", "named", "Unknown"),
        (True, "is_banned_word", "named", "Unknown"),
        (True, "contains_banned_word", "named", "Unknown"),
        (False, "custom_message", "named", "Unknown"),
    ],
)
def test_explicit_negative_evidence_stays_distinct_from_general_rejection(valid, reason, name, expected):
    assert x.result_for(name, response(valid, reason))["classification"] == expected


@pytest.mark.parametrize(
    "sample",
    [
        x.protocol.Response(403),
        x.protocol.Response(404),
        x.protocol.Response(429),
        x.protocol.Response(error="TimeoutError", complete=False),
        x.protocol.Response(error="ConnectionResetError", complete=False),
        x.protocol.Response(
            200, b'{"valid":true,"reason":"available"}', {"content-type": "application/json"}, complete=False
        ),
        x.protocol.Response(
            200, b'{"valid":true,"valid":false,"reason":"available"}', {"content-type": "application/json"}
        ),
        x.protocol.Response(
            200, b'{"valid":true,"reason":"available","unexpected":1}', {"content-type": "application/json"}
        ),
    ],
)
def test_failures_never_prove_availability(sample):
    assert x.result_for("named", sample)["classification"] == "Unknown"


def test_positive_stops_once_without_retries_and_preserves_large_job_configuration(repo, monkeypatch):
    monkeypatch.setattr(x.protocol, "fetch", lambda *a, **kw: pytest.fail("No live request"))
    identity = repo.prepare(x.input_spec("first\nother\nthird"))
    night = OvernightStore(repo.store, "X")
    night.enable(identity, True)
    before = repo.progress(identity)
    clock = Clock()
    calls = []

    def fetch(name, timeout):
        calls.append(name)
        return response(True, "available")

    XRunner(repo, fetcher=fetch, clock=clock.now, wait=clock.wait).run(identity)
    after = repo.progress(identity)
    assert calls == ["first"] and clock.waits == []
    assert after["state"] == "Restricted" and after["pending"] == 2 and after["completed"] == 1
    for key in ("id", "spec", "cursor", "interval_seconds", "skip_previous"):
        assert after[key] == before[key]
    assert night.get(identity)["attempts"] == 0 and night.get(identity)["state"] == "Review"
    assert not night.startup_candidates(repo)
    repo.recover()
    repo.recover()
    assert len(repo.history("first")) == 1 and repo.progress(identity)["pending"] == 2


def test_legacy_results_counts_filters_history_and_both_exports_agree_without_writes(repo, tmp_path):
    _, original = legacy_positive(repo)
    before = list(repo.store.db.iterdump())
    assert repo.counts() == {"Unknown": 1}
    assert repo.results("Available")[1] == 0
    row = repo.results("Unknown")[0][0]
    assert row["classification"] == "Unknown" and row["recorded_classification"] == "Available"
    assert row["assessment_reason"] == x.REASSESSMENT_REASON
    assert json.loads(row["evidence"]) == original
    assert repo.history("hzwu9")[0]["recorded_classification"] == "Available"
    search = ResultSearch(repo.store.path)
    assert search.page("X", ResultFilter(classification="Available"))[1] == 0
    rows, total, _ = search.page("X", ResultFilter(classification="Unknown"))
    assert total == 1 and search.last_counts == {"Unknown": 1}
    assert rows[0]["recorded_classification"] == "Available"
    for method, path in (
        (repo.export, tmp_path / "direct.csv"),
        (
            lambda p: search.export(p, "X", ResultFilter(classification="Unknown"), history=True),
            tmp_path / "searched.csv",
        ),
    ):
        assert method(path) == 1
        with path.open(encoding="utf-8-sig", newline="") as stream:
            exported = list(csv.DictReader(stream))[0]
        assert exported["classification"] == "Unknown" and exported["recorded_classification"] == "Available"
        assert exported["assessment_policy"] == x.ASSESSMENT_POLICY
        assert json.loads(exported["evidence"]) == original
    assert list(repo.store.db.iterdump()) == before


def test_policy_change_is_not_misrepresented_as_a_new_provider_classification_change(repo):
    legacy_positive(repo)
    identity = repo.prepare(x.input_spec("hzwu9"), skip=False)
    repo.finish(repo.claim(identity), x.result_for("hzwu9", response(True, "available")))
    latest = repo.history("hzwu9")[0]
    assert latest["classification"] == latest["previous_classification"] == "Unknown"
    assert latest["changed"] == 0 and latest["recorded_changed"] == 1
    assert latest["recorded_previous_classification"] == "Available"
    assert ResultSearch(repo.store.path).page("X", ResultFilter(changed=True))[1] == 0
    assert repo.prepare(x.input_spec("hzwu9"))
    assert repo.runs()[0]["skipped"] == 1


def test_gui_and_manager_show_unknown_while_retaining_original_evidence(window):
    panel = window.x_panel
    identity, _ = legacy_positive(panel.repo)
    panel.current_run = identity
    panel.refresh()
    assert panel.rows[0]["classification"] == "Unknown"
    assert "Unknown" in panel.table.item(0, 1).text()
    assert "Available" in panel.table.item(0, 1).toolTip()
    window.job_manager.refresh()
    row = next(row for row in window.job_manager.rows if row["id"] == identity)
    assert row["classifications"] == {"Unknown": 1}
    assert panel.repo.history("hzwu9")[0]["recorded_classification"] == "Available"
    assert not panel.busy and not panel.test_clock.calls


def test_crash_after_positive_commit_preserves_evidence_and_blocks_automatic_resume(tmp_path):
    path = tmp_path / "positive-crash.db"
    worker = textwrap.dedent("""\
        import os, sys
        from username_finder import x
        from username_finder.storage import Store
        from username_finder.x_storage import XStore
        from username_finder.x_runner import XRunner
        from username_finder.overnight import OvernightStore
        store = Store(sys.argv[1])
        repo = XStore(store)
        repo.set_interval(1.5)
        identity = repo.prepare(x.input_spec("first\\nother\\nthird"))
        OvernightStore(store, "X").enable(identity, True)
        def fetch(*args):
            return x.protocol.Response(200, b'{"valid":true,"reason":"available"}',
                                       {"content-type":"application/json"})
        XRunner(repo, fetcher=fetch, updated=lambda: os._exit(23)).run(identity)
        """)
    child = subprocess.run([sys.executable, "-c", worker, str(path)], capture_output=True, timeout=15)
    assert child.returncode == 23, child.stderr
    store = Store(path)
    try:
        repo = XStore(store)
        repo.recover()
        identity = repo.runs()[0]["id"]
        night = OvernightStore(store, "X")
        assert night.startup_candidates(repo) == []
        assert night.get(identity)["state"] == "Review"
        assert repo.control()["blocked"] == "api_available_does_not_establish_claimability"
        assert repo.progress(identity)["pending"] == 2
        assert repo.progress(identity)["interval_seconds"] == 1.5
        evidence = json.loads(repo.history("first")[0]["evidence"])
        assert evidence["parsed_fields"] == {"valid": True, "reason": "available"}
        assert evidence["raw_complete"] and evidence["classification"] == "Unknown"
        repo.recover()
        assert len(repo.history("first")) == 1 and repo.results()[1] == 1
        assert not night.startup_candidates(repo)
    finally:
        store.close()
