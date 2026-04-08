#!/usr/bin/env python3
"""infogdl-web — Web voting interface + scraper trigger for remote deployment.

Designed to run as a Docker container behind Caddy on ecb.pm.
Maintains a limited image set (10 per profile max, keeps 1 after vote).
Syncs profile lists back to a reference directory.
"""
import json
import logging
import os
import random
import shutil
import subprocess
import sys
import threading
import time
from collections import defaultdict
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from urllib.parse import unquote, urlparse

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger(__name__)

DATA_DIR = Path(os.environ.get("INFOGDL_DATA", "/data"))
REF_DIR = Path(os.environ.get("INFOGDL_REF", "/ref"))
IMG_DIR = DATA_DIR / "images"
VOTES_FILE = DATA_DIR / "votes.json"
QUEUE_FILE = DATA_DIR / "queue.json"
MAX_PER_PROFILE = 10
KEEP_AFTER_VOTE = 1

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}


def _load_json(path: Path, default=None):
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            pass
    return default if default is not None else {}


def _save_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False))


def scan_images() -> dict:
    """Scan image directory, group by profile."""
    collection = defaultdict(list)
    IMG_DIR.mkdir(parents=True, exist_ok=True)
    for f in sorted(IMG_DIR.rglob("*")):
        if not f.is_file() or f.suffix.lower() not in IMAGE_EXTS:
            continue
        meta = {}
        sidecar = f.with_suffix(".json")
        if sidecar.exists():
            try:
                meta = json.loads(sidecar.read_text())
            except Exception:
                pass
        key = f"{meta.get('platform', 'local')}/{meta.get('author', 'unknown')}"
        collection[key].append({
            "path": str(f),
            "rel": str(f.relative_to(IMG_DIR)),
            "name": f.name,
            "size_kb": f.stat().st_size / 1024,
            "meta": meta,
            "profile_key": key,
        })
    return dict(collection)


def enforce_limits(collection: dict):
    """Keep max MAX_PER_PROFILE images per profile, delete excess."""
    for key, images in collection.items():
        if len(images) > MAX_PER_PROFILE:
            excess = sorted(images, key=lambda x: x.get("meta", {}).get("downloaded_at", ""))
            to_remove = excess[:len(images) - MAX_PER_PROFILE]
            for img in to_remove:
                p = Path(img["path"])
                p.unlink(missing_ok=True)
                p.with_suffix(".json").unlink(missing_ok=True)
            log.info("Trimmed %s: removed %d excess images", key, len(to_remove))


def build_review(collection: dict, queue: dict) -> list[dict]:
    """Build review set: unrated images, 5 per profile."""
    rated = set(queue.get("rated", []))
    review = []
    for key, images in collection.items():
        unrated = [img for img in images if img["path"] not in rated]
        for img in unrated[:5]:
            review.append(img)
    return review


def apply_vote(img_path: str, vote: str, collection: dict, queue: dict, votes: dict):
    """Process a vote: mark rated, keep 1 representative, sync profiles."""
    queue.setdefault("rated", [])
    if img_path not in queue["rated"]:
        queue["rated"].append(img_path)

    # Find profile
    profile_key = None
    for key, images in collection.items():
        if any(img["path"] == img_path for img in images):
            profile_key = key
            break

    if not profile_key:
        return

    handle = profile_key.split("/")[-1]
    votes.setdefault("up", {})
    votes.setdefault("down", {})

    if vote == "up":
        votes["up"][handle] = votes["up"].get(handle, 0) + 1
    elif vote == "down":
        votes["down"][handle] = votes["down"].get(handle, 0) + 1

    # After voting on all images of a profile, keep only KEEP_AFTER_VOTE
    images = collection.get(profile_key, [])
    rated_imgs = [img for img in images if img["path"] in queue["rated"]]
    unrated_imgs = [img for img in images if img["path"] not in queue["rated"]]

    if not unrated_imgs and len(rated_imgs) > KEEP_AFTER_VOTE:
        to_remove = rated_imgs[KEEP_AFTER_VOTE:]
        for img in to_remove:
            Path(img["path"]).unlink(missing_ok=True)
            Path(img["path"]).with_suffix(".json").unlink(missing_ok=True)
        log.info("Kept %d representative image(s) for %s", KEEP_AFTER_VOTE, profile_key)


