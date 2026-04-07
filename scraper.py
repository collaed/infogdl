"""Scrape images from LinkedIn and Twitter profiles using Selenium.

Session handling modeled after gallery-dl:
- Direct SQLite cookie extraction from multiple browsers
- Netscape cookie file import
- Session validation before scraping
- Rate limiting with exponential backoff and retry
- Per-profile progress tracking (skip already-downloaded)
- Download archive in SQLite
"""
import time
import json
import logging
from pathlib import Path
from urllib.parse import urlparse

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options
import requests
from requests.adapters import HTTPAdapter, Retry

from cookies import load_cookies
from progress import ProgressTracker
from gdl_compat import load_gallery_dl_config, extract_cookie_source

log = logging.getLogger(__name__)

SESSION_CACHE = Path(".sessions")
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


def _profile_key(platform: str, url: str) -> str:
    """Stable key for a profile, used in progress DB."""
    return f"{platform}:{url.rstrip('/')}"


# -- Session management --

def _get_cookies(platform: str, cookie_file: str | None = None,
                 browser: str | None = None) -> dict[str, str]:
    """Get cookies with caching across runs.
    Falls back to gallery-dl config if no explicit source given."""
    domain = "linkedin.com" if platform == "linkedin" else "twitter.com"

    # If no explicit source, try gallery-dl config
    if not cookie_file and not browser:
        gdl_cfg = load_gallery_dl_config()
        if gdl_cfg:
            gdl_browser, gdl_cookie_file = extract_cookie_source(gdl_cfg, platform)
            if gdl_browser:
                browser = gdl_browser
                log.info("Using browser '%s' from gallery-dl config", browser)
            if gdl_cookie_file:
                cookie_file = gdl_cookie_file
                log.info("Using cookie file '%s' from gallery-dl config", cookie_file)

    # Check cache first
    cache = SESSION_CACHE / f"{platform}.json"
    if cache.exists():
        try:
            cached = json.loads(cache.read_text())
            if cached.get("expires", 0) > time.time():
                log.info("Using cached session for %s", platform)
                return cached["cookies"]
        except Exception:
            pass

    cookies = load_cookies(domain, cookie_file=cookie_file, browser=browser)
    if cookies:
        _cache_session(platform, cookies)
    return cookies


def _cache_session(platform: str, cookies: dict, ttl: int = 3600):
    SESSION_CACHE.mkdir(exist_ok=True)
    (SESSION_CACHE / f"{platform}.json").write_text(
        json.dumps({"cookies": cookies, "expires": time.time() + ttl}))


def _make_session(cookies: dict) -> requests.Session:
    s = requests.Session()
    retries = Retry(total=4, backoff_factor=1.5,
                    status_forcelist=[429, 500, 502, 503, 504])
    s.mount("https://", HTTPAdapter(max_retries=retries))
    s.headers["User-Agent"] = _UA
    for k, v in cookies.items():
        s.cookies.set(k, v)
    return s


def _verify_session(session: requests.Session, platform: str) -> bool:
    try:
        if platform == "linkedin":
            r = session.get("https://www.linkedin.com/feed/",
                            timeout=10, allow_redirects=False)
        else:
            r = session.get("https://x.com/home",
                            timeout=10, allow_redirects=False)
        return r.status_code == 200
    except Exception:
        return False


# -- Selenium --

def _make_driver(headless: bool = True) -> webdriver.Chrome:
    opts = Options()
    if headless:
        opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument(f"--user-agent={_UA}")
    return webdriver.Chrome(options=opts)


def _inject_cookies(driver: webdriver.Chrome, cookies: dict, domain: str):
    driver.get(f"https://{domain}")
    for name, value in cookies.items():
        try:
            driver.add_cookie({"name": name, "value": value, "domain": f".{domain}"})
        except Exception:
            pass


