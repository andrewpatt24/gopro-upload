from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Generator, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS assets (
    media_id TEXT PRIMARY KEY,
    filename TEXT NOT NULL,
    size_bytes INTEGER,
    mime_type TEXT,
    capture_time TEXT,
    gopro_checksum TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    bytes_uploaded INTEGER NOT NULL DEFAULT 0,
    drive_file_id TEXT,
    drive_upload_uri TEXT,
    download_url TEXT,
    download_url_expires_at TEXT,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_assets_status ON assets(status);
CREATE INDEX IF NOT EXISTS idx_assets_drive_file_id ON assets(drive_file_id);

CREATE TABLE IF NOT EXISTS transfer_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    media_id TEXT NOT NULL,
    event TEXT NOT NULL,
    detail TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (media_id) REFERENCES assets(media_id)
);

CREATE INDEX IF NOT EXISTS idx_transfer_log_media_id ON transfer_log(media_id);

CREATE TABLE IF NOT EXISTS sync_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_type TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    items_seen INTEGER DEFAULT 0,
    items_new INTEGER DEFAULT 0,
    detail TEXT
);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Asset:
    media_id: str
    filename: str
    size_bytes: int | None
    mime_type: str | None
    capture_time: str | None
    gopro_checksum: str | None
    status: str
    bytes_uploaded: int
    drive_file_id: str | None
    drive_upload_uri: str | None
    download_url: str | None
    download_url_expires_at: str | None
    attempt_count: int
    last_error: str | None
    updated_at: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Asset:
        return cls(**dict(row))


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def init_schema(self) -> None:
        with self.connect() as conn:
            conn.executescript(SCHEMA)
            conn.commit()

    @contextmanager
    def session(self) -> Generator[sqlite3.Connection, None, None]:
        conn = self.connect()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def upsert_asset(
        self,
        conn: sqlite3.Connection,
        *,
        media_id: str,
        filename: str,
        size_bytes: int | None = None,
        mime_type: str | None = None,
        capture_time: str | None = None,
        gopro_checksum: str | None = None,
        preserve_done: bool = True,
    ) -> bool:
        """Insert or update inventory row. Returns True if newly inserted."""
        now = utcnow()
        existing = conn.execute(
            "SELECT status FROM assets WHERE media_id = ?", (media_id,)
        ).fetchone()
        if existing:
            if preserve_done and existing["status"] == "done":
                conn.execute(
                    """
                    UPDATE assets SET filename=?, size_bytes=COALESCE(?, size_bytes),
                    mime_type=COALESCE(?, mime_type), capture_time=COALESCE(?, capture_time),
                    gopro_checksum=COALESCE(?, gopro_checksum), updated_at=?
                    WHERE media_id=?
                    """,
                    (
                        filename,
                        size_bytes,
                        mime_type,
                        capture_time,
                        gopro_checksum,
                        now,
                        media_id,
                    ),
                )
            else:
                conn.execute(
                    """
                    UPDATE assets SET filename=?, size_bytes=COALESCE(?, size_bytes),
                    mime_type=COALESCE(?, mime_type), capture_time=COALESCE(?, capture_time),
                    gopro_checksum=COALESCE(?, gopro_checksum), updated_at=?
                    WHERE media_id=?
                    """,
                    (
                        filename,
                        size_bytes,
                        mime_type,
                        capture_time,
                        gopro_checksum,
                        now,
                        media_id,
                    ),
                )
            return False
        conn.execute(
            """
            INSERT INTO assets (
                media_id, filename, size_bytes, mime_type, capture_time,
                gopro_checksum, status, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)
            """,
            (
                media_id,
                filename,
                size_bytes,
                mime_type,
                capture_time,
                gopro_checksum,
                now,
            ),
        )
        return True

    def get_asset(self, conn: sqlite3.Connection, media_id: str) -> Asset | None:
        row = conn.execute("SELECT * FROM assets WHERE media_id = ?", (media_id,)).fetchone()
        return Asset.from_row(row) if row else None

    def update_asset(self, conn: sqlite3.Connection, media_id: str, **fields: Any) -> None:
        fields["updated_at"] = utcnow()
        cols = ", ".join(f"{k} = ?" for k in fields)
        vals = list(fields.values()) + [media_id]
        conn.execute(f"UPDATE assets SET {cols} WHERE media_id = ?", vals)

    def log_event(
        self, conn: sqlite3.Connection, media_id: str, event: str, detail: str | None = None
    ) -> None:
        conn.execute(
            "INSERT INTO transfer_log (media_id, event, detail, created_at) VALUES (?, ?, ?, ?)",
            (media_id, event, detail, utcnow()),
        )

    def start_sync_run(self, conn: sqlite3.Connection, run_type: str) -> int:
        cur = conn.execute(
            "INSERT INTO sync_runs (run_type, started_at) VALUES (?, ?)",
            (run_type, utcnow()),
        )
        return cur.lastrowid  # type: ignore[return-value]

    def finish_sync_run(
        self,
        conn: sqlite3.Connection,
        run_id: int,
        *,
        items_seen: int,
        items_new: int,
        detail: str | None = None,
    ) -> None:
        conn.execute(
            """
            UPDATE sync_runs SET finished_at=?, items_seen=?, items_new=?, detail=?
            WHERE id=?
            """,
            (utcnow(), items_seen, items_new, detail, run_id),
        )

    def iter_work_queue(
        self, conn: sqlite3.Connection, *, stale_minutes: int, limit: int | None = None
    ) -> Iterator[Asset]:
        sql = """
            SELECT * FROM assets
            WHERE status IN ('pending', 'failed')
               OR (status = 'transferring' AND datetime(updated_at) < datetime('now', ?))
            ORDER BY
                CASE status WHEN 'transferring' THEN 0 WHEN 'pending' THEN 1 ELSE 2 END,
                updated_at ASC
        """
        stale = f"-{stale_minutes} minutes"
        params: list[Any] = [stale]
        if limit:
            sql += " LIMIT ?"
            params.append(limit)
        for row in conn.execute(sql, params):
            yield Asset.from_row(row)

    def status_summary(self, conn: sqlite3.Connection) -> dict[str, Any]:
        rows = conn.execute(
            """
            SELECT status, COUNT(*) as cnt,
                   COALESCE(SUM(size_bytes), 0) as total_bytes,
                   COALESCE(SUM(CASE WHEN status != 'done' THEN size_bytes ELSE 0 END), 0) as remaining_bytes
            FROM assets GROUP BY status
            """
        ).fetchall()
        summary: dict[str, Any] = {"by_status": {}, "total": 0}
        for r in rows:
            summary["by_status"][r["status"]] = {
                "count": r["cnt"],
                "bytes": r["total_bytes"],
            }
            summary["total"] += r["cnt"]
        done = summary["by_status"].get("done", {}).get("count", 0)
        summary["done"] = done
        summary["pending_work"] = summary["total"] - done
        return summary

    def reset_failed(self, conn: sqlite3.Connection) -> int:
        cur = conn.execute(
            """
            UPDATE assets SET status='pending', last_error=NULL, attempt_count=0,
                bytes_uploaded=0, drive_upload_uri=NULL, drive_file_id=NULL, updated_at=?
            WHERE status='failed'
            """,
            (utcnow(),),
        )
        return cur.rowcount

    def all_assets(self, conn: sqlite3.Connection) -> list[Asset]:
        return [Asset.from_row(r) for r in conn.execute("SELECT * FROM assets ORDER BY filename")]