def sync_profiles_to_ref():
    """Sync profile lists and votes to reference directory."""
    REF_DIR.mkdir(parents=True, exist_ok=True)

    # Copy profile lists
    profiles_src = DATA_DIR / "profiles"
    profiles_dst = REF_DIR / "profiles"
    if profiles_src.is_dir():
        if profiles_dst.exists():
            shutil.rmtree(profiles_dst)
        shutil.copytree(profiles_src, profiles_dst)

    # Copy votes
    if VOTES_FILE.exists():
        shutil.copy2(VOTES_FILE, REF_DIR / "votes.json")

    log.info("Synced profiles and votes to %s", REF_DIR)


def apply_removals(votes: dict):
    """Remove downvoted/flagged handles from profile lists."""
    profiles_dir = DATA_DIR / "profiles"
    if not profiles_dir.is_dir():
        return

    to_remove = set()
    for handle, count in votes.get("down", {}).items():
        if count >= 2:
            to_remove.add(handle.lower())
    for handle in votes.get("remove_handles", []):
        to_remove.add(handle.lower())

    if not to_remove:
        return

    for f in profiles_dir.glob("*.txt"):
        lines = f.read_text().splitlines()
        kept = []
        for line in lines:
            stripped = line.split("#")[0].strip()
            parts = stripped.split(None, 1)
            if len(parts) == 2:
                h = parts[1].lstrip("@").split("/")[0].lower()
                if h in to_remove:
                    log.info("❌ Removing %s from %s", h, f.name)
                    continue
            kept.append(line)
        f.write_text("\n".join(kept) + "\n")

    sync_profiles_to_ref()


class Handler(SimpleHTTPRequestHandler):
    collection = {}
    review = []
    queue = {}
    votes = {}
    scraping = False

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/":
            self._html()
        elif path == "/api/review":
            self._json(self.review)
        elif path == "/api/stats":
            self._json({
                "profiles": len(self.collection),
                "total": sum(len(v) for v in self.collection.values()),
                "in_review": len(self.review),
                "votes": self.votes,
                "scraping": self.scraping,
            })
        elif path.startswith("/img/"):
            fpath = IMG_DIR / unquote(path[5:])
            if fpath.exists():
                self.send_response(200)
                ct = {"png": "image/png", "webp": "image/webp"}.get(
                    fpath.suffix.lstrip("."), "image/jpeg")
                self.send_header("Content-Type", ct)
                self.end_headers()
                self.wfile.write(fpath.read_bytes())
            else:
                self.send_error(404)
        else:
            self.send_error(404)

    def do_POST(self):
        path = urlparse(self.path).path
        body = {}
        length = int(self.headers.get("Content-Length", 0))
        if length:
            body = json.loads(self.rfile.read(length))

        if path == "/api/vote":
            img_path = body.get("img_path", "")
            vote = body.get("vote", "")
            apply_vote(img_path, vote, self.collection, self.queue, self.votes)
            _save_json(VOTES_FILE, self.votes)
            _save_json(QUEUE_FILE, self.queue)
            self._refresh()
            self._json({"ok": True, "remaining": len(self.review)})

        elif path == "/api/remove":
            handle = body.get("handle", "")
            if handle:
                self.votes.setdefault("remove_handles", [])
                if handle not in self.votes["remove_handles"]:
                    self.votes["remove_handles"].append(handle)
                _save_json(VOTES_FILE, self.votes)
            self._json({"ok": True})

        elif path == "/api/apply":
            apply_removals(self.votes)
            self.votes = {"up": {}, "down": {}, "remove_handles": []}
            _save_json(VOTES_FILE, self.votes)
            self._refresh()
            self._json({"ok": True})

        elif path == "/api/scrape":
            if not self.scraping:
                Handler.scraping = True
                threading.Thread(target=self._run_scrape, daemon=True).start()
                self._json({"ok": True, "status": "started"})
            else:
                self._json({"ok": False, "status": "already_running"})

        elif path == "/api/sync":
            sync_profiles_to_ref()
            self._json({"ok": True})

        elif path == "/api/add_profile":
            platform = body.get("platform", "")
            handle = body.get("handle", "").strip().lstrip("@")
            if not handle or not platform:
                self._json({"ok": False, "error": "missing fields"})
                return
            # Build profile line
            line = f"{platform} @{handle}"
            # Pick the right file
            profiles_dir = DATA_DIR / "profiles"
            target = None
            for f in profiles_dir.glob("*.txt"):
                if platform in f.name.lower() or (platform == "twitter" and "x-" in f.name.lower()):
                    target = f
                    break
            if not target:
                target = profiles_dir / f"{platform}-profiles.txt"
            # Check for duplicates
            existing = target.read_text() if target.exists() else ""
            if handle.lower() in existing.lower():
                self._json({"ok": False, "error": f"{handle} already in list"})
                return
            with open(target, "a") as fh:
                fh.write(f"\n{line}\n")
            log.info("➕ Added %s @%s to %s", platform, handle, target.name)
            self._json({"ok": True})
        else:
            self.send_error(404)

    def _run_scrape(self):
        try:
            log.info("🔄 Starting scrape...")
            subprocess.run([
                sys.executable, "/app/infogdl.py",
                "-c", str(DATA_DIR / "config.json"),
                "-o", str(IMG_DIR),
                "-p", *[str(f) for f in (DATA_DIR / "profiles").glob("*.txt")],
                "-w", "2",
            ], timeout=600, cwd="/app")
        except Exception as e:
            log.error("Scrape failed: %s", e)
        finally:
            Handler.scraping = False
            self._refresh()
            log.info("Scrape complete.")

    def _refresh(self):
        Handler.collection = scan_images()
        enforce_limits(Handler.collection)
        Handler.review = build_review(Handler.collection, Handler.queue)

    def _json(self, data):
        body = json.dumps(data).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def _html(self):
        body = HTML.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


