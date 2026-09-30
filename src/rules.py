from datetime import datetime, timedelta, timezone

from .domain import (
    ConflictError,
    InvalidTransition,
    PermissionDenied,
    ValidationError,
)


def _now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _validate_calibration(actor, data, lookup):
    instrument = _find_one(lookup, "instrument", "id", data.get("instrument_id"))
    if not instrument:
        raise ValidationError("instrument does not exist")


def _validate_perform(actor, entity, data, lookup):
    if data.get("result") not in ("passed", "failed"):
        raise ValidationError("calibration result must be passed or failed")
    if data.get("result") == "passed" and not data.get("due_at"):
        raise ValidationError("passed calibration requires due_at")


def calibration_current(due_at, as_of):
    return str(due_at) >= str(as_of)


def _validate_result_release(actor, entity, data, lookup):
    instrument = _find_one(lookup, "instrument", "id", data.get("instrument_id"))
    method = _find_one(lookup, "method", "id", data.get("method_id"))
    if not instrument or instrument["status"] != "active":
        raise ValidationError("result requires an active instrument")
    if not calibration_current(instrument["data"].get("due_at", ""), "2026-09-24"):
        raise ValidationError("instrument calibration is not current")
    if not method or method["status"] != "validated":
        raise ValidationError("result requires a validated method")
    if data.get("instrument_id") not in method["data"].get("instrument_ids", []):
        raise ValidationError("method is not validated for this instrument")
    return {"released_by": actor.user_id}


def _validate_batch_create(actor, data, lookup):
    """现场登记：记录仪器、方法版本、通过/失败、不确定度和到期日。"""
    if data.get("result") not in ("passed", "failed"):
        raise ValidationError("batch result must be passed or failed")
    if data.get("result") == "passed" and not data.get("due_at"):
        raise ValidationError("passed batch requires due_at")
    instrument = _find_one(lookup, "instrument", "id", data.get("instrument_id"))
    if not instrument:
        raise ValidationError("instrument does not exist")
    method = _find_one(lookup, "method", "id", data.get("method_id"))
    if not method:
        raise ValidationError("method version does not exist")
    return {}


def _batch_release_blockers(entity, lookup):
    instrument = _find_one(lookup, "instrument", "id", entity["data"].get("instrument_id"))
    method = _find_one(lookup, "method", "id", entity["data"].get("method_id"))
    reasons = []
    if not instrument:
        reasons.append("instrument record not found")
    elif instrument["status"] != "active":
        reasons.append("instrument is %s" % instrument["status"])
    if not method:
        reasons.append("method version not found")
    elif method["status"] != "validated":
        reasons.append("method version is %s" % method["status"])
    return reasons


def _validate_batch_sync(actor, entity, data, lookup):
    """网络恢复后整批回传。

    方法版本已失效或仪器被隔离时，不直接放行（置为 held），但现场结果保留在批次数据中。
    """
    reasons = _batch_release_blockers(entity, lookup)
    if reasons:
        return {
            "_next_status": "held",
            "hold_reason": "; ".join(reasons),
            "synced_at": _now_iso(),
        }
    return {"synced_at": _now_iso()}


def _validate_batch_review(actor, entity, data, lookup):
    """两名人员分别复核；两名不同人员批准后才放行。"""
    decision = data.get("decision")
    if decision not in ("approve", "reject"):
        raise ValidationError("review decision must be approve or reject")
    reviewers = list(entity["data"].get("reviewers", []))
    reviewers.append(
        {"user_id": actor.user_id, "role": actor.role, "decision": decision, "at": _now_iso()}
    )
    if decision == "reject":
        return {"_next_status": "rejected", "reviewers": reviewers}

    reasons = _batch_release_blockers(entity, lookup)
    if reasons:
        return {
            "_next_status": "held",
            "hold_reason": "; ".join(reasons),
            "reviewers": reviewers,
        }

    distinct_approvers = {
        item["user_id"] for item in reviewers if item.get("decision") == "approve"
    }
    if len(distinct_approvers) >= 2:
        return {"_next_status": "released", "reviewers": reviewers}
    return {"reviewers": reviewers}


CUSTOM_CREATE = {'calibration': _validate_calibration, 'calibration_batch': _validate_batch_create}
CUSTOM_TRANSITIONS = {
    ('calibration', 'perform'): _validate_perform,
    ('result', 'release'): _validate_result_release,
    ('calibration_batch', 'sync'): _validate_batch_sync,
    ('calibration_batch', 'review'): _validate_batch_review,
}


