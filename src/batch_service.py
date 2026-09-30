"""Calibration batch use cases.

Flow:
  1. The offsite lab registers a batch locally (FieldStore) while offline.
  2. Once the network is back, the batch is uploaded; every object touched by
     the batch (instrument / method / calibration / result) is merged inside a
     single central transaction, so a batch can never be half applied.
  3. Two different reviewers (neither of whom uploaded the batch) sign off; on
     the second review the instrument state, method applicability and the
     pending results are updated together. Old rows are versioned and stay
     queryable.

Concurrent uploads are serialised with BEGIN IMMEDIATE. A repeated submission
of the same batch version is counted once; an older arriving version is
reported as stale; the highest version wins and every response reflects the
newest stored state.
"""

import re
from datetime import date, datetime, timedelta

from .domain import (
    ConflictError,
    NotFoundError,
    PermissionDenied,
    ValidationError,
)
from .rules import (
    BATCH_REVIEW_COUNT,
    BATCH_REVIEW_ROLES,
    gate_text,
    release_gate_reasons,
)

FIELD_ROLES = ("admin", "technician", "metrology")
RISK_WINDOW_DAYS = 30


def _slug(value):
    text = re.sub(r"[^0-9A-Za-z]+", "-", str(value)).strip("-").lower()
    return text or "x"


def _batch_key(batch_no):
    """Batch numbers are normalised so CAL-09 and cal-09 are the same batch."""
    key = _slug(batch_no)
    if not re.match(r"^[0-9a-z][0-9a-z-]*$", key):
        raise ValidationError("invalid batch_no: " + str(batch_no))
    return key