HTML = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>infogdl</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,sans-serif;background:#1a1a2e;color:#eee}
.hdr{padding:16px;text-align:center;background:#16213e}
.hdr h1{font-size:1.3em} .stats{color:#888;font-size:13px;margin-top:6px}
.bar{padding:8px;text-align:center}
.bar button{padding:7px 16px;margin:3px;border:none;border-radius:6px;cursor:pointer;font-size:13px}
.b1{background:#0f3460;color:#fff} .b2{background:#e94560;color:#fff} .b3{background:#333;color:#aaa}
.card{background:#16213e;margin:10px auto;max-width:860px;border-radius:8px;overflow:hidden}
.card img{width:100%;max-height:550px;object-fit:contain;background:#000}
.ci{padding:10px 14px;display:flex;justify-content:space-between;align-items:center}
.cm{font-size:12px;color:#aaa;max-width:55%} .cm .a{color:#e94560;font-weight:bold}
.vb button{padding:7px 14px;margin:0 3px;border:none;border-radius:6px;cursor:pointer;font-size:16px}
.up{background:#2d6a4f;color:#fff} .dn{background:#9b2226;color:#fff}
.rm{background:#444;color:#fff;font-size:11px!important;padding:5px 8px!important}
.voted{opacity:.12;pointer-events:none}
.toast{position:fixed;bottom:16px;right:16px;background:#0f3460;color:#fff;padding:10px 18px;
border-radius:8px;display:none;z-index:99;font-size:13px}
.badge{background:#e94560;color:#fff;padding:2px 7px;border-radius:10px;font-size:11px}
.spin{display:inline-block;animation:spin 1s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}
</style></head><body>
<div class="hdr"><h1>📊 infogdl</h1><div class="stats" id="st">...</div></div>
<div class="bar">
<button class="b1" onclick="scrape()">🔄 Scrape</button>
<button class="b2" onclick="apply()">✅ Apply Votes</button>
<button class="b3" onclick="sync()">📤 Sync to Ref</button>
<div style="margin:8px auto;max-width:500px;display:flex;gap:6px">
<select id="addplat" style="padding:6px;border-radius:6px;border:none;background:#16213e;color:#eee">
<option value="twitter">Twitter</option><option value="linkedin">LinkedIn</option><option value="instagram">Instagram</option></select>
<input id="addhandle" placeholder="@handle or slug" style="flex:1;padding:6px;border-radius:6px;border:none;background:#16213e;color:#eee">
<button class="b1" onclick="addProfile()">➕ Add</button>
</div>
</div>
<div id="cards"></div>
<div class="toast" id="toast"></div>
<script>
const B = window.location.pathname.replace(/\/$/, '');
let R=[];
async function load(){
 let[s,r]=await Promise.all([fetch(B+'/api/stats').then(r=>r.json()),fetch(B+'/api/review').then(r=>r.json())]);
 document.getElementById('st').innerHTML=
  s.profiles+' profiles · '+s.total+' imgs · <span class="badge">'+s.in_review+' to review</span>'+
  (s.scraping?' · <span class="spin">🔄</span> scraping':'');
 R=r;render();
}
function render(){
 let el=document.getElementById('cards');
 if(!R.length){el.innerHTML='<p style="text-align:center;padding:30px;color:#555">All reviewed! Hit 🔄 Scrape for more.</p>';return}
 el.innerHTML=R.map((s,i)=>`<div class="card" id="c${i}">
  <img src="${B}/img/${encodeURIComponent(s.rel)}" loading="lazy">
  <div class="ci"><div class="cm"><span class="a">${s.profile_key}</span><br>${s.name} · ${Math.round(s.size_kb)}KB
  ${s.meta&&s.meta.text?'<br><em>'+s.meta.text.substring(0,120)+'</em>':''}</div>
  <div class="vb"><button class="up" onclick="vote(${i},'up')">👍</button>
  <button class="dn" onclick="vote(${i},'down')">👎</button>
  <button class="rm" onclick="rm('${s.profile_key}',${i})">🚫</button></div></div></div>`).join('');
}
async function vote(i,d){
 let s=R[i];
 await fetch(B+'/api/vote',{method:'POST',headers:{'Content-Type':'application/json'},
  body:JSON.stringify({img_path:s.path,vote:d})});
 document.getElementById('c'+i).classList.add('voted');
 toast(d=='up'?'👍':'👎');setTimeout(load,500);
}
async function rm(k,i){
 let h=k.split('/').pop();
 if(!confirm('Remove '+h+'?'))return;
 await fetch(B+'/api/remove',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({handle:h})});
 document.getElementById('c'+i).classList.add('voted');toast('🚫 '+h);
}
async function scrape(){toast('🔄 Starting scrape...');
 await fetch(B+'/api/scrape',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});
 setTimeout(load,5000);setInterval(load,15000);
}
async function apply(){if(!confirm('Apply votes?'))return;
 await fetch(B+'/api/apply',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});
 toast('✅ Applied');load();
}
async function sync(){
 await fetch(B+'/api/sync',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});
 toast('📤 Synced to ref');
}
async function addProfile(){
 let p=document.getElementById('addplat').value;
 let h=document.getElementById('addhandle').value.trim().replace(/^@/,'');
 if(!h){toast('Enter a handle');return}
 let r=await fetch(B+'/api/add_profile',{method:'POST',headers:{'Content-Type':'application/json'},
  body:JSON.stringify({platform:p,handle:h})});
 let d=await r.json();
 document.getElementById('addhandle').value='';
 toast(d.ok?'➕ Added '+h:'❌ '+d.error);
}
function toast(m){let t=document.getElementById('toast');t.textContent=m;t.style.display='block';
 setTimeout(()=>t.style.display='none',2500);}
load();
</script></body></html>"""


def _periodic_rescan():
    """Rescan images every 2 hours."""
    while True:
        time.sleep(2 * 3600)
        try:
            Handler.collection = scan_images()
            enforce_limits(Handler.collection)
            Handler.queue = _load_json(QUEUE_FILE, {"rated": []})
            Handler.review = build_review(Handler.collection, Handler.queue)
            log.info("🔄 Periodic rescan: %d profiles, %d images",
                     len(Handler.collection),
                     sum(len(v) for v in Handler.collection.values()))
        except Exception as e:
            log.warning("Rescan failed: %s", e)


def main():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    IMG_DIR.mkdir(parents=True, exist_ok=True)
    (DATA_DIR / "profiles").mkdir(parents=True, exist_ok=True)

    Handler.votes = _load_json(VOTES_FILE, {"up": {}, "down": {}, "remove_handles": []})
    Handler.queue = _load_json(QUEUE_FILE, {"rated": []})
    Handler.collection = scan_images()
    enforce_limits(Handler.collection)
    Handler.review = build_review(Handler.collection, Handler.queue)

    # Background rescan thread
    threading.Thread(target=_periodic_rescan, daemon=True).start()

    port = int(os.environ.get("PORT", 8000))
    server = HTTPServer(("0.0.0.0", port), Handler)
    log.info("infogdl-web running on :%d (%d profiles, %d images, rescan every 2h)",
             port, len(Handler.collection),
             sum(len(v) for v in Handler.collection.values()))
    server.serve_forever()


if __name__ == "__main__":
    main()
