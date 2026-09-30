import tempfile
import threading
import unittest
from pathlib import Path

from src.batch_service import BatchService
from src.domain import (
    Actor,
    ConflictError,
    NotFoundError,
    PermissionDenied,
)
from src.field_store import FieldStore
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService

TODAY = "2026-09-30"


def entry(
    serial="SN-001",
    method_name="Assay-A",
    method_version="v1",
    outcome="passed",
    due_at="2099-01-01",
    sample_id="S-1",
    value=4.2,
):
    return {
        "instrument_name": "Analyzer",
        "instrument_serial": serial,
        "method_name": method_name,
        "method_version": method_version,
        "outcome": outcome,
        "uncertainty": 0.01,
        "due_at": due_at,
        "sample_id": sample_id,
        "measurement": sample_id,
        "value": value,
        "unit": "mg/L",
        "recorded_at": "2026-09-28",
    }


class BatchTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.repo = SQLiteRepository(base / "central.db")
        self.field = FieldStore(base / "field.db")
        self.rules = RuleEngine(clock=lambda: TODAY)
        self.service = DomainService(self.repo, self.rules)
        self.batches = BatchService(self.repo, self.rules, self.field)
        self.tech = Actor("tech-1", "technician")
        self.reviewer_a = Actor("qa-1", "authorizer")
        self.reviewer_b = Actor("met-1", "metrology")

    def tearDown(self):
        self.tmp.cleanup()

    def register_and_upload(self, batch_no="CAL-2026-09", entries=None, version=1, actor=None):
        actor = actor or self.tech
        self.batches.register_batch(
            actor, batch_no, version, entries or [entry()], site="offsite-lab"
        )
        return self.batches.upload_batch(actor, batch_no)


class HappyPathTest(BatchTestBase):
    def test_register_upload_dual_review_release_and_history(self):
        agg = self.register_and_upload()
        self.assertEqual(agg["status"], "merged")
        self.assertEqual(agg["batch_version"], 1)
        self.assertEqual(agg["entry_count"], 1)
        # Brand-new instrument arrives quarantined; the result waits for review.
        self.assertEqual(agg["entries"][0]["instrument_status"], "quarantined")
        self.assertEqual(agg["entries"][0]["result_status"], "held")

        agg = self.batches.add_review(self.reviewer_a, "CAL-2026-09", "looks good")
        self.assertEqual(agg["status"], "reviewing")
        agg = self.batches.add_review(self.reviewer_b, "CAL-2026-09", "agreed")
        self.assertEqual(agg["status"], "finalized")
        line = agg["entries"][0]
        self.assertEqual(line["instrument_status"], "active")
        self.assertEqual(line["result_status"], "released")
        self.assertFalse(line["held"])

        # Instrument, method scope and result were all updated.
        instrument = self.service.get(line["instrument_id"])
        self.assertEqual(instrument["status"], "active")
        self.assertEqual(instrument["data"]["due_at"], "2099-01-01")
        method = self.service.get(line["method_id"])
        self.assertIn(instrument["id"], method["data"]["instrument_ids"])

        # Old versions are retained and queryable.
        versions = self.service.versions(line["instrument_id"])
        self.assertEqual([item["version"] for item in versions], [1, 2])
        self.assertEqual(versions[0]["status"], "quarantined")
        self.assertEqual(versions[1]["status"], "active")
        result_versions = self.service.versions(line["result_id"])
        statuses = [item["status"] for item in result_versions]
        self.assertEqual(statuses[0], "held")
        self.assertEqual(statuses[-1], "released")


