"""LinkedIn Voyager API client — direct HTTP, no third-party library.

Uses the same internal API endpoints as LinkedIn's web app.
Requires li_at and JSESSIONID cookies from a logged-in browser session.
"""
import re
import json
import time
import random
import logging
import requests
from requests.adapters import HTTPAdapter, Retry

log = logging.getLogger(__name__)


class LinkedInAPI:
    """Direct LinkedIn Voyager API client."""

    def __init__(self, cookies: dict[str, str]):
        li_at = cookies.get("li_at", "")
        jsid = cookies.get("JSESSIONID", "").strip('"')

        if not li_at:
            raise ValueError("Missing li_at cookie")

        self.session = requests.Session()
        retries = Retry(total=3, backoff_factor=2.0, status_forcelist=[500, 502, 503])
        self.session.mount("https://", HTTPAdapter(max_retries=retries))
        self.session.cookies.set("li_at", li_at, domain=".linkedin.com")
        self.session.cookies.set("JSESSIONID", jsid, domain=".linkedin.com")
        self.session.headers.update({
            "csrf-token": jsid,
            "User-Agent": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
            "x-restli-protocol-version": "2.0.0",
        })
        self._root = "https://www.linkedin.com/voyager/api"

    def _get_profile_urn(self, public_id: str) -> str | None:
        r = self.session.get(
            f"{self._root}/identity/dash/profiles",
            params={"q": "memberIdentity", "memberIdentity": public_id},
            timeout=15)
        if r.status_code != 200:
            log.warning("Profile lookup failed for %s: HTTP %d", public_id, r.status_code)
            return None
        elements = r.json().get("elements", [])
        if not elements:
            return None
        return elements[0].get("entityUrn")

    def get_profile_media(self, public_id: str, post_count: int = 50) -> list[dict]:
        """Fetch image URLs from a profile's posts."""
        log.info("📋 Fetching posts for linkedin.com/in/%s", public_id)

        urn = self._get_profile_urn(public_id)
        if not urn:
            log.warning("Could not find profile URN for %s", public_id)
            return []

        all_media = []
        start = 0
        batch = min(post_count, 10)

        while start < post_count:
            r = self.session.get(
                f"{self._root}/identity/profileUpdatesV2",
                params={
                    "q": "memberShareFeed",
                    "moduleKey": "member-shares:phone",
                    "count": batch,
                    "start": start,
                    "profileUrn": urn,
                    "includeLongTermHistory": True,
                },
                timeout=15)

            if r.status_code == 429:
                wait = int(r.headers.get("Retry-After", 60))
                t = time.localtime(time.time() + wait)
                log.warning("⏳ LinkedIn rate limited — waiting %ds until %02d:%02d:%02d",
                            wait, t.tm_hour, t.tm_min, t.tm_sec)
                time.sleep(wait)
                continue

            if r.status_code != 200:
                log.warning("Posts fetch failed for %s: HTTP %d", public_id, r.status_code)
                break

            data = r.json()
            elements = data.get("elements", [])
            if not elements:
                break

            for el in elements:
                urls = _extract_image_urls(el)
                for url in urls:
                    all_media.append({"url": url, "ts": time.time()})

            token = data.get("metadata", {}).get("paginationToken", "")
            if not token:
                break
            start += batch
            time.sleep(random.uniform(1.0, 2.0))

        log.info("  Found %d images across posts for %s", len(all_media), public_id)
        return all_media


def _extract_image_urls(post: dict) -> list[str]:
    """Extract content image URLs from a post using recursive JSON traversal."""
    results = []
    seen = set()

    content = post.get("content", {})
    # Search all content component types
    for key, component in content.items():
        _find_image_urls(component, results, seen)

    return results


def _find_image_urls(obj, results: list, seen: set):
    """Recursively find rootUrl+artifacts pairs and build full image URLs."""
    if isinstance(obj, dict):
        if "rootUrl" in obj and "artifacts" in obj:
            root = obj["rootUrl"]
            # Skip profile pics, backgrounds, logos
            if any(s in root for s in ("profile-displayphoto", "profile-displaybackground",
                                        "company-logo")):
                return
            arts = obj["artifacts"]
            if arts:
                best = max(arts, key=lambda a: a.get("width", 0) * a.get("height", 0))
                seg = best.get("fileIdentifyingUrlPathSegment", "")
                if seg:
                    full = root + seg
                    base = full.split("?")[0]
                    if base not in seen:
                        seen.add(base)
                        results.append(full)
        for v in obj.values():
            _find_image_urls(v, results, seen)
    elif isinstance(obj, list):
        for v in obj:
            _find_image_urls(v, results, seen)