class BatchService:
    def __init__(self, repository, rules, field_store):
        self.repository = repository
        self.rules = rules
        self.field_store = field_store

    # ----- offline side -----------------------------------------------------

    def register_batch(self, actor, batch_no, batch_version, entries, site=None):
        self._ensure_role(actor, FIELD_ROLES)
        return self.field_store.register(
            _batch_key(batch_no), batch_version, entries, actor, site=site
        )

    def list_field_batches(self, include_uploaded=True):
        return self.field_store.list_batches(include_uploaded=include_uploaded)

    # ----- upload / merge ----------------------------------------------------

    def upload_batch(self, actor, batch_no):
        """Pull one registered offline batch into the central database."""
        self._ensure_role(actor, FIELD_ROLES)
        batch_no = _batch_key(batch_no)
        draft = self.field_store.get_batch(batch_no)
        if not draft:
            raise NotFoundError("field batch not found: " + batch_no)
        outcome = self.merge_batch(
            actor,
            draft["batch_no"],
            draft["batch_version"],
            draft["entries"],
            site=draft.get("site"),
        )
        if outcome["status"] != "stale":
            self.field_store.mark_uploaded(draft["batch_no"], draft["batch_version"])
        return outcome

    def merge_batch(self, actor, batch_no, batch_version, entries, site=None):
        """Merge one batch version into the central database atomically."""
        self._ensure_role(actor, FIELD_ROLES)
        batch_no = _batch_key(batch_no)
        version = int(batch_version)
        if version < 1:
            raise ValidationError("batch_version must be >= 1")
        if not entries:
            raise ValidationError("batch must contain at least one entry")

        outcome_status = self._merge_once(actor, batch_no, version, entries, site)
        # The transaction may have committed before a concurrent higher version
        # landed. Always answer from the newest committed state so callers of a
        # late-arriving version get "last writer wins" in the payload too.
        aggregate = self.get_batch(batch_no)
        aggregate["submitted_version"] = version
        aggregate["applied"] = aggregate["batch_version"] == version
        if outcome_status == "merged" and not aggregate["applied"]:
            # A higher version overtook this packet between its commit and the
            # response; the payload below already describes the newest state.
            outcome_status = "superseded"
        aggregate["status"] = outcome_status
        return aggregate

    def _merge_once(self, actor, batch_no, version, entries, site):
        with self.repository.transaction() as tx:
            existing = tx.get_batch(batch_no)
            if existing and version < existing["batch_version"]:
                # An older packet arrives late: nothing is applied.
                return "stale"
            if existing and version == existing["batch_version"]:
                # Same batch number + version submitted again: count once.
                return "duplicate"

            for line, raw in enumerate(entries, start=1):
                self._apply_entry(tx, actor, batch_no, version, line, raw)
            uploaded_at = tx.upsert_batch(
                batch_no,
                version,
                "reviewing",
                site,
                {"entries": entries, "site": site},
                actor.user_id,
            )
            tx.append_audit(
                batch_no,
                actor.user_id,
                actor.role,
                "batch_merge",
                None,
                "reviewing",
                {
                    "batch_version": version,
                    "superseded": existing["batch_version"] if existing else None,
                    "entries": len(entries),
                    "uploaded_at": uploaded_at,
                },
            )
            return "merged"

    def _apply_entry(self, tx, actor, batch_no, version, line, raw):
        from .field_store import validate_entry

        entry = validate_entry(raw)
        as_of = self.rules.today

        instrument = tx.find_entity("instrument", "serial", entry["instrument_serial"])
        if instrument:
            instrument_id = instrument["id"]
        else:
            instrument_id = "inst-" + _slug(entry["instrument_serial"])
            instrument = tx.insert_entity(
                instrument_id,
                "instrument",
                "quarantined",
                {
                    "name": entry["instrument_name"],
                    "serial": entry["instrument_serial"],
                    "due_at": entry["due_at"] if entry["outcome"] == "passed" else None,
                    "source": "field_batch",
                },
                actor.user_id,
            )
            tx.append_audit(
                instrument_id, actor.user_id, actor.role,
                "create", None, "quarantined",
                {"kind": "instrument", "batch_no": batch_no, "batch_version": version},
            )

        method = self._find_method(
            tx, entry["method_name"], entry["method_version"]
        )
        if method:
            method_id = method["id"]
        else:
            # The field paperwork names a method version the central system
            # does not know: keep it as a draft record. It is never released
            # through the batch gate.
            method_id = "method-%s-%s" % (
                _slug(entry["method_name"]),
                _slug(entry["method_version"]),
            )
            method = tx.insert_entity(
                method_id,
                "method",
                "draft",
                {
                    "name": entry["method_name"],
                    "version": entry["method_version"],
                    "parameters": {},
                    "instrument_ids": [],
                    "source": "field_batch",
                },
                actor.user_id,
            )
            tx.append_audit(
                method_id, actor.user_id, actor.role,
                "create", None, "draft",
                {"kind": "method", "batch_no": batch_no, "batch_version": version},
            )

        calibration_id = "calib-%s-v%s-l%s" % (_slug(batch_no), version, line)
        calibration = tx.insert_entity(
            calibration_id,
            "calibration",
            "passed" if entry["outcome"] == "passed" else "failed",
            {
                "instrument_id": instrument_id,
                "requested_at": entry["recorded_at"],
                "performed_at": entry["recorded_at"],
                "result": entry["outcome"],
                "uncertainty": entry["uncertainty"],
                "due_at": entry["due_at"],
                "batch_no": batch_no,
                "batch_version": version,
                "line": line,
                "site": entry.get("site"),
                "source": "field_batch",
            },
            actor.user_id,
        )
        tx.append_audit(
            calibration_id, actor.user_id, actor.role,
            "create", None, calibration["status"],
            {"kind": "calibration", "batch_no": batch_no, "batch_version": version, "line": line},
        )

        reasons = list(release_gate_reasons(instrument, method, as_of))
        if entry["outcome"] == "failed":
            reasons.append("calibration_failed")
        result_id = "result-%s-v%s-l%s" % (_slug(batch_no), version, line)
        result = tx.insert_entity(
            result_id,
            "result",
            "held" if reasons else "pending",
            {
                "sample_id": entry["sample_id"],
                "measurement": entry.get("measurement", entry["sample_id"]),
                "value": entry["value"],
                "unit": entry["unit"],
                "instrument_id": instrument_id,
                "method_id": method_id,
                "uncertainty": entry["uncertainty"],
                "due_at": entry["due_at"],
                "recorded_at": entry["recorded_at"],
                "batch_no": batch_no,
                "batch_version": version,
                "line": line,
                "held_reasons": reasons,
                "field_data": entry,
                "source": "field_batch",
            },
            actor.user_id,
        )
        tx.append_audit(
            result_id, actor.user_id, actor.role,
            "create", None, result["status"],
            {"kind": "result", "reasons": reasons, "batch_no": batch_no,
             "batch_version": version, "line": line},
        )

        tx.replace_entry(
            {
                "batch_no": batch_no,
                "batch_version": version,
                "line": line,
                "instrument_id": instrument_id,
                "method_id": method_id,
                "calibration_id": calibration_id,
                "result_id": result_id,
                "outcome": entry["outcome"],
                "held": bool(reasons),
                "reasons": reasons,
            }
        )
        return tx.list_entries(batch_no, version)[-1]

    @staticmethod
    def _find_method(tx, name, method_version):
        for method in tx.list_entities(kind="method"):
            if method["data"].get("name") == name and method["data"].get("version") == method_version:
                return method
        return None

    # ----- dual review -------------------------------------------------------

    def add_review(self, actor, batch_no, note=""):
        self._ensure_role(actor, BATCH_REVIEW_ROLES)
        batch_no = _batch_key(batch_no)
        with self.repository.transaction() as tx:
            batch = tx.get_batch(batch_no)
            if not batch:
                raise NotFoundError("batch not found: " + batch_no)
            if batch["status"] == "finalized":
                raise ConflictError("batch already finalized: " + batch_no)
            if actor.user_id == batch["uploaded_by"]:
                raise PermissionDenied("the uploader cannot review their own batch")
            reviews = tx.list_reviews(batch_no, batch["batch_version"])
            if any(item["reviewer_id"] == actor.user_id for item in reviews):
                raise ConflictError("reviewer already signed this batch version")
            tx.add_review(
                batch_no, batch["batch_version"], actor.user_id, actor.role, note
            )
            tx.append_audit(
                batch_no, actor.user_id, actor.role,
                "batch_review", "reviewing", "reviewing",
                {"batch_version": batch["batch_version"], "note": note or ""},
            )
            reviews = tx.list_reviews(batch_no, batch["batch_version"])
            finalized = len(reviews) >= BATCH_REVIEW_COUNT
            if finalized:
                self._finalize(tx, actor, batch)
            aggregate = self._aggregate(tx, tx.get_batch(batch_no))
            aggregate["status"] = "finalized" if finalized else "reviewing"
            return aggregate

    def _finalize(self, tx, actor, batch):
        """Second review landed: update instrument/method/results together."""
        batch_no = batch["batch_no"]
        version = batch["batch_version"]
        as_of = self.rules.today
        for entry in tx.list_entries(batch_no, version):
            instrument = tx.get_entity(entry["instrument_id"])
            method = tx.get_entity(entry["method_id"])
            calibration = tx.get_entity(entry["calibration_id"])

            # 1) instrument state follows the returned calibration outcome.
            instrument_before = instrument["status"]
            instrument_data = dict(instrument["data"])
            instrument_data["due_at"] = calibration["data"].get("due_at")
            if entry["outcome"] == "passed":
                instrument_data.pop("quarantine_reason", None)
                instrument = tx.update_entity(
                    instrument["id"], instrument["version"], "active", instrument_data
                )
            else:
                instrument_data["quarantine_reason"] = "field calibration failed"
                instrument = tx.update_entity(
                    instrument["id"], instrument["version"], "quarantined", instrument_data
                )
            if instrument_before != instrument["status"]:
                tx.append_audit(
                    instrument["id"], actor.user_id, actor.role,
                    "batch_finalize", instrument_before, instrument["status"],
                    {"batch_no": batch_no, "batch_version": version, "line": entry["line"]},
                )

            # 2) method applicability. The dual review validates the method
            #    version the lab brought back: a draft method registered by the
            #    field is promoted to validated and scoped to the instrument.
            #    Centrally draft/revoked methods are never promoted by a batch.
            method_before = method["status"]
            method_data = dict(method["data"])
            scope = list(method_data.get("instrument_ids", []))
            if instrument["id"] not in scope:
                scope.append(instrument["id"])
                method_data["instrument_ids"] = scope
            if method["status"] == "validated":
                if method_data != method["data"]:
                    method = tx.update_entity(
                        method["id"], method["version"], "validated", method_data
                    )
                    tx.append_audit(
                        method["id"], actor.user_id, actor.role,
                        "batch_scope_extend", "validated", "validated",
                        {"instrument_id": instrument["id"], "batch_no": batch_no,
                         "batch_version": version, "line": entry["line"]},
                    )
            elif method["status"] == "draft" and method["data"].get("source") == "field_batch":
                method_data["parameters"] = method_data.get("parameters") or {
                    "uncertainty": calibration["data"].get("uncertainty")
                }
                method = tx.update_entity(
                    method["id"], method["version"], "validated", method_data
                )
                tx.append_audit(
                    method["id"], actor.user_id, actor.role,
                    "validate_method", "draft", "validated",
                    {"via": "batch_finalize", "instrument_id": instrument["id"],
                     "batch_no": batch_no, "batch_version": version, "line": entry["line"]},
                )

            # 3) release only if the cross-object gate now passes.
            result = tx.get_entity(entry["result_id"])
            reasons = list(release_gate_reasons(instrument, method, as_of))
            if entry["outcome"] == "failed":
                reasons.append("calibration_failed")
            result_before = result["status"]
            result_data = dict(result["data"])
            result_data["held_reasons"] = reasons
            if not reasons and result_before in ("pending", "held"):
                result_data["released_by"] = actor.user_id
                result_data["released_at"] = self.rules.today
                result = tx.update_entity(
                    result["id"], result["version"], "released", result_data
                )
                tx.append_audit(
                    result["id"], actor.user_id, actor.role,
                    "release", result_before, "released",
                    {"batch_no": batch_no, "batch_version": version, "line": entry["line"]},
                )
            else:
                result = tx.update_entity(
                    result["id"], result["version"], "held", result_data
                )
                if result_before != "held":
                    tx.append_audit(
                        result["id"], actor.user_id, actor.role,
                        "hold", result_before, "held",
                        {"reasons": reasons, "batch_no": batch_no,
                         "batch_version": version, "line": entry["line"]},
                    )
            entry["held"] = bool(reasons)
            entry["reasons"] = reasons
            tx.replace_entry(entry)

        finalized_at = self.rules.today
        tx.mark_batch_finalized(batch_no, finalized_at)
        tx.append_audit(
            batch_no, actor.user_id, actor.role,
            "batch_finalize", "reviewing", "finalized",
            {"batch_version": version, "finalized_at": finalized_at},
        )

    def recheck(self, actor, batch_no):
        """Re-evaluate held results after the blocking issue was remediated."""
        self._ensure_role(actor, BATCH_REVIEW_ROLES)
        batch_no = _batch_key(batch_no)
        with self.repository.transaction() as tx:
            batch = tx.get_batch(batch_no)
            if not batch:
                raise NotFoundError("batch not found: " + batch_no)
            if batch["status"] != "finalized":
                raise ConflictError("batch is not finalized yet: " + batch_no)
            as_of = self.rules.today
            changed = []
            for entry in tx.list_entries(batch_no, batch["batch_version"]):
                if not entry["held"]:
                    continue
                result = tx.get_entity(entry["result_id"])
                # A remediation may have re-pointed the result (e.g. to a new
                # validated method version); evaluate the current references.
                instrument = tx.get_entity(
                    result["data"].get("instrument_id", entry["instrument_id"])
                )
                method = tx.get_entity(
                    result["data"].get("method_id", entry["method_id"])
                )
                reasons = list(release_gate_reasons(instrument, method, as_of))
                if entry["outcome"] == "failed":
                    reasons.append("calibration_failed")
                result_data = dict(result["data"])
                result_data["held_reasons"] = reasons
                if not reasons and result["status"] == "held":
                    result_data["released_by"] = actor.user_id
                    result_data["released_at"] = as_of
                    tx.update_entity(result["id"], result["version"], "released", result_data)
                    tx.append_audit(
                        result["id"], actor.user_id, actor.role,
                        "release", "held", "released",
                        {"batch_no": batch_no, "recheck": True, "line": entry["line"]},
                    )
                    entry["held"] = False
                    entry["reasons"] = []
                    entry["method_id"] = method["id"]
                    tx.replace_entry(entry)
                    changed.append(entry["line"])
                elif reasons != entry["reasons"]:
                    entry["reasons"] = reasons
                    tx.replace_entry(entry)
            tx.append_audit(
                batch_no, actor.user_id, actor.role,
                "batch_recheck", "finalized", "finalized",
                {"released_lines": changed},
            )
            aggregate = self._aggregate(tx, batch)
            aggregate["status"] = "finalized"
            aggregate["released_on_recheck"] = changed
            return aggregate

    # ----- reads --------------------------------------------------------------

    def get_batch(self, batch_no):
        batch_no = _batch_key(batch_no)
        with self.repository.transaction() as tx:
            batch = tx.get_batch(batch_no)
            if not batch:
                raise NotFoundError("batch not found: " + batch_no)
            return self._aggregate(tx, batch)

    def list_batches(self):
        with self.repository.transaction() as tx:
            return [self._aggregate(tx, batch) for batch in tx.list_batches()]

    def _aggregate(self, tx, batch):
        entries = tx.list_entries(batch["batch_no"], batch["batch_version"])
        reviews = tx.list_reviews(batch["batch_no"], batch["batch_version"])
        items = []
        for entry in entries:
            instrument = tx.get_entity(entry["instrument_id"])
            method = tx.get_entity(entry["method_id"])
            result = tx.get_entity(entry["result_id"])
            calibration = tx.get_entity(entry["calibration_id"])
            items.append(
                {
                    "line": entry["line"],
                    "outcome": entry["outcome"],
                    "held": entry["held"],
                    "reasons": entry["reasons"],
                    "reason_text": gate_text(entry["reasons"]),
                    "instrument_id": entry["instrument_id"],
                    "instrument_status": instrument["status"] if instrument else None,
                    "method_id": entry["method_id"],
                    "method_status": method["status"] if method else None,
                    "calibration_id": entry["calibration_id"],
                    "calibration_status": calibration["status"] if calibration else None,
                    "result_id": entry["result_id"],
                    "result_status": result["status"] if result else None,
                    "due_at": result["data"].get("due_at") if result else None,
                }
            )
        held = [item["line"] for item in items if item["held"]]
        return {
            "batch_no": batch["batch_no"],
            "batch_version": batch["batch_version"],
            "status": batch["status"],
            "site": batch.get("site"),
            "uploaded_by": batch["uploaded_by"],
            "uploaded_at": batch["uploaded_at"],
            "finalized_at": batch.get("finalized_at"),
            "reviews": reviews,
            "reviews_required": BATCH_REVIEW_COUNT,
            "entry_count": len(items),
            "held_lines": held,
            "entries": items,
        }

    # ----- risk ---------------------------------------------------------------

    def risk_report(self, window_days=RISK_WINDOW_DAYS):
        """Expiry/quarantine visibility across all instruments.

        Without this, instruments that went offline are invisible to the
        release desk until their calibration silently expires.
        """
        as_of = self.rules.today
        today = date.fromisoformat(as_of)
        horizon = today + timedelta(days=int(window_days))
        instruments = self.repository.list_entities(kind="instrument")
        expired, due_soon, quarantined = [], [], []
        for instrument in instruments:
            due_raw = instrument["data"].get("due_at")
            item = {
                "instrument_id": instrument["id"],
                "name": instrument["data"].get("name"),
                "serial": instrument["data"].get("serial"),
                "status": instrument["status"],
                "due_at": due_raw,
            }
            if instrument["status"] == "quarantined":
                quarantined.append(item)
            if due_raw:
                try:
                    due = datetime.fromisoformat(str(due_raw)[:10]).date()
                except ValueError:
                    continue
                if due < today:
                    expired.append(item)
                elif due <= horizon:
                    due_soon.append(item)
        held_results = [
            {
                "result_id": result["id"],
                "sample_id": result["data"].get("sample_id"),
                "reasons": result["data"].get("held_reasons", []),
                "batch_no": result["data"].get("batch_no"),
            }
            for result in self.repository.list_entities(kind="result", status="held")
        ]
        return {
            "as_of": as_of,
            "window_days": int(window_days),
            "expired": expired,
            "due_soon": due_soon,
            "quarantined": quarantined,
            "held_results": held_results,
        }

    @staticmethod
    def _ensure_role(actor, allowed):
        if "*" not in allowed and actor.role not in allowed:
            raise PermissionDenied("role %s is not allowed here" % actor.role)
