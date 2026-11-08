"""Offline replay tests reuse synthetic journals, never live account fixtures."""

import csv
import importlib.util
from pathlib import Path

import pytest
from test_bulk_validation import (
    Clock,
    comparison_setup,
    expanded_setup,
    original_bytes,
    poc,
    record,
    response,
    simulate_expanded,
)
from test_bulk_validation import (
    manifest as source_manifest,
)
from test_bulk_validation import (
    no_network_or_database as source_offline_guard,
)

spec = importlib.util.spec_from_file_location(
    "offline_bulk_policy", Path(__file__).resolve().parents[1] / "policy_evaluation.py"
)
policy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(policy)
policy.experiment = poc


@pytest.fixture(name="manifest")
def synthetic_manifest():
    return source_manifest.__wrapped__()


@pytest.fixture(autouse=True)
def offline_guard(monkeypatch):
    source_offline_guard.__wrapped__(monkeypatch)


def policy_sources(root, manifest):
    comparison_path, comparison_manifest, _, pilot = comparison_setup(root, manifest)
    clock = Clock()
    names = [r["requested_username"] for r in comparison_manifest["candidates"]]

    def transport(request):
        clock.at += 0.125
        code = 2 if request["usernames"] == [names[12]] else 1
        return response({"code": code, "message": "Synthetic response"})

    poc.run_experiment(
        comparison_path,
        comparison=True,
        transport=transport,
        acknowledge=lambda _: "RUN " + poc.fingerprint(comparison_manifest),
        monotonic=clock.now,
        wait=clock.wait,
    )
    poc.write_new(root / "random-pilot/manifest.json", pilot)
    expanded = poc.prepare_expanded(root)
    expanded_path = root / "expanded-100"
    poc.review_manifest(
        expanded_path, expanded=True, acknowledge=lambda _: "REVIEW " + poc.fingerprint(expanded)
    )
    return expanded_path, expanded, pilot


