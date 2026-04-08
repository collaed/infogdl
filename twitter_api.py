"""Twitter/X GraphQL API client — ported from gallery-dl's twitter extractor.

Uses the same internal API endpoints and authentication that the X.com
web app uses. Requires an auth_token cookie from a logged-in browser session.
No Selenium needed.
"""
import json
import time
import random
import logging
import requests
from requests.adapters import HTTPAdapter, Retry

log = logging.getLogger(__name__)

_BEARER = ("Bearer AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejR"
           "COuH5E6I8xnZz4puTs%3D1Zv7ttfk8LF81IUq16cHjhLTvJu"
           "4FA33AGWWjCpTnA")

_FEATURES = {
    "rweb_video_screen_enabled": False,
    "profile_label_improvements_pcf_label_in_post_enabled": True,
    "rweb_tipjar_consumption_enabled": True,
    "verified_phone_label_enabled": False,
    "creator_subscriptions_tweet_preview_api_enabled": True,
    "responsive_web_graphql_timeline_navigation_enabled": True,
    "responsive_web_graphql_skip_user_profile_image_extensions_enabled": False,
    "premium_content_api_read_enabled": False,
    "communities_web_enable_tweet_community_results_fetch": True,
    "c9s_tweet_anatomy_moderator_badge_enabled": True,
    "responsive_web_grok_analyze_button_fetch_trends_enabled": False,
    "articles_preview_enabled": True,
    "responsive_web_edit_tweet_api_enabled": True,
    "graphql_is_translatable_rweb_tweet_is_translatable_enabled": True,
    "view_counts_everywhere_api_enabled": True,
    "longform_notetweets_consumption_enabled": True,
    "responsive_web_twitter_article_tweet_consumption_enabled": True,
    "tweet_awards_web_tipping_enabled": False,
    "freedom_of_speech_not_reach_fetch_enabled": True,
    "standardized_nudges_misinfo": True,
    "tweet_with_visibility_results_prefer_gql_limited_actions_policy_enabled": True,
    "longform_notetweets_rich_text_read_enabled": True,
    "longform_notetweets_inline_media_enabled": True,
    "responsive_web_enhance_cards_enabled": False,
}


