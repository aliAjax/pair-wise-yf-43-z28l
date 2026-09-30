import tempfile
import threading
import unittest
from pathlib import Path

from src.domain import Actor, ConflictError, InvalidTransition
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class CalibrationBatchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.admin = Actor("admin", "admin")
        self.tech = Actor("tech", "technician")
        self.r1 = Actor("r1", "authorizer")
        self.r2 = Actor("r2", "metrology")

    def tearDown(self):
        self.tmp.cleanup()

    def _setup_instrument_method(self, method_status="validated", instrument_status="active"):
        inst = self.service.create(
            self.admin, "instrument", {"name": "Analyzer", "serial": "A-1"}
        )
        method = self.service.create(
            self.admin, "method", {"name": "Assay", "version": "v1"}
        )
        self.service.transition(
            self.admin,
            method["id"],
            "validate_method",
            {"parameters": {"range": [0, 10]}, "instrument_ids": [inst["id"]]},
        )
        if method_status == "revoked":
            self.service.transition(
                self.admin, method["id"], "revoke_method", {"reason": "superseded"}
            )
        if instrument_status == "quarantined":
            self.service.transition(
                self.admin, inst["id"], "quarantine", {"reason": "suspect drift"}
            )
        return inst, method

    def _register(self, inst, method, batch_no="B-001", result="passed", due_at="2099-01-01"):
        return self.service.create(
            self.tech,
            "calibration_batch",
            {
                "batch_no": batch_no,
                "instrument_id": inst["id"],
                "method_id": method["id"],
                "result": result,
                "uncertainty": 0.01,
                "due_at": due_at,
            },
        )

    def test_field_registration_records_all_fields(self):
        inst, method = self._setup_instrument_method()
        batch = self._register(inst, method)
        self.assertEqual(batch["kind"], "calibration_batch")
        self.assertEqual(batch["status"], "registered")
        data = batch["data"]
        self.assertEqual(data["batch_no"], "B-001")
        self.assertEqual(data["instrument_id"], inst["id"])
        self.assertEqual(data["method_id"], method["id"])
        self.assertEqual(data["result"], "passed")
        self.assertEqual(data["uncertainty"], 0.01)
        self.assertEqual(data["due_at"], "2099-01-01")

    def test_same_batch_number_submitted_once(self):
        inst, method = self._setup_instrument_method()
        first = self._register(inst, method, batch_no="B-DUP")
        second = self._register(inst, method, batch_no="B-DUP")
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(len(self.service.list("calibration_batch")), 1)

    def test_resync_is_idempotent(self):
        inst, method = self._setup_instrument_method()
        batch = self._register(inst, method)
        synced = self.service.sync_batch(self.tech, batch["id"])
        self.assertEqual(synced["status"], "synced")
        again = self.service.sync_batch(self.tech, batch["id"])
        self.assertEqual(again["status"], "synced")
        self.assertEqual(again["version"], synced["version"])

    def test_sync_with_revoked_method_holds_and_keeps_field_results(self):
        inst, method = self._setup_instrument_method(method_status="revoked")
        batch = self._register(inst, method)
        held = self.service.sync_batch(self.tech, batch["id"])
        self.assertEqual(held["status"], "held")
        self.assertIn("revoked", held["data"]["hold_reason"])
        # 现场结果保留
        self.assertEqual(held["data"]["result"], "passed")
        self.assertEqual(held["data"]["uncertainty"], 0.01)
        self.assertEqual(held["data"]["due_at"], "2099-01-01")
        # 没有直接放行：仪器状态、方法、结果均未被动
        instrument = self.service.get(inst["id"])
        self.assertEqual(instrument["status"], "active")
        self.assertNotIn("last_batch_no", instrument["data"])
        results = self.service.list("result")
        self.assertEqual(results, [])

    def test_sync_with_quarantined_instrument_holds(self):
        inst, method = self._setup_instrument_method(instrument_status="quarantined")
        batch = self._register(inst, method)
        held = self.service.sync_batch(self.tech, batch["id"])
        self.assertEqual(held["status"], "held")
        self.assertIn("quarantined", held["data"]["hold_reason"])
        results = self.service.list("result")
        self.assertEqual(results, [])

    def test_two_person_review_releases_together(self):
        inst, method = self._setup_instrument_method()
        batch = self._register(inst, method)
        self.service.sync_batch(self.tech, batch["id"])

        first = self.service.review_batch(self.r1, batch["id"], {"decision": "approve"})
        self.assertEqual(first["status"], "synced")
        # 只有一名复核人，尚未放行
        self.assertEqual(self.service.list("result"), [])
        instrument = self.service.get(inst["id"])
        self.assertEqual(instrument["status"], "active")
        self.assertNotIn("last_batch_no", instrument["data"])

        released = self.service.review_batch(self.r2, batch["id"], {"decision": "approve"})
        self.assertEqual(released["status"], "released")

        # 仪器状态更新
        instrument = self.service.get(inst["id"])
        self.assertEqual(instrument["status"], "active")
        self.assertEqual(instrument["data"]["due_at"], "2099-01-01")
        self.assertEqual(instrument["data"]["last_batch_no"], "B-001")
        # 方法适用范围更新
        method = self.service.get(method["id"])
        self.assertIn(inst["id"], method["data"]["instrument_ids"])
        self.assertIn(inst["id"], method["data"]["scope"])
        # 待放行结果已更新为已放行
        results = self.service.list("result")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["status"], "released")
        self.assertEqual(results[0]["data"]["batch_no"], "B-001")
        self.assertEqual(results[0]["data"]["reviewed_by"], ["r1", "r2"])

    def test_same_reviewer_twice_does_not_release(self):
        inst, method = self._setup_instrument_method()
        batch = self._register(inst, method)
        self.service.sync_batch(self.tech, batch["id"])
        self.service.review_batch(self.r1, batch["id"], {"decision": "approve"})
        again = self.service.review_batch(self.r1, batch["id"], {"decision": "approve"})
        self.assertEqual(again["status"], "synced")
        self.assertEqual(self.service.list("result"), [])

    def test_failed_batch_quarantines_instrument_and_releases_result(self):
        inst, method = self._setup_instrument_method()
        batch = self._register(inst, method, batch_no="B-FAIL", result="failed", due_at=None)
        self.service.sync_batch(self.tech, batch["id"])
        self.service.review_batch(self.r1, batch["id"], {"decision": "approve"})
        released = self.service.review_batch(self.r2, batch["id"], {"decision": "approve"})
        self.assertEqual(released["status"], "released")
        instrument = self.service.get(inst["id"])
        self.assertEqual(instrument["status"], "quarantined")
        results = self.service.list("result")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["data"]["result"], "failed")

    def test_old_results_remain_queryable(self):
        inst, method = self._setup_instrument_method()
        for batch_no in ("B-OLD", "B-NEW"):
            batch = self._register(inst, method, batch_no=batch_no)
            self.service.sync_batch(self.tech, batch["id"])
            self.service.review_batch(self.r1, batch["id"], {"decision": "approve"})
            self.service.review_batch(self.r2, batch["id"], {"decision": "approve"})
        results = self.service.list("result")
        self.assertEqual(len(results), 2)
        self.assertEqual({r["data"]["batch_no"] for r in results}, {"B-OLD", "B-NEW"})

    def test_review_version_conflict_rolls_back(self):
        inst, method = self._setup_instrument_method()
        batch = self._register(inst, method)
        self.service.sync_batch(self.tech, batch["id"])
        self.service.review_batch(self.r1, batch["id"], {"decision": "approve"})
        with self.assertRaises(ConflictError):
            self.service.review_batch(
                self.r2, batch["id"], {"decision": "approve"}, expected_version=999
            )
        # 未放行：没有一半成功
        self.assertEqual(self.service.list("result"), [])
        current = self.service.get(batch["id"])
        self.assertEqual(current["status"], "synced")

    def test_cannot_review_released_batch_again(self):
        inst, method = self._setup_instrument_method()
        batch = self._register(inst, method)
        self.service.sync_batch(self.tech, batch["id"])
        self.service.review_batch(self.r1, batch["id"], {"decision": "approve"})
        self.service.review_batch(self.r2, batch["id"], {"decision": "approve"})
        with self.assertRaises(InvalidTransition):
            self.service.review_batch(self.r2, batch["id"], {"decision": "approve"})

    def test_concurrent_release_applies_exactly_once(self):
        inst, method = self._setup_instrument_method()
        batch = self._register(inst, method)
        self.service.sync_batch(self.tech, batch["id"])
        self.service.review_batch(self.r1, batch["id"], {"decision": "approve"})

        failures = []

        def release(actor):
            try:
                self.service.review_batch(actor, batch["id"], {"decision": "approve"})
            except Exception as exc:  # noqa: BLE001 - 并发下的失败都应被记录
                failures.append(type(exc).__name__)

        t1 = threading.Thread(target=release, args=(Actor("r2", "metrology"),))
        t2 = threading.Thread(target=release, args=(Actor("r3", "authorizer"),))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        # 恰好一个放行成功，另一个因版本冲突/状态失效回滚，不能各自成功一半
        self.assertEqual(len(failures), 1)
        results = self.service.list("result")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["status"], "released")
        current = self.service.get(batch["id"])
        self.assertEqual(current["status"], "released")
        instrument = self.service.get(inst["id"])
        self.assertEqual(instrument["status"], "active")
        method = self.service.get(method["id"])
        self.assertIn(inst["id"], method["data"]["instrument_ids"])

    def test_concurrent_sync_one_winner(self):
        inst, method = self._setup_instrument_method()
        batch = self._register(inst, method)
        failures = []

        def sync():
            try:
                self.service.sync_batch(self.tech, batch["id"])
            except Exception as exc:  # noqa: BLE001
                failures.append(type(exc).__name__)

        t1 = threading.Thread(target=sync)
        t2 = threading.Thread(target=sync)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        # 批次只有一份、状态一致、版本只前进一次；不重复应用、不产生半成品
        self.assertEqual(len(self.service.list("calibration_batch")), 1)
        current = self.service.get(batch["id"])
        self.assertEqual(current["status"], "synced")
        self.assertEqual(current["version"], 2)
        self.assertIn(len(failures), (0, 1))


if __name__ == "__main__":
    unittest.main()
