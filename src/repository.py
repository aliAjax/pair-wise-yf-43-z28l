import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from .domain import ConflictError, NotFoundError


def utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _entity_from_row(row):
    return {
        "id": row["id"],
        "kind": row["kind"],
        "status": row["status"],
        "version": int(row["version"]),
        "data": json.loads(row["data"]),
        "created_by": row["created_by"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _insert_entity(conn, entity_id, kind, status, data, actor_id, now=None):
    now = now or utcnow()
    payload = json.dumps(data, ensure_ascii=False, sort_keys=True)
    conn.execute(
        "INSERT INTO entities(id, kind, status, version, data, created_by, created_at, updated_at) "
        "VALUES (?, ?, ?, 1, ?, ?, ?, ?)",
        (entity_id, kind, status, payload, actor_id, now, now),
    )
    conn.execute(
        "INSERT INTO entity_versions(entity_id, version, kind, status, data, created_by, created_at, updated_at) "
        "VALUES (?, 1, ?, ?, ?, ?, ?, ?)",
        (entity_id, kind, status, payload, actor_id, now, now),
    )


def _update_entity(conn, entity_id, expected_version, status, data, now=None):
    now = now or utcnow()
    row = conn.execute(
        "SELECT version FROM entities WHERE id = ?", (entity_id,)
    ).fetchone()
    if not row:
        raise NotFoundError("entity not found: " + entity_id)
    current_version = int(row["version"])
    if expected_version is not None and current_version != int(expected_version):
        raise ConflictError(
            "version conflict: expected %s, found %s"
            % (expected_version, current_version)
        )
    payload = json.dumps(data, ensure_ascii=False, sort_keys=True)
    cursor = conn.execute(
        "UPDATE entities SET status = ?, version = version + 1, data = ?, updated_at = ? "
        "WHERE id = ? AND version = ?",
        (status, payload, now, entity_id, current_version),
    )
    if cursor.rowcount != 1:
        raise ConflictError("version conflict: " + entity_id)
    conn.execute(
        "INSERT INTO entity_versions(entity_id, version, kind, status, data, created_by, created_at, updated_at) "
        "SELECT id, version, kind, ?, data, created_by, created_at, ? FROM entities WHERE id = ?",
        (status, now, entity_id),
    )
    return current_version + 1


def _append_audit(conn, entity_id, actor_id, actor_role, action, from_status, to_status, detail, now=None):
    conn.execute(
        "INSERT INTO audit_log(entity_id, actor_id, actor_role, action, from_status, to_status, detail, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            entity_id,
            actor_id,
            actor_role,
            action,
            from_status,
            to_status,
            json.dumps(detail, ensure_ascii=False, sort_keys=True),
            now or utcnow(),
        ),
    )


class Transaction:
    """A single write transaction spanning any number of objects."""

    def __init__(self, connection):
        self.connection = connection

    def get_entity(self, entity_id):
        row = self.connection.execute(
            "SELECT * FROM entities WHERE id = ?", (entity_id,)
        ).fetchone()
        return _entity_from_row(row) if row else None

    def list_entities(self, kind=None, status=None):
        clauses, params = [], []
        if kind:
            clauses.append("kind = ?")
            params.append(kind)
        if status:
            clauses.append("status = ?")
            params.append(status)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = self.connection.execute(
            "SELECT * FROM entities" + where + " ORDER BY created_at, id", params
        ).fetchall()
        return [_entity_from_row(row) for row in rows]

    def find_entity(self, kind, field, value):
        for entity in self.list_entities(kind=kind):
            if (entity["id"] == value if field == "id" else entity["data"].get(field) == value):
                return entity
        return None

    def insert_entity(self, entity_id, kind, status, data, actor_id):
        _insert_entity(self.connection, entity_id, kind, status, data, actor_id)
        return self.get_entity(entity_id)

    def update_entity(self, entity_id, expected_version, status, data):
        _update_entity(self.connection, entity_id, expected_version, status, data)
        return self.get_entity(entity_id)

    def append_audit(self, entity_id, actor_id, actor_role, action, from_status, to_status, detail):
        _append_audit(
            self.connection, entity_id, actor_id, actor_role,
            action, from_status, to_status, detail,
        )

    # ----- calibration batches -----

    def get_batch(self, batch_no):
        row = self.connection.execute(
            "SELECT * FROM calibration_batches WHERE batch_no = ?", (batch_no,)
        ).fetchone()
        if not row:
            return None
        return {
            "batch_no": row["batch_no"],
            "batch_version": int(row["batch_version"]),
            "status": row["status"],
            "site": row["site"],
            "payload": json.loads(row["payload"]),
            "uploaded_by": row["uploaded_by"],
            "uploaded_at": row["uploaded_at"],
            "finalized_at": row["finalized_at"],
        }

    def list_batches(self):
        rows = self.connection.execute(
            "SELECT * FROM calibration_batches ORDER BY uploaded_at, batch_no"
        ).fetchall()
        result = []
        for row in rows:
            result.append(
                {
                    "batch_no": row["batch_no"],
                    "batch_version": int(row["batch_version"]),
                    "status": row["status"],
                    "site": row["site"],
                    "uploaded_by": row["uploaded_by"],
                    "uploaded_at": row["uploaded_at"],
                    "finalized_at": row["finalized_at"],
                }
            )
        return result

    def upsert_batch(self, batch_no, batch_version, status, site, payload, uploaded_by, finalized_at=None):
        now = utcnow()
        self.connection.execute(
            "INSERT INTO calibration_batches(batch_no, batch_version, status, site, payload, uploaded_by, uploaded_at, finalized_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(batch_no) DO UPDATE SET batch_version=excluded.batch_version, status=excluded.status, "
            "site=excluded.site, payload=excluded.payload, uploaded_by=excluded.uploaded_by, "
            "uploaded_at=excluded.uploaded_at, finalized_at=excluded.finalized_at",
            (
                batch_no,
                int(batch_version),
                status,
                site,
                json.dumps(payload, ensure_ascii=False, sort_keys=True),
                uploaded_by,
                now,
                finalized_at,
            ),
        )
        return now

    def mark_batch_finalized(self, batch_no, finalized_at):
        self.connection.execute(
            "UPDATE calibration_batches SET status='finalized', finalized_at=? WHERE batch_no=?",
            (finalized_at, batch_no),
        )

    def replace_entry(self, entry):
        self.connection.execute(
            "INSERT OR REPLACE INTO batch_entries(batch_no, batch_version, line, instrument_id, method_id, "
            "calibration_id, result_id, outcome, held, reasons, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                entry["batch_no"],
                int(entry["batch_version"]),
                int(entry["line"]),
                entry["instrument_id"],
                entry["method_id"],
                entry["calibration_id"],
                entry["result_id"],
                entry["outcome"],
                1 if entry["held"] else 0,
                json.dumps(entry["reasons"], ensure_ascii=False, sort_keys=True),
                utcnow(),
            ),
        )

    def list_entries(self, batch_no, batch_version):
        rows = self.connection.execute(
            "SELECT * FROM batch_entries WHERE batch_no=? AND batch_version=? ORDER BY line",
            (batch_no, int(batch_version)),
        ).fetchall()
        return [
            {
                "batch_no": row["batch_no"],
                "batch_version": int(row["batch_version"]),
                "line": int(row["line"]),
                "instrument_id": row["instrument_id"],
                "method_id": row["method_id"],
                "calibration_id": row["calibration_id"],
                "result_id": row["result_id"],
                "outcome": row["outcome"],
                "held": bool(row["held"]),
                "reasons": json.loads(row["reasons"]),
            }
            for row in rows
        ]

    def add_review(self, batch_no, batch_version, reviewer_id, reviewer_role, note):
        self.connection.execute(
            "INSERT INTO batch_reviews(batch_no, batch_version, reviewer_id, reviewer_role, note, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (batch_no, int(batch_version), reviewer_id, reviewer_role, note or "", utcnow()),
        )

    def list_reviews(self, batch_no, batch_version):
        rows = self.connection.execute(
            "SELECT reviewer_id, reviewer_role, note, created_at FROM batch_reviews "
            "WHERE batch_no=? AND batch_version=? ORDER BY id",
            (batch_no, int(batch_version)),
        ).fetchall()
        return [
            {
                "reviewer_id": row["reviewer_id"],
                "reviewer_role": row["reviewer_role"],
                "note": row["note"],
                "created_at": row["created_at"],
            }
            for row in rows
        ]


