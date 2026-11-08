"""Offline policy replay only. No transport, jobs, database, or live execution entrypoint."""

from __future__ import annotations

import argparse
import base64
import csv
import math
import os
import re
import statistics
import sys
import uuid
from collections import Counter
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bulk_validation as experiment  # noqa: E402

POLICY = "offline-account-vs-registration-v1"
CURRENT = "ConfirmedCurrentNameMatchByAPI"
HISTORICAL = "HistoricalOrDifferentCurrentName"
MISSING = "MissingBulkRecord"
UNSAFE = "UnsafeOrContradictoryResponse"
UNKNOWN = "Unknown"


def require(condition, message):
    if not condition:
        raise ValueError(message)


def read_bytes(path):
    path = Path(path)
    require(
        not any(
            p.is_symlink() or (hasattr(p, "is_junction") and p.is_junction()) for p in (path, *path.parents)
        ),
        "Redirected source path",
    )
    with path.open("rb") as stream:
        raw = stream.read(experiment.MAX_FILE + 1)
    require(len(raw) <= experiment.MAX_FILE, "Source exceeds bounded offline replay size")
    return raw


def read_json(path):
    return experiment.strict_json(read_bytes(path))


def timestamp(value):
    require(isinstance(value, str), "Missing timestamp")
    result = datetime.fromisoformat(value)
    require(result.tzinfo is not None, "Source timestamp has no timezone")
    return result


def decode_response(evidence):
    require(isinstance(evidence, dict), "Missing response evidence")
    require(evidence.get("status") is None or type(evidence["status"]) is int, "Invalid HTTP status type")
    require(
        all(
            type(evidence.get(k)) is bool
            for k in ("complete", "challenge", "body_omitted_for_sensitive_fields", "body_text_is_preview")
        ),
        "Invalid response flags",
    )
    require(
        not evidence["body_omitted_for_sensitive_fields"], "Raw response unavailable: sensitive body omitted"
    )
    require(
        isinstance(evidence.get("error"), str) and isinstance(evidence.get("headers"), dict),
        "Invalid response metadata",
    )
    require(
        all(k in experiment.SAFE_HEADERS and isinstance(v, str) for k, v in evidence["headers"].items()),
        "Unsupported saved headers",
    )
    latency = evidence.get("latency_seconds")
    require(
        type(latency) in (int, float) and math.isfinite(latency) and latency >= 0, "Invalid response latency"
    )
    require(isinstance(evidence.get("body_base64"), str), "Missing raw body")
    raw = base64.b64decode(evidence["body_base64"], validate=True)
    require(
        len(raw) <= experiment.protocol.MAX_BODY and experiment.digest(raw) == evidence.get("body_sha256"),
        "Response body hash mismatch or unavailable full body",
    )
    require(
        evidence.get("body_text") == raw[:65536].decode("utf-8", errors="replace")
        and evidence["body_text_is_preview"] == (len(raw) > 65536),
        "Response preview integrity mismatch",
    )
    if evidence["complete"] and not evidence["error"] and "content-length" in evidence["headers"]:
        require(evidence["headers"]["content-length"] == str(len(raw)), "Complete response length mismatch")
    return experiment.protocol.Response(
        status=evidence["status"],
        headers=evidence["headers"],
        body=raw,
        elapsed=latency,
        error=evidence["error"],
        complete=evidence["complete"],
        challenge=evidence["challenge"],
    )


def source_reference(run, journal_hash, attempt, event):
    return {
        "run": str(run.resolve()),
        "journal_sha256": journal_hash,
        "attempt": attempt,
        "response_sha256": event["response"]["body_sha256"],
        "response_at_utc": event["response_at_utc"],
        "latency_seconds": event["response"]["latency_seconds"],
        "http_status": event["response"]["status"],
    }


