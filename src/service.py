from uuid import uuid4

from .audit import AuditTrail
from .domain import ConflictError, NotFoundError
from .rules import RuleEngine


class DomainService:
    def __init__(self, repository, rules=None):
        self.repository = repository
        self.rules = rules or RuleEngine()
        self.audit = AuditTrail(repository)

    def _lookup(self, kind, field, value):
        return self.repository.find_entities(self.rules.normalize_kind(kind), field, value)

    def health(self):
        return {"status": "ok" if self.repository.ping() else "error"}

    def create(self, actor, kind, data, idempotency_key=None):
        kind = self.rules.normalize_kind(kind)
        payload = dict(data or {})
        if idempotency_key:
            existing = self.repository.get_idempotency(actor.user_id, idempotency_key)
            if existing:
                entity = self.repository.get_entity(existing)
                if entity:
                    return entity
        if kind == "calibration_batch":
            batch_no = payload.get("batch_no")
            if batch_no:
                duplicates = self._lookup("calibration_batch", "batch_no", batch_no)
                if duplicates:
                    return duplicates[0]
        self.rules.validate_create(actor, kind, payload, self._lookup)
        entity_id = str(payload.pop("id", "") or uuid4())
        if self.repository.get_entity(entity_id):
            raise ConflictError("entity already exists: " + entity_id)
        status = self.rules.initial_status(kind)
        entity = self.repository.create_entity(entity_id, kind, status, payload, actor.user_id)
        self.audit.record(entity_id, actor, "create", None, status, {"kind": kind})
        if idempotency_key:
            self.repository.save_idempotency(actor.user_id, idempotency_key, entity_id)
        return entity

    def transition(self, actor, entity_id, action, data=None, expected_version=None):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        expected = int(expected_version) if expected_version is not None else entity["version"]
        next_status, patch = self.rules.validate_transition(
            actor, entity, action, dict(data or {}), self._lookup
        )
        merged = dict(entity["data"])
        merged.update(patch)
        updated = self.repository.update_entity(entity_id, expected, next_status, merged)
        self.audit.record(
            entity_id,
            actor,
            action,
            entity["status"],
            updated["status"],
            {"patch": patch},
        )
        return updated

    def sync_batch(self, actor, batch_id, data=None, expected_version=None):
        """网络恢复后整批回传。同一批次重复回传只算一次，不重复应用。"""
        batch = self.repository.get_entity(batch_id)
        if not batch:
            raise NotFoundError("batch not found: " + batch_id)
        if batch["status"] != "registered":
            return batch
        return self.transition(actor, batch_id, "sync", data, expected_version)

    def review_batch(self, actor, batch_id, data=None, expected_version=None):
        """两名人员分别复核；满两名不同人员批准后，在同一事务里放行。"""
        batch = self.repository.get_entity(batch_id)
        if not batch:
            raise NotFoundError("batch not found: " + batch_id)
        expected = int(expected_version) if expected_version is not None else batch["version"]
        next_status, patch = self.rules.validate_transition(
            actor, batch, "review", dict(data or {}), self._lookup
        )
        if next_status == "released":
            return self._apply_batch_release(batch, patch, expected, actor)
        merged = dict(batch["data"])
        merged.update(patch)
        updated = self.repository.update_entity(batch_id, expected, next_status, merged)
        self.audit.record(
            batch_id, actor, "review", batch["status"], updated["status"], {"patch": patch}
        )
        return updated

    def _apply_batch_release(self, batch, patch, expected, actor):
        data = batch["data"]
        instrument_id = data["instrument_id"]
        method_id = data["method_id"]
        passed = data.get("result") == "passed"

        instrument = self.repository.get_entity(instrument_id)
        method = self.repository.get_entity(method_id)

        instrument_next = "active" if passed else "quarantined"
        instrument_patch = {
            "due_at": data.get("due_at"),
            "last_batch_no": data.get("batch_no"),
            "last_calibration_result": data.get("result"),
        }
        if not passed:
            instrument_patch["quarantine_reason"] = "failed calibration batch %s" % data.get("batch_no")

        scope = list(method["data"].get("instrument_ids", []))
        if instrument_id not in scope:
            scope.append(instrument_id)
        method_patch = {
            "instrument_ids": scope,
            "scope": scope,
            "last_batch_no": data.get("batch_no"),
        }

        result_id = str(uuid4())
        approvers = [
            item["user_id"] for item in patch.get("reviewers", []) if item.get("decision") == "approve"
        ]
        result_data = {
            "batch_no": data.get("batch_no"),
            "instrument_id": instrument_id,
            "method_id": method_id,
            "result": data.get("result"),
            "uncertainty": data.get("uncertainty"),
            "due_at": data.get("due_at"),
            "value": data.get("uncertainty"),
            "unit": data.get("unit", ""),
            "released_by": actor.user_id,
            "reviewed_by": approvers,
        }

        self.repository.apply_batch_release(
            batch_id=batch["id"],
            expected_version=expected,
            batch_patch=patch,
            instrument_id=instrument_id,
            instrument_next_status=instrument_next,
            instrument_patch=instrument_patch,
            method_id=method_id,
            method_next_status=method["status"],
            method_patch=method_patch,
            result_id=result_id,
            result_data=result_data,
            actor_id=actor.user_id,
            actor_role=actor.role,
        )
        return self.repository.get_entity(batch["id"])

    def get(self, entity_id):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        return entity

    def list(self, kind=None, status=None):
        if kind:
            kind = self.rules.normalize_kind(kind)
        return self.repository.list_entities(kind=kind, status=status)

    def audit_log(self, entity_id=None):
        return self.repository.list_audit(entity_id=entity_id)
