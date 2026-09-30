from datetime import date, datetime, timedelta

from .domain import (
    ConflictError,
    InvalidTransition,
    PermissionDenied,
    ValidationError,
)

# Roles allowed to independently review a returned calibration batch.
BATCH_REVIEW_ROLES = ("admin", "metrology", "authorizer")
# A batch needs two distinct reviewers, neither of whom uploaded it.
BATCH_REVIEW_COUNT = 2


def _validate_calibration(actor, data, lookup):
    instrument = _find_one(lookup, "instrument", "id", data.get("instrument_id"))
    if not instrument:
        raise ValidationError("instrument does not exist")


def _validate_perform(actor, entity, data, lookup, as_of=None):
    if data.get("result") not in ("passed", "failed"):
        raise ValidationError("calibration result must be passed or failed")
    if data.get("result") == "passed" and not data.get("due_at"):
        raise ValidationError("passed calibration requires due_at")


def calibration_current(due_at, as_of):
    return str(due_at)[:10] >= str(as_of)[:10]


def release_gate_reasons(instrument, method, as_of=None):
    """Cross-object checks that gate a test-result release.

    Returns a list of machine-readable reason codes; an empty list means
    release is allowed. Used both by the online action and by batch merge.
    """
    as_of = str(as_of or date.today().isoformat())[:10]
    reasons = []
    if not instrument:
        reasons.append("instrument_missing")
    elif instrument["status"] == "quarantined":
        reasons.append("instrument_quarantined")
    elif instrument["status"] != "active":
        reasons.append("instrument_not_active")
    if instrument and not calibration_current(instrument["data"].get("due_at", ""), as_of):
        reasons.append("calibration_expired")
    if not method:
        reasons.append("method_missing")
    elif method["status"] == "revoked":
        reasons.append("method_revoked")
    elif method["status"] != "validated":
        reasons.append("method_not_validated")
    if instrument and method and instrument["id"] not in method["data"].get("instrument_ids", []):
        reasons.append("method_out_of_scope")
    return reasons


GATE_REASON_TEXT = {
    "instrument_missing": "instrument does not exist",
    "instrument_quarantined": "instrument is quarantined",
    "instrument_not_active": "instrument is not active",
    "calibration_expired": "instrument calibration is not current",
    "method_missing": "method does not exist",
    "method_revoked": "method version has been revoked",
    "method_not_validated": "method is not validated",
    "method_out_of_scope": "method is not validated for this instrument",
}


def gate_text(reasons):
    return [GATE_REASON_TEXT.get(code, code) for code in reasons]


def _validate_result_release(actor, entity, data, lookup, as_of=None):
    instrument = _find_one(lookup, "instrument", "id", data.get("instrument_id"))
    method = _find_one(lookup, "method", "id", data.get("method_id"))
    reasons = release_gate_reasons(instrument, method, as_of)
    if reasons:
        raise ValidationError(gate_text(reasons)[0])
    return {"released_by": actor.user_id}


CUSTOM_CREATE = {'calibration': _validate_calibration}
CUSTOM_TRANSITIONS = {('calibration', 'perform'): _validate_perform, ('result', 'release'): _validate_result_release}


class RuleEngine:
    ALIASES = {'instruments': 'instrument', 'calibrations': 'calibration', 'methods': 'method', 'results': 'result'}
    INITIAL_STATUS = {'instrument': 'active', 'calibration': 'requested', 'method': 'draft', 'result': 'pending'}
    TRANSITIONS = {'instrument': {'send_calibration': (('active',), 'calibrating'), 'calibrate': (('calibrating',), 'active'), 'quarantine': (('active',), 'quarantined'), 'restore': (('quarantined',), 'active')}, 'calibration': {'perform': (('requested', 'failed'), 'passed'), 'approve': (('passed',), 'approved'), 'reject': (('failed',), 'rejected')}, 'method': {'validate_method': (('draft',), 'validated'), 'revoke_method': (('validated',), 'revoked')}, 'result': {'release': (('pending', 'held'), 'released'), 'block': (('pending', 'held'), 'blocked'), 'reanalyze': (('blocked', 'held'), 'pending')}}
    CREATE_REQUIRED = {'instrument': ('name', 'serial'), 'calibration': ('instrument_id', 'requested_at'), 'method': ('name', 'version'), 'result': ('sample_id', 'measurement')}
    ACTION_REQUIRED = {('instrument', 'calibrate'): ('due_at', 'passed'), ('instrument', 'quarantine'): ('reason',), ('calibration', 'perform'): ('result', 'performed_at', 'uncertainty'), ('calibration', 'approve'): ('authorized_by',), ('calibration', 'reject'): ('reason',), ('method', 'validate_method'): ('parameters', 'instrument_ids'), ('method', 'revoke_method'): ('reason',), ('result', 'release'): ('instrument_id', 'method_id', 'value', 'unit'), ('result', 'block'): ('reason',), ('result', 'reanalyze'): ('reason',)}
    CREATE_ROLES = {'instrument': ('admin', 'technician'), 'calibration': ('admin', 'metrology'), 'method': ('admin', 'authorizer'), 'result': ('admin', 'analyst')}
    ROLE_ACTIONS = {'send_calibration': ('admin', 'technician'), 'calibrate': ('admin', 'metrology'), 'quarantine': ('admin', 'metrology'), 'restore': ('admin', 'metrology'), 'perform': ('admin', 'metrology'), 'approve': ('admin', 'authorizer'), 'reject': ('admin', 'authorizer'), 'validate_method': ('admin', 'authorizer'), 'revoke_method': ('admin', 'authorizer'), 'release': ('admin', 'analyst'), 'block': ('admin', 'analyst'), 'reanalyze': ('admin', 'analyst')}

    def __init__(self, clock=None):
        # clock() returns the current date as an ISO string; injectable for tests.
        self._clock = clock or (lambda: date.today().isoformat())

    @property
    def today(self):
        return str(self._clock())[:10]

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
        as_of = data.pop("_as_of", None) or self.today
        extra = custom(actor, entity, data, lookup, as_of) if custom else {}
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