def source_snapshot(root):
    return {str(p): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def test_ig49_and_drux_semantics_replayed_with_synthetic_names(tmp_path, manifest, monkeypatch):
    path, expanded, pilot = policy_sources(tmp_path, manifest)
    summary, _, _, _ = simulate_expanded(
        path,
        expanded,
        fault=lambda r, a, n: (
            response({"code": 2, "message": "Username not appropriate for Roblox"}) if a == 23 else None
        ),
    )
    assert summary["dispatch_intents_including_failures"] == 23
    before = source_snapshot(tmp_path)
    result, rows = policy.evaluate(tmp_path, poc.fingerprint(pilot), poc.fingerprint(expanded))
    assert result["rows"] == 120 and result["production_adoption_recommendation"] == "NO-GO"
    first, second = result["cohorts"]
    assert first["exact_taken_agreements"] == 19
    assert second["individually_observed"] == 18 and second["missing_individual_observations"] == 82
    assert second["exact_taken_agreements"] == 17 and len(second["semantic_divergences"]) == 1
    counterexample = rows[37]
    assert counterexample["account_match_state"] == policy.CURRENT
    assert (
        counterexample["individual_classification"]
        == counterexample["policy_a_registration_classification"]
        == "Invalid"
    )
    assert (
        counterexample["policy_b_would_skip"]
        and counterexample["policy_b_output"] == "AccountNameCollisionByAPI"
    )
    assert counterexample["policy_b_registration_classification"] is None
    assert counterexample["known_classification_information_lost"] == "Invalid"
    unusual = rows[12]
    assert unusual["account_match_state"] == policy.HISTORICAL
    assert (
        unusual["individual_classification"] == unusual["policy_b_registration_classification"] == "Invalid"
    )
    assert not unusual["policy_b_would_skip"]
    assert all(
        r["api_relationship"] == "NotCompared" and r["individual_classification"] is None for r in rows[38:]
    )
    assert all(not r["registration_classification_independently_verified"] for r in rows)
    assert source_snapshot(tmp_path) == before
    monkeypatch.setattr(poc, "DATA", tmp_path)
    monkeypatch.setattr(poc, "Transport", lambda *a, **k: pytest.fail("No transport"))
    monkeypatch.setattr(poc, "run_experiment", lambda *a, **k: pytest.fail("No experiment execution"))
    args = ["--pilot-run", poc.fingerprint(pilot), "--expanded-run", poc.fingerprint(expanded)]
    assert policy.main(args) == 0 and policy.main(args) == 0
    destinations = list((tmp_path / "policy-evaluation").glob("analysis-*"))
    assert len(destinations) == 2
    assert all(before[p] == Path(p).read_bytes() for p in before)
    for output in destinations:
        with (output / "usernames.csv").open(encoding="utf-8-sig", newline="") as stream:
            assert len(list(csv.DictReader(stream))) == 120
        assert "NO-GO" in (output / "report.md").read_text(encoding="utf-8")


def test_unresolved_states_fallback_classifications_and_request_costs(tmp_path, manifest):
    path, expanded, pilot = policy_sources(tmp_path, manifest)

    def fault(request, attempt, names):
        if attempt == 1:
            return response(
                {
                    "data": [
                        record(n, id=1000 + names.index(n), name="renamed_account" if n == names[0] else n)
                        for n in request["usernames"]
                        if n != names[1]
                    ]
                }
            )
        if request["kind"] == "individual" and request["usernames"] == [names[1]]:
            return response({"code": 0, "message": "Username is valid"})

    simulate_expanded(path, expanded, fault=fault)
    result, rows = policy.evaluate(tmp_path, poc.fingerprint(pilot), poc.fingerprint(expanded))
    cohort = result["cohorts"][1]
    assert rows[20]["account_match_state"] == policy.HISTORICAL
    assert rows[21]["account_match_state"] == policy.MISSING
    assert rows[21]["individual_classification"] == "Available" and not rows[21]["policy_b_would_skip"]
    assert rows[20]["policy_b_registration_classification"] == "Taken"  # Individual API evidence only.
    assert cohort["policy_a"] == {
        "individual_requests_required": 100,
        "skips": 0,
        "bulk_overhead": 5,
        "hypothetical_total_requests": 105,
        "net_requests_saved": -5,
    }
    estimate = cohort["policy_b_hypothetical_coarse_output_only"]
    assert estimate["candidate_individual_skips"] == 98 and estimate["fallback_individual_requests"] == 2
    assert estimate["hypothetical_total_requests"] == 7 and estimate["net_requests_saved"] == 93
    assert estimate["request_count_reduction_percent"] == 93
    assert estimate["measured_wall_clock_speedup"] is None and not estimate["claimability_guaranteed"]
    assert result["accuracy_percentage"] is None


@pytest.mark.parametrize(
    "fault",
    [
        response(status=429),
        response(body=b"{"),
        response({"data": [record("a000", id=True)]}),
        response(complete=False),
    ],
)
def test_unsafe_or_restricted_bulk_never_becomes_match_or_availability(tmp_path, manifest, fault):
    path, value, _ = expanded_setup(tmp_path, manifest)
    _, _, _, run = simulate_expanded(path, value, fault=lambda *_: fault)
    source = policy.load_run(tmp_path, run, comparison=False)
    rows = policy.evaluate_rows(source, {})
    assert sum(r["account_match_state"] == policy.UNSAFE for r in rows) == 20
    assert all(
        not r["policy_b_would_skip"] and r["policy_a_registration_classification"] == "Unknown" for r in rows
    )
    result = policy.summarize(source, rows)
    assert not result["policy_b_hypothetical_coarse_output_only"][
        "estimate_supported_by_complete_bulk_evidence"
    ]
    assert result["exact_taken_agreements"] == 0


def test_conflicting_batch_identities_disable_all_skips(tmp_path, manifest):
    path, value, _ = expanded_setup(tmp_path, manifest)

    def fault(request, attempt, names):
        if attempt == 2:
            return response(
                {
                    "data": [
                        record(n, id=1000 if n == names[20] else 1000 + names.index(n))
                        for n in request["usernames"]
                    ]
                }
            )

    _, _, _, run = simulate_expanded(path, value, fault=fault)
    source = policy.load_run(tmp_path, run, comparison=False)
    rows = policy.evaluate_rows(source, {})
    assert rows[0]["account_match_state"] == policy.UNSAFE
    assert not any(r["policy_b_would_skip"] for r in rows)
    assert (
        policy.summarize(source, rows)["policy_b_hypothetical_coarse_output_only"][
            "candidate_individual_skips"
        ]
        is None
    )


@pytest.mark.parametrize(
    "change",
    [
        "body_hash",
        "base64",
        "preview",
        "manifest",
        "request",
        "interpretation",
        "duplicate_response",
        "truncated",
        "protocol",
    ],
)
def test_corrupt_bindings_or_raw_evidence_block_without_mutation(tmp_path, manifest, change):
    path, value, _ = expanded_setup(tmp_path, manifest)
    _, _, _, run = simulate_expanded(
        path, value, fault=lambda r, a, n: response(status=429) if a == 2 else None
    )
    events, _ = poc.read_events(run / "events.jsonl")
    response_event = next(e for e in events if e["event"] == "response")
    if change == "body_hash":
        response_event["response"]["body_sha256"] = "0" * 64
    elif change == "base64":
        response_event["response"]["body_base64"] = "!"
    elif change == "preview":
        response_event["response"]["body_text"] = "altered"
    elif change == "manifest":
        events[0]["manifest_sha256"] = "0" * 64
    elif change == "protocol":
        events[0]["individual_protocol_sha256"] = "0" * 64
    elif change == "request":
        next(e for e in events if e["event"] == "request_intent")["bulk_parameters"]["excludeBannedUsers"] = (
            True
        )
    elif change == "interpretation":
        next(e for e in events if e["event"] == "bulk_interpretation")["stop_reason"] = "altered"
    elif change == "duplicate_response":
        events.insert(events.index(response_event) + 1, dict(response_event))
    raw = b"".join(poc.encoded(e) + b"\n" for e in events)
    (run / "events.jsonl").write_bytes(raw[:-1] if change == "truncated" else raw)
    before = original_bytes(run)
    with pytest.raises(ValueError):
        policy.load_run(tmp_path, run, comparison=False)
    assert original_bytes(run) == before


def test_missing_and_uncertain_evidence_not_fabricated(tmp_path, manifest):
    with pytest.raises(ValueError, match="missing or ambiguous"):
        policy.evaluate(tmp_path, "a" * 64, "b" * 64)
    path, value, _ = expanded_setup(tmp_path, manifest)

    def uncertain(*args):
        raise KeyboardInterrupt

    _, _, _, run = simulate_expanded(path, value, fault=uncertain)
    source = policy.load_run(tmp_path, run, comparison=False)
    rows = policy.evaluate_rows(source, {})
    assert source["uncertain_attempts"] == [1]
    assert all(
        r["account_match_state"] == policy.UNKNOWN and r["individual_classification"] is None for r in rows
    )
    assert not any(r["policy_b_candidate_for_skip"] for r in rows)


def test_manual_notes_keep_user_report_distinct_from_audited_confirmation(tmp_path, manifest):
    path, value, _ = expanded_setup(tmp_path, manifest)
    _, _, _, run = simulate_expanded(
        path,
        value,
        fault=lambda r, a, n: (
            response({"code": 2, "message": "Synthetic moderation rejection"}) if a == 6 else None
        ),
    )
    name = value["candidates"][0]["requested_username"]
    note = {
        "version": 1,
        "observations": [
            {
                "username": name,
                "provenance": "user_reported",
                "independently_audited": False,
                "context": "Official signup",
                "observation": "Rejected; exact UI text not preserved",
                "reference": "Synthetic user report",
            }
        ],
    }
    note_path = tmp_path / "manual.json"
    poc.write_new(note_path, note)
    notes, reference = policy.manual_notes(note_path)
    rows = policy.evaluate_rows(policy.load_run(tmp_path, run, comparison=False), notes)
    assert rows[0]["manual_status"].endswith("NotAudited")
    assert not rows[0]["registration_classification_independently_verified"]
    assert rows[0]["individual_classification"] == "Invalid"
    assert reference["sha256"] == poc.digest(note_path.read_bytes())
    note["observations"][0]["independently_audited"] = True
    note_path.write_bytes(poc.encoded(note))
    with pytest.raises(ValueError):
        policy.manual_notes(note_path)


@pytest.mark.parametrize(
    "current,expected",
    [
        ("pRoBe", policy.CURRENT),
        ("ChangedName", policy.HISTORICAL),
        ("roblox_user_12345678901", policy.HISTORICAL),
        (None, policy.MISSING),
    ],
)
def test_account_states_never_invent_registration_classifications(tmp_path, manifest, current, expected):
    path, value, _ = expanded_setup(tmp_path, manifest)
    names = [r["requested_username"] for r in value["candidates"]]
    # Reuse an actual validated source shape, then test pure policy evaluation with one synthetic row.
    _, _, _, run = simulate_expanded(
        path, value, fault=lambda r, a, n: response(status=429) if a == 1 else None
    )
    source = policy.load_run(tmp_path, run, comparison=False)
    mapping, stop = poc.interpret_bulk(
        ["PrObE"], response({"data": [] if current is None else [record("PROBE", name=current)]})
    )
    assert not stop
    source["manifest"] = {"candidates": [{"requested_username": "PrObE"}]}
    source["bulk"], source["original_bulk"] = mapping, mapping
    source["unsafe_names"], source["restrictions"] = set(), []
    source["individuals"] = {}
    row = policy.evaluate_rows(source, {})[0]
    assert row["account_match_state"] == expected
    assert row["individual_classification"] is None
    assert row["policy_a_registration_classification"] == "Unknown"
    assert row["policy_b_registration_classification"] in (None, "Unknown")
    assert row["api_relationship"] == "NotCompared"
    assert row["policy_b_would_skip"] is (expected == policy.CURRENT)
    assert len(names) == 100


@pytest.mark.parametrize(
    "code,classification",
    [(0, "Available"), (1, "Taken"), (2, "Invalid"), (12, "Reserved"), (999, "Unknown")],
)
def test_missing_bulk_record_preserves_each_observed_individual_class(
    tmp_path, manifest, code, classification
):
    path, value, _ = expanded_setup(tmp_path, manifest)

    def fault(request, attempt, names):
        if request["kind"] == "bulk":
            return response({"data": []})
        if attempt == 6:
            return response({"code": code, "message": "Synthetic response"})

    _, _, _, run = simulate_expanded(path, value, fault=fault)
    source = policy.load_run(tmp_path, run, comparison=False)
    rows = policy.evaluate_rows(source, {})
    row = rows[0]
    assert row["account_match_state"] == policy.MISSING
    assert row["policy_a_registration_classification"] == classification
    assert row["policy_b_registration_classification"] == classification
    assert not row["policy_b_would_skip"]
    summary = policy.summarize(source, rows)
    estimate = summary["policy_b_hypothetical_coarse_output_only"]
    assert estimate["candidate_individual_skips"] == 0
    assert estimate["hypothetical_total_requests"] == 105 and estimate["net_requests_saved"] == -5
    assert summary["exact_taken_agreements"] == 0