def load_run(root, run, *, comparison):
    """Verify archived bindings, request prefix, hashes, and recorded interpretations before replay."""
    files = {name: read_bytes(run / name) for name in ("manifest.json", "review.json", "events.jsonl")}
    manifest, review = (experiment.strict_json(files[n]) for n in ("manifest.json", "review.json"))
    validator = (
        experiment.validate_comparison_manifest if comparison else experiment.validate_expanded_manifest
    )
    require(
        not validator(manifest) and run.name == experiment.fingerprint(manifest),
        "Invalid archived manifest/folder binding",
    )
    raw = files["events.jsonl"]
    require(raw.endswith(b"\n"), "Truncated source journal; preserve and review it")
    events = [experiment.strict_json(line) for line in raw.splitlines()]
    require(
        events and all(isinstance(e, dict) for e in events) and events[0].get("event") == "begin",
        "Missing journal begin",
    )
    dates = [timestamp(e.get("recorded_at_utc")) for e in events]
    require(dates == sorted(dates), "Nonchronological journal")
    begin = events[0]
    mode = experiment.COMPARISON_MODE if comparison else experiment.EXPANDED_MODE
    limit = 20 if comparison else 105
    require(
        begin.get("mode") == mode
        and begin.get("manifest_sha256") == run.name
        and begin.get("maximum_attempts") == limit
        and begin.get("planned_attempts") == limit,
        "Invalid begin binding/budget",
    )
    require(
        isinstance(review, dict)
        and review.get("manifest_sha256") == run.name
        and review.get("acknowledgment") == "REVIEW " + run.name,
        "Invalid original review",
    )
    require(
        review.get("protocol_sha256")
        == begin.get("individual_protocol_sha256")
        == experiment.protocol_fingerprint(),
        "Historical protocol differs; compatibility review required",
    )
    require(
        begin.get("interpretation_policy") == experiment.BULK_POLICY, "Unsupported historical parser policy"
    )
    plan = experiment.request_plan(manifest)
    bulk, original_bulk, references, individuals = {}, {}, {}, {}
    hashes = {name: experiment.digest(value) for name, value in files.items()}
    if comparison:
        saved_raw = read_bytes(run / "source_analysis.json")
        hashes["source_analysis.json"] = experiment.digest(saved_raw)
        saved = experiment.strict_json(saved_raw)
        require(
            experiment.fingerprint(saved) == manifest["source_analysis_sha256"],
            "Source analysis hash mismatch",
        )
        actual = experiment.saved_pilot_analysis(root, manifest["source_run_id"])
        require(
            saved == actual
            and not actual["corrected_stop_reason"]
            and saved["manifest"]["candidates"] == manifest["candidates"],
            "Saved pilot evidence changed or cannot be interpreted",
        )
        bulk.update(actual["corrected_mapping"])
        original_bulk.update(actual["original_events"][3]["mapping"])
        source_run = Path(root) / "random-pilot/runs" / manifest["source_run_id"]
        ref = source_reference(
            source_run, actual["source_files_sha256"]["events.jsonl"], 1, actual["original_events"][2]
        )
        references.update(dict.fromkeys(bulk, ref))
        plan = [p for p in plan if p["kind"] == "individual"]
    else:
        require(
            begin.get("safety_acknowledgment") == "SAFE " + run.name, "Original safety acknowledgment missing"
        )
        experiment.expanded_source_binding(run.parent.parent, manifest)
    intents, responses, expected_interpretations = {}, {}, {}
    interpreted, unsafe_names, restrictions = set(), set(), []
    halted, stop = "", None
    allowed = {
        "request_intent",
        "response",
        "bulk_interpretation",
        "individual_interpretation",
        "stop",
        "pacing_intent",
        "pacing_result",
        "dispatch_timing",
    }
    candidates = {experiment.key(r["requested_username"]): r for r in manifest["candidates"]}
    for event in events[1:]:
        kind = event.get("event")
        require(kind in allowed and stop is None, "Unsupported event or records after durable stop")
        if kind == "request_intent":
            attempt = event.get("attempt")
            require(
                type(attempt) is int and attempt == len(intents) + 1 and attempt <= limit and not halted,
                "Duplicate/out-of-budget dispatch or dispatch after safety stop",
            )
            if intents:
                require(
                    attempt - 1 in responses and attempt - 1 in interpreted,
                    "Prior request unfinished before another dispatch",
                )
            request = event.get("request")
            require(request == plan[attempt - 1], "Request does not match frozen sequential plan")
            names = request["usernames"]
            require(
                event.get("bulk_parameters")
                == ({"usernames": names, "excludeBannedUsers": False} if request["kind"] == "bulk" else None),
                "Bulk parameters changed",
            )
            require(
                event.get("individual_parameters")
                == (
                    None
                    if request["kind"] == "bulk"
                    else {
                        "Username": names[0],
                        "Birthday": experiment.protocol.BIRTHDAY,
                        "Context": experiment.protocol.CONTEXT,
                    }
                ),
                "Individual parameters changed",
            )
            timestamp(event.get("request_at_utc"))
            intents[attempt] = event
        elif kind == "response":
            attempt = event.get("attempt")
            require(
                type(attempt) is int and attempt == len(intents) and attempt not in responses,
                "Duplicate or unbound response",
            )
            require(
                timestamp(event.get("response_at_utc")) >= timestamp(intents[attempt]["request_at_utc"]),
                "Response predates request",
            )
            response = decode_response(event.get("response"))
            responses[attempt] = event
            request = intents[attempt]["request"]
            names = [experiment.key(n) for n in request["usernames"]]
            halted = experiment.response_stop(response)
            if halted:
                restrictions.append(
                    {
                        "attempt": attempt,
                        "reason": halted,
                        "status": response.status,
                        "headers": response.headers,
                    }
                )
            if request["kind"] == "bulk":
                mapping, parser_stop = experiment.interpret_bulk(request["usernames"], response)
                conflict_keys = []
                if not parser_stop:
                    parser_stop, conflict_keys = experiment.cross_batch_conflict(bulk, mapping)
                references.update(
                    dict.fromkeys(names, source_reference(run, hashes["events.jsonl"], attempt, event))
                )
                if parser_stop:
                    unsafe_names.update(names + conflict_keys)
                if not halted:
                    expected_interpretations[attempt] = {"mapping": mapping, "stop_reason": parser_stop}
                halted = halted or parser_stop
                bulk.update(mapping)
            else:
                name = names[0]
                classification, reason, code, message = experiment.protocol.classify(response)
                if halted:
                    classification, reason = "Unknown", halted
                value = {
                    "classification": classification,
                    "reason": reason,
                    "api_code": code,
                    "message": message,
                    "preliminary": True,
                    "independently_verified": False,
                }
                if not halted:
                    try:
                        payload = experiment.strict_json(response.body)
                    except (ValueError, UnicodeError, RecursionError):
                        halted = "malformed_individual_json"
                    else:
                        if not isinstance(payload, dict) or set(payload) != {"code", "message"}:
                            halted = "unexpected_individual_schema"
                        else:
                            halted = (
                                "unknown_individual_" + reason
                                if classification == "Unknown"
                                else experiment.compare_evidence(candidates[name], bulk[name], value)
                            )
                            expected_interpretations[attempt] = {
                                "result": value,
                                "stop_reason": halted,
                                "username_key": name,
                            }
                individuals[name] = {
                    "result": value,
                    "reference": source_reference(run, hashes["events.jsonl"], attempt, event),
                }
        elif kind in ("bulk_interpretation", "individual_interpretation"):
            attempt = event.get("attempt")
            require(
                type(attempt) is int
                and attempt in expected_interpretations
                and attempt not in interpreted
                and attempt == len(intents),
                "Unbound or duplicate interpretation",
            )
            request_kind = intents[attempt]["request"]["kind"]
            require(kind == request_kind + "_interpretation", "Interpretation has wrong request kind")
            require(
                all(event.get(k) == v for k, v in expected_interpretations[attempt].items()),
                "Recorded interpretation differs from verified raw response",
            )
            interpreted.add(attempt)
            if kind == "bulk_interpretation":
                original_bulk.update(event["mapping"])
        elif kind == "stop":
            require(
                event.get("dispatch_intents") == len(intents) and isinstance(event.get("reason"), str),
                "Stop count/reason mismatch",
            )
            if halted:
                require(event["reason"] == halted, "Stop reason differs from observed safety stop")
            if event["reason"] == "completed":
                require(
                    len(intents) == limit and len(responses) == limit and len(interpreted) == limit,
                    "Incomplete run recorded as complete",
                )
            if "elapsed_seconds_including_pacing" in event:
                elapsed = event["elapsed_seconds_including_pacing"]
                require(
                    type(elapsed) in (int, float) and math.isfinite(elapsed) and elapsed >= 0,
                    "Invalid historical elapsed timing",
                )
            stop = event
    return {
        "manifest": manifest,
        "bulk": bulk,
        "original_bulk": original_bulk,
        "references": references,
        "individuals": individuals,
        "unsafe_names": unsafe_names,
        "restrictions": restrictions,
        "run": str(run.resolve()),
        "file_hashes": hashes,
        "stop_reason": stop["reason"] if stop else "NoDurableStop",
        "bulk_attempts": 1 if comparison else sum(e["request"]["kind"] == "bulk" for e in intents.values()),
        "individual_attempts": sum(e["request"]["kind"] == "individual" for e in intents.values()),
        "uncertain_attempts": sorted(intents.keys() - responses.keys()),
        "bulk_batches_expected": 1 if comparison else 5,
        "source_post_response_pacing_seconds": begin.get("pacing_seconds"),
        "elapsed_seconds_including_pacing": stop.get("elapsed_seconds_including_pacing") if stop else None,
    }


