"""Frozen, two-stage account lookup. Evidence files never enter production history."""

from __future__ import annotations

import csv
import os
import secrets
import statistics
import string
import threading
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from . import roblox_bulk_protocol as bulk
from . import roblox_protocol as individual
from .roblox_activity import RobloxActivity

ALPHABET = string.ascii_lowercase + string.digits
PHASES = ("stage1", "comparison", "stage2")
STATES = (
    "CurrentAccountMatchByAPI",
    "HistoricalOrDifferentCurrentName",
    "NoMatchingBulkRecord",
    "Unknown",
    "UnsafeOrContradictoryResponse",
)


def write_new(path, value):
    with Path(path).open("xb") as stream:
        stream.write(bulk.encoded(value) + b"\n")
        stream.flush()
        os.fsync(stream.fileno())


def policy_identity():
    return bulk.digest(
        Path(bulk.__file__).read_bytes()
        + Path(individual.__file__).read_bytes()
        + Path(__file__).read_bytes()
    )


class BulkExperiment:
    def __init__(
        self,
        directory,
        database,
        *,
        control=lambda: {},
        transport=None,
        clock=time.monotonic,
        wall=lambda: datetime.now(UTC),
        wait=None,
        updated=lambda: None,
    ):
        self.directory = Path(directory)
        self.database = Path(database)
        self.control = control
        self.transport = transport or bulk.Transport()
        self.clock, self.wall = clock, wall
        self.cancel = threading.Event()
        self.wait = wait or self.cancel.wait
        self.updated = updated
        self.manifest_path = self.directory / "manifest.json"
        self.journal_path = self.directory / "events.jsonl"

    def prepare(self, exclusion_manifests=()):
        if self.manifest_path.exists():
            return self.manifest()
        if self.journal_path.exists():
            raise ValueError("Evidence exists without its manifest; review required")
        excluded, sources = set(), []
        for path in exclusion_manifests:
            path = Path(path)
            if not path.exists():
                continue
            data = path.read_bytes()
            value = bulk.strict_json(data)
            rows = value.get("candidates")
            if not isinstance(rows, list):
                raise ValueError("Unexpected exclusion manifest")
            for row in rows:
                name = row.get("requested_username")
                if not isinstance(name, str):
                    raise ValueError("Unexpected exclusion candidate")
                excluded.add(name.casefold())
            sources.append({"source": path.name, "sha256": bulk.digest(data), "count": len(rows)})
        names, seen = [], set(excluded)
        while len(names) < 1000:
            name = "".join(secrets.choice(ALPHABET) for _ in range(4))
            if name not in seen:
                names.append(name)
                seen.add(name)
        subset = secrets.SystemRandom().sample(names[:200], 20)
        value = {
            "version": 1,
            "created_at": bulk.utcnow(),
            "policy": bulk.POLICY,
            "protocol_sha256": policy_identity(),
            "alphabet": ALPHABET,
            "generation": "Independent uniform secrets.choice; rejection of casefold duplicates/exclusions",
            "excluded_sources": sources,
            "excluded_unique": len(excluded),
            "candidates": names,
            "comparison_random": subset,
            "stage1_names": 200,
            "batch_size": 20,
            "pace_seconds": 10,
            "timeout_seconds": 10,
            "bulk_url": bulk.BULK_URL,
            "excludeBannedUsers": False,
            "individual_endpoint": individual.ENDPOINT,
            "Birthday": individual.BIRTHDAY,
            "Context": individual.CONTEXT,
            "comparison_budget": 20,
        }
        value["fingerprint"] = bulk.digest(bulk.encoded(value))
        self.directory.mkdir(parents=True, exist_ok=True)
        write_new(self.manifest_path, value)
        return self.manifest()

    def manifest(self):
        data = bulk.strict_json(self.manifest_path.read_bytes())
        fingerprint = data.pop("fingerprint")
        if fingerprint != bulk.digest(bulk.encoded(data)):
            raise ValueError("Manifest fingerprint mismatch")
        names, subset = data["candidates"], data["comparison_random"]
        if (
            len(names) != 1000
            or len(set(names)) != 1000
            or any(len(n) != 4 or any(c not in ALPHABET for c in n) for n in names)
            or len(subset) != 20
            or len(set(subset)) != 20
            or not set(subset) <= set(names[:200])
        ):
            raise ValueError("Invalid frozen sample")
        data["fingerprint"] = fingerprint
        fixed = {
            "version": 1,
            "policy": bulk.POLICY,
            "alphabet": ALPHABET,
            "stage1_names": 200,
            "batch_size": 20,
            "pace_seconds": 10,
            "timeout_seconds": 10,
            "bulk_url": bulk.BULK_URL,
            "excludeBannedUsers": False,
            "individual_endpoint": individual.ENDPOINT,
            "Birthday": individual.BIRTHDAY,
            "Context": individual.CONTEXT,
            "comparison_budget": 20,
        }
        if any(type(data.get(k)) is not type(v) or data[k] != v for k, v in fixed.items()):
            raise ValueError("Frozen request parameters or budgets are unsupported")
        return data

    def events(self):
        events, warning = self.read_evidence()
        if warning:
            raise ValueError(warning)
        return events

    def read_evidence(self):
        """A corrupt tail remains inspectable, but can never authorize traffic."""
        if not self.journal_path.exists():
            return [], ""
        fingerprint, previous, events = self.manifest()["fingerprint"], "", []
        with self.journal_path.open("rb") as stream:
            while line := stream.readline(bulk.MAX_FILE + 1):
                try:
                    if not line.endswith(b"\n"):
                        raise ValueError("Interrupted journal write; preserve bytes and review")
                    event = bulk.strict_json(line)
                    sha = event.pop("sha256")
                    if (
                        event["previous"] != previous
                        or event["manifest"] != fingerprint
                        or sha != bulk.digest(bulk.encoded(event))
                    ):
                        raise ValueError("Journal integrity failure; no replay permitted")
                    event["sha256"] = previous = sha
                    events.append(event)
                except (ValueError, UnicodeError, KeyError, TypeError, RecursionError) as exc:
                    return events, f"Evidence stopped at record {len(events) + 1}: {exc}"
        return events, ""

    def append(self, event):
        event = dict(event, timestamp=self.wall().isoformat(), previous=self.previous, manifest=self.identity)
        event["sha256"] = bulk.digest(bulk.encoded(event))
        with self.journal_path.open("ab") as stream:
            stream.write(bulk.encoded(event) + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        self.previous = event["sha256"]

    def report(self):
        manifest = self.manifest()
        events, warning = self.read_evidence()
        rows, comparisons, phases, intents, responses, latencies = {}, {}, {}, [], [], []
        pacing, conflicts, observations = 0.0, [], []
        for event in events:
            kind, phase = event["event"], event["phase"]
            if kind == "begin":
                phases[phase] = {
                    "state": "Interrupted",
                    "stop_reason": "No durable end; never replay",
                    "elapsed_seconds": None,
                }
            elif kind == "end":
                phases[phase] = {k: event[k] for k in ("state", "stop_reason", "elapsed_seconds")}
            elif kind == "intent":
                intents.append(event)
            elif kind == "response":
                responses.append(event)
                latencies.append(event["response"]["latency_seconds"])
            elif kind == "interpretation":
                rows.update(event["mapping"])
            elif kind == "individual":
                comparisons[event["username"]] = event["result"]
                if event["disagreement"]:
                    conflicts.append(event["username"])
            elif kind == "pacing":
                pacing += event["elapsed_seconds"]
            if kind == "response" and (
                event["response"]["error"]
                or event["response"]["status"] != 200
                or event["response"]["challenge"]
                or "retry-after" in event["response"]["headers"]
                or event["response"]["headers"].get("x-ratelimit-remaining") == "0"
            ):
                observations.append(event["response"])
        complete = [r for r in responses if r["response"]["complete"]]
        matched_compared = [n for n in comparisons if rows[n]["state"] == "CurrentAccountMatchByAPI"]
        return {
            "manifest": manifest,
            "phases": phases,
            "account_observations": rows,
            "individual_observations": comparisons,
            "semantic_disagreements": conflicts,
            "counts": dict(Counter(v["state"] for v in rows.values())),
            "dispatch_intents": len(intents),
            "requested_bulk_names": sum(
                len(e["request"]["usernames"]) for e in intents if e["phase"] != "comparison"
            ),
            "completed_bulk_batches": sum(e["phase"] != "comparison" for e in complete),
            "complete_http_responses": len(complete),
            "uncertain_or_missing_responses": len(intents) - len(complete),
            "per_phase_intents": dict(Counter(e["phase"] for e in intents)),
            "comparison_coverage": len(comparisons),
            "current_match_compared": len(matched_compared),
            "observed_taken_agreements": sum(
                comparisons[n]["classification"] == "Taken" for n in matched_compared
            ),
            "observed_disagreements": len(conflicts),
            "unresolved": sum(r["state"] != "CurrentAccountMatchByAPI" for r in rows.values()),
            "latencies_seconds": latencies,
            "mean_latency_seconds": statistics.mean(latencies) if latencies else None,
            "p95_latency_seconds": sorted(latencies)[max(0, (95 * len(latencies) + 99) // 100 - 1)]
            if latencies
            else None,
            "deliberate_pacing_seconds": pacing,
            "incomplete_pacing_intervals": sum(e["event"] == "pacing_intent" for e in events)
            - sum(e["event"] == "pacing" for e in events),
            "total_elapsed_runtime_seconds": sum(v["elapsed_seconds"] for v in phases.values())
            if phases and all(v["elapsed_seconds"] is not None for v in phases.values())
            else None,
            "runtime_definition": "Phase loops; excludes manual review gaps and report writing",
            "known_dispatch_elapsed_seconds": sum(e["dispatch_elapsed_seconds"] for e in responses),
            "provider_observations": observations,
            "hypothetical_only": {
                "individual_baseline_requests": 1000,
                "bulk_plan_requests": 50,
                "comparison_budget": 20,
                "full_bulk_plus_comparison_plan": 70,
                "arithmetic_request_difference": 930,
                "precise_classifications_replaced": False,
                "note": "Different output semantics; no measured speedup or availability accuracy",
            },
            "evidence": events,
            "journal_integrity_warning": warning,
            "journal_sha256": bulk.digest(self.journal_path.read_bytes())
            if self.journal_path.exists()
            else "",
            "manual_confirmation": "Not performed",
        }

    def review(self, phase):
        self.events()  # Never authorize a valid prefix of incomplete evidence.
        if phase not in PHASES:
            raise ValueError("Unknown phase")
        report = self.report()
        if phase in report["phases"]:
            raise ValueError("This phase was already authorized; no retry or replay")
        if any(v["state"] != "Completed" for v in report["phases"].values()):
            raise ValueError("Stopped/interrupted evidence requires review; no further phase")
        if phase != "stage1" and report["phases"].get("stage1", {}).get("state") != "Completed":
            raise ValueError("Complete and review Stage 1 first")
        if phase == "stage2" and report["completed_bulk_batches"] != 10:
            raise ValueError("Stage 1 boundary is not complete")
        if report["semantic_disagreements"]:
            raise ValueError("Semantic disagreement requires review")
        manifest = report["manifest"]
        if manifest["protocol_sha256"] != policy_identity():
            raise ValueError("Protocol changed after preparation; review required")
        tail = report["evidence"][-1]["sha256"] if report["evidence"] else ""
        return bulk.digest(bulk.encoded([phase, manifest["fingerprint"], tail]))

    def plan(self, phase):
        manifest = self.manifest()
        if phase == "comparison":
            return [
                {"kind": "individual", "method": "GET", "url": individual.request_url(n), "usernames": [n]}
                for n in manifest["comparison_random"]
            ]
        names = manifest["candidates"][:200] if phase == "stage1" else manifest["candidates"][200:]
        return [
            {
                "kind": "bulk",
                "method": "POST",
                "url": bulk.BULK_URL,
                "excludeBannedUsers": False,
                "usernames": names[i : i + 20],
            }
            for i in range(0, len(names), 20)
        ]

    def run(self, phase, authorization):
        if not authorization or authorization != self.review(phase):
            raise ValueError("Explicit authorization of current evidence required")
        with RobloxActivity(self.database) as guard:
            # Recheck after obtaining cross-process exclusion, before authorizing any request.
            if authorization != self.review(phase):
                raise ValueError("Evidence changed since review")
            control = self.control()
            deadline = control.get("not_before")
            if (
                control.get("active_run")
                or control.get("blocked")
                or control.get("restriction_streak", 0)
                or (deadline and datetime.fromisoformat(deadline) > self.wall())
            ):
                raise ValueError("Individual checker active or provider wait/review unresolved")
            events = self.events()
            self.previous = events[-1]["sha256"] if events else ""
            self.identity = self.manifest()["fingerprint"]
            guard.mark_pending()
            self.append(
                {
                    "event": "begin",
                    "phase": phase,
                    "authorization": authorization,
                    "policy": bulk.POLICY,
                    "budget": len(self.plan(phase)),
                }
            )
            started, last_finished = self.clock(), None
            previous_responses = [e for e in events if e["event"] == "response"]
            state, stop = "Stopped", "cancelled"
            try:
                for ordinal, request in enumerate(self.plan(phase)):
                    if self.cancel.is_set():
                        stop = "cancelled"
                        break
                    delay = max(0, 10 - (self.clock() - last_finished)) if last_finished is not None else 0
                    if ordinal == 0 and previous_responses:
                        at = datetime.fromisoformat(previous_responses[-1]["timestamp"])
                        delay = max(0, 10 - (self.wall() - at).total_seconds())
                    if delay:
                        wait_started = self.clock()
                        self.append({"event": "pacing_intent", "phase": phase, "requested_seconds": delay})
                        cancelled = self.wait(delay)
                        self.append(
                            {
                                "event": "pacing",
                                "phase": phase,
                                "elapsed_seconds": self.clock() - wait_started,
                            }
                        )
                        if cancelled or self.cancel.is_set():
                            stop = "cancelled"
                            break
                    self.append({"event": "intent", "phase": phase, "ordinal": ordinal, "request": request})
                    dispatch_start = self.clock()
                    response = self.transport(request)
                    last_finished = self.clock()
                    evidence = bulk.public_response(response)
                    self.append(
                        {
                            "event": "response",
                            "phase": phase,
                            "ordinal": ordinal,
                            "response": evidence,
                            "dispatch_elapsed_seconds": last_finished - dispatch_start,
                        }
                    )
                    stop = bulk.response_stop(response)
                    if evidence["body_omitted_for_sensitive_fields"]:
                        stop = "unexpected_sensitive_response"
                    if phase == "comparison":
                        name = request["usernames"][0]
                        classification, reason, code, message = (
                            ("Unknown", stop, None, "") if stop else individual.classify(response)
                        )
                        row = self.report()["account_observations"][name]
                        disagreement = row["state"] == "CurrentAccountMatchByAPI" and classification in (
                            "Available",
                            "Reserved",
                            "Invalid",
                        )
                        self.append(
                            {
                                "event": "individual",
                                "phase": phase,
                                "username": name,
                                "result": {
                                    "classification": classification,
                                    "reason": reason,
                                    "api_code": code,
                                    "message": message,
                                    "source": "individual_validator_preliminary",
                                    "account_state_at_comparison": row["state"],
                                    "response_event_sha256": self.previous,
                                },
                                "disagreement": disagreement,
                            }
                        )
                        if classification == "Unknown":
                            stop = stop or "unknown_individual_response"
                        elif disagreement:
                            stop = stop or "account_vs_registration_semantic_disagreement"
                    else:
                        mapping, parse_stop = bulk.interpret_bulk(request["usernames"], response)
                        stop = stop or parse_stop
                        previous = self.report()["account_observations"]
                        conflict, conflicting_names = bulk.cross_batch_conflict(previous, mapping)
                        stop = stop or conflict
                        if stop:
                            unsafe = any(
                                s in stop for s in ("schema", "record", "conflict", "json", "duplicate")
                            )
                            mapping = {
                                n: {
                                    "state": "UnsafeOrContradictoryResponse" if unsafe else "Unknown",
                                    "reason": stop,
                                }
                                for n in request["usernames"]
                            }
                            for name in conflicting_names:
                                if name in previous:
                                    mapping[name] = {
                                        "state": "UnsafeOrContradictoryResponse",
                                        "reason": conflict,
                                        "previous_observation": previous[name],
                                    }
                        response_reference = next(
                            e for e in reversed(self.events()) if e["event"] == "response"
                        )
                        for value in mapping.values():
                            value["response_event_sha256"] = response_reference["sha256"]
                            value["http_status"] = response.status
                            value["observed_at_utc"] = response_reference["timestamp"]
                            value["latency_seconds"] = response.elapsed
                            value["source"] = "bulk_account_lookup_only"
                        self.append({"event": "interpretation", "phase": phase, "mapping": mapping})
                    self.updated()
                    if stop:
                        guard.restrict(stop)
                        break
                else:
                    state, stop = "Completed", "stage_boundary"
            except BaseException as exc:
                # An intent may have been dispatched. Never retry this phase.
                stop = "unexpected_execution_error:" + type(exc).__name__
                guard.restrict(stop)
                if not isinstance(exc, (Exception, KeyboardInterrupt)):
                    raise
            finally:
                self.append(
                    {
                        "event": "end",
                        "phase": phase,
                        "state": state,
                        "stop_reason": stop,
                        "elapsed_seconds": self.clock() - started,
                    }
                )
                self.save_report()
                self.updated()
                if state == "Completed" or stop == "cancelled":
                    guard.clear_pending()
        return self.report()

    def save_report(self):
        report = self.report()
        write_new(self.directory / ("report-" + uuid4().hex + ".json"), report)
        return report

    def export(self, path, kind):
        report = self.report()
        if kind == "json":
            write_new(path, report)
            return
        with Path(path).open("x", encoding="utf-8-sig", newline="") as stream:
            fields = [
                "username",
                "account_evidence_state",
                "account_reason",
                "account_id",
                "current_name",
                "individual_classification",
                "individual_api_code",
                "individual_reason",
                "bulk_http_status",
                "bulk_observed_at_utc",
                "bulk_latency_seconds",
                "bulk_response_event_sha256",
                "individual_response_event_sha256",
                "semantic_disagreement",
                "manual_confirmation",
            ]
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            for name in report["manifest"]["candidates"]:
                account = report["account_observations"].get(name, {})
                validation = report["individual_observations"].get(name, {})
                record = account.get("record", {})
                # Prefix formula-significant cells, including future API text.
                row = dict(
                    zip(
                        fields,
                        [
                            name,
                            account.get("state", "NotAttempted"),
                            account.get("reason", ""),
                            record.get("id", ""),
                            record.get("name", ""),
                            validation.get("classification", ""),
                            validation.get("api_code", ""),
                            validation.get("reason", ""),
                            account.get("http_status", ""),
                            account.get("observed_at_utc", ""),
                            account.get("latency_seconds", ""),
                            account.get("response_event_sha256", ""),
                            validation.get("response_event_sha256", ""),
                            name in report["semantic_disagreements"],
                            "Not performed",
                        ],
                        strict=True,
                    )
                )
                writer.writerow(
                    {
                        k: "'" + v if isinstance(v, str) and v.startswith(("=", "+", "-", "@")) else v
                        for k, v in row.items()
                    }
                )
            stream.flush()
            os.fsync(stream.fileno())
