"""Frame sync — push curated infographics to digital photo frames.

Supports:
  - Frameo: via shared folder (USB/SD) or cloud sync directory
  - Generic: rsync/copy to any target directory (NAS, Dropbox, Google Drive)
  - ADB: push to Android-based frames over USB/WiFi

Usage:
    python framesync.py                          # sync upvoted images to frame
    python framesync.py --target /media/usb      # USB/SD card
    python framesync.py --target user@nas:/photos # rsync to NAS
    python framesync.py --adb 192.168.1.50       # ADB push to Android frame
"""
import argparse
import json
import logging
import shutil
import subprocess
import sys
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger(__name__)

VOTES_FILE = Path(".infogdl_votes.json")
SYNC_STATE = Path(".infogdl_framesync.json")


def load_votes() -> dict:
    if VOTES_FILE.exists():
        return json.loads(VOTES_FILE.read_text())
    return {}


def load_sync_state() -> dict:
    if SYNC_STATE.exists():
        return json.loads(SYNC_STATE.read_text())
    return {"synced": []}


def save_sync_state(state: dict):
    SYNC_STATE.write_text(json.dumps(state, indent=2))


def collect_upvoted(output_dir: Path, votes: dict, min_votes: int = 1) -> list[Path]:
    """Collect images from upvoted profiles."""
    upvoted = {h.lower() for h, c in votes.get("up", {}).items() if c >= min_votes}
    if not upvoted:
        log.info("No upvoted profiles. Vote in review.py first.")
        return []

    IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp"}
    images = []
    for f in output_dir.rglob("*"):
        if not f.is_file() or f.suffix.lower() not in IMAGE_EXTS:
            continue
        # Check sidecar for author
        meta_path = f.with_suffix(".json")
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text())
                author = meta.get("author", "").lower()
                if author in upvoted:
                    images.append(f)
                    continue
            except Exception:
                pass
        # Fallback: check if filename contains an upvoted handle
        name_lower = f.stem.lower()
        if any(h in name_lower for h in upvoted):
            images.append(f)

    log.info("Found %d images from %d upvoted profiles", len(images), len(upvoted))
    return images


def sync_to_directory(images: list[Path], target: Path, max_images: int = 100):
    """Copy images to a local directory (USB, SD card, shared folder)."""
    target.mkdir(parents=True, exist_ok=True)
    state = load_sync_state()
    synced = set(state.get("synced", []))
    count = 0

    for img in images:
        if str(img) in synced or count >= max_images:
            continue
        dest = target / img.name
        # Avoid name collisions
        if dest.exists():
            dest = target / f"{img.stem}_{count}{img.suffix}"
        shutil.copy2(img, dest)
        synced.add(str(img))
        count += 1
        log.info("📤 %s -> %s", img.name, dest)

    state["synced"] = list(synced)
    save_sync_state(state)
    log.info("Synced %d new images to %s", count, target)


def sync_via_rsync(images: list[Path], target: str):
    """Sync images to remote target via rsync."""
    import tempfile
    state = load_sync_state()
    synced = set(state.get("synced", []))
    to_sync = [img for img in images if str(img) not in synced]

    if not to_sync:
        log.info("All images already synced.")
        return

    # Write file list for rsync
    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
        for img in to_sync:
            f.write(str(img) + "\n")
        listfile = f.name

    try:
        cmd = ["rsync", "-avz", "--files-from", listfile, "/", target]
        log.info("Running: %s", " ".join(cmd))
        subprocess.run(cmd, check=True)
        for img in to_sync:
            synced.add(str(img))
        state["synced"] = list(synced)
        save_sync_state(state)
        log.info("Synced %d images via rsync", len(to_sync))
    except Exception as e:
        log.error("rsync failed: %s", e)
    finally:
        Path(listfile).unlink(missing_ok=True)


def sync_via_adb(images: list[Path], device: str, remote_dir: str = "/sdcard/Pictures/infogdl"):
    """Push images to Android frame via ADB."""
    state = load_sync_state()
    synced = set(state.get("synced", []))
    to_sync = [img for img in images if str(img) not in synced]

    if not to_sync:
        log.info("All images already synced.")
        return

    adb_base = ["adb"]
    if device:
        adb_base = ["adb", "-s", device]

    # Create remote directory
    subprocess.run(adb_base + ["shell", "mkdir", "-p", remote_dir],
                   capture_output=True)

    count = 0
    for img in to_sync:
        try:
            subprocess.run(adb_base + ["push", str(img), f"{remote_dir}/{img.name}"],
                           check=True, capture_output=True)
            synced.add(str(img))
            count += 1
            log.info("📤 [adb] %s", img.name)
        except Exception as e:
            log.warning("ADB push failed for %s: %s", img.name, e)

    state["synced"] = list(synced)
    save_sync_state(state)
    log.info("Pushed %d images via ADB", count)


def main():
    parser = argparse.ArgumentParser(description="Sync infographics to digital photo frame")
    parser.add_argument("-d", "--dir", default="./output", help="Output directory to sync from")
    parser.add_argument("--target", help="Target directory or rsync path (e.g. /media/usb or user@host:/path)")
    parser.add_argument("--adb", nargs="?", const="", metavar="DEVICE",
                        help="Push via ADB (optionally specify device IP:port)")
    parser.add_argument("--max", type=int, default=100, help="Max images to sync")
    parser.add_argument("--min-votes", type=int, default=1, help="Min upvotes to include")
    args = parser.parse_args()

    output_dir = Path(args.dir)
    if not output_dir.is_dir():
        log.error("Directory not found: %s", output_dir)
        sys.exit(1)

    votes = load_votes()
    images = collect_upvoted(output_dir, votes, min_votes=args.min_votes)
    if not images:
        return

    if args.adb is not None:
        sync_via_adb(images, args.adb)
    elif args.target:
        if ":" in args.target and "@" in args.target:
            sync_via_rsync(images, args.target)
        else:
            sync_to_directory(images, Path(args.target), max_images=args.max)
    else:
        # Default: sync to ./frame_output
        sync_to_directory(images, Path("./frame_output"), max_images=args.max)


if __name__ == "__main__":
    main()