def account_state(value, unsafe=False):
    if unsafe:
        return UNSAFE
    if not value:
        return UNKNOWN
    if value["preliminary_classification"] == "Taken":
        return CURRENT
    return HISTORICAL if value.get("record") else MISSING


def manual_notes(path):
    if path is None:
        return {}, None
    raw = read_bytes(path)
    value = experiment.strict_json(raw)
    require(
        isinstance(value, dict) and value.get("version") == 1 and isinstance(value.get("observations"), list),
        "Unsupported manual observations file",
    )
    notes = {}
    for note in value["observations"]:
        require(
            isinstance(note, dict)
            and experiment.valid_name(note.get("username"))
            and note.get("provenance") == "user_reported"
            and note.get("independently_audited") is False,
            "Manual observations must preserve unaudited user-reported provenance",
        )
        key = experiment.key(note["username"])
        require(
            key not in notes
            and all(isinstance(note.get(k), str) for k in ("context", "observation", "reference")),
            "Duplicate/incomplete manual note",
        )
        notes[key] = note
    return notes, {"path": str(Path(path).resolve()), "sha256": experiment.digest(raw)}


def evaluate_rows(source, notes):
    rows = []
    for candidate in source["manifest"]["candidates"]:
        username = candidate["requested_username"]
        name = experiment.key(username)
        bulk = source["bulk"].get(name)
        state = account_state(bulk, name in source["unsafe_names"])
        observed = source["individuals"].get(name)
        individual = observed["result"] if observed else None
        skip = state == CURRENT and not source["restrictions"] and not source["unsafe_names"]
        divergence = (
            state == CURRENT
            and individual is not None
            and individual["classification"] not in ("Taken", "Unknown")
        )
        note = notes.get(name)
        rows.append(
            {
                "username": username,
                "source_run": source["run"],
                "account_match_state": state,
                "bulk_original_interpretation": source["original_bulk"].get(name),
                "bulk_replayed_observation": bulk,
                "bulk_reference": source["references"].get(name),
                "individual_observed": observed is not None,
                "individual_classification": individual["classification"] if individual else None,
                "individual_api_code": individual["api_code"] if individual else None,
                "individual_reason": individual["reason"] if individual else "NoIndividualObservation",
                "individual_message": individual["message"] if individual else "",
                "individual_reference": observed["reference"] if observed else None,
                "api_relationship": "AccountMatchVsRegistrationDivergence"
                if divergence
                else "ExactTakenAgreement"
                if state == CURRENT and individual and individual["classification"] == "Taken"
                else "NotCompared"
                if not individual
                else "DifferentQuestions_NoExactTakenAgreement",
                "registration_classification_independently_verified": False,
                "manual_status": "UserReportedOfficialInterfaceObservation_NotAudited"
                if note
                else "NotRecorded",
                "manual_observation": note,
                "policy_a_would_skip": False,
                "policy_a_registration_classification": individual["classification"]
                if individual
                else "Unknown",
                "policy_a_classification_source": "individual_api_preliminary"
                if individual
                else "not_observed",
                "policy_b_candidate_for_skip": state == CURRENT,
                "policy_b_would_skip": skip,
                "policy_b_output": "AccountNameCollisionByAPI"
                if skip
                else "StopForUnsafeEvidence"
                if state == UNSAFE or source["restrictions"] or source["unsafe_names"]
                else "RequireIndividualValidation",
                "policy_b_registration_classification": None
                if skip
                else individual["classification"]
                if individual
                else "Unknown",
                "known_classification_information_lost": individual["classification"]
                if skip and individual
                else None,
                "classification_information_loss_unknown": skip and individual is None,
                "remaining_uncertainty": (
                    "No independently audited claimability/ownership proof; "
                    "API account identity does not determine registration outcome"
                )
                + ("; no individual result" if not individual else ""),
            }
        )
    return rows


