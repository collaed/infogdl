"""LinkedIn Voyager API client — uses the linkedin-api library.

Direct HTTP to LinkedIn's internal API. No Selenium needed.
Requires li_at and JSESSIONID cookies from a logged-in browser session.
"""
import time
import random
import logging
from linkedin_api import Linkedin

log = logging.getLogger(__name__)


def create_client(cookies: dict[str, str]) -> Linkedin | None:
    """Create a LinkedIn API client from browser cookies."""
    li_at = cookies.get("li_at", "")
    jsessionid = cookies.get("JSESSIONID", "").strip('"')

    if not li_at:
        log.error("Missing 'li_at' cookie — are you logged into LinkedIn?")
        return None

    try:
        api = Linkedin("", "", cookies={"li_at": li_at, "JSESSIONID": jsessionid})
        log.info("✓ LinkedIn API client initialized")
        return api
    except Exception as e:
        log.error("Failed to create LinkedIn client: %s", e)
        return None


def get_profile_media(api: Linkedin, public_id: str,
                      post_count: int = 50) -> list[dict]:
    """Fetch image URLs from a LinkedIn profile's posts.

    Returns list of {"url": ..., "post_urn": ..., "ts": ...} dicts.
    """
    log.info("📋 Fetching posts for linkedin.com/in/%s", public_id)

    try:
        posts = api.get_profile_posts(public_id=public_id, post_count=post_count)
    except Exception as e:
        log.warning("Failed to get posts for %s: %s", public_id, e)
        return []

    media_items = []
    for post in posts:
        try:
            images = _extract_images(post)
            post_urn = post.get("updateMetadata", {}).get("urn", "")
            for url in images:
                media_items.append({
                    "url": url,
                    "post_urn": post_urn,
                    "ts": time.time(),
                })
        except Exception:
            continue

    log.info("  Found %d images across %d posts for %s",
             len(media_items), len(posts), public_id)

    # Courtesy delay
    time.sleep(random.uniform(1.0, 2.0))
    return media_items


def get_following(api: Linkedin) -> list[str]:
    """Get public IDs of profiles the user follows.
    Note: linkedin-api doesn't have a direct 'following' endpoint,
    so we use connections as a proxy."""
    try:
        connections = api.get_profile_connections(limit=500)
        return [c.get("public_id") for c in connections
                if c.get("public_id")]
    except Exception as e:
        log.warning("Failed to get connections: %s", e)
        return []


def _extract_images(post: dict) -> list[str]:
    """Extract image URLs from a LinkedIn post object."""
    urls = []

    # Navigate the post content structure
    content = post.get("content", {})
    if not content:
        # Try alternate structure
        value = post.get("value", {})
        content = value.get("com.linkedin.voyager.feed.render.UpdateV2", {})

    # Images in post content
    for key in ("images", "articleComponent", "carouselContent"):
        items = content.get(key, [])
        if isinstance(items, dict):
            items = items.get("images", [items])
        if isinstance(items, list):
            for item in items:
                _collect_image_urls(item, urls)

    # Direct image content
    _collect_image_urls(content, urls)

    # Check reshared content
    reshared = content.get("resharedContent", {})
    if reshared:
        _collect_image_urls(reshared, urls)

    return urls


def _collect_image_urls(obj: dict, urls: list):
    """Recursively find image URLs in a nested dict."""
    if not isinstance(obj, dict):
        return

    # Direct URL fields
    for key in ("url", "originalUrl", "digitalmediaAsset",
                "fileIdentifyingUrlPathSegment"):
        val = obj.get(key, "")
        if isinstance(val, str) and val.startswith("http") and _is_image_url(val):
            if val not in urls:
                urls.append(val)

    # Nested image attributes
    for key in ("attributes", "vectorImage", "rootUrl", "artifacts"):
        val = obj.get(key)
        if isinstance(val, list):
            for item in val:
                _collect_image_urls(item, urls)
        elif isinstance(val, dict):
            _collect_image_urls(val, urls)
        elif isinstance(val, str) and val.startswith("http"):
            if val not in urls:
                urls.append(val)

    # Build full URL from rootUrl + artifacts
    root = obj.get("rootUrl", "")
    artifacts = obj.get("artifacts", [])
    if root and artifacts:
        # Pick the largest artifact
        best = max(artifacts, key=lambda a: a.get("width", 0) * a.get("height", 0),
                   default=None)
        if best:
            segment = best.get("fileIdentifyingUrlPathSegment", "")
            if segment:
                full = root + segment
                if full not in urls:
                    urls.append(full)


def _is_image_url(url: str) -> bool:
    """Quick check if URL looks like an image."""
    lower = url.lower()
    return any(ext in lower for ext in
               (".jpg", ".jpeg", ".png", ".webp", ".gif",
                "media.licdn.com", "dms/image"))