class GateTest(BatchTestBase):
    def test_revoked_method_blocks_release_until_remediated(self):
        # A validated method that was revoked centrally while the lab was offline.
        method = self.service.create(
            Actor("admin", "admin"),
            "method",
            {"name": "Assay-A", "version": "v1"},
        )
        method = self.service.transition(
            Actor("admin", "admin"),
            method["id"],
            "validate_method",
            {"parameters": {"range": [0, 10]}, "instrument_ids": ["other-inst"]},
        )
        self.service.transition(
            Actor("admin", "admin"),
            method["id"],
            "revoke_method",
            {"reason": "superseded"},
        )
        agg = self.register_and_upload(entries=[entry()])
        line = agg["entries"][0]
        self.assertTrue(line["held"])
        self.assertIn("method_revoked", line["reasons"])

        self.batches.add_review(self.reviewer_a, "CAL-2026-09", "pending method fix")
        self.batches.add_review(self.reviewer_b, "CAL-2026-09", "pending method fix")
        after = self.batches.get_batch("CAL-2026-09")
        line = after["entries"][0]
        # Field result is retained, but not released.
        self.assertEqual(line["result_status"], "held")
        self.assertEqual(line["instrument_status"], "active")
        self.assertIn("method_revoked", line["reasons"])

        # Remediate: a new validated version arrives in scope, then recheck.
        new_method = self.service.create(
            Actor("admin", "admin"),
            "method",
            {"name": "Assay-A", "version": "v2"},
        )
        self.service.transition(
            Actor("admin", "admin"),
            new_method["id"],
            "validate_method",
            {"parameters": {"range": [0, 10]}, "instrument_ids": [line["instrument_id"]]},
        )
        # The held result still references the revoked v1; recheck alone cannot
        # guess the replacement version, so it stays held with the same reason.
        checked = self.batches.recheck(self.reviewer_a, "CAL-2026-09")
        self.assertTrue(checked["entries"][0]["held"])
        self.assertIn("method_revoked", checked["entries"][0]["reasons"])

        # Once the result is re-pointed to v2 (data fix), recheck releases it.
        result = self.service.get(line["result_id"])
        data = dict(result["data"])
        data["method_id"] = new_method["id"]
        self.repo.update_entity(result["id"], result["version"], "held", data)
        checked = self.batches.recheck(self.reviewer_a, "CAL-2026-09")
        self.assertEqual(checked["entries"][0]["result_status"], "released")
        self.assertEqual(checked["released_on_recheck"], [1])

    def test_quarantined_instrument_at_upload_is_held(self):
        # Same serial already exists centrally and was quarantined.
        instrument = self.service.create(
            Actor("admin", "admin"),
            "instrument",
            {"name": "Analyzer", "serial": "SN-001", "due_at": "2099-01-01"},
        )
        self.service.transition(
            Actor("admin", "admin"),
            instrument["id"],
            "quarantine",
            {"reason": "suspected drift"},
        )
        method = self.service.create(
            Actor("admin", "admin"),
            "method",
            {"name": "Assay-A", "version": "v1"},
        )
        self.service.transition(
            Actor("admin", "admin"),
            method["id"],
            "validate_method",
            {"parameters": {"range": [0, 10]}, "instrument_ids": [instrument["id"]]},
        )
        agg = self.register_and_upload(entries=[entry()])
        self.assertEqual(agg["entries"][0]["instrument_id"], instrument["id"])
        self.assertIn("instrument_quarantined", agg["entries"][0]["reasons"])

    def test_failed_calibration_keeps_instrument_quarantined(self):
        agg = self.register_and_upload(
            entries=[entry(outcome="failed", due_at="2026-09-01")]
        )
        self.batches.add_review(self.reviewer_a, "CAL-2026-09", "failed")
        agg = self.batches.add_review(self.reviewer_b, "CAL-2026-09", "failed")
        line = agg["entries"][0]
        self.assertEqual(line["instrument_status"], "quarantined")
        self.assertEqual(line["result_status"], "held")
        self.assertIn("calibration_failed", line["reasons"])

    def test_expired_due_at_is_held_and_shown_in_risk_report(self):
        self.register_and_upload(entries=[entry(due_at="2026-09-01")])
        self.batches.add_review(self.reviewer_a, "CAL-2026-09", "x")
        self.batches.add_review(self.reviewer_b, "CAL-2026-09", "x")
        report = self.batches.risk_report()
        self.assertEqual(report["as_of"], TODAY)
        self.assertEqual(len(report["expired"]), 1)
        self.assertEqual(report["expired"][0]["serial"], "SN-001")
        self.assertEqual(len(report["held_results"]), 1)

    def test_due_soon_window(self):
        # 20 days left: not expired, but within the 30-day warning window.
        self.register_and_upload(entries=[entry(due_at="2026-10-20")])
        self.batches.add_review(self.reviewer_a, "CAL-2026-09", "x")
        self.batches.add_review(self.reviewer_b, "CAL-2026-09", "x")
        report = self.batches.risk_report()
        self.assertEqual(report["expired"], [])
        self.assertEqual(len(report["due_soon"]), 1)


class ReviewRulesTest(BatchTestBase):
    def test_uploader_cannot_review(self):
        self.register_and_upload(actor=self.tech)
        with self.assertRaises(PermissionDenied):
            self.batches.add_review(Actor("tech-1", "metrology"), "CAL-2026-09", "self")
        with self.assertRaises(PermissionDenied):
            self.batches.add_review(self.tech, "CAL-2026-09", "self")

    def test_same_reviewer_twice_counts_once(self):
        self.register_and_upload()
        self.batches.add_review(self.reviewer_a, "CAL-2026-09", "first")
        with self.assertRaises(ConflictError):
            self.batches.add_review(self.reviewer_a, "CAL-2026-09", "again")
        # Still only one review; batch not finalized.
        agg = self.batches.get_batch("CAL-2026-09")
        self.assertEqual(agg["status"], "reviewing")
        self.assertEqual(len(agg["reviews"]), 1)

    def test_review_unknown_batch(self):
        with self.assertRaises(NotFoundError):
            self.batches.add_review(self.reviewer_a, "NOPE")


