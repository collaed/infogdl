#!/usr/bin/env python3
"""infogdl - Download, analyze, sort, and resize infographics from LinkedIn/Twitter."""
import json
import logging
import argparse
import time
import random
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from PIL import Image

from scraper import scrape_profile, discover_following
from analyze import analyze
from sorter import sort_path
from resize import process
from progress import ProgressTracker

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("infogdl.log", encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)


def load_config(path: str) -> dict:
    with open(path) as f:
        return json.load(f)


def _dir_size_gb(path: Path) -> float:
    """Total size of all files under path, in GB."""
    if not path.exists():
        return 0.0
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file()) / (1024**3)


def _check_limits(out_dir: Path, limits: dict[str, float]) -> dict[str, bool]:
    """Check which orientations have hit their storage limit.
    Returns {orientation: is_full}."""
    result = {}
    for orient, max_gb in limits.items():
        size = _dir_size_gb(out_dir / orient)
        result[orient] = size >= max_gb
        if result[orient]:
            log.info("Storage limit reached for %s: %.2f/%.1f GB", orient, size, max_gb)
    return result


def _analyze_and_sort(fpath: Path, out_dir: Path, cfg: dict,
                      delete: bool = False,
                      invert_threshold: float | None = None,
                      limits: dict[str, float] | None = None) -> str | None:
    """Analyze, sort, crop, resize, and compress a single image.
    Returns orientation or None if skipped due to limit."""
    try:
        img = Image.open(fpath)
    except Exception as e:
        log.warning("Cannot open %s: %s", fpath, e)
        return None

    info = analyze(img)

    # Check storage limit before writing
    if limits:
        full = _check_limits(out_dir, limits)
        if full.get(info["orientation"], False):
            img.close()
            return None

    subfolder = sort_path(
        info["orientation"], info["colors"], info["fill_rate"],
        cfg["color_bins"], cfg["fill_bins"],
    )
    dest_dir = out_dir / subfolder
    dest_dir.mkdir(parents=True, exist_ok=True)
    process(img, dest_dir / fpath.name,
            cfg["target_width"], cfg["target_height"], cfg["max_file_size_kb"],
            invert_threshold=invert_threshold)
    img.close()

    if delete:
        fpath.unlink()
        log.info("%s -> %s/ (deleted original)", fpath.name, subfolder)
    else:
        log.info("%s -> %s/ (colors=%d, fill=%.2f, %s)",
                 fpath.name, subfolder, info["colors"],
                 info["fill_rate"], info["orientation"])
    return info["orientation"]


def _scrape_one(profile: dict, cfg: dict, raw_dir: Path,
                tracker: ProgressTracker, full_rescan: bool) -> list[Path]:
    """Scrape a single profile. Designed to run in a thread."""
    platform = profile["platform"]
    url = profile["url"]
    slug = url.rstrip("/").split("/")[-1] or platform
    dl_dir = raw_dir / f"{platform}_{slug}"

    last_ts = tracker.get_last_ts(f"{platform}:{url.rstrip('/')}")
    if last_ts and not full_rescan:
        log.info("Resuming %s from last checkpoint", url)
    else:
        log.info("Full scan of %s", url)

    try:
        files = scrape_profile(
            platform, url, dl_dir,
            headless=cfg.get("headless", True),
            scroll_count=cfg.get("scroll_count", 5),
            scroll_delay=cfg.get("scroll_delay", 2.0),
            cookie_file=cfg.get("cookie_file"),
            browser=cfg.get("browser"),
            tracker=tracker,
            full_rescan=full_rescan,
        )
    except Exception as e:
        log.error("Scrape failed for %s: %s", url, e)
        files = []

    log.info("Downloaded %d new images from %s", len(files), url)

    # Throttle between profiles — gallery-dl style escalating cooldown
    # Short pause after few images, longer pause after many
    if len(files) > 20:
        delay = random.uniform(120, 180)  # 2-3 min after heavy scrape
    elif len(files) > 5:
        delay = random.uniform(30, 60)    # 30-60s after moderate scrape
    else:
        delay = random.uniform(5, 15)     # 5-15s after light scrape

    until = time.time() + delay
    t = time.localtime(until)
    log.info("⏸ Cooling down %.0fs until %02d:%02d:%02d before next profile",
             delay, t.tm_hour, t.tm_min, t.tm_sec)
    time.sleep(delay)

    return files


def run(cfg: dict, full_rescan: bool = False,
        invert_threshold: float | None = None,
        discover: str | None = None,
        limits: dict[str, float] | None = None,
        workers: int = 4):
    out_dir = Path(cfg["output_dir"])
    raw_dir = out_dir / "_raw"
    tracker = ProgressTracker()

    profiles = list(cfg.get("profiles", []))

    # Auto-discover followed profiles (parallel per platform)
    if discover:
        platforms = [p.strip() for p in discover.split(",")]
        with ThreadPoolExecutor(max_workers=len(platforms)) as pool:
            futures = {
                pool.submit(
                    discover_following, p,
                    headless=cfg.get("headless", True),
                    cookie_file=cfg.get("cookie_file"),
                    browser=cfg.get("browser"),
                    scroll_count=cfg.get("scroll_count", 5),
                ): p for p in platforms
            }
            for fut in as_completed(futures):
                plat = futures[fut]
                try:
                    profiles.extend(fut.result())
                except Exception as e:
                    log.error("Discovery failed for %s: %s", plat, e)

    if not profiles:
        log.warning("No profiles to scrape.")
        tracker.close()
        return

    log.info("Scraping %d profiles with %d parallel workers", len(profiles), workers)

    # Scrape all profiles in parallel
    all_files = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(_scrape_one, p, cfg, raw_dir, tracker, full_rescan): p
            for p in profiles
        }
        for fut in as_completed(futures):
            try:
                all_files.extend(fut.result())
            except Exception as e:
                p = futures[fut]
                log.error("Scrape failed for %s: %s", p.get("url", "?"), e)

    # Process images (check limits after each)
    processed = {"vertical": 0, "horizontal": 0}
    skipped = 0
    for fpath in all_files:
        # Early exit if both orientations are full
        if limits:
            full = _check_limits(out_dir, limits)
            if all(full.values()):
                log.info("All storage limits reached. Stopping.")
                break

        orient = _analyze_and_sort(fpath, out_dir, cfg,
                                   invert_threshold=invert_threshold,
                                   limits=limits)
        if orient:
            processed[orient] = processed.get(orient, 0) + 1
        else:
            skipped += 1

    log.info("Done. Processed: %s. Skipped (limit): %d", processed, skipped)
    tracker.close()


IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tiff", ".tif"}


def collect_images(root: Path) -> list[Path]:
    return sorted(f for f in root.rglob("*")
                  if f.is_file() and f.suffix.lower() in IMAGE_EXTS)


def process_local(cfg: dict, input_dir: str, output_dir: str | None,
                  delete: bool = False,
                  invert_threshold: float | None = None,
                  limits: dict[str, float] | None = None):
    out_dir = Path(output_dir) if output_dir else Path(cfg["output_dir"])
    src = Path(input_dir)
    if not src.is_dir():
        log.error("Input is not a directory: %s", src)
        return

    files = collect_images(src)
    log.info("Found %d images in %s", len(files), src)

    for fpath in files:
        if limits:
            full = _check_limits(out_dir, limits)
            if all(full.values()):
                log.info("All storage limits reached. Stopping.")
                break
        _analyze_and_sort(fpath, out_dir, cfg, delete=delete,
                          invert_threshold=invert_threshold, limits=limits)


def _parse_limits(val: str) -> dict[str, float]:
    """Parse 'vertical:30,horizontal:30' or just '30' (both)."""
    limits = {}
    for part in val.split(","):
        if ":" in part:
            orient, gb = part.split(":", 1)
            limits[orient.strip()] = float(gb)
        else:
            gb = float(part)
            limits = {"vertical": gb, "horizontal": gb}
    return limits


def _load_profile_lists(paths: list[str]) -> list[dict]:
    """Load profiles from one or more .txt files.

    Format per line:
        platform url           # e.g. "linkedin https://www.linkedin.com/in/someone/recent-activity/shares/"
        platform @handle       # e.g. "twitter @someone" (auto-expands to media URL)
    Lines starting with # are comments. Blank lines are skipped.
    """
    profiles = []
    for path in paths:
        with open(path) as f:
            for line in f:
                # Strip inline comments
                line = line.split("#")[0].strip()
                if not line:
                    continue
                parts = line.split(None, 1)
                if len(parts) != 2:
                    continue
                platform, target = parts[0].lower(), parts[1].strip()
                # Expand shorthand handles
                if target.startswith("@"):
                    handle = target.lstrip("@")
                    if platform == "twitter":
                        target = f"https://x.com/{handle}/media"
                    elif platform == "linkedin":
                        target = f"https://www.linkedin.com/in/{handle}/recent-activity/shares/"
                profiles.append({"platform": platform, "url": target})
    log.info("Loaded %d profiles from %d file(s)", len(profiles), len(paths))
    return profiles


def main():
    parser = argparse.ArgumentParser(
        description="Infographic downloader & organizer")
    parser.add_argument("-c", "--config", default="config.json",
                        help="Config file path")
    parser.add_argument("-i", "--input",
                        help="Input directory of images (recursive)")
    parser.add_argument("-o", "--output",
                        help="Output directory (overrides config)")
    parser.add_argument("--delete", action="store_true",
                        help="Delete original files after processing")
    parser.add_argument("--full-rescan", action="store_true",
                        help="Ignore progress and re-download everything")
    parser.add_argument("--discover", metavar="PLATFORM",
                        help="Auto-discover followed profiles (twitter,linkedin)")
    parser.add_argument("--invert-bright", nargs="?", type=float,
                        const=0.70, default=None, metavar="THRESHOLD",
                        help="Invert colors on bright images (default threshold: 0.70)")
    parser.add_argument("--max-storage", metavar="LIMIT",
                        help="Stop when output reaches limit in GB "
                             "(e.g. '30' for both, or 'vertical:30,horizontal:30')")
    parser.add_argument("-w", "--workers", type=int, default=4,
                        help="Parallel scraping workers (default: 4)")
    parser.add_argument("-p", "--profiles", nargs="+", metavar="FILE",
                        help="Profile list file(s) to load")
    args = parser.parse_args()

    cfg = load_config(args.config)
    if args.output:
        cfg["output_dir"] = args.output
    if args.profiles:
        cfg.setdefault("profiles", []).extend(_load_profile_lists(args.profiles))

    limits = _parse_limits(args.max_storage) if args.max_storage else None

    if args.input:
        process_local(cfg, args.input, args.output, delete=args.delete,
                      invert_threshold=args.invert_bright, limits=limits)
    else:
        run(cfg, full_rescan=args.full_rescan,
            invert_threshold=args.invert_bright,
            discover=args.discover, limits=limits,
            workers=args.workers)


if __name__ == "__main__":
    main()