def _scroll_and_collect(driver: webdriver.Chrome, url: str,
                        scroll_count: int, scroll_delay: float,
                        min_size: int = 200) -> list[dict]:
    """Scroll page and collect image URLs with timestamps.
    Returns list of {url, ts} dicts, newest first."""
    driver.get(url)
    time.sleep(3)
    seen = set()
    results = []

    for scroll_i in range(scroll_count):
        imgs = driver.find_elements(By.TAG_NAME, "img")
        for img in imgs:
            src = img.get_attribute("src") or ""
            if not src.startswith("http") or src in seen:
                continue
            nat_w = driver.execute_script("return arguments[0].naturalWidth", img)
            nat_h = driver.execute_script("return arguments[0].naturalHeight", img)
            if nat_w and nat_h and int(nat_w) > min_size and int(nat_h) > min_size:
                seen.add(src)
                # Use scroll position as rough ordering proxy (earlier = newer)
                results.append({"url": src, "ts": time.time(), "order": len(results)})

        driver.execute_script("window.scrollBy(0, window.innerHeight);")
        time.sleep(scroll_delay)

    return results


# -- Main entry point --

def scrape_profile(platform: str, url: str, download_dir: Path,
                   headless: bool = True, scroll_count: int = 5,
                   scroll_delay: float = 2.0,
                   cookie_file: str | None = None,
                   browser: str | None = None,
                   tracker: ProgressTracker | None = None,
                   full_rescan: bool = False) -> list[Path]:
    """Scrape images from a profile URL.

    Args:
        tracker: If provided, skips already-downloaded URLs and records new ones.
        full_rescan: If True, ignores progress and re-downloads everything.

    Returns list of downloaded file paths.
    """
    domain = _resolve_domain(platform, url)
    pkey = _profile_key(platform, url)

    # Get and validate cookies
    cookies = _get_cookies(platform, cookie_file, browser)
    if not cookies:
        log.warning("No cookies found for %s — log in via your browser first.", platform)

    session = _make_session(cookies)
    if cookies and not _verify_session(session, platform):
        log.warning("Session for %s appears expired. Retrying...", platform)
        cache = SESSION_CACHE / f"{platform}.json"
        cache.unlink(missing_ok=True)
        cookies = _get_cookies(platform, cookie_file, browser)
        session = _make_session(cookies)

    # Scrape with Selenium
    driver = _make_driver(headless)
    downloaded = []
    try:
        if cookies:
            _inject_cookies(driver, cookies, domain)

        items = _scroll_and_collect(driver, url, scroll_count, scroll_delay)
        log.info("Found %d candidate images on %s", len(items), url)

        # Filter already-known URLs unless full rescan
        if tracker and not full_rescan:
            before = len(items)
            items = [it for it in items if not tracker.is_known(pkey, it["url"])]
            skipped = before - len(items)
            if skipped:
                log.info("Skipping %d already-downloaded images", skipped)

        download_dir.mkdir(parents=True, exist_ok=True)
        for i, item in enumerate(items):
            img_url = item["url"]
            try:
                resp = session.get(img_url, timeout=15)
                resp.raise_for_status()
            except requests.exceptions.HTTPError as e:
                if e.response and e.response.status_code == 429:
                    wait = int(e.response.headers.get("Retry-After", 60))
                    log.warning("Rate limited. Waiting %ds...", wait)
                    time.sleep(wait)
                    try:
                        resp = session.get(img_url, timeout=15)
                        resp.raise_for_status()
                    except Exception:
                        log.warning("Failed after rate limit wait: %s", img_url)
                        continue
                else:
                    log.warning("Failed to download %s: %s", img_url, e)
                    continue
            except Exception as e:
                log.warning("Failed to download %s: %s", img_url, e)
                continue

            ext = _guess_ext(resp.headers.get("content-type", ""), img_url)
            fname = download_dir / f"img_{i:04d}{ext}"
            fname.write_bytes(resp.content)
            downloaded.append(fname)

            if tracker:
                tracker.record(pkey, img_url, item["ts"])

    finally:
        driver.quit()

    return downloaded


def _resolve_domain(platform: str, url: str) -> str:
    if platform == "linkedin":
        return "linkedin.com"
    return "x.com" if "x.com" in url else "twitter.com"


# -- Following discovery --