class RuleEngine:
    ALIASES = {
        'instruments': 'instrument', 'calibrations': 'calibration', 'methods': 'method', 'results': 'result',
        'batches': 'calibration_batch', 'batch': 'calibration_batch', 'calibration_batches': 'calibration_batch',
    }
    INITIAL_STATUS = {
        'instrument': 'active', 'calibration': 'requested', 'method': 'draft', 'result': 'pending',
        'calibration_batch': 'registered',
    }
    TRANSITIONS = {
        'instrument': {
            'send_calibration': (('active',), 'calibrating'),
            'calibrate': (('calibrating',), 'active'),
            'quarantine': (('active',), 'quarantined'),
            'restore': (('quarantined',), 'active'),
        },
        'calibration': {
            'perform': (('requested', 'failed'), 'passed'),
            'approve': (('passed',), 'approved'),
            'reject': (('failed',), 'rejected'),
        },
        'method': {
            'validate_method': (('draft',), 'validated'),
            'revoke_method': (('validated',), 'revoked'),
        },
        'result': {
            'release': (('pending',), 'released'),
            'block': (('pending',), 'blocked'),
            'reanalyze': (('blocked',), 'pending'),
        },
        'calibration_batch': {
            'sync': (('registered',), 'synced'),
            'review': (('synced', 'held'), 'synced'),
        },
    }
    CREATE_REQUIRED = {
        'instrument': ('name', 'serial'),
        'calibration': ('instrument_id', 'requested_at'),
        'method': ('name', 'version'),
        'result': ('sample_id', 'measurement'),
        'calibration_batch': ('batch_no', 'instrument_id', 'method_id', 'result', 'uncertainty'),
    }
    ACTION_REQUIRED = {
        ('instrument', 'calibrate'): ('due_at', 'passed'),
        ('instrument', 'quarantine'): ('reason',),
        ('calibration', 'perform'): ('result', 'performed_at', 'uncertainty'),
        ('calibration', 'approve'): ('authorized_by',),
        ('calibration', 'reject'): ('reason',),
        ('method', 'validate_method'): ('parameters', 'instrument_ids'),
        ('method', 'revoke_method'): ('reason',),
        ('result', 'release'): ('instrument_id', 'method_id', 'value', 'unit'),
        ('result', 'block'): ('reason',),
        ('result', 'reanalyze'): ('reason',),
        ('calibration_batch', 'review'): ('decision',),
    }
    CREATE_ROLES = {
        'instrument': ('admin', 'technician'),
        'calibration': ('admin', 'metrology'),
        'method': ('admin', 'authorizer'),
        'result': ('admin', 'analyst'),
        'calibration_batch': ('admin', 'technician'),
    }
    ROLE_ACTIONS = {
        'send_calibration': ('admin', 'technician'),
        'calibrate': ('admin', 'metrology'),
        'quarantine': ('admin', 'metrology'),
        'restore': ('admin', 'metrology'),
        'perform': ('admin', 'metrology'),
        'approve': ('admin', 'authorizer'),
        'reject': ('admin', 'authorizer'),
        'validate_method': ('admin', 'authorizer'),
        'revoke_method': ('admin', 'authorizer'),
        'release': ('admin', 'analyst'),
        'block': ('admin', 'analyst'),
        'reanalyze': ('admin', 'analyst'),
        'sync': ('admin', 'technician', 'metrology'),
        'review': ('admin', 'authorizer', 'metrology'),
    }

    def normalize_kind(self, kind):
        return self.ALIASES.get(kind, kind)

    def initial_status(self, kind):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        return self.INITIAL_STATUS[kind]

    @staticmethod
    def _ensure_role(actor, allowed):
        if "*" not in allowed and actor.role not in allowed:
            raise PermissionDenied("role %s is not allowed here" % actor.role)

    @staticmethod
    def _require(data, fields):
        for field in fields:
            value = data.get(field)
            if value is None or value == "" or value == [] or value == {}:
                raise ValidationError("missing required field: " + field)

    def validate_create(self, actor, kind, data, lookup=None):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        self._ensure_role(actor, self.CREATE_ROLES.get(kind, ("admin",)))
        self._require(data, self.CREATE_REQUIRED.get(kind, ()))
        custom = CUSTOM_CREATE.get(kind)
        if custom:
            custom(actor, data, lookup)
        return dict(data)

    def validate_transition(self, actor, entity, action, data, lookup=None):
        kind = self.normalize_kind(entity["kind"])
        transition = self.TRANSITIONS.get(kind, {}).get(action)
        if not transition:
            raise InvalidTransition("unknown action %s for %s" % (action, kind))
        allowed_statuses, next_status = transition
        if entity["status"] not in allowed_statuses:
            raise InvalidTransition(
                "cannot %s from status %s" % (action, entity["status"])
            )
        allowed_roles = self.ROLE_ACTIONS.get(
            (kind, action), self.ROLE_ACTIONS.get(action, ("admin",))
        )
        self._ensure_role(actor, allowed_roles)
        self._require(data, self.ACTION_REQUIRED.get((kind, action), ()))
        custom = CUSTOM_TRANSITIONS.get((kind, action))
        extra = custom(actor, entity, data, lookup) if custom else {}
        extra = extra or {}
        next_status = extra.pop("_next_status", next_status)
        patch = dict(data)
        if extra:
            patch.update(extra)
        return next_status, patch


def _find_one(lookup, kind, field, value):
    if lookup is None:
        return None
    rows = lookup(kind, field, value) or []
    return rows[0] if rows else None


def _date_ordinal(value):
    return datetime.fromisoformat(str(value)[:10]).date().toordinal()
