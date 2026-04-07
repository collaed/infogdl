"""Scrape images from LinkedIn and Twitter profiles using Selenium."""
import time
import logging
from pathlib import Path
from urllib.parse import urlparse
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options
import browser_cookie3
import requests

log = logging.getLogger(__name__)


def _get_cookies(platform: str) -> dict:
    """Try to load browser cookies for the platform domain."""
    domain = "linkedin.com" if platform == "linkedin" else "twitter.com"
    for loader in (browser_cookie3.chrome, browser_cookie3.firefox):
        try:
            cj = loader(domain_name=f".{domain}")
            return {c.name: c.value for c in cj if domain in c.domain}
        except Exception:
            continue
    return {}


def _make_driver(headless: bool = True) -> webdriver.Chrome:
    opts = Options()
    if headless:
        opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    return webdriver.Chrome(options=opts)


def _inject_cookies(driver: webdriver.Chrome, cookies: dict, domain: str):
    driver.get(f"https://{domain}")
    for name, value in cookies.items():
        driver.add_cookie({"name": name, "value": value, "domain": f".{domain}"})


def _scroll_and_collect_images(driver: webdriver.Chrome, url: str,
                                scroll_count: int, scroll_delay: float) -> list[str]:
    driver.get(url)
    time.sleep(3)
    img_urls = set()

    for _ in range(scroll_count):
        imgs = driver.find_elements(By.TAG_NAME, "img")
        for img in imgs:
            src = img.get_attribute("src") or ""
            # Filter for likely infographic images (skip tiny icons/avatars)
            w = img.get_attribute("width")
            h = img.get_attribute("height")
            nat_w = driver.execute_script("return arguments[0].naturalWidth", img)
            nat_h = driver.execute_script("return arguments[0].naturalHeight", img)
            if nat_w and nat_h and int(nat_w) > 200 and int(nat_h) > 200:
                if src.startswith("http"):
                    img_urls.add(src)
        driver.execute_script("window.scrollBy(0, window.innerHeight);")
        time.sleep(scroll_delay)

    return list(img_urls)


def scrape_profile(platform: str, url: str, download_dir: Path,
                   headless: bool = True, scroll_count: int = 5,
                   scroll_delay: float = 2.0) -> list[Path]:
    """Scrape images from a profile URL. Returns list of downloaded file paths."""
    domain = "linkedin.com" if platform == "linkedin" else ("x.com" if "x.com" in url else "twitter.com")
    cookies = _get_cookies(platform)
    if not cookies:
        log.warning(f"No browser cookies found for {platform}. Login may be required.")

    driver = _make_driver(headless)
    downloaded = []
    try:
        if cookies:
            _inject_cookies(driver, cookies, domain)

        img_urls = _scroll_and_collect_images(driver, url, scroll_count, scroll_delay)
        log.info(f"Found {len(img_urls)} images on {url}")

        # Download with a requests session carrying the same cookies
        session = requests.Session()
        for k, v in cookies.items():
            session.cookies.set(k, v)

        download_dir.mkdir(parents=True, exist_ok=True)
        for i, img_url in enumerate(img_urls):
            try:
                resp = session.get(img_url, timeout=15)
                resp.raise_for_status()
                ext = _guess_ext(resp.headers.get("content-type", ""), img_url)
                fname = download_dir / f"img_{i:04d}{ext}"
                fname.write_bytes(resp.content)
                downloaded.append(fname)
            except Exception as e:
                log.warning(f"Failed to download {img_url}: {e}")
    finally:
        driver.quit()

    return downloaded


def _guess_ext(content_type: str, url: str) -> str:
    ct_map = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp", "image/gif": ".gif"}
    for ct, ext in ct_map.items():
        if ct in content_type:
            return ext
    path = urlparse(url).path.lower()
    for ext in (".png", ".jpg", ".jpeg", ".webp", ".gif"):
        if path.endswith(ext):
            return ext
    return ".jpg"
