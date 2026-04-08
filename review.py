#!/usr/bin/env python3
"""infogdl-review — Visual review and voting tool for downloaded infographics.

Opens a local web UI to browse, sample, and vote on your collection.
Votes update profile list files: upvoted profiles get kept, downvoted get removed.
Profiles with broken handles (0 downloads) are flagged for removal.

Usage:
    python review.py                    # review output/ directory
    python review.py -d /path/to/output # custom directory
    python review.py --port 8899        # custom port
"""
import argparse
import json
import logging
import os
import random
import sys
import threading
import webbrowser
from collections import defaultdict
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlparse, unquote

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)

VOTES_FILE = Path(".infogdl_votes.json")
QUEUE_FILE = Path(".infogdl_queue.json")
PER_PROFILE = 5


def scan_collection(root: Path) -> dict:
    """Scan output directory, group images by profile/subfolder."""
    IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
    collection = defaultdict(list)

    for f in sorted(root.rglob("*")):
        if f.is_file() and f.suffix.lower() in IMAGE_EXTS:
            try:
                rel = f.relative_to(root)
            except ValueError:
                continue
            if str(rel).startswith("_raw"):
                continue

            meta_path = f.with_suffix(".json")
            meta = {}
            if meta_path.exists():
                try:
                    meta = json.loads(meta_path.read_text())
                except Exception:
                    pass

            author = meta.get("author", "unknown")
            platform = meta.get("platform", "unknown")
            key = f"{platform}/{author}"

            collection[key].append({
                "path": str(f),
                "rel": str(rel),
                "name": f.name,
                "size_kb": f.stat().st_size / 1024,
                "meta": meta,
            })

    return dict(collection)


def load_queue() -> dict:
    """Load review queue: {profile_key: [list of rated image paths]}"""
    if QUEUE_FILE.exists():
        try:
            return json.loads(QUEUE_FILE.read_text())
        except Exception:
            pass
    return {"rated": {}}


def save_queue(queue: dict):
    QUEUE_FILE.write_text(json.dumps(queue, indent=2))


def build_review_set(collection: dict, queue: dict) -> list[dict]:
    """Build review set: exactly PER_PROFILE unrated images per profile."""
    rated = queue.get("rated", {})
    review = []
    needs_scrape = []

    for key, images in collection.items():
        rated_paths = set(rated.get(key, []))
        unrated = [img for img in images if img["path"] not in rated_paths]

        if len(unrated) >= PER_PROFILE:
            picked = random.sample(unrated, PER_PROFILE)
        elif unrated:
            picked = unrated
            needs_scrape.append(key)
        else:
            needs_scrape.append(key)
            continue

        for img in picked:
            img["profile_key"] = key
        review.extend(picked)

    if needs_scrape:
        log.info("⚠ %d profiles need more images: %s",
                 len(needs_scrape), ", ".join(needs_scrape))

    return review, needs_scrape


def trigger_scrape(profiles_needing: list[str]):
    """Trigger infogdl scrape for profiles that need more images."""
    if not profiles_needing:
        return

    log.info("🔄 Triggering scrape for %d profiles...", len(profiles_needing))

    # Build profile dicts from keys like "twitter/SahilBloom"
    profiles = []
    for key in profiles_needing:
        parts = key.split("/", 1)
        if len(parts) != 2:
            continue
        platform, author = parts
        if platform == "twitter":
            profiles.append({"platform": "twitter",
                             "url": f"https://x.com/{author}/media"})
        elif platform == "linkedin":
            profiles.append({"platform": "linkedin",
                             "url": f"https://www.linkedin.com/in/{author}/recent-activity/shares/"})

    if not profiles:
        return

    # Run scraper in subprocess to avoid import tangles
    import subprocess
    import tempfile

    tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
    json.dump(profiles, tmp)
    tmp.close()

    try:
        # Quick scrape: just 1 worker, small batch
        subprocess.run([
            sys.executable, "-c",
            f"""
import json, sys
sys.path.insert(0, '.')
from scraper import scrape_profile
from progress import ProgressTracker
tracker = ProgressTracker()
profiles = json.load(open('{tmp.name}'))
for p in profiles:
    try:
        scrape_profile(p['platform'], p['url'],
                       __import__('pathlib').Path('./output/_raw/' + p['platform'] + '_refill'),
                       tracker=tracker, scroll_count=3)
    except Exception as e:
        print(f"Scrape failed for {{p['url']}}: {{e}}")
tracker.close()
"""
        ], timeout=300)
    except Exception as e:
        log.warning("Scrape subprocess failed: %s", e)
    finally:
        os.unlink(tmp.name)


