"""Field-side offline store.

The calibration lab registers instruments, method versions, pass/fail
outcomes, uncertainties and due dates while the network is down. Each
registration is stored locally in its own SQLite file and uploaded as one
batch once connectivity is restored.
"""

import json
import sqlite3
from datetime import datetime, timezone

from .domain import ConflictError, ValidationError


def utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


REQUIRED_ENTRY_FIELDS = (
    "instrument_name",
    "instrument_serial",
    "method_name",
    "method_version",
    "outcome",
    "uncertainty",
    "due_at",
    "sample_id",
    "value",
    "unit",
    "recorded_at",
)


def validate_entry(data):
    payload = dict(data or {})
    for field in REQUIRED_ENTRY_FIELDS:
        value = payload.get(field)
        if value is None or value == "" or value == []:
            raise ValidationError("missing required field: " + field)
    if payload["outcome"] not in ("passed", "failed"):
        raise ValidationError("outcome must be passed or failed")
    if payload["outcome"] == "passed" and not str(payload.get("due_at")):
        raise ValidationError("passed calibration requires due_at")
    return payload


class FieldStore:
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
                CREATE TABLE IF NOT EXISTS field_batches (
                    batch_no TEXT PRIMARY KEY,
                    batch_version INTEGER NOT NULL,
                    site TEXT,
                    registered_by TEXT NOT NULL,
                    registered_at TEXT NOT NULL,
                    uploaded INTEGER NOT NULL DEFAULT 0,
                    uploaded_at TEXT
                );
                CREATE TABLE IF NOT EXISTS field_entries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_no TEXT NOT NULL,
                    line INTEGER NOT NULL,
                    payload TEXT NOT NULL,
                    UNIQUE(batch_no, line)
                );
                CREATE INDEX IF NOT EXISTS idx_field_entries_batch
                    ON field_entries(batch_no);
            """)

    @staticmethod
    def _batch_from_row(row, entries=None):
        result = {
            "batch_no": row["batch_no"],
            "batch_version": int(row["batch_version"]),
            "site": row["site"],
            "registered_by": row["registered_by"],
            "registered_at": row["registered_at"],
            "uploaded": bool(row["uploaded"]),
            "uploaded_at": row["uploaded_at"],
        }
        if entries is not None:
            result["entries"] = entries
        return result

    def register(self, batch_no, batch_version, entries, actor, site=None):
        """Register or replace one offline batch draft.

        The same batch number may be re-registered with a higher version
        (corrected paperwork). A lower/equal version is rejected.
        """
        if not batch_no:
            raise ValidationError("batch_no is required")
        version = int(batch_version)
        if version < 1:
            raise ValidationError("batch_version must be >= 1")
        if not entries:
            raise ValidationError("batch must contain at least one entry")
        clean_entries = [validate_entry(item) for item in entries]
        now = utcnow()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT batch_version, uploaded FROM field_batches WHERE batch_no=?",
                (batch_no,),
            ).fetchone()
            if row:
                if int(row["batch_version"]) >= version:
                    raise ConflictError(
                        "batch %s version %s is not newer than registered version %s"
                        % (batch_no, version, int(row["batch_version"]))
                    )
                if row["uploaded"]:
                    raise ConflictError(
                        "batch %s already uploaded at version %s; register a higher version"
                        % (batch_no, int(row["batch_version"]))
                    )
                connection.execute("DELETE FROM field_entries WHERE batch_no=?", (batch_no,))
            connection.execute(
                "INSERT INTO field_batches(batch_no, batch_version, site, registered_by, registered_at) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(batch_no) DO UPDATE SET batch_version=excluded.batch_version, "
                "site=excluded.site, registered_by=excluded.registered_by, registered_at=excluded.registered_at, "
                "uploaded=0, uploaded_at=NULL",
                (batch_no, version, site, actor.user_id, now),
            )
            for line, entry in enumerate(clean_entries, start=1):
                connection.execute(
                    "INSERT INTO field_entries(batch_no, line, payload) VALUES (?, ?, ?)",
                    (batch_no, line, json.dumps(entry, ensure_ascii=False, sort_keys=True)),
                )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.get_batch(batch_no)

    def get_batch(self, batch_no):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM field_batches WHERE batch_no=?", (batch_no,)
            ).fetchone()
            if not row:
                return None
            entries = [
                json.loads(item["payload"])
                for item in connection.execute(
                    "SELECT payload FROM field_entries WHERE batch_no=? ORDER BY line",
                    (batch_no,),
                ).fetchall()
            ]
        return self._batch_from_row(row, entries)

    def list_batches(self, include_uploaded=True):
        with self._connect() as connection:
            query = "SELECT * FROM field_batches"
            if not include_uploaded:
                query += " WHERE uploaded=0"
            query += " ORDER BY registered_at, batch_no"
            rows = connection.execute(query).fetchall()
        return [self._batch_from_row(row) for row in rows]

    def mark_uploaded(self, batch_no, batch_version):
        now = utcnow()
        with self._connect() as connection:
            connection.execute(
                "UPDATE field_batches SET uploaded=1, uploaded_at=? WHERE batch_no=? AND batch_version=?",
                (now, batch_no, int(batch_version)),
            )
        return now