class SQLiteRepository:
    def __init__(self, path):
        self.path = str(path)
        self._initialize()

    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self):
        with self._connect() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS entities (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    data TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_entities_kind_status
                    ON entities(kind, status);
                CREATE TABLE IF NOT EXISTS entity_versions (
                    entity_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    data TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(entity_id, version)
                );
                CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    entity_id TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    actor_role TEXT NOT NULL,
                    action TEXT NOT NULL,
                    from_status TEXT,
                    to_status TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_audit_entity
                    ON audit_log(entity_id, id);
                CREATE TABLE IF NOT EXISTS idempotency (
                    actor_id TEXT NOT NULL,
                    idem_key TEXT NOT NULL,
                    entity_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(actor_id, idem_key)
                );
                CREATE TABLE IF NOT EXISTS calibration_batches (
                    batch_no TEXT PRIMARY KEY,
                    batch_version INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    site TEXT,
                    payload TEXT NOT NULL,
                    uploaded_by TEXT NOT NULL,
                    uploaded_at TEXT NOT NULL,
                    finalized_at TEXT
                );
                CREATE TABLE IF NOT EXISTS batch_entries (
                    batch_no TEXT NOT NULL,
                    batch_version INTEGER NOT NULL,
                    line INTEGER NOT NULL,
                    instrument_id TEXT NOT NULL,
                    method_id TEXT,
                    calibration_id TEXT NOT NULL,
                    result_id TEXT NOT NULL,
                    outcome TEXT NOT NULL,
                    held INTEGER NOT NULL,
                    reasons TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(batch_no, batch_version, line)
                );
                CREATE TABLE IF NOT EXISTS batch_reviews (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_no TEXT NOT NULL,
                    batch_version INTEGER NOT NULL,
                    reviewer_id TEXT NOT NULL,
                    reviewer_role TEXT NOT NULL,
                    note TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(batch_no, batch_version, reviewer_id)
                );
            """)

    @contextmanager
    def transaction(self):
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield Transaction(connection)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def create_entity(self, entity_id, kind, status, data, actor_id):
        now = utcnow()
        with self._connect() as connection:
            _insert_entity(connection, entity_id, kind, status, data, actor_id, now)
        return self.get_entity(entity_id)

    def get_entity(self, entity_id):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM entities WHERE id = ?", (entity_id,)
            ).fetchone()
        return _entity_from_row(row) if row else None

    def list_entities(self, kind=None, status=None):
        clauses = []
        params = []
        if kind:
            clauses.append("kind = ?")
            params.append(kind)
        if status:
            clauses.append("status = ?")
            params.append(status)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM entities" + where + " ORDER BY created_at, id", params
            ).fetchall()
        return [_entity_from_row(row) for row in rows]

    def find_entities(self, kind, field, value):
        return [
            entity
            for entity in self.list_entities(kind=kind)
            if (entity["id"] == value if field == "id" else entity["data"].get(field) == value)
        ]

    def update_entity(self, entity_id, expected_version, status, data):
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            _update_entity(connection, entity_id, expected_version, status, data)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.get_entity(entity_id)

    def list_versions(self, entity_id):
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM entity_versions WHERE entity_id = ? ORDER BY version",
                (entity_id,),
            ).fetchall()
        return [
            {
                "entity_id": row["entity_id"],
                "version": int(row["version"]),
                "kind": row["kind"],
                "status": row["status"],
                "data": json.loads(row["data"]),
                "created_by": row["created_by"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }
            for row in rows
        ]

    def append_audit(self, entity_id, actor_id, actor_role, action, from_status, to_status, detail):
        with self._connect() as connection:
            _append_audit(
                connection, entity_id, actor_id, actor_role,
                action, from_status, to_status, detail,
            )

    def list_audit(self, entity_id=None):
        with self._connect() as connection:
            if entity_id:
                rows = connection.execute(
                    "SELECT * FROM audit_log WHERE entity_id = ? ORDER BY id", (entity_id,)
                ).fetchall()
            else:
                rows = connection.execute("SELECT * FROM audit_log ORDER BY id").fetchall()
        return [
            {
                "id": row["id"],
                "entity_id": row["entity_id"],
                "actor_id": row["actor_id"],
                "actor_role": row["actor_role"],
                "action": row["action"],
                "from_status": row["from_status"],
                "to_status": row["to_status"],
                "detail": json.loads(row["detail"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def get_idempotency(self, actor_id, idem_key):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT entity_id FROM idempotency WHERE actor_id = ? AND idem_key = ?",
                (actor_id, idem_key),
            ).fetchone()
        return row["entity_id"] if row else None

    def save_idempotency(self, actor_id, idem_key, entity_id):
        with self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO idempotency(actor_id, idem_key, entity_id, created_at) "
                "VALUES (?, ?, ?, ?)",
                (actor_id, idem_key, entity_id, utcnow()),
            )

    def ping(self):
        with self._connect() as connection:
            connection.execute("SELECT 1").fetchone()
        return True