def load_votes() -> dict:
    if VOTES_FILE.exists():
        return json.loads(VOTES_FILE.read_text())
    return {"up": {}, "down": {}, "remove_handles": []}


def save_votes(votes: dict):
    VOTES_FILE.write_text(json.dumps(votes, indent=2))


def apply_votes(votes: dict, threshold_up: int = 3, threshold_down: int = 2):
    """Apply accumulated votes to profile list files."""
    profile_dir = Path("profiles")
    if not profile_dir.is_dir():
        log.warning("No profiles/ directory found")
        return

    files = list(profile_dir.glob("*.txt"))
    if not files:
        return

    # Remove downvoted profiles
    to_remove = set()
    for handle, count in votes.get("down", {}).items():
        if count >= threshold_down:
            to_remove.add(handle.lower())
            log.info("❌ Removing %s (downvoted %d times)", handle, count)

    # Also remove flagged broken handles
    for handle in votes.get("remove_handles", []):
        to_remove.add(handle.lower())
        log.info("❌ Removing broken handle: %s", handle)

    if to_remove:
        for f in files:
            lines = f.read_text().splitlines()
            kept = []
            for line in lines:
                stripped = line.split("#")[0].strip()
                parts = stripped.split(None, 1)
                if len(parts) == 2:
                    handle = parts[1].lstrip("@").split("/")[0].lower()
                    if handle in to_remove:
                        continue
                kept.append(line)
            f.write_text("\n".join(kept) + "\n")

    # Log upvoted (kept) profiles
    for handle, count in votes.get("up", {}).items():
        if count >= threshold_up:
            log.info("⭐ Keeping %s (upvoted %d times)", handle, count)

    # Clear applied votes
    save_votes({"up": {}, "down": {}, "remove_handles": []})
    log.info("Votes applied and reset.")


class ReviewHandler(SimpleHTTPRequestHandler):
    """HTTP handler for the review web UI."""

    collection = {}
    samples = []
    root_dir = Path(".")
    votes = {}
    queue = {}

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/":
            self._serve_html()
        elif path == "/api/samples":
            self._json_response(self.samples)
        elif path == "/api/stats":
            rated_count = sum(len(v) for v in self.queue.get("rated", {}).values())
            stats = {
                "profiles": len(self.collection),
                "total_images": sum(len(v) for v in self.collection.values()),
                "in_review": len(self.samples),
                "rated": rated_count,
                "votes": self.votes,
            }
            self._json_response(stats)
        elif path.startswith("/img/"):
            img_path = unquote(path[5:])
            full = self.root_dir / img_path
            if full.exists():
                self.send_response(200)
                ct = {"png": "image/png", "webp": "image/webp"}.get(
                    full.suffix.lstrip("."), "image/jpeg")
                self.send_header("Content-Type", ct)
                self.end_headers()
                self.wfile.write(full.read_bytes())
            else:
                self.send_error(404)
        elif path == "/api/apply":
            apply_votes(self.votes)
            self._json_response({"status": "ok"})
        else:
            self.send_error(404)

    def do_POST(self):
        parsed = urlparse(self.path)
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length)) if length else {}

        if parsed.path == "/api/vote":
            profile = body.get("profile", "")
            vote = body.get("vote", "")
            img_path = body.get("img_path", "")
            handle = profile.split("/")[-1]

            if vote in ("up", "down"):
                self.votes.setdefault(vote, {})
                self.votes[vote][handle] = self.votes[vote].get(handle, 0) + 1
                save_votes(self.votes)

            # Mark image as rated
            if img_path:
                self.queue.setdefault("rated", {})
                self.queue["rated"].setdefault(profile, [])
                if img_path not in self.queue["rated"][profile]:
                    self.queue["rated"][profile].append(img_path)
                    save_queue(self.queue)

            # Rebuild review set and check if scraping needed
            self.samples, needs = build_review_set(self.collection, self.queue)
            self._json_response({
                "status": "ok",
                "remaining": len(self.samples),
                "needs_scrape": needs,
            })

        elif parsed.path == "/api/remove_handle":
            handle = body.get("handle", "")
            if handle:
                self.votes.setdefault("remove_handles", [])
                if handle not in self.votes["remove_handles"]:
                    self.votes["remove_handles"].append(handle)
                    save_votes(self.votes)
            self._json_response({"status": "ok"})

        elif parsed.path == "/api/refill":
            # Trigger scrape for profiles needing more images
            _, needs = build_review_set(self.collection, self.queue)
            if needs:
                threading.Thread(target=self._refill, args=(needs,),
                                 daemon=True).start()
                self._json_response({"status": "scraping", "profiles": needs})
            else:
                self._json_response({"status": "all_full"})

        elif parsed.path == "/api/resample":
            # Reset queue and resample
            self.queue = {"rated": {}}
            save_queue(self.queue)
            self.samples, _ = build_review_set(self.collection, self.queue)
            self._json_response(self.samples)
        else:
            self.send_error(404)

    def _refill(self, needs):
        """Background scrape to refill profiles."""
        trigger_scrape(needs)
        # Rescan collection after scrape
        ReviewHandler.collection = scan_collection(self.root_dir)
        ReviewHandler.samples, _ = build_review_set(
            self.collection, self.queue)
        log.info("Refill complete. %d images in review.", len(self.samples))

    def _json_response(self, data):
        body = json.dumps(data).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def _serve_html(self):
        html = _HTML_PAGE
        body = html.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass  # Suppress request logs


