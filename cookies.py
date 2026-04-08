"""Cookie extraction from browsers — direct SQLite access.

Supports Chrome, Firefox, Edge, Brave, Opera on Windows/macOS/Linux.
Falls back to Netscape cookie file import.
"""
import http.cookiejar
import logging
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

log = logging.getLogger(__name__)


def load_cookies(domain: str, cookie_file: str | None = None,
                 browser: str | None = None) -> dict[str, str]:
    """Extract cookies for a domain. Returns {name: value} dict.

    Priority: cookie_file > specified browser > auto-detect from all browsers.
    """
    if cookie_file:
        cookies = _load_cookie_file(cookie_file, domain)
        if cookies:
            return cookies

    if browser:
        browsers = [browser]
    else:
        browsers = ["chrome", "firefox", "edge", "brave", "opera"]

    for name in browsers:
        try:
            log.info("Trying %s for %s cookies...", name, domain)
            cookies = _extract_from_browser(name, domain)
            if cookies:
                log.info("✓ Extracted %d cookies from %s for %s",
                         len(cookies), name, domain)
                return cookies
            else:
                log.info("  %s: no cookies found for %s", name, domain)
        except Exception as e:
            log.info("  %s: failed (%s)", name, e)

    log.warning("No cookies found for %s from any browser", domain)
    return {}


