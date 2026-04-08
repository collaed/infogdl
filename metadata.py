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
