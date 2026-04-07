"""Import settings from gallery-dl configuration.

Reads gallery-dl's config.json and extracts:
- Cookie sources (browser name or cookie file path)
- Archive database paths (for progress import)
- Profile URLs from known extractors
"""
import json
import logging
import os
import sqlite3
import sys
from pathlib import Path

log = logging.getLogger(__name__)


def find_gallery_dl_config() -> Path | None:
    """Locate gallery-dl config file using its standard search paths."""
    candidates = []
    if sys.platform in ("win32", "cygwin"):
        appdata = os.environ.get("APPDATA", "")
        home = os.path.expanduser("~")
        candidates = [
            Path(appdata) / "gallery-dl" / "config.json",
            Path(home) / "gallery-dl" / "config.json",
            Path(home) / "gallery-dl.conf",
        ]
    else:
        xdg = os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config"))
        home = os.path.expanduser("~")
        candidates = [
            Path(xdg) / "gallery-dl" / "config.json",
            Path(home) / ".config" / "gallery-dl" / "config.json",
            Path(home) / ".gallery-dl.conf",
        ]

    for p in candidates:
        if p.is_file():
            log.info("Found gallery-dl config at %s", p)
            return p
    return None


def load_gallery_dl_config(path: Path | None = None) -> dict | None:
    """Load and parse gallery-dl config."""
    if path is None:
        path = find_gallery_dl_config()
    if path is None:
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        log.warning("Failed to read gallery-dl config: %s", e)
        return None


def extract_cookie_source(gdl_cfg: dict, platform: str) -> tuple[str | None, str | None]:
    """Extract browser name or cookie file from gallery-dl config.

    Returns (browser, cookie_file) — one or both may be None.
    """
    site = "twitter" if platform == "twitter" else "linkedin"
    cookies = (gdl_cfg.get("extractor", {}).get(site, {}).get("cookies")
               or gdl_cfg.get("extractor", {}).get("cookies"))

    if cookies is None:
        return None, None

    # ["firefox"] or ["chrome", "Profile 1"]
    if isinstance(cookies, list) and cookies:
        return cookies[0], None

    # "/path/to/cookies.txt"
    if isinstance(cookies, str):
        expanded = os.path.expandvars(os.path.expanduser(cookies))
        if os.path.isfile(expanded):
            return None, expanded
        # Might be a browser name
        return cookies, None

    # {"name": "value", ...} dict — not directly usable as browser/file
    return None, None


def import_archive_urls(gdl_cfg: dict, platform: str) -> set[str]:
    """Import already-downloaded URLs from gallery-dl's SQLite archive.

    Returns set of archive entry strings (usually URLs or ID-based keys).
    """
    site = "twitter" if platform == "twitter" else "linkedin"
    archive_path = (gdl_cfg.get("extractor", {}).get(site, {}).get("archive")
                    or gdl_cfg.get("extractor", {}).get("archive"))

    if not archive_path:
        return set()

    archive_path = os.path.expandvars(os.path.expanduser(archive_path))
    if not os.path.isfile(archive_path):
        return set()

    try:
        db = sqlite3.connect(archive_path)
        rows = db.execute("SELECT entry FROM archive").fetchall()
        db.close()
        entries = {r[0] for r in rows}
        log.info("Imported %d entries from gallery-dl archive %s", len(entries), archive_path)
        return entries
    except Exception as e:
        log.warning("Failed to read gallery-dl archive: %s", e)
        return set()
