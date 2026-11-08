import csv
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import x_poc as poc  # noqa: E402


def response(valid=True, reason="available", **fields):
    return poc.Response(
        200,
        json.dumps(dict(valid=valid, reason=reason, **fields)).encode(),
        {"content-type": "application/json"},
    )


class Clock:
    def __init__(self):
        self.seconds = 0.0
        self.calls = []
        self.waits = []

    def now(self):
        return datetime(2026, 11, 7, tzinfo=UTC) + timedelta(seconds=self.seconds)

    def monotonic(self):
        return self.seconds

    def wait(self, delay):
        self.waits.append(delay)
        self.seconds += delay
        return False

    def fetch(self, username, timeout):
        self.calls.append((username, self.seconds))
        self.seconds += 0.125
        # Synthetic negative fixture, not a claim that invalid names are really taken.
        return response(False, "taken")


class ParsingTests(unittest.TestCase):
    def test_syntax_boundaries_characters_and_independence(self):
        for length, valid in ((4, False), (5, True), (14, True), (15, True), (16, False)):
            with self.subTest(length=length):
                result = poc.validate_syntax("a" * length)
                self.assertEqual(result["valid"], valid)
                self.assertEqual(result["boundary_needs_verification"], length == 15)
        for name in ("Abc12", "_name", "name_", "a_b_c", "xname", "admin", "12345"):
            with self.subTest(name=name):
                self.assertTrue(poc.validate_syntax(name)["valid"])
                self.assertEqual(poc.parse(response(False, "custom_message"), name)[0], "Unknown")
        for name in (
            "",
            "bad-name",
            "bad.name",
            "bad name",
            "@abcde",
            "abcde!",
            "abcde\n",
            "abc\u00e9d",
            "\u0645\u0633\u062a\u062e\u062f\u0645",
        ):
            with self.subTest(name=name):
                self.assertFalse(poc.validate_syntax(name)["valid"])
                self.assertEqual(poc.parse(response(), name)[0], "Unknown")

    def test_observed_long_length_and_coherent_variants(self):
        for msg, desc in (
            ("Username is too long", "Your username must be shorter than 15 characters."),
            ("USERNAME IS TOO LONG!", "Your username cannot be longer than 15 characters."),
            ("Username too long", "Your username must be at most 15 characters long."),
        ):
            with self.subTest(msg=msg):
                sample = response(False, "invalid_username", msg=msg, desc=desc)
                classification, reason, parsed = poc.parse(sample, "a" * 16)
                self.assertEqual(classification, "Invalid")
                self.assertEqual(reason, "preliminary_api_invalid_username_explicit_long_length_rejection")
                self.assertEqual(parsed["desc"], desc)
                self.assertEqual(poc.parse(sample, "a" * 15)[0], "Unknown")
        self.assertEqual(
            poc.parse(
                response(
                    False,
                    "invalid_username",
                    msg="Username too short!",
                    desc="Your username must be at least 5 characters.",
                ),
                "abcd",
            )[0],
            "Invalid",
        )

    def test_incoherent_format_length_and_mixed_messages_remain_unknown(self):
        for sample, name in (
            (response(False, "improper_format"), "valid_name"),
            (
                response(
                    False,
                    "invalid_username",
                    msg="Username is too long",
                    desc="Your username must be longer than 4 characters.",
                ),
                "a" * 16,
            ),
            (
                response(
                    False,
                    "invalid_username",
                    msg="Username is too long",
                    desc="Your username must be shorter than 14 characters.",
                ),
                "a" * 16,
            ),
            (
                response(
                    True,
                    "invalid_username",
                    msg="Username is too long",
                    desc="Your username must be shorter than 15 characters.",
                ),
                "a" * 16,
            ),
            (response(False, "taken", msg="Unavailable. Available!"), "valid_name"),
            (response(False, "is_banned_word", msg="Unavailable. Available!"), "valid_name"),
            (response(False, "contains_banned_word", msg="Invalid format"), "bad-name"),
            (response(False, "custom_message", msg="Username is too long"), "a" * 16),
        ):
            with self.subTest(sample=sample):
                self.assertEqual(poc.parse(sample, name)[0], "Unknown")

    def test_invalid_username_explicit_short_length_and_evidence(self):
        sample = response(
            False,
            "invalid_username",
            msg="Username is too short",
            desc="Your username must be longer than 4 characters.",
        )
        classification, reason, parsed = poc.parse(sample, "abcd")
        self.assertEqual(classification, "Invalid")
        self.assertEqual(reason, "preliminary_api_invalid_username_explicit_short_length_rejection")
        self.assertEqual(parsed, json.loads(sample.body))
        self.assertEqual(poc.parse(sample, "abc")[0], "Invalid")
        self.assertEqual(poc.parse(sample, "abcde")[0], "Unknown")

    def test_invalid_username_ambiguous_long_conflicting_or_unknown_stays_unknown(self):
        samples = [
            response(
                True,
                "invalid_username",
                msg="Username is too short",
                desc="Your username must be longer than 4 characters.",
            ),
            response(False, "invalid_username"),
            response(False, "invalid_username", msg="Unavailable"),
            response(False, "invalid_username", msg="Username is too short", desc="Available!"),
            response(False, "invalid_username", msg="Taken"),
            response(
                False, "invalid_username", msg="Username is too long", desc="Your username is too long."
            ),
            response(False, "unknown_reason"),
            response(False, "invalid"),
        ]
        for sample in samples:
            with self.subTest(sample=sample):
                self.assertEqual(poc.parse(sample, "abcd")[0], "Unknown")

    def test_supported_rejection_reasons_with_true_are_unknown(self):
        for reason in (
            "taken",
            "is_banned_word",
            "contains_banned_word",
            "improper_format",
            "invalid_username",
        ):
            with self.subTest(reason=reason):
                self.assertEqual(poc.parse(response(True, reason), "valid_name")[0], "Unknown")

    def test_observed_improper_format_and_existing_mappings(self):
        sample = response(
            False,
            "improper_format",
            msg="Only use letters, numbers and '_'",
            desc="Your username can only contain letters, numbers and '_'",
        )
        classification, reason, parsed = poc.parse(sample, "bad-name")
        self.assertEqual(classification, "Invalid")
        self.assertEqual(
            reason, "preliminary_api_improper_format_syntax_rejection_not_ownership_or_availability"
        )
        self.assertEqual(parsed["reason"], "improper_format")
        for valid, api_reason, expected in (
            (True, "available", "Available"),
            (False, "taken", "Taken"),
            (False, "is_banned_word", "Reserved"),
            (False, "contains_banned_word", "Reserved"),
        ):
            with self.subTest(api_reason=api_reason):
                self.assertEqual(poc.parse(response(valid, api_reason), "valid_name")[0], expected)

    def test_improper_format_conflicts_and_unrecognized_reasons_are_unknown(self):
        for sample in (
            response(True, "improper_format"),
            response(False, "IMPROPER_FORMAT"),
            response(False, "unknown_format"),
            response(False, "invalid"),
            response(False, "improper_format", msg="Available!"),
            response(False, "improper_format", msg="Already taken"),
            response(False, "improper_format", msg="Reserved"),
        ):
            with self.subTest(sample=sample):
                self.assertEqual(poc.parse(sample, "bad-name")[0], "Unknown")

    def test_observed_banned_word_is_preliminary_rejection_not_ownership(self):
        sample = response(
            False,
            "is_banned_word",
            msg="Username is unavailable",
            desc="This username is unavailable. Please try another.",
        )
        classification, reason, parsed = poc.parse(sample, "github")
        self.assertEqual(classification, "Reserved")
        self.assertEqual(reason, "preliminary_api_is_banned_word_rejection_not_account_ownership")
        self.assertEqual(parsed["reason"], "is_banned_word")

    def test_banned_word_conflicts_and_unexpected_reasons_remain_unknown(self):
        for sample in (
            response(True, "is_banned_word"),
            response(False, "is_banned_word", msg="Available!"),
            response(False, "is_banned_word", desc="Username has been taken"),
            response(False, "is_banned_word", msg="CAPTCHA required"),
            response(False, "IS_BANNED_WORD"),
            response(False, "banned"),
            response(False, "new_rejection"),
        ):
            with self.subTest(sample=sample):
                self.assertEqual(poc.parse(sample, "github")[0], "Unknown")

    def test_contains_banned_word_is_preliminary_and_preserves_api_reason(self):
        sample = response(
            False,
            "contains_banned_word",
            msg="Username is unavailable",
            desc="This username is unavailable. Please try another.",
        )
        classification, reason, parsed = poc.parse(sample, "admin")
        self.assertEqual(classification, "Reserved")
        self.assertEqual(reason, "preliminary_api_contains_banned_word_rejection_not_account_ownership")
        self.assertEqual(parsed["reason"], "contains_banned_word")

    def test_contains_banned_word_conflicts_restrictions_and_unknown_reasons(self):
        samples = [
            response(True, "contains_banned_word"),
            response(False, "contains_banned_word", msg="Available!"),
            response(False, "contains_banned_word", desc="Already taken"),
            response(False, "contains_banned_word", msg="CAPTCHA required"),
            response(False, "CONTAINS_BANNED_WORD"),
            response(False, "reserved"),
            response(False, "invalid"),
            response(False, "banned"),
            response(False, "new_reason"),
        ]
        for sample in samples:
            with self.subTest(sample=sample):
                self.assertEqual(poc.parse(sample, "admin")[0], "Unknown")

    def test_supported_pairs_are_preliminary_only(self):
        self.assertEqual(poc.parse(response(), "fresh_name")[0], "Available")
        self.assertEqual(poc.parse(response(False, "taken"), "OpenAI")[0], "Taken")

    def test_ambiguous_unmapped_and_contradictory_results_are_unknown(self):
        samples = [
            response(True, "taken"),
            response(False, "available"),
            response(False, "reserved"),
            response(False, "invalid"),
            response(False, "suspended"),
            response(1, "available"),
            response(None, "available"),
            response(True, "AVAILABLE"),
            response(extra=True),
            response(msg="This username is taken"),
            response(False, "taken", msg="Available!"),
            poc.Response(200, b"{}"),
            poc.Response(200, b"[]"),
            poc.Response(200, b"null"),
            poc.Response(200, b"not json"),
            poc.Response(200, b""),
            poc.Response(200, b"\xff"),
            poc.Response(200, b'{"valid":true,"valid":false,"reason":"available"}'),
            response(msg=["unexpected"]),
            poc.Response(200, b'{"valid":NaN,"reason":"available"}'),
        ]
        for sample in samples:
            with self.subTest(sample=sample):
                self.assertEqual(poc.parse(sample, "fresh_name")[0], "Unknown")
        self.assertEqual(poc.parse(response(), "bad-name")[0], "Unknown")

    def test_http_restrictions_retry_headers_and_network_failures(self):
        for status in (None, 201, 301, 302, 400, 401, 403, 404, 429, 500):
            with self.subTest(status=status):
                self.assertEqual(poc.parse(poc.Response(status, response().body), "validname")[0], "Unknown")
        for headers in (
            {"retry-after": "120"},
            {"retry-after": ""},
            {"x-rate-limit-remaining": "0"},
            {"x-ratelimit-remaining": " 0 "},
            {"x-rate-limit-remaining": "-1"},
            {"x-rate-limit-remaining": "unrecognized"},
            {"challenge-indicator": "present"},
        ):
            with self.subTest(headers=headers):
                self.assertEqual(
                    poc.parse(poc.Response(200, response().body, headers), "validname")[0], "Unknown"
                )
        self.assertEqual(
            poc.parse(poc.Response(error="TimeoutError", complete=False), "validname")[0], "Unknown"
        )
        self.assertEqual(poc.parse(response(msg="CAPTCHA required"), "validname")[0], "Unknown")
        self.assertEqual(
            poc.parse(poc.Response(200, response().body, complete=False), "validname")[0], "Unknown"
        )

    def test_sensitive_values_are_redacted(self):
        result = poc.redact(
            {
                "token": "secret-value",
                "nested": {"session": "private"},
                "msg": "Bearer abc123 mail@example.com",
                "valid": True,
            }
        )
        text = json.dumps(result)
        self.assertNotIn("secret-value", text)
        self.assertNotIn("private", text)
        self.assertNotIn("abc123", text)
        self.assertNotIn("mail@example.com", text)
        self.assertEqual(result["valid"], True)
        self.assertNotIn("hiddenvalue", poc.redact('<input name="token" value="hiddenvalue">'))
        self.assertNotIn("scriptsecret", poc.redact("<script>scriptsecret</script>"))

    def test_cli_ctrl_c_before_execution_is_graceful(self):
        with (
            patch.object(sys, "argv", ["x_poc.py", "prepare"]),
            patch.object(poc, "prepare", side_effect=KeyboardInterrupt),
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(poc.main(), 130)


class ExperimentTests(unittest.TestCase):
    def historical_long_stop(self):
        self.historical_length_stop()
        original_parse = poc.parse

        def legacy_parse(sample, username):
            if username == "a" * 16:
                return (
                    "Unknown",
                    "unsupported_or_contradictory_invalid_username_evidence",
                    json.loads(sample.body),
                )
            return original_parse(sample, username)

        def historical_fetch(username, timeout):
            self.clock.fetch(username, timeout)
            return response(
                False,
                "invalid_username",
                msg="Username is too long",
                desc="Your username must be shorter than 15 characters.",
            )

        with patch.object(poc, "parse", legacy_parse):
            self.run_stage(2, resume_reviewed_invalid_username=True, fetcher=historical_fetch)

    def test_long_resume_preserves_fifteen_records_four_reviews_and_checks_five_only(self):
        self.historical_long_stop()
        rows = [tuple(r) for r in self.experiment.db.execute("SELECT * FROM outcomes ORDER BY position")]
        reviews = self.experiment.reviews()
        files = {f.name: f.read_bytes() for f in self.folder.iterdir() if f.is_file()}
        proposal = self.experiment.review_stage2_invalid_username()
        self.assertEqual(proposal["remaining_stage2"], 5)
        self.assertEqual(proposal["review"]["position"], 15)
        self.assertFalse(proposal["applied"])
        self.assertEqual(files, {f.name: f.read_bytes() for f in self.folder.iterdir() if f.is_file()})
        report = self.run_stage(2, resume_reviewed_invalid_username=True)
        self.assertEqual(
            [
                tuple(r)
                for r in self.experiment.db.execute("SELECT * FROM outcomes ORDER BY position LIMIT 15")
            ],
            rows,
        )
        self.assertEqual(self.experiment.reviews()[:4], reviews)
        self.assertEqual(len(self.experiment.reviews()), 5)
        self.assertEqual(self.experiment.records()[14]["original_classification"], "Unknown")
        self.assertEqual(self.experiment.records()[14]["classification"], "Invalid")
        self.assertEqual(len(self.clock.calls), 20)
        self.assertEqual(len({name.casefold() for name, _ in self.clock.calls}), 20)
        self.assertEqual(self.clock.waits, [5] * 19)
        self.assertEqual(report["stages"][2]["attempted_unique"], 15)
        self.assertEqual(report["stages"][3]["attempted_unique"], 0)
        # Invalid syntax candidates still reached the API fixture; none were skipped.
        self.assertEqual([name for name, _ in self.clock.calls[15:18]], ["bad.name", "bad name", "@abcde"])
        self.run_stage(2, resume_reviewed_invalid_username=True)
        self.assertEqual(len(self.clock.calls), 20)

    def test_long_review_cancel_reopen_and_rollback(self):
        self.historical_long_stop()
        reviews = self.experiment.reviews()
        with patch.object(self.experiment, "set", side_effect=RuntimeError("audit failure")):
            with self.assertRaises(RuntimeError):
                self.experiment.review_stage2_invalid_username(apply=True)
        self.assertEqual(self.experiment.reviews(), reviews)
        report = self.run_stage(2, resume_reviewed_invalid_username=True, wait=lambda seconds: True)
        self.assertFalse(report["blocked"])
        self.assertEqual(len(self.clock.calls), 15)
        other = poc.Experiment(self.folder)
        try:
            other.run(
                2,
                fetcher=self.clock.fetch,
                clock=self.clock.now,
                monotonic=self.clock.monotonic,
                wait=self.clock.wait,
            )
        finally:
            other.close()
        self.assertEqual(len(self.clock.calls), 20)
        self.assertEqual(len({name.casefold() for name, _ in self.clock.calls}), 20)

    def test_long_review_cannot_unlock_future_unknown_or_restriction(self):
        self.historical_long_stop()
        for sample in (
            response(False, "custom_message"),
            poc.Response(429, b"{}", {"retry-after": "60"}),
            poc.Response(403, b"denied"),
            poc.Response(500, b"error"),
            response(False, "invalid_username", msg="Unsupported"),
        ):
            with self.subTest(sample=sample), tempfile.TemporaryDirectory() as directory:
                target = Path(directory) / "case"
                poc.prepare(target)
                (target / "manifest.json").write_bytes((self.folder / "manifest.json").read_bytes())
                other = poc.Experiment(target)
                try:
                    self.experiment.db.backup(other.db)
                    report = other.run(
                        2,
                        resume_reviewed_invalid_username=True,
                        fetcher=lambda *a, sample=sample: sample,
                        clock=self.clock.now,
                        monotonic=self.clock.monotonic,
                        wait=self.clock.wait,
                    )
                    self.assertTrue(report["blocked"])
                    self.assertEqual(report["stages"][2]["attempted_unique"], 11)
                    with self.assertRaises(ValueError):
                        other.run(
                            2,
                            resume_reviewed_invalid_username=True,
                            fetcher=lambda *a: self.fail("No request"),
                        )
                finally:
                    other.close()

    def test_long_review_raw_tamper_prevents_resume(self):
        self.historical_long_stop()
        with self.experiment.db:
            record = json.loads(
                self.experiment.db.execute("SELECT record FROM outcomes WHERE position=15").fetchone()[0]
            )
            record["raw_response_redacted"] += " "
            self.experiment.db.execute(
                "UPDATE outcomes SET record=? WHERE position=15", (json.dumps(record),)
            )
        with self.assertRaises(ValueError):
            self.run_stage(2, resume_reviewed_invalid_username=True)
        self.assertEqual(len(self.clock.calls), 15)

    def historical_length_stop(self):
        self.historical_format_stop()
        original_parse = poc.parse

        def legacy_parse(sample, username):
            if username == "abcd":
                return "Unknown", "unrecognized_or_contradictory_pair", json.loads(sample.body)
            return original_parse(sample, username)

        def historical_fetch(username, timeout):
            self.clock.fetch(username, timeout)
            return response(
                False,
                "invalid_username",
                msg="Username is too short",
                desc="Your username must be longer than 4 characters.",
            )

        with patch.object(poc, "parse", legacy_parse):
            self.run_stage(2, resume_reviewed_improper_format=True, fetcher=historical_fetch)

    def test_length_resume_preserves_fourteen_records_and_three_reviews_checks_six_only(self):
        self.historical_length_stop()
        originals = [tuple(r) for r in self.experiment.db.execute("SELECT * FROM outcomes ORDER BY position")]
        old_reviews = self.experiment.reviews()
        files = {f.name: f.read_bytes() for f in self.folder.iterdir() if f.is_file()}
        proposal = self.experiment.review_stage2_invalid_username()
        self.assertFalse(proposal["applied"])
        self.assertEqual(proposal["remaining_stage2"], 6)
        self.assertEqual(files, {f.name: f.read_bytes() for f in self.folder.iterdir() if f.is_file()})
        with self.assertRaises(ValueError):
            self.run_stage(2)
        report = self.run_stage(2, resume_reviewed_invalid_username=True)
        self.assertEqual(
            [
                tuple(r)
                for r in self.experiment.db.execute("SELECT * FROM outcomes ORDER BY position LIMIT 14")
            ],
            originals,
        )
        self.assertEqual(self.experiment.reviews()[:3], old_reviews)
        self.assertEqual(len(self.experiment.reviews()), 4)
        abcd = self.experiment.records()[13]
        self.assertEqual(abcd["classification"], "Invalid")
        self.assertEqual(abcd["original_classification"], "Unknown")
        self.assertEqual(abcd["parsed_fields"]["reason"], "invalid_username")
        self.assertEqual(len(self.clock.calls), 20)
        self.assertEqual(len({name.casefold() for name, _ in self.clock.calls}), 20)
        self.assertEqual(self.clock.waits, [5] * 19)
        self.assertEqual(report["stages"][1]["attempted_unique"], 5)
        self.assertEqual(report["stages"][2]["attempted_unique"], 15)
        self.assertEqual(report["stages"][3]["attempted_unique"], 0)
        self.assertEqual(self.experiment.review_stage2_invalid_username()["remaining_stage2"], 0)
        self.run_stage(2, resume_reviewed_invalid_username=True)
        self.assertEqual(len(self.clock.calls), 20)

    def test_length_resume_dynamic_pending_count_after_cancel_and_reopen(self):
        self.historical_length_stop()
        waits = iter((False, True))
        self.run_stage(2, resume_reviewed_invalid_username=True, wait=lambda seconds: next(waits))
        self.assertEqual(len(self.clock.calls), 15)
        self.assertEqual(self.experiment.review_stage2_invalid_username()["remaining_stage2"], 5)
        other = poc.Experiment(self.folder)
        try:
            report = other.run(
                2,
                fetcher=self.clock.fetch,
                clock=self.clock.now,
                monotonic=self.clock.monotonic,
                wait=self.clock.wait,
            )
        finally:
            other.close()
        self.assertEqual(len(self.clock.calls), 20)
        self.assertEqual(len({name.casefold() for name, _ in self.clock.calls}), 20)
        self.assertEqual(report["stages"][3]["attempted_unique"], 0)

    def test_length_review_rollback_and_restriction_stopping(self):
        self.historical_length_stop()
        reviews = self.experiment.reviews()
        with patch.object(self.experiment, "set", side_effect=RuntimeError("local audit failure")):
            with self.assertRaises(RuntimeError):
                self.experiment.review_stage2_invalid_username(apply=True)
        self.assertEqual(self.experiment.reviews(), reviews)
        self.assertEqual(self.experiment.setting("blocked"), "unrecognized_or_contradictory_pair")
        for sample in (
            response(False, "invalid_username"),
            response(True, "improper_format"),
            poc.Response(403, b"denied"),
            poc.Response(429, response().body, {"retry-after": "60"}),
        ):
            with self.subTest(sample=sample), tempfile.TemporaryDirectory() as directory:
                target = Path(directory) / "case"
                poc.prepare(target)
                (target / "manifest.json").write_bytes((self.folder / "manifest.json").read_bytes())
                other = poc.Experiment(target)
                try:
                    self.experiment.db.backup(other.db)
                    report = other.run(
                        2,
                        resume_reviewed_invalid_username=True,
                        fetcher=lambda *a, sample=sample: sample,
                        clock=self.clock.now,
                        monotonic=self.clock.monotonic,
                        wait=self.clock.wait,
                    )
                    self.assertTrue(report["blocked"])
                    self.assertEqual(report["stages"][2]["attempted_unique"], 10)
                    for stage, options in (
                        (1, {"resume_reviewed_is_banned_word": True}),
                        (2, {"resume_reviewed_contains_banned_word": True}),
                        (2, {"resume_reviewed_improper_format": True}),
                        (2, {"resume_reviewed_invalid_username": True}),
                    ):
                        with self.assertRaises(ValueError):
                            other.run(stage, fetcher=lambda *a: self.fail("No dispatch"), **options)
                finally:
                    other.close()

    def test_length_wrong_stage_and_changed_evidence_are_rejected(self):
        self.historical_length_stop()
        for stage in (1, 3):
            with self.assertRaises(ValueError):
                self.run_stage(stage, resume_reviewed_invalid_username=True)
        with self.experiment.db:
            record = json.loads(
                self.experiment.db.execute("SELECT record FROM outcomes WHERE position=14").fetchone()[0]
            )
            record["raw_response_redacted"] += " "
            self.experiment.db.execute(
                "UPDATE outcomes SET record=? WHERE position=14", (json.dumps(record),)
            )
        with self.assertRaises(ValueError):
            self.run_stage(2, resume_reviewed_invalid_username=True)
        self.assertEqual(len(self.clock.calls), 14)

    def test_length_cli_offline_preview_and_explicit_resume(self):
        self.historical_length_stop()
        with (
            patch.object(
                sys, "argv", ["x_poc.py", "review-stage2-invalid-username", "--output", str(self.folder)]
            ),
            redirect_stdout(io.StringIO()) as output,
        ):
            self.assertEqual(poc.main(), 0)
        self.assertEqual(json.loads(output.getvalue())["remaining_stage2"], 6)
        self.assertEqual(len(self.experiment.reviews()), 3)
        with (
            patch.object(
                sys,
                "argv",
                [
                    "x_poc.py",
                    "run",
                    "--stage",
                    "2",
                    "--output",
                    str(self.folder),
                    "--resume-reviewed-invalid-username",
                ],
            ),
            patch.object(poc.Experiment, "run", return_value={"blocked": None}) as run,
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(poc.main(), 0)
        run.assert_called_once_with(
            2,
            delay=5.0,
            timeout=15.0,
            resume_reviewed_is_banned_word=False,
            resume_reviewed_contains_banned_word=False,
            resume_reviewed_improper_format=False,
            resume_reviewed_invalid_username=True,
        )

    def historical_format_stop(self):
        self.historical_stage2_stop()
        original_parse = poc.parse

        def legacy_parse(sample, username):
            if username == "bad-name":
                return "Unknown", "unrecognized_or_contradictory_pair", json.loads(sample.body)
            return original_parse(sample, username)

        def historical_fetch(username, timeout):
            self.clock.fetch(username, timeout)
            if username == "bad-name":
                return response(
                    False,
                    "improper_format",
                    msg="Only use letters, numbers and '_'",
                    desc="Your username can only contain letters, numbers and '_'",
                )
            return response(False, "contains_banned_word", msg="Username is unavailable")

        with patch.object(poc, "parse", legacy_parse):
            self.run_stage(2, resume_reviewed_contains_banned_word=True, fetcher=historical_fetch)

    def test_format_resume_only_seven_with_all_original_evidence_and_reviews_unchanged(self):
        self.historical_format_stop()
        rows = [tuple(r) for r in self.experiment.db.execute("SELECT * FROM outcomes ORDER BY position")]
        reviews = self.experiment.reviews()
        files = {f.name: f.read_bytes() for f in self.folder.iterdir() if f.is_file()}
        proposal = self.experiment.review_stage2_improper_format()
        self.assertFalse(proposal["applied"])
        self.assertEqual(proposal["remaining_stage2"], 7)
        self.assertEqual(proposal["review"]["classification"], "Invalid")
        self.assertEqual(files, {f.name: f.read_bytes() for f in self.folder.iterdir() if f.is_file()})
        with self.assertRaises(ValueError):
            self.run_stage(2)
        report = self.run_stage(2, resume_reviewed_improper_format=True)
        self.assertEqual(len(self.clock.calls), 20)
        self.assertEqual(len({name.casefold() for name, _ in self.clock.calls}), 20)
        self.assertEqual(
            [
                tuple(r)
                for r in self.experiment.db.execute("SELECT * FROM outcomes ORDER BY position LIMIT 13")
            ],
            rows,
        )
        self.assertEqual(self.experiment.reviews()[:2], reviews)
        self.assertEqual(len(self.experiment.reviews()), 3)
        record = self.experiment.records()[12]
        self.assertEqual(record["original_classification"], "Unknown")
        self.assertEqual(record["classification"], "Invalid")
        self.assertEqual(record["parsed_fields"]["reason"], "improper_format")
        self.assertEqual(report["stages"][1]["attempted_unique"], 5)
        self.assertEqual(report["stages"][2]["attempted_unique"], 15)
        self.assertEqual(report["stages"][2]["classification_counts"]["Invalid"], 1)
        self.assertEqual(report["stages"][3]["attempted_unique"], 0)
        self.assertEqual(self.clock.waits, [5] * 19)
        self.run_stage(2, resume_reviewed_improper_format=True)
        self.assertEqual(len(self.clock.calls), 20)

    def test_format_review_cancellation_reopen_and_safe_resume(self):
        self.historical_format_stop()
        report = self.run_stage(2, resume_reviewed_improper_format=True, wait=lambda seconds: True)
        self.assertFalse(report["blocked"])
        self.assertEqual(len(self.clock.calls), 13)
        other = poc.Experiment(self.folder)
        try:
            report = other.run(
                2,
                fetcher=self.clock.fetch,
                clock=self.clock.now,
                monotonic=self.clock.monotonic,
                wait=self.clock.wait,
            )
        finally:
            other.close()
        self.assertEqual(len(self.clock.calls), 20)
        self.assertEqual(len({name.casefold() for name, _ in self.clock.calls}), 20)
        self.assertEqual(report["stages"][3]["attempted_unique"], 0)

    def test_format_review_rollback_retains_two_prior_reviews_and_stop(self):
        self.historical_format_stop()
        reviews = self.experiment.reviews()
        with patch.object(self.experiment, "set", side_effect=RuntimeError("interrupted audit")):
            with self.assertRaises(RuntimeError):
                self.experiment.review_stage2_improper_format(apply=True)
        self.assertEqual(self.experiment.reviews(), reviews)
        self.assertEqual(self.experiment.setting("blocked"), "unrecognized_or_contradictory_pair")
        self.assertEqual(len(self.clock.calls), 13)

    def test_format_resume_rejects_later_unknown_restrictions_and_other_review_flags(self):
        self.historical_format_stop()
        for sample in (
            response(True, "improper_format"),
            response(False, "unknown_reason"),
            poc.Response(403, b"denied"),
            poc.Response(429, response().body, {"retry-after": "60"}),
        ):
            with self.subTest(sample=sample), tempfile.TemporaryDirectory() as directory:
                target = Path(directory) / "case"
                poc.prepare(target)
                (target / "manifest.json").write_bytes((self.folder / "manifest.json").read_bytes())
                other = poc.Experiment(target)
                try:
                    self.experiment.db.backup(other.db)
                    report = other.run(
                        2,
                        resume_reviewed_improper_format=True,
                        fetcher=lambda *args, sample=sample: sample,
                        clock=self.clock.now,
                        monotonic=self.clock.monotonic,
                        wait=self.clock.wait,
                    )
                    self.assertTrue(report["blocked"])
                    self.assertEqual(report["stages"][2]["attempted_unique"], 9)
                    for stage, options in (
                        (1, {"resume_reviewed_is_banned_word": True}),
                        (2, {"resume_reviewed_contains_banned_word": True}),
                        (2, {"resume_reviewed_improper_format": True}),
                    ):
                        with self.assertRaises(ValueError):
                            other.run(stage, fetcher=lambda *args: self.fail("Must not dispatch"), **options)
                finally:
                    other.close()

    def test_format_review_wrong_stage_and_altered_response_are_rejected(self):
        self.historical_format_stop()
        for stage in (1, 3):
            with self.assertRaises(ValueError):
                self.run_stage(stage, resume_reviewed_improper_format=True)
        with self.assertRaises(ValueError):
            self.run_stage(2, resume_reviewed_improper_format=True, resume_reviewed_contains_banned_word=True)
        with self.experiment.db:
            record = json.loads(
                self.experiment.db.execute("SELECT record FROM outcomes WHERE position=13").fetchone()[0]
            )
            record["raw_response_redacted"] += " "
            self.experiment.db.execute(
                "UPDATE outcomes SET record=? WHERE position=13", (json.dumps(record),)
            )
        with self.assertRaises(ValueError):
            self.run_stage(2, resume_reviewed_improper_format=True)
        self.assertEqual(len(self.clock.calls), 13)

    def test_format_cli_preview_is_offline_and_explicit_resume_is_forwarded(self):
        self.historical_format_stop()
        with (
            patch.object(
                sys, "argv", ["x_poc.py", "review-stage2-improper-format", "--output", str(self.folder)]
            ),
            redirect_stdout(io.StringIO()) as output,
        ):
            self.assertEqual(poc.main(), 0)
        self.assertEqual(json.loads(output.getvalue())["remaining_stage2"], 7)
        self.assertEqual(len(self.experiment.reviews()), 2)
        with (
            patch.object(
                sys,
                "argv",
                [
                    "x_poc.py",
                    "run",
                    "--stage",
                    "2",
                    "--output",
                    str(self.folder),
                    "--resume-reviewed-improper-format",
                ],
            ),
            patch.object(poc.Experiment, "run", return_value={"blocked": None}) as run,
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(poc.main(), 0)
        run.assert_called_once_with(
            2,
            delay=5.0,
            timeout=15.0,
            resume_reviewed_is_banned_word=False,
            resume_reviewed_contains_banned_word=False,
            resume_reviewed_improper_format=True,
            resume_reviewed_invalid_username=False,
        )

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name) / "evidence"
        poc.prepare(self.folder)
        self.experiment = poc.Experiment(self.folder)
        self.addCleanup(self.experiment.close)
        self.clock = Clock()

    def run_stage(self, stage, **options):
        return self.experiment.run(
            stage,
            clock=self.clock.now,
            monotonic=self.clock.monotonic,
            fetcher=options.pop("fetcher", self.clock.fetch),
            wait=options.pop("wait", self.clock.wait),
            **options,
        )

    def historical_stop(self):
        original_parse = poc.parse

        def legacy_parse(sample, username):
            if username == "github":
                return "Unknown", "unrecognized_or_contradictory_pair", json.loads(sample.body)
            return original_parse(sample, username)

        def historical_fetch(username, timeout):
            self.clock.fetch(username, timeout)
            return (
                response(
                    False,
                    "is_banned_word",
                    msg="Username is unavailable",
                    desc="This username is unavailable. Please try another.",
                )
                if username == "github"
                else response(False, "taken")
            )

        with patch.object(poc, "parse", legacy_parse):
            self.run_stage(1, fetcher=historical_fetch)
        return self.experiment.db.execute("SELECT * FROM outcomes ORDER BY position").fetchall()

    def test_review_preview_is_read_only_and_unreviewed_resume_is_blocked(self):
        self.historical_stop()
        before = (self.folder / "evidence.sqlite3").read_bytes()
        proposal = self.experiment.review_stage1()
        self.assertEqual(proposal["review"]["classification"], "Reserved")
        self.assertFalse(proposal["applied"])
        self.assertEqual(before, (self.folder / "evidence.sqlite3").read_bytes())
        self.assertEqual(self.experiment.report()["stages"][1]["classification_counts"]["Unknown"], 1)
        with self.assertRaises(ValueError):
            self.run_stage(1)
        self.assertEqual(len(self.clock.calls), 2)

    def test_explicit_review_resume_preserves_evidence_and_only_sends_remaining_three(self):
        originals = [tuple(row) for row in self.historical_stop()]
        manual = (self.folder / "manual-confirmation.csv").read_bytes()
        report = self.run_stage(1, resume_reviewed_is_banned_word=True)
        self.assertEqual(len(self.clock.calls), 5)
        self.assertEqual(len({name for name, _ in self.clock.calls}), 5)
        self.assertEqual(report["stages"][1]["classification_counts"]["Reserved"], 1)
        self.assertEqual(report["stages"][1]["classification_counts"]["Unknown"], 0)
        for stage in (2, 3):
            self.assertEqual(report["stages"][stage]["attempted_unique"], 0)
        saved = self.experiment.db.execute("SELECT * FROM outcomes ORDER BY position LIMIT 2").fetchall()
        self.assertEqual([tuple(row) for row in saved], originals)
        self.assertEqual(manual, (self.folder / "manual-confirmation.csv").read_bytes())
        review = report["classification_reviews"][0]
        self.assertEqual(review["original_classification"], "Unknown")
        self.assertEqual(review["original_reason"], "unrecognized_or_contradictory_pair")
        with (self.folder / "results.csv").open(newline="") as stream:
            github = list(csv.DictReader(stream))[1]
        self.assertEqual(github["classification"], "Reserved")
        self.assertEqual(github["original_classification"], "Unknown")
        self.assertEqual(self.clock.waits, [5] * 4)
        reopened = poc.Experiment(self.folder)
        try:
            self.assertEqual(reopened.report()["classification_reviews"], report["classification_reviews"])
        finally:
            reopened.close()
        self.run_stage(1, resume_reviewed_is_banned_word=True)
        self.assertEqual(len(self.clock.calls), 5)
        self.assertEqual(len(self.experiment.reviews()), 1)

    def test_reviewed_resume_cancellation_can_resume_without_duplicates(self):
        self.historical_stop()
        report = self.run_stage(1, resume_reviewed_is_banned_word=True, wait=lambda seconds: True)
        self.assertEqual(len(self.clock.calls), 2)
        self.assertFalse(report["blocked"])
        self.run_stage(1)
        self.assertEqual(len(self.clock.calls), 5)
        self.assertEqual(len({name for name, _ in self.clock.calls}), 5)

    def test_review_cannot_unlock_new_unknown_or_provider_restriction(self):
        for sample in (
            response(True, "is_banned_word"),
            response(False, "unexpected_reason"),
            poc.Response(429, response().body, {"retry-after": "60"}),
        ):
            with self.subTest(sample=sample), tempfile.TemporaryDirectory() as directory:
                poc.prepare(Path(directory) / "case")
                other = poc.Experiment(Path(directory) / "case")
                try:
                    # Copy the two original records/settings into an isolated database.
                    if not self.clock.calls:
                        self.historical_stop()
                    with other.db:
                        other.db.executemany(
                            "INSERT INTO outcomes VALUES(?,?,?,?)",
                            [tuple(r) for r in self.experiment.db.execute("SELECT * FROM outcomes")],
                        )
                        other.set("blocked", "unrecognized_or_contradictory_pair")
                    report = other.run(
                        1,
                        resume_reviewed_is_banned_word=True,
                        fetcher=lambda *args, sample=sample: sample,
                        wait=lambda seconds: False,
                    )
                    self.assertTrue(report["blocked"])
                    self.assertEqual(report["stages"][1]["attempted_unique"], 3)
                    with self.assertRaises(ValueError):
                        other.run(
                            1,
                            resume_reviewed_is_banned_word=True,
                            fetcher=lambda *args: self.fail("Must not dispatch"),
                        )
                finally:
                    other.close()

    def test_review_rejects_other_stage_restrictions_and_tampered_evidence(self):
        self.historical_stop()
        with self.assertRaises(ValueError):
            self.run_stage(2, resume_reviewed_is_banned_word=True)
        with self.experiment.db:
            self.experiment.set("blocked", "unexpected_http_status:403")
        with self.assertRaises(ValueError):
            self.run_stage(1, resume_reviewed_is_banned_word=True)
        with self.experiment.db:
            self.experiment.set("blocked", "unrecognized_or_contradictory_pair")
            row = self.experiment.db.execute("SELECT record FROM outcomes WHERE position=2").fetchone()
            record = json.loads(row[0])
            record["raw_response_redacted"] += " "
            self.experiment.db.execute("UPDATE outcomes SET record=? WHERE position=2", (json.dumps(record),))
        with self.assertRaises(ValueError):
            self.run_stage(1, resume_reviewed_is_banned_word=True)
        self.assertEqual(len(self.clock.calls), 2)

    def test_audited_original_mutation_is_detected(self):
        self.historical_stop()
        self.run_stage(1, resume_reviewed_is_banned_word=True, wait=lambda seconds: True)
        with self.experiment.db:
            self.experiment.db.execute("UPDATE outcomes SET record=record || ' ' WHERE position=2")
        with self.assertRaises(ValueError):
            self.experiment.report()

    def test_new_reserved_response_does_not_stop_or_start_later_stage(self):
        report = self.run_stage(1, fetcher=lambda *args: response(False, "is_banned_word"))
        self.assertFalse(report["blocked"])
        self.assertEqual(report["stages"][1]["classification_counts"]["Reserved"], 5)
        self.assertEqual(report["stages"][2]["attempted_unique"], 0)
        self.assertEqual(report["stages"][3]["attempted_unique"], 0)

    def test_review_schema_interruption_can_recover_atomically(self):
        self.historical_stop()
        # Simulate a crash after schema creation but before the review transaction.
        self.experiment.db.execute("""CREATE TABLE classification_reviews(
            position INTEGER PRIMARY KEY, original_sha256 TEXT,
            original_classification TEXT, original_reason TEXT,
            classification TEXT, reason TEXT, reviewed_at TEXT)""")
        with patch.object(self.experiment, "set", side_effect=RuntimeError("local interruption")):
            with self.assertRaises(RuntimeError):
                self.experiment.review_stage1(apply=True)
        self.assertEqual(self.experiment.reviews(), [])
        self.assertEqual(self.experiment.setting("blocked"), "unrecognized_or_contradictory_pair")
        report = self.run_stage(1, resume_reviewed_is_banned_word=True)
        self.assertFalse(report["blocked"])
        self.assertEqual(len(self.experiment.reviews()), 1)
        self.assertEqual(len(self.clock.calls), 5)

    def test_cli_review_is_offline_and_explicit_resume_flag_is_forwarded(self):
        self.historical_stop()
        base = ["x_poc.py", "review-stage1", "--output", str(self.folder)]
        with patch.object(sys, "argv", base), redirect_stdout(io.StringIO()) as output:
            self.assertEqual(poc.main(), 0)
        self.assertEqual(json.loads(output.getvalue())["remaining_stage1"], 3)
        self.assertTrue(self.experiment.setting("blocked"))
        args = [
            "x_poc.py",
            "run",
            "--output",
            str(self.folder),
            "--stage",
            "1",
            "--resume-reviewed-is-banned-word",
        ]
        with (
            patch.object(sys, "argv", args),
            patch.object(poc.Experiment, "run", return_value={"blocked": None}) as run,
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(poc.main(), 0)
        run.assert_called_once_with(
            1,
            delay=5.0,
            timeout=15.0,
            resume_reviewed_is_banned_word=True,
            resume_reviewed_contains_banned_word=False,
            resume_reviewed_improper_format=False,
            resume_reviewed_invalid_username=False,
        )

    def historical_stage2_stop(self):
        self.historical_stop()
        self.run_stage(1, resume_reviewed_is_banned_word=True)
        original_parse = poc.parse

        def legacy_parse(sample, username):
            if username == "admin":
                return "Unknown", "unrecognized_or_contradictory_pair", json.loads(sample.body)
            return original_parse(sample, username)

        def stage2_fetch(username, timeout):
            self.clock.fetch(username, timeout)
            return response(
                False,
                "contains_banned_word" if username == "admin" else "is_banned_word",
                msg="Username is unavailable",
                desc="This username is unavailable. Please try another.",
            )

        with patch.object(poc, "parse", legacy_parse):
            self.run_stage(2, fetcher=stage2_fetch)

    def test_stage2_preview_and_explicit_resume_preserve_all_eight_records_and_reviews(self):
        self.historical_stage2_stop()
        original_rows = [
            tuple(r) for r in self.experiment.db.execute("SELECT * FROM outcomes ORDER BY position")
        ]
        old_reviews = self.experiment.reviews()
        before = {f.name: f.read_bytes() for f in self.folder.iterdir() if f.is_file()}
        proposal = self.experiment.review_stage2()
        self.assertFalse(proposal["applied"])
        self.assertEqual(proposal["remaining_stage2"], 12)
        self.assertEqual(proposal["review"]["original_classification"], "Unknown")
        self.assertEqual(before, {f.name: f.read_bytes() for f in self.folder.iterdir() if f.is_file()})
        with self.assertRaises(ValueError):
            self.run_stage(2)
        report = self.run_stage(2, resume_reviewed_contains_banned_word=True)
        self.assertEqual(len(self.clock.calls), 20)
        self.assertEqual(len({name.casefold() for name, _ in self.clock.calls}), 20)
        self.assertEqual(report["stages"][1]["attempted_unique"], 5)
        self.assertEqual(report["stages"][2]["attempted_unique"], 15)
        self.assertEqual(report["stages"][3]["attempted_unique"], 0)
        self.assertEqual(
            [
                tuple(r)
                for r in self.experiment.db.execute("SELECT * FROM outcomes ORDER BY position LIMIT 8")
            ],
            original_rows,
        )
        self.assertEqual(self.experiment.reviews()[:1], old_reviews)
        self.assertEqual(len(self.experiment.reviews()), 2)
        admin = self.experiment.records()[7]
        self.assertEqual(admin["classification"], "Reserved")
        self.assertEqual(admin["original_classification"], "Unknown")
        self.assertEqual(admin["parsed_fields"]["reason"], "contains_banned_word")
        self.assertEqual(self.clock.waits, [5] * 19)
        self.run_stage(2, resume_reviewed_contains_banned_word=True)
        self.assertEqual(len(self.clock.calls), 20)
        self.assertEqual(len(self.experiment.reviews()), 2)

    def test_stage2_cancel_reopen_and_resume_only_pending_names(self):
        self.historical_stage2_stop()
        report = self.run_stage(2, resume_reviewed_contains_banned_word=True, wait=lambda seconds: True)
        self.assertFalse(report["blocked"])
        self.assertEqual(len(self.clock.calls), 8)
        reopened = poc.Experiment(self.folder)
        try:
            report = reopened.run(
                2,
                fetcher=self.clock.fetch,
                clock=self.clock.now,
                monotonic=self.clock.monotonic,
                wait=self.clock.wait,
            )
        finally:
            reopened.close()
        self.assertEqual(len(self.clock.calls), 20)
        self.assertEqual(len({name.casefold() for name, _ in self.clock.calls}), 20)
        self.assertEqual(report["stages"][3]["attempted_unique"], 0)

    def test_stage2_audit_failure_rolls_back_without_changing_prior_review(self):
        self.historical_stage2_stop()
        reviews = self.experiment.reviews()
        with patch.object(self.experiment, "set", side_effect=RuntimeError("local interruption")):
            with self.assertRaises(RuntimeError):
                self.experiment.review_stage2(apply=True)
        self.assertEqual(self.experiment.reviews(), reviews)
        self.assertEqual(self.experiment.setting("blocked"), "unrecognized_or_contradictory_pair")
        self.assertEqual(len(self.clock.calls), 8)
        self.run_stage(2, resume_reviewed_contains_banned_word=True)
        self.assertEqual(len(self.clock.calls), 20)

    def test_stage2_new_restriction_or_unknown_cannot_be_unlocked_by_either_review(self):
        self.historical_stage2_stop()
        for sample in (
            response(True, "contains_banned_word"),
            response(False, "unrecognized"),
            poc.Response(429, response().body, {"retry-after": "60"}),
        ):
            with self.subTest(sample=sample), tempfile.TemporaryDirectory() as directory:
                target = Path(directory) / "case"
                poc.prepare(target)
                (target / "manifest.json").write_bytes((self.folder / "manifest.json").read_bytes())
                other = poc.Experiment(target)
                try:
                    # SQLite backup copies fixture evidence, including the prior Stage 1 review.
                    self.experiment.db.backup(other.db)
                    report = other.run(
                        2,
                        resume_reviewed_contains_banned_word=True,
                        fetcher=lambda *args, sample=sample: sample,
                        clock=self.clock.now,
                        monotonic=self.clock.monotonic,
                        wait=self.clock.wait,
                    )
                    self.assertTrue(report["blocked"])
                    self.assertEqual(report["stages"][2]["attempted_unique"], 4)
                    for stage, options in (
                        (1, {"resume_reviewed_is_banned_word": True}),
                        (2, {"resume_reviewed_contains_banned_word": True}),
                    ):
                        with self.assertRaises(ValueError):
                            other.run(stage, fetcher=lambda *a: self.fail("No request permitted"), **options)
                finally:
                    other.close()

    def test_stage2_review_rejects_wrong_stage_restriction_and_changed_evidence(self):
        self.historical_stage2_stop()
        for stage in (1, 3):
            with self.assertRaises(ValueError):
                self.run_stage(stage, resume_reviewed_contains_banned_word=True)
        with self.experiment.db:
            self.experiment.set("blocked", "unexpected_http_status:403")
        with self.assertRaises(ValueError):
            self.run_stage(2, resume_reviewed_contains_banned_word=True)
        with self.experiment.db:
            self.experiment.set("blocked", "unrecognized_or_contradictory_pair")
            record = json.loads(
                self.experiment.db.execute("SELECT record FROM outcomes WHERE position=8").fetchone()[0]
            )
            record["raw_response_redacted"] += " "
            self.experiment.db.execute("UPDATE outcomes SET record=? WHERE position=8", (json.dumps(record),))
        with self.assertRaises(ValueError):
            self.run_stage(2, resume_reviewed_contains_banned_word=True)
        self.assertEqual(len(self.clock.calls), 8)

    def test_stage2_cli_preview_is_offline_and_resume_is_explicit(self):
        self.historical_stage2_stop()
        with (
            patch.object(sys, "argv", ["x_poc.py", "review-stage2", "--output", str(self.folder)]),
            redirect_stdout(io.StringIO()) as output,
        ):
            self.assertEqual(poc.main(), 0)
        self.assertEqual(json.loads(output.getvalue())["remaining_stage2"], 12)
        self.assertEqual(len(self.experiment.reviews()), 1)
        with (
            patch.object(
                sys,
                "argv",
                [
                    "x_poc.py",
                    "run",
                    "--stage",
                    "2",
                    "--output",
                    str(self.folder),
                    "--resume-reviewed-contains-banned-word",
                ],
            ),
            patch.object(poc.Experiment, "run", return_value={"blocked": None}) as run,
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(poc.main(), 0)
        run.assert_called_once_with(
            2,
            delay=5.0,
            timeout=15.0,
            resume_reviewed_is_banned_word=False,
            resume_reviewed_contains_banned_word=True,
            resume_reviewed_improper_format=False,
            resume_reviewed_invalid_username=False,
        )

    def test_manifest_exact_unique_categories_and_stage_boundaries(self):
        rows = json.loads((self.folder / "manifest.json").read_text())["candidates"]
        self.assertEqual(len(rows), 100)
        self.assertEqual(len({row["username"].casefold() for row in rows}), 100)
        self.assertEqual(poc.Counter(row["stage"] for row in rows), poc.STAGES)
        self.assertTrue(
            {
                "existing_account_candidate",
                "invalid_format",
                "restricted_looking",
                "valid_short",
                "valid_long",
                "random_valid",
            }
            <= {r["category"] for r in rows}
        )
        self.assertEqual(self.experiment.report()["stages"][1]["attempted_unique"], 0)
        with self.assertRaises(ValueError):
            poc.prepare(self.folder)

    def test_explicit_stages_do_not_advance_and_never_repeat(self):
        with self.assertRaises(ValueError):
            self.run_stage(2)
        for stage, cumulative in ((1, 5), (2, 20), (3, 100)):
            report = self.run_stage(stage)
            self.assertEqual(len(self.clock.calls), cumulative)
            self.assertFalse(report["blocked"])
            self.assertEqual(report["stages"][stage]["attempted_unique"], poc.STAGES[stage])
            self.run_stage(stage)
            self.assertEqual(len(self.clock.calls), cumulative)
        self.assertEqual(len({user.casefold() for user, _ in self.clock.calls}), 100)
        with (self.folder / "results.csv").open(newline="") as stream:
            self.assertEqual(len(list(csv.DictReader(stream))), 100)

    def test_actual_post_response_pacing_and_latency_report(self):
        report = self.run_stage(1, delay=7)
        self.assertEqual(self.clock.waits, [7] * 4)
        gaps = [
            later[1] - earlier[1] - 0.125
            for earlier, later in zip(self.clock.calls, self.clock.calls[1:], strict=False)
        ]
        self.assertEqual(gaps, [7] * 4)
        data = report["stages"][1]
        self.assertEqual(data["mean_latency_seconds"], 0.125)
        self.assertEqual(data["p95_latency_seconds"], 0.125)
        self.assertEqual(data["classification_counts"]["Taken"], 5)
        self.assertEqual(data["http_status_counts"], {"200": 5})
        self.assertEqual(data["runtime_seconds_including_pacing"], 28.625)
        self.assertAlmostEqual(data["attempts_per_minute"], 300 / 28.625)
        self.assertEqual(data["stopping_reason"], "stage_completed")

    def test_every_restriction_stops_immediately_and_blocks_all_restarts(self):
        result = self.run_stage(
            1, fetcher=lambda *a: poc.Response(429, response().body, {"retry-after": "60"})
        )
        self.assertTrue(result["blocked"])
        self.assertEqual(result["stages"][1]["attempted_unique"], 1)
        self.assertEqual(result["stages"][1]["classification_counts"]["Unknown"], 1)
        for stage in (1, 2, 3):
            with self.assertRaises(ValueError):
                self.run_stage(stage)
        self.assertFalse(self.clock.calls)

    def test_unknown_schema_stops_without_retry(self):
        report = self.run_stage(
            1,
            fetcher=lambda *a: poc.Response(
                200, b'{"new_schema":true}', {"content-type": "application/json"}
            ),
        )
        self.assertEqual(report["stages"][1]["attempted_unique"], 1)
        self.assertEqual(report["blocked"], "invalid_json_or_unexpected_schema")

    def test_cancel_between_responses_and_explicit_same_stage_resume(self):
        def cancel_wait(seconds):
            return True

        self.run_stage(1, wait=cancel_wait)
        self.assertEqual(len(self.clock.calls), 1)
        self.assertFalse(self.experiment.setting("blocked"))
        self.run_stage(1)
        self.assertEqual(len(self.clock.calls), 5)
        self.assertEqual(len({user for user, _ in self.clock.calls}), 5)

    def test_ctrl_c_inflight_is_unknown_and_not_resent(self):
        def interrupted(username, timeout):
            self.assertEqual(
                self.experiment.db.execute("SELECT state FROM outcomes").fetchone()[0], "InFlight"
            )
            raise KeyboardInterrupt

        result = self.run_stage(1, fetcher=interrupted)
        self.assertEqual(result["blocked"], "interrupted_delivery_uncertain")
        self.assertEqual(result["stages"][1]["completed_responses"], 0)
        with self.assertRaises(ValueError):
            self.run_stage(1)
        record = json.loads(self.experiment.db.execute("SELECT record FROM outcomes").fetchone()[0])
        self.assertTrue(record["delivery_uncertain"])

    def test_reopen_recovers_hard_interruption_without_another_request(self):
        with self.experiment.db:
            self.experiment.db.execute(
                "INSERT INTO outcomes VALUES(1,1,'InFlight',?)",
                (
                    json.dumps(
                        {"username": "OpenAI", "stage": 1, "classification": "Unknown", "reason": "in_flight"}
                    ),
                ),
            )
        reopened = poc.Experiment(self.folder)
        try:
            with self.assertRaises(ValueError):
                reopened.run(1, fetcher=lambda *a: self.fail("Request must not be sent"))
            self.assertEqual(reopened.report()["blocked"], "interrupted_delivery_uncertain")
            self.assertEqual(reopened.report()["stages"][1]["attempted_unique"], 1)
        finally:
            reopened.close()

    def test_raw_evidence_redaction_and_manual_confirmation_separation(self):
        self.run_stage(1, fetcher=lambda *a: response(token="serversecret", msg="mail@example.com"))
        csv_text = (self.folder / "results.csv").read_text()
        self.assertNotIn("serversecret", csv_text)
        self.assertNotIn("mail@example.com", csv_text)
        manual = (self.folder / "manual-confirmation.csv").read_text()
        self.assertEqual(manual.count("NotChecked"), 100)
        report = self.experiment.report()
        self.assertFalse(report["live_accuracy_independently_verified"])

    def test_unexpected_fetch_error_is_durable_and_blocks(self):
        def failed(*args):
            raise RuntimeError("unexpected")

        report = self.run_stage(1, fetcher=failed)
        self.assertTrue(report["blocked"])
        self.assertEqual(report["stages"][1]["attempted_unique"], 1)

    def test_manifest_modification_is_rejected_before_network(self):
        with (self.folder / "manifest.json").open("a") as stream:
            stream.write(" ")
        with self.assertRaises(ValueError):
            self.run_stage(1)
        self.assertFalse(self.clock.calls)

    def test_individual_failures_stop_after_one_request(self):
        samples = [poc.Response(status, b"denied") for status in (401, 403, 404, 429, 500)]
        samples += [
            poc.Response(error="TimeoutError", complete=False),
            response(msg="CAPTCHA required"),
            poc.Response(200, b"malformed", {"content-type": "application/json"}),
            poc.Response(200, response().body, {"content-type": "text/html"}),
        ]
        for index, sample in enumerate(samples):
            with self.subTest(sample=sample):
                folder = Path(self.temporary.name) / f"failure-{index}"
                poc.prepare(folder)
                experiment = poc.Experiment(folder)
                try:
                    calls = []
                    report = experiment.run(
                        1, fetcher=lambda *a, calls=calls, sample=sample: (calls.append(True), sample)[1]
                    )
                    self.assertEqual(len(calls), 1)
                    self.assertTrue(report["blocked"])
                finally:
                    experiment.close()

    def test_non_sensitive_raw_json_keeps_original_whitespace(self):
        raw = b'{\n  "valid": false,\n  "reason": "taken"\n}\n'
        self.run_stage(1, fetcher=lambda *a: poc.Response(200, raw, {"content-type": "application/json"}))
        record = json.loads(self.experiment.db.execute("SELECT record FROM outcomes LIMIT 1").fetchone()[0])
        self.assertEqual(record["raw_response_redacted"], raw.decode())

    def test_p95_nearest_rank_with_different_latencies(self):
        durations = iter((1, 2, 3, 4, 5))

        def fetch(*args):
            self.clock.seconds += next(durations)
            return response(False, "taken")

        data = self.run_stage(1, fetcher=fetch)["stages"][1]
        self.assertEqual(data["mean_latency_seconds"], 3)
        self.assertEqual(data["p95_latency_seconds"], 5)

    def test_pacing_arguments_reject_nonfinite_and_fast_intervals(self):
        for delay in (0, 0.5, 4.9, float("nan"), float("inf")):
            with self.subTest(delay=delay), self.assertRaises(ValueError):
                self.run_stage(1, delay=delay)
        self.assertFalse(self.clock.calls)

    def test_process_lock_prevents_parallel_execution(self):
        with poc.exclusive(self.folder), self.assertRaises(ValueError):
            self.run_stage(1)

    def test_fetch_is_one_get_without_proxy_auth_cookie_or_redirects(self):
        class Stream:
            status = 200
            headers = {"Content-Type": "application/json", "Set-Cookie": "private=secret"}

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def read(self, size):
                self.size = size
                return response().body

        class Opener:
            def open(inner, request, timeout):
                self.assertEqual(request.get_method(), "GET")
                self.assertNotIn("Authorization", dict(request.header_items()))
                self.assertNotIn("Cookie", dict(request.header_items()))
                self.assertIn("username=bad+name", request.full_url)
                return Stream()

        with patch.object(poc.urllib.request, "build_opener", return_value=Opener()) as build:
            result = poc.fetch("bad name", 15)
            self.assertEqual(build.call_count, 1)
            self.assertEqual(build.call_args.args[0].proxies, {})
            self.assertIsInstance(build.call_args.args[1], poc.NoRedirect)
            self.assertNotIn("set-cookie", result.headers)
        self.assertIsNone(poc.NoRedirect().redirect_request(None, None, None, None, None, None))


if __name__ == "__main__":
    unittest.main()