class IdempotencyAndConflictTest(BatchTestBase):
    def test_same_batch_version_submitted_twice_counts_once(self):
        first = self.register_and_upload()
        second = self.batches.merge_batch(
            self.tech, "CAL-2026-09", 1, [entry()]
        )
        self.assertEqual(second["status"], "duplicate")
        self.assertEqual(first["entries"][0]["result_id"],
                         second["entries"][0]["result_id"])
        # Only one calibration / result object per line.
        self.assertEqual(len(self.repo.list_entities(kind="calibration")), 1)
        self.assertEqual(len(self.repo.list_entities(kind="result")), 1)

    def test_older_arriving_version_is_stale_and_not_applied(self):
        self.batches.merge_batch(self.tech, "B1", 2, [entry(sample_id="S-new")])
        outcome = self.batches.merge_batch(self.tech, "B1", 1, [entry(sample_id="S-old")])
        self.assertEqual(outcome["status"], "stale")
        self.assertEqual(outcome["batch_version"], 2)
        self.assertEqual(outcome["entries"][0]["result_status"], "held")
        result = self.service.get(outcome["entries"][0]["result_id"])
        self.assertEqual(result["data"]["sample_id"], "S-new")
        self.assertEqual(len(self.repo.list_entities(kind="result")), 1)

    def test_new_version_supersedes_old_and_resets_reviews(self):
        self.batches.merge_batch(self.tech, "B1", 1, [entry(value=1.0)])
        self.batches.add_review(self.reviewer_a, "B1", "v1 review")
        outcome = self.batches.merge_batch(self.tech, "B1", 2, [entry(value=2.0)])
        self.assertEqual(outcome["status"], "merged")
        self.assertEqual(outcome["batch_version"], 2)
        self.assertEqual(outcome["reviews"], [])
        self.assertEqual(outcome["status_detail"] if "status_detail" in outcome else "reviewing", "reviewing")
        # Old line-1 objects from v1 are still queryable.
        old = self.service.get("result-b1-v1-l1")
        self.assertEqual(old["data"]["value"], 1.0)
        new = self.service.get("result-b1-v2-l1")
        self.assertEqual(new["data"]["value"], 2.0)

    def test_field_rejects_lower_or_equal_redraft(self):
        self.batches.register_batch(self.tech, "B1", 1, [entry()])
        with self.assertRaises(ConflictError):
            self.batches.register_batch(self.tech, "B1", 1, [entry()])

    def test_concurrent_uploads_never_half_apply(self):
        # Two threads upload different versions of the same batch. Exactly one
        # of them wins (highest version under last-writer-wins); both reports
        # must describe the same stored newest state, with no orphan objects.
        self.batches.register_batch(self.tech, "B1", 1, [entry(value=1.0)])
        self.batches.register_batch(self.tech, "B1", 2, [entry(value=2.0)])

        results = []

        def upload(version):
            try:
                results.append(
                    self.batches.merge_batch(self.tech, "B1", version,
                                             [entry(value=float(version))])
                )
            except Exception as exc:  # pragma: no cover - failure path
                results.append(exc)

        threads = [
            threading.Thread(target=upload, args=(2,)),
            threading.Thread(target=upload, args=(1,)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(len(results), 2)
        statuses = {item["status"] for item in results if not isinstance(item, Exception)}
        self.assertTrue(statuses <= {"merged", "superseded", "duplicate", "stale"})
        stored = self.batches.get_batch("B1")
        self.assertEqual(stored["batch_version"], 2)
        # Every response, even one whose packet lost the race, reports newest.
        for item in results:
            if not isinstance(item, Exception):
                self.assertEqual(item["batch_version"], 2)
        # The version-2 packet must report it actually applied; v1 did not.
        by_version = {item["submitted_version"]: item for item in results
                      if not isinstance(item, Exception)}
        self.assertFalse(by_version[1]["applied"])
        self.assertTrue(by_version[2]["applied"])
        # Exactly one v2 set of objects was ever created.
        self.assertEqual(len(self.repo.list_entities(kind="calibration")), 1)
        self.assertEqual(len(self.repo.list_entities(kind="result")), 1)

    def test_failure_inside_merge_rolls_back_everything(self):
        original = self.batches._apply_entry
        calls = {"count": 0}

        def flaky(tx, actor, batch_no, version, line, raw):
            calls["count"] += 1
            if calls["count"] == 2:
                raise RuntimeError("network blew up mid-batch")
            return original(tx, actor, batch_no, version, line, raw)

        self.batches._apply_entry = flaky
        with self.assertRaises(RuntimeError):
            self.batches.merge_batch(
                self.tech,
                "B9",
                1,
                [entry(serial="SN-1"), entry(serial="SN-2")],
            )
        # First entry's writes must have been rolled back too.
        self.assertEqual(self.repo.list_entities(kind="instrument"), [])
        self.assertEqual(self.repo.list_entities(kind="result"), [])
        with self.assertRaises(NotFoundError):
            self.batches.get_batch("B9")
