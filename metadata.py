"""Filename templates and metadata sidecar files.

Filename format keys:
  {platform}     - twitter / linkedin
  {author}       - screen name or public ID
  {id}           - tweet ID or post URN
  {num}          - sequential number (zero-padded)
  {date}         - YYYY-MM-DD
  {ext}          - file extension (without dot)
"""
import json
import logging
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

DEFAULT_FMT = "{platform}_{author}_{id}_{num:04d}.{ext}"


def format_filename(fmt: str, **kwargs) -> str:
    """Apply template to generate a filename."""
    try:
        return fmt.format(**kwargs)
    except (KeyError, ValueError):
        # Fallback to safe default
        return "{platform}_{num:04d}.{ext}".format(**kwargs)


def build_metadata(platform: str, author: str, item: dict,
                   img_url: str) -> dict:
    """Build metadata dict from a downloaded item."""
    meta = {
        "platform": platform,
        "author": author,
        "url": img_url,
        "downloaded_at": datetime.utcnow().isoformat() + "Z",
    }

    if platform == "twitter":
        tweet_id = item.get("tweet_id", "")
        meta["tweet_id"] = tweet_id
        # Extract text and engagement from the raw tweet if available
        tweet = item.get("_tweet")
        if tweet:
            legacy = tweet.get("legacy", {})
            meta["text"] = legacy.get("full_text", "")
            meta["retweet_count"] = legacy.get("retweet_count", 0)
            meta["favorite_count"] = legacy.get("favorite_count", 0)
            meta["reply_count"] = legacy.get("reply_count", 0)
            meta["created_at"] = legacy.get("created_at", "")
            try:
                user = tweet["core"]["user_results"]["result"]["legacy"]
                meta["author_name"] = user.get("name", "")
                meta["author_followers"] = user.get("followers_count", 0)
            except (KeyError, TypeError):
                pass

    elif platform == "linkedin":
        meta["post_urn"] = item.get("post_urn", "")

    return meta


def save_sidecar(image_path: Path, metadata: dict):
    """Save metadata as a .json sidecar file next to the image."""
    sidecar = image_path.with_suffix(".json")
    sidecar.write_text(json.dumps(metadata, indent=2, ensure_ascii=False),
                       encoding="utf-8")


def detect_source(image_path: Path) -> dict:
    """Try to detect platform/author from an image's existing metadata.

    Checks (in order):
    1. Existing .json sidecar (from previous processing or manual drop)
    2. Filename patterns (twitter_author_id, li_0001, etc.)
    3. EXIF/XMP metadata (some platforms embed source URLs)
    4. Image URL in PNG/JPEG metadata chunks
    """
    meta = {"platform": "local", "author": "unknown"}

    # 1. Existing sidecar
    sidecar = image_path.with_suffix(".json")
    if sidecar.exists():
        try:
            existing = json.loads(sidecar.read_text())
            if existing.get("author") and existing.get("author") != "unknown":
                return existing
        except Exception:
            pass

    # Also check sidecar with same stem in parent dirs
    for parent_json in image_path.parent.glob(f"{image_path.stem}*.json"):
        try:
            existing = json.loads(parent_json.read_text())
            if existing.get("author") and existing.get("author") != "unknown":
                return existing
        except Exception:
            pass

    name = image_path.stem.lower()

    # 2. Filename patterns
    import re

    # twitter_SahilBloom_2041198315530793441_0000
    m = re.match(r"twitter_([a-z0-9_]+)_(\d{10,})_\d+", name)
    if m:
        meta = {"platform": "twitter", "author": m.group(1),
                "tweet_id": m.group(2)}
        return meta

    # linkedin_chiphuyen_li1234_0000
    m = re.match(r"linkedin_([a-z0-9_-]+)_", name)
    if m:
        meta = {"platform": "linkedin", "author": m.group(1)}
        return meta

    # instagram_username_postid_0000
    m = re.match(r"instagram_([a-z0-9_.]+)_", name)
    if m:
        meta = {"platform": "instagram", "author": m.group(1)}
        return meta

    # li_0001 (old format)
    if name.startswith("li_"):
        meta["platform"] = "linkedin"
        return meta

    # img_0001 (generic scrape)
    if name.startswith("img_"):
        return meta

    # Facebook downloads often have numeric IDs
    m = re.match(r"(\d{10,})_(\d{10,})_", name)
    if m:
        meta["platform"] = "facebook"
        return meta

    # 3. Try reading EXIF/metadata for source URL
    try:
        from PIL import Image as PILImage
        img = PILImage.open(image_path)
        exif = img.info

        # PNG text chunks or JPEG comments sometimes have URLs
        for key in ("Comment", "Description", "Source", "url",
                    "XML:com.adobe.xmp"):
            val = exif.get(key, "")
            if isinstance(val, bytes):
                val = val.decode("utf-8", errors="ignore")
            if "twitter.com" in val or "x.com" in val:
                meta["platform"] = "twitter"
                m = re.search(r"(?:twitter|x)\.com/([A-Za-z0-9_]+)", val)
                if m:
                    meta["author"] = m.group(1)
                break
            elif "linkedin.com" in val:
                meta["platform"] = "linkedin"
                m = re.search(r"/in/([A-Za-z0-9_-]+)", val)
                if m:
                    meta["author"] = m.group(1)
                break
            elif "instagram.com" in val:
                meta["platform"] = "instagram"
                m = re.search(r"instagram\.com/([A-Za-z0-9_.]+)", val)
                if m:
                    meta["author"] = m.group(1)
                break
            elif "facebook.com" in val or "fbcdn" in val:
                meta["platform"] = "facebook"
                break
        img.close()
    except Exception:
        pass

    return meta