def _load_cookie_file(path: str, domain: str) -> dict[str, str]:
    """Load Netscape-format cookie file — lenient parser."""
    cookies = {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split("\t")
                if len(parts) >= 7:
                    host, _, _, _, _, name, value = parts[:7]
                    if domain in host:
                        cookies[name] = value
        if cookies:
            log.info("Loaded %d cookies from file for %s", len(cookies), domain)
    except Exception as e:
        log.warning("Failed to load cookie file %s: %s", path, e)
    return cookies


def _extract_from_browser(browser: str, domain: str) -> dict[str, str]:
    """Extract cookies directly from browser SQLite database."""
    if browser == "firefox":
        return _extract_firefox(domain)
    return _extract_chromium(browser, domain)


# -- Firefox --

def _firefox_profile_dir() -> list[str]:
    if sys.platform in ("win32", "cygwin"):
        base = os.path.expandvars(R"%APPDATA%\Mozilla\Firefox\Profiles")
    elif sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Application Support/Firefox/Profiles")
    else:
        candidates = [
            os.path.expanduser("~/.mozilla/firefox"),
            os.path.expanduser("~/.var/app/org.mozilla.firefox/.mozilla/firefox"),
            os.path.expanduser("~/snap/firefox/common/.mozilla/firefox"),
        ]
        xdg = os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config"))
        candidates.insert(0, os.path.join(xdg, "mozilla/firefox"))
        return [c for c in candidates if os.path.isdir(c)]
    return [base] if os.path.isdir(base) else []


def _extract_firefox(domain: str) -> dict[str, str]:
    for profile_root in _firefox_profile_dir():
        db_path = _find_newest(profile_root, "cookies.sqlite")
        if not db_path:
            continue
        with _safe_sqlite(db_path) as db:
            rows = db.execute(
                "SELECT name, value FROM moz_cookies "
                "WHERE host LIKE ? OR host LIKE ?",
                (f"%{domain}", f"%.{domain}")
            ).fetchall()
            if rows:
                return dict(rows)
    return {}


# -- Chromium-based --

_CHROMIUM_DIRS = {
    "chrome": {
        "win32": R"%LOCALAPPDATA%\Google\Chrome\User Data",
        "darwin": "~/Library/Application Support/Google/Chrome",
        "linux": "~/.config/google-chrome",
    },
    "edge": {
        "win32": R"%LOCALAPPDATA%\Microsoft\Edge\User Data",
        "darwin": "~/Library/Application Support/Microsoft Edge",
        "linux": "~/.config/microsoft-edge",
    },
    "brave": {
        "win32": R"%LOCALAPPDATA%\BraveSoftware\Brave-Browser\User Data",
        "darwin": "~/Library/Application Support/BraveSoftware/Brave-Browser",
        "linux": "~/.config/BraveSoftware/Brave-Browser",
    },
    "opera": {
        "win32": R"%APPDATA%\Opera Software\Opera Stable",
        "darwin": "~/Library/Application Support/com.operasoftware.Opera",
        "linux": "~/.config/opera",
    },
}


def _chromium_dir(browser: str) -> str | None:
    plat = "linux" if sys.platform.startswith("linux") else sys.platform
    if plat in ("win32", "cygwin"):
        plat = "win32"
    dirs = _CHROMIUM_DIRS.get(browser, {})
    raw = dirs.get(plat)
    if not raw:
        return None
    path = os.path.expandvars(os.path.expanduser(raw))
    return path if os.path.isdir(path) else None


def _extract_chromium(browser: str, domain: str) -> dict[str, str]:
    base = _chromium_dir(browser)
    if not base:
        return {}
    db_path = _find_newest(base, "Cookies")
    if not db_path:
        return {}
    with _safe_sqlite(db_path) as db:
        try:
            rows = db.execute(
                "SELECT name, value FROM cookies "
                "WHERE host_key LIKE ? OR host_key LIKE ?",
                (f"%{domain}", f"%.{domain}")
            ).fetchall()
        except sqlite3.OperationalError:
            return {}
        # Chromium encrypts cookies — unencrypted `value` field is empty for
        # encrypted ones. We only return cookies that have a plaintext value.
        # For full decryption, browser_cookie3 or platform-specific crypto
        # would be needed. The unencrypted session cookies are usually enough.
        result = {name: val for name, val in rows if val}
        if not result:
            log.debug("%s cookies are encrypted; falling back to browser_cookie3", browser)
            try:
                import browser_cookie3
                loader = getattr(browser_cookie3, browser, None)
                if loader:
                    cj = loader(domain_name=f".{domain}")
                    return {c.name: c.value for c in cj if domain in c.domain}
            except Exception:
                pass
        return result


# -- Helpers --

def _find_newest(root: str, filename: str) -> str | None:
    """Find the most recently modified file with given name under root."""
    matches = []
    for dirpath, _, filenames in os.walk(root):
        if filename in filenames:
            matches.append(os.path.join(dirpath, filename))
    if not matches:
        return None
    return max(matches, key=lambda p: os.path.getmtime(p))


class _safe_sqlite:
    """Context manager that opens SQLite DB read-only, copying if locked.
    On Windows, always copies first since browsers hold exclusive locks."""

    def __init__(self, path: str):
        self.path = path
        self.tmpdir = None
        self.conn = None

    def _open_copy(self) -> sqlite3.Connection:
        self.tmpdir = tempfile.TemporaryDirectory(prefix="infogdl-")
        copy = os.path.join(self.tmpdir.name, "cookies.sqlite")
        # On Windows, use raw file read to bypass locks
        if sys.platform in ("win32", "cygwin"):
            try:
                with open(self.path, "rb") as src, open(copy, "wb") as dst:
                    dst.write(src.read())
            except PermissionError:
                # Last resort: use system copy which may handle locks better
                import subprocess
                subprocess.run(["cmd", "/c", "copy", "/y",
                                self.path, copy],
                               capture_output=True, timeout=10)
        else:
            shutil.copy2(self.path, copy)
        return sqlite3.connect(copy, timeout=5)

    def __enter__(self) -> sqlite3.Connection:
        # On Windows, always copy first — browsers hold exclusive locks
        if sys.platform in ("win32", "cygwin"):
            self.conn = self._open_copy()
            return self.conn
        try:
            uri = f"file:{self.path}?mode=ro&immutable=1"
            self.conn = sqlite3.connect(uri, uri=True, timeout=5)
            return self.conn
        except sqlite3.OperationalError:
            self.conn = self._open_copy()
            return self.conn

    def __exit__(self, *exc):
        if self.conn:
            self.conn.close()
        if self.tmpdir:
            self.tmpdir.cleanup()


def export_cookies(cookies: dict[str, str], domain: str,
                   path: str | None = None) -> str | None:
    """Export cookies to Netscape format file. Returns path written."""
    if not cookies or not path:
        return None
    try:
        lines = ["# Netscape HTTP Cookie File", ""]
        for name, value in cookies.items():
            lines.append(f".{domain}\tTRUE\t/\tFALSE\t0\t{name}\t{value}")
        Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")
        log.info("Exported %d cookies to %s", len(cookies), path)
        return path
    except Exception as e:
        log.warning("Failed to export cookies: %s", e)
        return None