def summarize(source, rows):
    count = len(rows)
    matches = sum(r["account_match_state"] == CURRENT for r in rows)
    bulk_complete = (
        len(source["references"]) == count
        and len(source["bulk"]) == count
        and not source["unsafe_names"]
        and not source["restrictions"]
        and source["bulk_attempts"] == source["bulk_batches_expected"]
    )
    skipped = matches if bulk_complete else None
    bulk_cost = source["bulk_batches_expected"]
    unique_bulk = {(r["run"], r["attempt"]): r for r in source["references"].values()}
    latencies = {
        "bulk": [r["latency_seconds"] for r in unique_bulk.values()],
        "individual": [r["reference"]["latency_seconds"] for r in source["individuals"].values()],
    }
    return {
        "source_run": source["run"],
        "source_file_hashes": source["file_hashes"],
        "source_stop_reason": source["stop_reason"],
        "candidates": count,
        "account_states": dict(Counter(r["account_match_state"] for r in rows)),
        "individually_observed": sum(r["individual_observed"] for r in rows),
        "missing_individual_observations": sum(not r["individual_observed"] for r in rows),
        "exact_taken_agreements": sum(r["api_relationship"] == "ExactTakenAgreement" for r in rows),
        "semantic_divergences": [
            {
                "username": r["username"],
                "individual_classification": r["individual_classification"],
                "api_code": r["individual_api_code"],
                "reason": r["individual_reason"],
            }
            for r in rows
            if r["api_relationship"] == "AccountMatchVsRegistrationDivergence"
        ],
        "observed_individual_classifications": dict(
            Counter(r["individual_classification"] for r in rows if r["individual_observed"])
        ),
        "known_policy_b_information_lost": dict(
            Counter(
                r["known_classification_information_lost"]
                for r in rows
                if r["known_classification_information_lost"]
            )
        ),
        "skipped_names_without_individual_evidence": sum(
            r["classification_information_loss_unknown"] for r in rows
        ),
        "source_bulk_attempts": source["bulk_attempts"],
        "source_individual_attempts": source["individual_attempts"],
        "source_uncertain_attempts": source["uncertain_attempts"],
        "source_restrictions": source["restrictions"],
        "source_elapsed_seconds_including_pacing": source["elapsed_seconds_including_pacing"],
        "source_post_response_pacing_seconds": source["source_post_response_pacing_seconds"],
        "historical_latency_seconds": {
            kind: {
                "count": len(values),
                "mean": statistics.mean(values) if values else None,
                "median": statistics.median(values) if values else None,
            }
            for kind, values in latencies.items()
        },
        "policy_a": {
            "individual_requests_required": count,
            "skips": 0,
            "bulk_overhead": bulk_cost,
            "hypothetical_total_requests": count + bulk_cost,
            "net_requests_saved": -bulk_cost,
        },
        "policy_b_hypothetical_coarse_output_only": {
            "estimate_supported_by_complete_bulk_evidence": bulk_complete,
            "candidate_individual_skips": skipped,
            "bulk_overhead": bulk_cost,
            "fallback_individual_requests": count - skipped if skipped is not None else None,
            "hypothetical_total_requests": bulk_cost + count - skipped if skipped is not None else None,
            "net_requests_saved": skipped - bulk_cost if skipped is not None else None,
            "request_count_reduction_percent": 100 * (skipped - bulk_cost) / count
            if skipped is not None
            else None,
            "assumptions": (
                "Counterfactual coarse account-collision output only, complete bulk lookup, "
                "fallback for every unresolved name, no restrictions; unchanged observations assumed. "
                "Missing individual outcomes remain unknown."
            ),
            "precise_registration_classifications_preserved": False,
            "claimability_guaranteed": False,
            "measured_wall_clock_speedup": None,
        },
        "independent_accuracy_percentage": None,
    }


