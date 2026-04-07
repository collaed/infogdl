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
import random
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
    time.sleep(2)
    injected = 0
    for name, value in cookies.items():
        try:
            driver.add_cookie({"name": name, "value": value, "domain": f".{domain}"})
            injected += 1
        except Exception as e:
            log.debug("Cookie inject failed for %s: %s", name, e)
    log.info("Injected %d/%d cookies for %s", injected, len(cookies), domain)


def _scroll_and_collect(driver: webdriver.Chrome, url: str,
                        scroll_count: int, scroll_delay: float,
                        min_size: int = 200) -> list[dict]:
    """Scroll page and collect image URLs with timestamps."""
    log.info("Loading %s", url)
    driver.get(url)
    time.sleep(4)

    # Log page state for debugging
    title = driver.title
    cur_url = driver.current_url
    log.info("Page loaded: '%s' (url: %s)", title, cur_url)

    # Check if we got redirected to login
    if any(x in cur_url.lower() for x in ["login", "signin", "authwall", "checkpoint"]):
        log.error("❌ Redirected to login page — cookies are not working. "
                   "Make sure you're logged in to this site in your browser.")
        return []

    seen = set()
    results = []

    for scroll_i in range(scroll_count):
        # Count all images on page
        all_imgs = driver.find_elements(By.TAG_NAME, "img")
        new_this_scroll = 0

        for img in all_imgs:
            try:
                src = img.get_attribute("src") or ""
                if not src.startswith("http") or src in seen:
                    continue
                nat_w = driver.execute_script("return arguments[0].naturalWidth", img)
                nat_h = driver.execute_script("return arguments[0].naturalHeight", img)
            except Exception:
                continue
            if nat_w and nat_h and int(nat_w) > min_size and int(nat_h) > min_size:
                seen.add(src)
                results.append({"url": src, "ts": time.time(), "order": len(results)})
                new_this_scroll += 1
            elif nat_w and nat_h:
                log.debug("Skipped small image %dx%d: %s", nat_w, nat_h, src[:80])

        log.info("Scroll %d/%d: %d images on page, %d new candidates (total: %d)",
                 scroll_i + 1, scroll_count, len(all_imgs), new_this_scroll, len(results))

        driver.execute_script("window.scrollBy(0, window.innerHeight);")
        time.sleep(scroll_delay)

    if not results:
        # Dump page info for debugging
        body_len = len(driver.page_source or "")
        img_count = len(driver.find_elements(By.TAG_NAME, "img"))
        log.warning("⚠ No candidate images found. Page has %d bytes, %d <img> tags. "
                    "This may mean: cookies expired, page layout changed, or no images >%dpx.",
                    body_len, img_count, min_size)

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
                    # Exponential backoff: 60s, 120s, 240s (gallery-dl style)
                    base_wait = int(e.response.headers.get("Retry-After", 60))
                    wait = base_wait * (2 ** min(getattr(scrape_profile, '_429_count', 0), 3))
                    scrape_profile._429_count = getattr(scrape_profile, '_429_count', 0) + 1
                    until = time.time() + wait
                    t = time.localtime(until)
                    log.warning("⏳ Rate limited by %s — backing off %ds until %02d:%02d:%02d",
                                platform, wait, t.tm_hour, t.tm_min, t.tm_sec)
                    time.sleep(wait)
                    try:
                        resp = session.get(img_url, timeout=15)
                        resp.raise_for_status()
                        scrape_profile._429_count = 0  # reset on success
                    except Exception:
                        log.warning("Failed after rate limit wait: %s", img_url)
                        continue
                elif e.response and e.response.status_code in (401, 403):
                    log.warning("⛔ %s blocked access (HTTP %d) — stopping this profile",
                                platform, e.response.status_code)
                    break
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
            size_kb = len(resp.content) / 1024
            log.info("📥 [%s] %d/%d  %s  (%.0f KB)",
                     platform, i + 1, len(items), fname.name, size_kb)

            if tracker:
                tracker.record(pkey, img_url, item["ts"])

            # Per-request throttle (gallery-dl style: 0.5-1.5s between downloads)
            time.sleep(random.uniform(0.5, 1.5))

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
    # Get username: navigate to profile settings which redirects to /username
    driver.get("https://x.com/settings/profile")
    time.sleep(3)

    username = None
    # Method 1: extract from current URL after redirect
    for _ in range(5):
        url = driver.current_url
        # Settings page URL contains username in some cases, or check page source
        username = driver.execute_script("""
            // Try multiple known locations for screen_name
            var el = document.querySelector('a[href$="/following"]');
            if (el) return el.href.split('/').slice(-2)[0];
            el = document.querySelector('a[data-testid="AppTabBar_Profile_Link"]');
            if (el) return el.href.split('/').pop();
            // Try the <link rel="canonical"> or meta tags
            var links = document.querySelectorAll('link[rel="canonical"]');
            for (var l of links) { var m = l.href.match(/x\\.com\\/([^/]+)/); if (m) return m[1]; }
            return null;
        """)
        if username:
            break
        time.sleep(1)

    # Method 2: go to /home and scrape from sidebar
    if not username:
        driver.get("https://x.com/home")
        time.sleep(3)
        username = driver.execute_script("""
            // Sidebar nav profile link
            var links = document.querySelectorAll('nav a[href]');
            for (var a of links) {
                var m = a.href.match(/x\\.com\\/([A-Za-z0-9_]+)$/);
                if (m && !['home','explore','notifications','messages','settings','i'].includes(m[1]))
                    return m[1];
            }
            return null;
        """)

    if not username:
        log.error("Could not determine Twitter username. Are you logged in to Firefox?")
        return []

    log.info("Discovered Twitter username: %s", username)
    driver.get(f"https://x.com/{username}/following")
    time.sleep(3)

    seen = set()
    profiles = []
    for _ in range(scroll_count):
        # Collect all user profile links on the page
        hrefs = driver.execute_script("""
            var urls = new Set();
            document.querySelectorAll('a[href]').forEach(function(a) {
                var m = a.href.match(/^https:\\/\\/x\\.com\\/([A-Za-z0-9_]+)$/);
                if (m) urls.add(m[1]);
            });
            return Array.from(urls);
        """)
        skip = {username, "home", "explore", "notifications", "messages",
                "settings", "i", "search", "compose", "tos", "privacy"}
        for name in hrefs:
            if name not in seen and name.lower() not in skip:
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
    # LinkedIn has multiple pages where followed/connected people appear
    urls_to_try = [
        "https://www.linkedin.com/mynetwork/network-manager/people-follow/following/",
        "https://www.linkedin.com/mynetwork/invite-connect/connections/",
    ]

    seen = set()
    profiles = []

    for page_url in urls_to_try:
        driver.get(page_url)
        time.sleep(4)

        for _ in range(scroll_count):
            # Use JS to grab all /in/ links — works regardless of CSS class changes
            slugs = driver.execute_script("""
                var results = new Set();
                document.querySelectorAll('a[href*="/in/"]').forEach(function(a) {
                    var m = a.href.match(/\\/in\\/([A-Za-z0-9_-]+)/);
                    if (m) results.add(m[1]);
                });
                return Array.from(results);
            """)
            for slug in slugs:
                if slug not in seen:
                    seen.add(slug)
                    profiles.append({
                        "platform": "linkedin",
                        "url": f"https://www.linkedin.com/in/{slug}/recent-activity/shares/"
                    })

            # Try clicking "Show more" button if present
            driver.execute_script("""
                var btn = document.querySelector('button.scaffold-finite-scroll__load-button')
                       || document.querySelector('button[aria-label*="more"]');
                if (btn) btn.click();
            """)
            driver.execute_script("window.scrollBy(0, window.innerHeight);")
            time.sleep(2)

        log.info("Found %d profiles from %s", len(seen), page_url)

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