def discover_following(platform: str, headless: bool = True,
                       cookie_file: str | None = None,
                       browser: str | None = None,
                       scroll_count: int = 10) -> list[dict]:
    """Discover profiles the authenticated user follows.

    Returns list of {"platform": ..., "url": ...} dicts suitable for config.
    """
    cookies = _get_cookies(platform, cookie_file, browser)
    if not cookies:
        log.error("Cannot discover following without cookies for %s", platform)
        return []

    domain = "linkedin.com" if platform == "linkedin" else "x.com"
    driver = _make_driver(headless)
    profiles = []

    try:
        _inject_cookies(driver, cookies, domain)

        if platform == "twitter":
            profiles = _discover_twitter_following(driver, scroll_count)
        elif platform == "linkedin":
            profiles = _discover_linkedin_following(driver, scroll_count)
    except Exception as e:
        log.error("Failed to discover following on %s: %s", platform, e)
    finally:
        driver.quit()

    log.info("Discovered %d followed profiles on %s", len(profiles), platform)
    return profiles


def _discover_twitter_following(driver: webdriver.Chrome,
                                scroll_count: int) -> list[dict]:
    # First get our own screen name
    driver.get("https://x.com/home")
    time.sleep(3)

    # Navigate to following page via the profile link
    try:
        # Try to find the profile link to get username
        profile_links = driver.find_elements(
            By.CSS_SELECTOR, 'a[data-testid="AppTabBar_Profile_Link"]')
        if profile_links:
            href = profile_links[0].get_attribute("href")  # https://x.com/username
            username = href.rstrip("/").split("/")[-1]
        else:
            # Fallback: look at any link that looks like a profile
            username = driver.execute_script(
                "return document.querySelector('[data-testid=\"UserName\"] a')?.href?.split('/').pop()")
            if not username:
                log.error("Could not determine Twitter username")
                return []
    except Exception:
        log.error("Could not determine Twitter username")
        return []

    driver.get(f"https://x.com/{username}/following")
    time.sleep(3)

    seen = set()
    profiles = []
    for _ in range(scroll_count):
        links = driver.find_elements(By.CSS_SELECTOR,
            'div[data-testid="UserCell"] a[role="link"]')
        for link in links:
            href = link.get_attribute("href") or ""
            # Filter to profile links like https://x.com/username (no /following etc)
            parts = href.rstrip("/").split("/")
            if len(parts) == 4 and parts[2] == "x.com":
                name = parts[3]
                if name not in seen and name != username:
                    seen.add(name)
                    profiles.append({
                        "platform": "twitter",
                        "url": f"https://x.com/{name}/media"
                    })
        driver.execute_script("window.scrollBy(0, window.innerHeight);")
        time.sleep(1.5)

    return profiles


def _discover_linkedin_following(driver: webdriver.Chrome,
                                 scroll_count: int) -> list[dict]:
    driver.get("https://www.linkedin.com/mynetwork/network-manager/people-follow/following/")
    time.sleep(3)

    seen = set()
    profiles = []
    for _ in range(scroll_count):
        links = driver.find_elements(By.CSS_SELECTOR,
            'a.mn-connection-card__link, a.ember-view[href*="/in/"]')
        for link in links:
            href = link.get_attribute("href") or ""
            if "/in/" in href:
                # Normalize to just the profile slug
                slug = href.split("/in/")[1].rstrip("/").split("?")[0]
                if slug and slug not in seen:
                    seen.add(slug)
                    profiles.append({
                        "platform": "linkedin",
                        "url": f"https://www.linkedin.com/in/{slug}/recent-activity/shares/"
                    })
        driver.execute_script("window.scrollBy(0, window.innerHeight);")
        time.sleep(1.5)

    return profiles


def _guess_ext(content_type: str, url: str) -> str:
    ct_map = {"image/png": ".png", "image/jpeg": ".jpg",
              "image/webp": ".webp", "image/gif": ".gif"}
    for ct, ext in ct_map.items():
        if ct in content_type:
            return ext
    path = urlparse(url).path.lower()
    for ext in (".png", ".jpg", ".jpeg", ".webp", ".gif"):
        if path.endswith(ext):
            return ext
    return ".jpg"