class TwitterAPI:
    """Minimal Twitter GraphQL API client using browser cookies."""

    def __init__(self, cookies: dict[str, str]):
        self.session = requests.Session()
        retries = Retry(total=3, backoff_factor=2.0,
                        status_forcelist=[500, 502, 503])
        self.session.mount("https://", HTTPAdapter(max_retries=retries))

        # Set cookies
        for k, v in cookies.items():
            self.session.cookies.set(k, v, domain=".x.com")

        csrf = cookies.get("ct0", "")
        if not csrf:
            import secrets
            csrf = secrets.token_hex(16)
            self.session.cookies.set("ct0", csrf, domain=".x.com")

        auth_token = cookies.get("auth_token")

        from useragent import get_session_ua
        self.headers = {
            "Accept": "*/*",
            "Referer": "https://x.com/",
            "content-type": "application/json",
            "x-twitter-auth-type": "OAuth2Session" if auth_token else None,
            "x-csrf-token": csrf,
            "x-twitter-client-language": "en",
            "x-twitter-active-user": "yes",
            "authorization": _BEARER,
            "User-Agent": get_session_ua(),
        }
        self._root = "https://x.com/i/api"

    def _call(self, endpoint: str, params: dict) -> dict:
        url = self._root + endpoint
        resp = self.session.get(url, params=params, headers=self.headers,
                                timeout=20)

        # Update csrf token from response
        if ct0 := resp.cookies.get("ct0"):
            self.headers["x-csrf-token"] = ct0

        if resp.status_code == 429:
            reset = resp.headers.get("x-rate-limit-reset")
            if reset:
                wait = max(int(reset) - time.time(), 30)
            else:
                wait = 60
            t = time.localtime(time.time() + wait)
            log.warning("⏳ X API rate limited — waiting %ds until %02d:%02d:%02d",
                        int(wait), t.tm_hour, t.tm_min, t.tm_sec)
            time.sleep(wait)
            resp = self.session.get(url, params=params, headers=self.headers,
                                    timeout=20)

        resp.raise_for_status()
        return resp.json()

    def user_by_screen_name(self, screen_name: str) -> dict:
        endpoint = "/graphql/ck5KkZ8t5cOmoLssopN99Q/UserByScreenName"
        features = {
            "hidden_profile_subscriptions_enabled": True,
            "responsive_web_graphql_skip_user_profile_image_extensions_enabled": False,
            "responsive_web_graphql_timeline_navigation_enabled": True,
            "subscriptions_verification_info_is_identity_verified_enabled": True,
            "subscriptions_verification_info_verified_since_enabled": True,
        }
        params = {
            "variables": json.dumps({
                "screen_name": screen_name,
                "withGrokTranslatedBio": False,
            }),
            "features": json.dumps(features),
        }
        data = self._call(endpoint, params)
        return data["data"]["user"]["result"]

    def user_media(self, screen_name: str, count: int = 50) -> list[dict]:
        """Fetch media tweets for a user. Returns list of tweet dicts with media URLs."""
        user = self.user_by_screen_name(screen_name)
        user_id = user["rest_id"]

        endpoint = "/graphql/jCRhbOzdgOHp6u9H4g2tEg/UserMedia"
        variables = {
            "userId": user_id,
            "count": count,
            "includePromotedContent": False,
            "withClientEventToken": False,
            "withBirdwatchNotes": False,
            "withVoice": True,
        }
        params = {
            "variables": json.dumps(variables),
            "features": json.dumps(_FEATURES),
            "fieldToggles": json.dumps({"withArticlePlainText": False}),
        }

        all_media = []
        cursor = None

        while True:
            if cursor:
                variables["cursor"] = cursor
                params["variables"] = json.dumps(variables)

            data = self._call(endpoint, params)

            # Navigate the response tree
            try:
                result = data["data"]["user"]["result"]
                tl = (result.get("timeline_v2") or result.get("timeline", {}))
                instructions = tl["timeline"]["instructions"]
            except (KeyError, TypeError):
                log.debug("Unexpected API response structure: %s",
                          json.dumps(data)[:500])
                break

            entries = []
            next_cursor = None
            for instr in instructions:
                if instr.get("type") == "TimelineAddEntries":
                    entries.extend(instr.get("entries", []))
                elif instr.get("type") == "TimelineAddToModule":
                    entries.extend(instr.get("moduleItems", []))

            tweets_found = 0
            for entry in entries:
                eid = entry.get("entryId", "")
                if eid.startswith("cursor-bottom-"):
                    next_cursor = (entry.get("content", {})
                                   .get("value") or
                                   entry.get("content", {})
                                   .get("itemContent", {})
                                   .get("value"))
                    continue

                # Grid entries contain multiple tweets
                if eid.startswith("profile-grid-"):
                    items = entry.get("content", {}).get("items", [])
                    for item in items:
                        tweet = _extract_tweet(item)
                        if tweet:
                            media_urls = _extract_media_urls(tweet)
                            if media_urls:
                                tweet_id = tweet.get("rest_id", "")
                                for url in media_urls:
                                    all_media.append({
                                        "url": url,
                                        "tweet_id": tweet_id,
                                        "ts": time.time(),
                                        "_tweet": tweet,
                                    })
                                tweets_found += 1
                    continue

                # Single tweet entries
                tweet = _extract_tweet(entry)
                if not tweet:
                    continue

                media_urls = _extract_media_urls(tweet)
                if media_urls:
                    tweet_id = tweet.get("rest_id", "")
                    for url in media_urls:
                        all_media.append({
                            "url": url,
                            "tweet_id": tweet_id,
                            "ts": time.time(),
                            "_tweet": tweet,
                        })
                    tweets_found += 1

            log.info("  API page: %d tweets with media, %d total images",
                     tweets_found, len(all_media))

            # Rate-limit courtesy
            time.sleep(random.uniform(1.0, 2.0))

            if not next_cursor or not tweets_found:
                break
            cursor = next_cursor

        return all_media

    def user_following(self, screen_name: str) -> list[str]:
        """Get list of screen_names the user follows."""
        user = self.user_by_screen_name(screen_name)
        user_id = user["rest_id"]

        endpoint = "/graphql/SaWqzw0TFAWMx1nXWjXoaQ/Following"
        variables = {
            "userId": user_id,
            "count": 100,
            "includePromotedContent": False,
        }
        params = {
            "variables": json.dumps(variables),
            "features": json.dumps(_FEATURES),
        }

        following = []
        cursor = None

        while True:
            if cursor:
                variables["cursor"] = cursor
                params["variables"] = json.dumps(variables)

            data = self._call(endpoint, params)
            try:
                instructions = (data["data"]["user"]["result"]["timeline"]
                                ["timeline"]["instructions"])
            except (KeyError, TypeError):
                break

            next_cursor = None
            for instr in instructions:
                if instr.get("type") != "TimelineAddEntries":
                    continue
                for entry in instr.get("entries", []):
                    eid = entry.get("entryId", "")
                    if eid.startswith("user-"):
                        try:
                            u = (entry["content"]["itemContent"]
                                 ["user_results"]["result"])
                            name = u["legacy"]["screen_name"]
                            following.append(name)
                        except (KeyError, TypeError):
                            pass
                    elif eid.startswith("cursor-bottom-"):
                        next_cursor = entry["content"].get("value")

            time.sleep(random.uniform(1.0, 2.0))

            if not next_cursor or next_cursor.startswith(("-1|", "0|")):
                break
            cursor = next_cursor

        return following


def _extract_tweet(entry: dict) -> dict | None:
    """Navigate nested entry structure to find the tweet result."""
    try:
        content = entry.get("content") or entry.get("item", {})
        item = content.get("itemContent", content)
        result = item["tweet_results"]["result"]
        if "tweet" in result:
            result = result["tweet"]
        return result
    except (KeyError, TypeError):
        return None


def _extract_media_urls(tweet: dict) -> list[str]:
    """Extract image/video URLs from a tweet object."""
    urls = []
    try:
        legacy = tweet.get("legacy", tweet)
        entities = legacy.get("extended_entities") or legacy.get("entities", {})
        for media in entities.get("media", []):
            mtype = media.get("type", "")
            if mtype == "photo":
                url = media.get("media_url_https", "")
                if url:
                    # Get original resolution
                    urls.append(url + "?format=jpg&name=orig")
            elif mtype in ("video", "animated_gif"):
                variants = (media.get("video_info", {})
                            .get("variants", []))
                # Pick highest bitrate mp4
                best = None
                for v in variants:
                    if v.get("content_type") == "video/mp4":
                        if not best or v.get("bitrate", 0) > best.get("bitrate", 0):
                            best = v
                if best:
                    urls.append(best["url"])
    except (KeyError, TypeError):
        pass
    return urls
