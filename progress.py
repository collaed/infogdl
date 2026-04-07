"""Per-profile progress tracking and download archive.

Stores in a SQLite database:
- Which image URLs have already been downloaded (archive)
- The last seen post reference per profile (progress cursor)

Inspired by gallery-dl's archive.py.
"""
import sqlite3
import logging
from pathlib import Path

log = logging.getLogger(__name__)

_DB_PATH = Path(".infogdl.db")


class ProgressTracker:
    """Tracks download progress per profile using SQLite."""

    def __init__(self, db_path: Path | str = _DB_PATH):
        self.db = sqlite3.connect(str(db_path), timeout=30)
        self.db.isolation_level = None
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS archive "
            "(profile TEXT, url TEXT, ts REAL, "
            "PRIMARY KEY (profile, url))"
        )
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS progress "
            "(profile TEXT PRIMARY KEY, last_url TEXT, last_ts REAL)"
        )

    def is_known(self, profile_key: str, url: str) -> bool:
        """Check if this URL was already downloaded for this profile."""
        row = self.db.execute(
            "SELECT 1 FROM archive WHERE profile=? AND url=? LIMIT 1",
            (profile_key, url)
        ).fetchone()
        return row is not None

    def record(self, profile_key: str, url: str, ts: float):
        """Record a downloaded URL and update progress cursor."""
        self.db.execute(
            "INSERT OR IGNORE INTO archive (profile, url, ts) VALUES (?,?,?)",
            (profile_key, url, ts)
        )
        # Update progress to the latest timestamp seen
        self.db.execute(
            "INSERT INTO progress (profile, last_url, last_ts) VALUES (?,?,?) "
            "ON CONFLICT(profile) DO UPDATE SET last_url=excluded.last_url, "
            "last_ts=excluded.last_ts WHERE excluded.last_ts > progress.last_ts",
            (profile_key, url, ts)
        )

    def get_last_ts(self, profile_key: str) -> float | None:
        """Get the timestamp of the last downloaded post for a profile."""
        row = self.db.execute(
            "SELECT last_ts FROM progress WHERE profile=?", (profile_key,)
        ).fetchone()
        return row[0] if row else None

    def reset(self, profile_key: str | None = None):
        """Reset progress for a profile, or all profiles if None."""
        if profile_key:
            self.db.execute("DELETE FROM progress WHERE profile=?", (profile_key,))
            self.db.execute("DELETE FROM archive WHERE profile=?", (profile_key,))
        else:
            self.db.execute("DELETE FROM progress")
            self.db.execute("DELETE FROM archive")

    def close(self):
        self.db.close()
