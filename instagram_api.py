"""Instagram GraphQL API client — cookie-based, no official API needed.

Uses the same internal endpoints as the Instagram web app.
Requires sessionid cookie from a logged-in browser session.
"""
import json
import time
import random
import logging
import requests
from requests.adapters import HTTPAdapter, Retry
from useragent import get_session_ua

log = logging.getLogger(__name__)


class InstagramAPI:
    """Instagram GraphQL API client using browser cookies."""

    def __init__(self, cookies: dict[str, str]):
        self.session = requests.Session()
        retries = Retry(total=3, backoff_factor=2.0, status_forcelist=[500, 502, 503])
        self.session.mount("https://", HTTPAdapter(max_retries=retries))

        for k, v in cookies.items():
            self.session.cookies.set(k, v, domain=".instagram.com")

        csrf = cookies.get("csrftoken", "")
        self.session.headers.update({
            "User-Agent": get_session_ua(),
            "X-CSRFToken": csrf,
            "X-IG-App-ID": "936619743392459",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": "https://www.instagram.com/",
        })

    def user_media(self, username: str, count: int = 50) -> list[dict]:
        """Fetch media posts for a user. Returns list of {url, post_id, ts} dicts."""
        user_id = self._get_user_id(username)
        if not user_id:
            return []

        log.info("📷 Fetching Instagram media for @%s (id: %s)", username, user_id)

        all_media = []
        end_cursor = None
        batch = min(count, 12)

        while len(all_media) < count:
            variables = {
                "id": user_id,
                "first": batch,
            }
            if end_cursor:
                variables["after"] = end_cursor

            params = {
                "query_hash": "e769aa130647d2571c27c44596cb68bd",
                "variables": json.dumps(variables),
            }

            try:
                resp = self.session.get(
                    "https://www.instagram.com/graphql/query/",
                    params=params, timeout=15)

                if resp.status_code == 429:
                    wait = 60
                    t = time.localtime(time.time() + wait)
                    log.warning("⏳ Instagram rate limited — waiting %ds until %02d:%02d:%02d",
                                wait, t.tm_hour, t.tm_min, t.tm_sec)
                    time.sleep(wait)
                    continue

                resp.raise_for_status()
                data = resp.json()
            except Exception as e:
                log.warning("Instagram API error: %s", e)
                break

            try:
                media = data["data"]["user"]["edge_owner_to_timeline_media"]
                edges = media.get("edges", [])
                page_info = media.get("page_info", {})
            except (KeyError, TypeError):
                # Try alternate response structure
                try:
                    media = data["data"]["xdt_api__v1__feed__user_timeline_graphql_connection"]
                    edges = media.get("edges", [])
                    page_info = media.get("page_info", {})
                except (KeyError, TypeError):
                    log.debug("Unexpected Instagram response: %s", json.dumps(data)[:500])
                    break

            for edge in edges:
                node = edge.get("node", edge)
                urls = _extract_ig_media(node)
                post_id = node.get("id", node.get("pk", ""))
                for url in urls:
                    all_media.append({
                        "url": url,
                        "post_id": str(post_id),
                        "ts": time.time(),
                        "caption": (node.get("edge_media_to_caption", {})
                                    .get("edges", [{}])[0]
                                    .get("node", {}).get("text", "")),
                    })

            log.info("  API page: %d posts, %d total images",
                     len(edges), len(all_media))

            if not page_info.get("has_next_page"):
                break
            end_cursor = page_info.get("end_cursor")
            time.sleep(random.uniform(2.0, 4.0))  # IG is strict on rate limits

        return all_media

    def _get_user_id(self, username: str) -> str | None:
        try:
            resp = self.session.get(
                f"https://www.instagram.com/api/v1/users/web_profile_info/",
                params={"username": username}, timeout=10)
            resp.raise_for_status()
            return resp.json()["data"]["user"]["id"]
        except Exception as e:
            log.warning("Failed to get Instagram user ID for %s: %s", username, e)
            return None


def _extract_ig_media(node: dict) -> list[str]:
    """Extract image URLs from an Instagram post node."""
    urls = []
    typename = node.get("__typename", "")

    if typename == "GraphSidecar" or "edge_sidecar_to_children" in node:
        # Carousel post
        children = (node.get("edge_sidecar_to_children", {})
                    .get("edges", []))
        for child in children:
            child_node = child.get("node", child)
            url = child_node.get("display_url", "")
            if url:
                urls.append(url)
    else:
        # Single image/video
        url = node.get("display_url", "")
        if url:
            urls.append(url)

    # Also check image_versions2 (newer API format)
    versions = node.get("image_versions2", {}).get("candidates", [])
    if versions:
        best = max(versions, key=lambda v: v.get("width", 0) * v.get("height", 0))
        url = best.get("url", "")
        if url and url not in urls:
            urls.append(url)

    return urls