def evaluate(root, pilot_run, expanded_run, *, observations=None):
    require(
        all(re.fullmatch(r"[0-9a-f]{64}", value or "") for value in (pilot_run, expanded_run)),
        "Full archived run fingerprints required",
    )
    root = Path(root)
    comparison_runs = sorted((root / "individual-comparison" / pilot_run / "runs").glob("*/manifest.json"))
    require(
        len(comparison_runs) == 1,
        "Expected one saved individual comparison for the pilot; missing or ambiguous private evidence",
    )
    comparison_run = comparison_runs[0].parent
    require(
        read_json(comparison_run / "manifest.json").get("source_run_id") == pilot_run,
        "Wrong comparison source",
    )
    sources = [
        load_run(root, comparison_run, comparison=True),
        load_run(root, root / "expanded-100/runs" / expanded_run, comparison=False),
    ]
    notes, note_reference = manual_notes(observations)
    rows, summaries = [], []
    for source in sources:
        current_rows = evaluate_rows(source, notes)
        rows.extend(current_rows)
        summaries.append(summarize(source, current_rows))
    require(
        len({experiment.key(r["username"]) for r in rows}) == len(rows),
        "Cohorts overlap; cannot aggregate independent candidates",
    )
    summary = {
        "evaluation_policy": POLICY,
        "generated_at_utc": experiment.utcnow(),
        "replay_protocol_sha256": experiment.protocol_fingerprint(),
        "manual_note_reference": note_reference,
        "unmatched_manual_note_usernames": sorted(set(notes) - {experiment.key(r["username"]) for r in rows}),
        "cohorts": summaries,
        "rows": len(rows),
        "live_requests": 0,
        "production_adoption_recommendation": "NO-GO",
        "recommendation_reasons": [
            "Policy A needs individual checks for precise classification, adding bulk overhead.",
            "Policy B excludes API account collisions, losing outcomes such as moderation rejection.",
            "Missing comparisons and unaudited claimability prevent general accuracy/throughput conclusions.",
        ],
        "policy_b_research_only": (
            "CONDITIONAL for a separate coarse account-collision output, independently validated "
            "with representative official-interface evidence; not the production classification enum"
        ),
        "accuracy_percentage": None,
        "sustained_throughput_estimate": None,
    }
    return summary, rows


