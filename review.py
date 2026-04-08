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


def scan_collection(root: Path) -> dict:
    """Scan output directory, group images by profile/subfolder."""
    IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
    collection = defaultdict(list)

    for f in sorted(root.rglob("*")):
        if f.is_file() and f.suffix.lower() in IMAGE_EXTS:
            # Skip _raw directory
            try:
                rel = f.relative_to(root)
            except ValueError:
                continue
            if str(rel).startswith("_raw"):
                continue

            # Try to read sidecar metadata
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


def sample_for_review(collection: dict, per_profile: int = 3) -> list[dict]:
    """Statistically sample images for review — proportional to collection size."""
    samples = []
    for key, images in collection.items():
        n = min(per_profile, len(images))
        picked = random.sample(images, n)
        for img in picked:
            img["profile_key"] = key
        samples.extend(picked)
    random.shuffle(samples)
    return samples


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

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/":
            self._serve_html()
        elif path == "/api/samples":
            self._json_response(self.samples)
        elif path == "/api/stats":
            stats = {
                "profiles": len(self.collection),
                "total_images": sum(len(v) for v in self.collection.values()),
                "samples": len(self.samples),
                "votes": self.votes,
            }
            self._json_response(stats)
        elif path.startswith("/img/"):
            # Serve image file
            img_path = unquote(path[5:])
            full = self.root_dir / img_path
            if full.exists():
                self.send_response(200)
                ct = "image/jpeg"
                if full.suffix == ".png":
                    ct = "image/png"
                elif full.suffix == ".webp":
                    ct = "image/webp"
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
            vote = body.get("vote", "")  # "up" or "down"
            handle = profile.split("/")[-1]

            if vote in ("up", "down"):
                self.votes.setdefault(vote, {})
                self.votes[vote][handle] = self.votes[vote].get(handle, 0) + 1
                save_votes(self.votes)

            self._json_response({"status": "ok", "votes": self.votes})

        elif parsed.path == "/api/remove_handle":
            handle = body.get("handle", "")
            if handle:
                self.votes.setdefault("remove_handles", [])
                if handle not in self.votes["remove_handles"]:
                    self.votes["remove_handles"].append(handle)
                    save_votes(self.votes)
            self._json_response({"status": "ok"})

        elif parsed.path == "/api/resample":
            ReviewHandler.samples = sample_for_review(self.collection)
            self._json_response(self.samples)
        else:
            self.send_error(404)

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
  .btn-resample { background: #0f3460; color: #fff; }
  .btn-apply { background: #e94560; color: #fff; }
  .card { background: #16213e; margin: 12px auto; max-width: 900px; border-radius: 10px;
    overflow: hidden; }
  .card img { width: 100%; max-height: 600px; object-fit: contain; background: #000; }
  .card-info { padding: 12px 16px; display: flex; justify-content: space-between;
    align-items: center; }
  .card-meta { font-size: 13px; color: #aaa; }
  .card-meta .author { color: #e94560; font-weight: bold; }
  .vote-btns button { padding: 8px 16px; margin: 0 4px; border: none; border-radius: 6px;
    cursor: pointer; font-size: 18px; }
  .btn-up { background: #2d6a4f; color: #fff; }
  .btn-down { background: #9b2226; color: #fff; }
  .btn-remove { background: #555; color: #fff; font-size: 12px !important; padding: 6px 10px !important; }
  .voted { opacity: 0.4; }
  .toast { position: fixed; bottom: 20px; right: 20px; background: #0f3460; color: #fff;
    padding: 12px 20px; border-radius: 8px; display: none; z-index: 99; }
</style>
</head><body>
<div class="header">
  <h1>📊 infogdl Collection Review</h1>
  <div class="stats" id="stats">Loading...</div>
</div>
<div class="controls">
  <button class="btn-resample" onclick="resample()">🔄 New Sample</button>
  <button class="btn-apply" onclick="applyVotes()">✅ Apply Votes to Profile Lists</button>
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
  document.getElementById('stats').textContent =
    `${statsR.profiles} profiles · ${statsR.total_images} images · showing ${statsR.samples} samples`;
  samples = samplesR;
  render();
}

function render() {
  const el = document.getElementById('cards');
  el.innerHTML = samples.map((s, i) => `
    <div class="card" id="card-${i}">
      <img src="/img/${encodeURIComponent(s.rel)}" loading="lazy">
      <div class="card-info">
        <div class="card-meta">
          <span class="author">${s.profile_key}</span><br>
          ${s.name} · ${Math.round(s.size_kb)} KB
          ${s.meta.text ? '<br>' + s.meta.text.substring(0, 120) + '...' : ''}
        </div>
        <div class="vote-btns">
          <button class="btn-up" onclick="vote(${i},'up')">👍</button>
          <button class="btn-down" onclick="vote(${i},'down')">👎</button>
          <button class="btn-remove" onclick="removeHandle('${s.profile_key}')">🚫 Remove Profile</button>
        </div>
      </div>
    </div>
  `).join('');
}

async function vote(idx, dir) {
  const s = samples[idx];
  await fetch('/api/vote', {method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({profile: s.profile_key, vote: dir})});
  document.getElementById('card-'+idx).classList.add('voted');
  toast(dir === 'up' ? '👍 Upvoted' : '👎 Downvoted');
}

async function removeHandle(profileKey) {
  const handle = profileKey.split('/').pop();
  await fetch('/api/remove_handle', {method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({handle})});
  toast('🚫 Flagged ' + handle + ' for removal');
}

async function resample() {
  const r = await fetch('/api/resample', {method:'POST',
    headers:{'Content-Type':'application/json'}, body:'{}'});
  samples = await r.json();
  render();
  toast('🔄 New sample loaded');
}

async function applyVotes() {
  if (!confirm('Apply all votes? This will edit your profile list files.')) return;
  await fetch('/api/apply');
  toast('✅ Votes applied to profile lists');
}

function toast(msg) {
  const t = document.getElementById('toast');
  t.textContent = msg; t.style.display = 'block';
  setTimeout(() => t.style.display = 'none', 2000);
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

    if args.apply:
        apply_votes(votes)
        return

    log.info("Scanning %s...", root)
    collection = scan_collection(root)
    log.info("Found %d profiles, %d images",
             len(collection), sum(len(v) for v in collection.values()))

    samples = sample_for_review(collection)

    ReviewHandler.collection = collection
    ReviewHandler.samples = samples
    ReviewHandler.root_dir = root
    ReviewHandler.votes = votes

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