_HTML_PAGE = """<!DOCTYPE html>
<html><head>
<meta charset="utf-8">
<title>infogdl Review</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, sans-serif; background: #1a1a2e; color: #eee; }
  .header { padding: 20px; text-align: center; background: #16213e; }
  .header h1 { font-size: 1.5em; }
  .stats { color: #888; margin-top: 8px; }
  .controls { padding: 10px; text-align: center; }
  .controls button { padding: 8px 20px; margin: 4px; border: none; border-radius: 6px;
    cursor: pointer; font-size: 14px; }
  .btn-refill { background: #0f3460; color: #fff; }
  .btn-apply { background: #e94560; color: #fff; }
  .btn-reset { background: #333; color: #aaa; }
  .card { background: #16213e; margin: 12px auto; max-width: 900px; border-radius: 10px;
    overflow: hidden; transition: opacity 0.3s; }
  .card img { width: 100%; max-height: 600px; object-fit: contain; background: #000;
    cursor: pointer; }
  .card-info { padding: 12px 16px; display: flex; justify-content: space-between;
    align-items: center; }
  .card-meta { font-size: 13px; color: #aaa; max-width: 60%; }
  .card-meta .author { color: #e94560; font-weight: bold; }
  .vote-btns button { padding: 8px 16px; margin: 0 4px; border: none; border-radius: 6px;
    cursor: pointer; font-size: 18px; }
  .btn-up { background: #2d6a4f; color: #fff; }
  .btn-down { background: #9b2226; color: #fff; }
  .btn-remove { background: #555; color: #fff; font-size: 12px !important; padding: 6px 10px !important; }
  .voted { opacity: 0.15; pointer-events: none; }
  .toast { position: fixed; bottom: 20px; right: 20px; background: #0f3460; color: #fff;
    padding: 12px 20px; border-radius: 8px; display: none; z-index: 99; }
  .badge { display: inline-block; background: #e94560; color: #fff; padding: 2px 8px;
    border-radius: 10px; font-size: 11px; margin-left: 6px; }
</style>
</head><body>
<div class="header">
  <h1>📊 infogdl Collection Review</h1>
  <div class="stats" id="stats">Loading...</div>
</div>
<div class="controls">
  <button class="btn-refill" onclick="refill()">🔄 Scrape to Refill</button>
  <button class="btn-apply" onclick="applyVotes()">✅ Apply Votes</button>
  <button class="btn-reset" onclick="resetQueue()">↩ Reset Queue</button>
</div>
<div id="cards"></div>
<div class="toast" id="toast"></div>

<script>
let samples = [];

async function load() {
  const [statsR, samplesR] = await Promise.all([
    fetch('/api/stats').then(r=>r.json()),
    fetch('/api/samples').then(r=>r.json())
  ]);
  document.getElementById('stats').innerHTML =
    `${statsR.profiles} profiles · ${statsR.total_images} images · `+
    `<span class="badge">${statsR.in_review} to review</span> · `+
    `${statsR.rated} rated`;
  samples = samplesR;
  render();
}

function render() {
  const el = document.getElementById('cards');
  if (!samples.length) {
    el.innerHTML = '<p style="text-align:center;padding:40px;color:#666">'+
      'All images rated! Click 🔄 Scrape to Refill for more.</p>';
    return;
  }
  el.innerHTML = samples.map((s, i) => `
    <div class="card" id="card-${i}">
      <img src="/img/${encodeURIComponent(s.rel)}" loading="lazy"
           onclick="this.style.maxHeight=this.style.maxHeight?'':'none'">
      <div class="card-info">
        <div class="card-meta">
          <span class="author">${s.profile_key}</span><br>
          ${s.name} · ${Math.round(s.size_kb)} KB
          ${s.meta && s.meta.text ? '<br><em>' + s.meta.text.substring(0, 140) + '...</em>' : ''}
        </div>
        <div class="vote-btns">
          <button class="btn-up" onclick="vote(${i},'up')">👍</button>
          <button class="btn-down" onclick="vote(${i},'down')">👎</button>
          <button class="btn-remove" onclick="removeHandle('${s.profile_key}',${i})">🚫</button>
        </div>
      </div>
    </div>
  `).join('');
}

async function vote(idx, dir) {
  const s = samples[idx];
  const r = await fetch('/api/vote', {method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({profile: s.profile_key, vote: dir, img_path: s.path})});
  const data = await r.json();
  document.getElementById('card-'+idx).classList.add('voted');
  toast(dir === 'up' ? '👍 Upvoted' : '👎 Downvoted');
  // Refresh stats
  samples = (await fetch('/api/samples').then(r=>r.json()));
  setTimeout(() => {
    document.getElementById('stats').querySelector('.badge').textContent =
      data.remaining + ' to review';
  }, 500);
}

async function removeHandle(profileKey, idx) {
  const handle = profileKey.split('/').pop();
  if (!confirm('Remove profile ' + handle + '?')) return;
  await fetch('/api/remove_handle', {method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({handle})});
  document.getElementById('card-'+idx).classList.add('voted');
  toast('🚫 Flagged ' + handle + ' for removal');
}

async function refill() {
  toast('🔄 Scraping for more images...');
  const r = await fetch('/api/refill', {method:'POST',
    headers:{'Content-Type':'application/json'}, body:'{}'});
  const data = await r.json();
  if (data.status === 'all_full') {
    toast('All profiles have 5+ unrated images');
  } else {
    toast('Scraping ' + data.profiles.length + ' profiles in background...');
    // Poll for completion
    setTimeout(async () => {
      samples = await fetch('/api/samples').then(r=>r.json());
      render();
      toast('Refill complete!');
    }, 30000);
  }
}

async function resetQueue() {
  if (!confirm('Reset all ratings and start fresh?')) return;
  const r = await fetch('/api/resample', {method:'POST',
    headers:{'Content-Type':'application/json'}, body:'{}'});
  samples = await r.json();
  render();
  load();
  toast('↩ Queue reset');
}

async function applyVotes() {
  if (!confirm('Apply all votes? This will edit your profile list files.')) return;
  await fetch('/api/apply');
  toast('✅ Votes applied to profile lists');
}

function toast(msg) {
  const t = document.getElementById('toast');
  t.textContent = msg; t.style.display = 'block';
  setTimeout(() => t.style.display = 'none', 3000);
}

load();
</script>
</body></html>"""