def write_report(root, summary, rows):
    destination = experiment.data_directory(Path(root) / "policy-evaluation") / (
        "analysis-" + str(uuid.uuid4())
    )
    destination.mkdir()
    experiment.write_new(destination / "summary.json", summary)
    with (destination / "usernames.csv").open("x", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows({k: json_value(v) for k, v in row.items()} for row in rows)
        stream.flush()
        os.fsync(stream.fileno())
    lines = [
        "# Offline Roblox bulk-first policy evaluation",
        "",
        "Production adoption: **NO-GO**.",
        "",
        "Account lookup differs from registration validation. API agreement is not independent verification.",
        "",
        "| Cohort | Names | Matches | Observed | Exact Taken | Divergences | Missing |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for cohort in summary["cohorts"]:
        lines.append(
            "| "
            + " | ".join(
                str(v)
                for v in (
                    f"{cohort['candidates']}-name",
                    cohort["candidates"],
                    cohort["account_states"].get(CURRENT, 0),
                    cohort["individually_observed"],
                    cohort["exact_taken_agreements"],
                    len(cohort["semantic_divergences"]),
                    cohort["missing_individual_observations"],
                )
            )
            + " |"
        )
    lines.extend(
        [
            "",
            "Policy A preserves the individual API classification and saves no individual requests. "
            "Policy B supplies AccountNameCollisionByAPI, discarding classifications for skipped names.",
            "",
            "Request savings in summary.json are hypothetical counts, not observed speedup. "
            "They include bulk overhead and fallback. Pacing, quotas and endpoint limits may differ. "
            "No general accuracy percentage or sustained-throughput estimate is supported.",
            "",
            "User-reported interface notes remain unaudited; absent individual results are not agreements.",
            "",
        ]
    )
    lines.extend(
        [
            "## Hypothetical request counts and information loss",
            "",
            "| Names | Baseline | A total | B total | B skips | B unknown skipped outcomes |",
            "|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for cohort in summary["cohorts"]:
        estimate = cohort["policy_b_hypothetical_coarse_output_only"]
        lines.append(
            "| "
            + " | ".join(
                str(v)
                for v in (
                    cohort["candidates"],
                    cohort["candidates"],
                    cohort["policy_a"]["hypothetical_total_requests"],
                    estimate["hypothetical_total_requests"],
                    estimate["candidate_individual_skips"],
                    cohort["skipped_names_without_individual_evidence"],
                )
            )
            + " |"
        )
    lines.append("")
    for cohort in summary["cohorts"]:
        lines.append(
            f"Known precise classifications discarded for {cohort['candidates']} names: "
            + str(cohort["known_policy_b_information_lost"])
            + "."
        )
    lines.extend(["", "## User-reported interface observations (not independently audited)", ""])
    for row in rows:
        if note := row["manual_observation"]:
            lines.append(
                f"- `{row['username']}`: {note['observation']} ({note['context']}). "
                "This supports only the reported observation; no general accuracy is established."
            )
    lines.extend(["", "## Observed semantic divergences", ""])
    for cohort in summary["cohorts"]:
        for item in cohort["semantic_divergences"]:
            lines.append(
                f"- `{item['username']}`: current-name match versus individual "
                f"{item['individual_classification']}, API code {item['api_code']} ({item['reason']})."
            )
    with (destination / "report.md").open("x", encoding="utf-8", newline="\n") as stream:
        stream.write("\n".join(lines) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    return destination


def json_value(value):
    return experiment.encoded(value).decode("utf-8") if isinstance(value, (dict, list)) else value


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pilot-run", required=True)
    parser.add_argument("--expanded-run", required=True)
    parser.add_argument("--manual-observations", type=Path)
    args = parser.parse_args(argv)
    try:
        summary, rows = evaluate(
            experiment.DATA, args.pilot_run, args.expanded_run, observations=args.manual_observations
        )
        output = write_report(experiment.DATA, summary, rows)
        print(
            experiment.encoded(
                {
                    "output": str(output.resolve()),
                    "rows": len(rows),
                    "recommendation": "NO-GO",
                    "live_requests": 0,
                }
            ).decode()
        )
        return 0
    except (OSError, ValueError, KeyError, TypeError, RecursionError) as exc:
        print(f"Offline replay blocked; original evidence preserved: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