def main():
    parser = argparse.ArgumentParser(description="Review and vote on infogdl collection")
    parser.add_argument("-d", "--dir", default="./output", help="Output directory to review")
    parser.add_argument("--port", type=int, default=8877, help="Web UI port")
    parser.add_argument("--apply", action="store_true", help="Apply pending votes and exit")
    args = parser.parse_args()

    root = Path(args.dir)
    if not root.is_dir():
        log.error("Directory not found: %s", root)
        sys.exit(1)

    votes = load_votes()
    queue = load_queue()

    if args.apply:
        apply_votes(votes)
        return

    log.info("Scanning %s...", root)
    collection = scan_collection(root)
    log.info("Found %d profiles, %d images",
             len(collection), sum(len(v) for v in collection.values()))

    review_set, needs = build_review_set(collection, queue)
    if needs:
        log.info("⚠ %d profiles have fewer than %d unrated images", len(needs), PER_PROFILE)

    ReviewHandler.collection = collection
    ReviewHandler.samples = review_set
    ReviewHandler.root_dir = root
    ReviewHandler.votes = votes
    ReviewHandler.queue = queue

    server = HTTPServer(("127.0.0.1", args.port), ReviewHandler)
    url = f"http://127.0.0.1:{args.port}"
    log.info("Review UI at %s", url)

    threading.Timer(1.0, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Shutting down")
        server.shutdown()


if __name__ == "__main__":
    main()
