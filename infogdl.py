#!/usr/bin/env python3
"""infogdl - Download, analyze, sort, and resize infographics from LinkedIn/Twitter."""
import json
import logging
import argparse
from pathlib import Path
from PIL import Image

from scraper import scrape_profile
from analyze import analyze
from sorter import sort_path
from resize import process
from progress import ProgressTracker

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)


def load_config(path: str) -> dict:
    with open(path) as f:
        return json.load(f)


def _analyze_and_sort(fpath: Path, out_dir: Path, cfg: dict,
                      delete: bool = False,
                      invert_threshold: float | None = None):
    """Analyze, sort, crop, resize, and compress a single image."""
    try:
        img = Image.open(fpath)
    except Exception as e:
        log.warning("Cannot open %s: %s", fpath, e)
        return

    info = analyze(img)
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


def run(cfg: dict, full_rescan: bool = False,
        invert_threshold: float | None = None):
    out_dir = Path(cfg["output_dir"])
    raw_dir = out_dir / "_raw"
    tracker = ProgressTracker()

    all_files = []
    for profile in cfg["profiles"]:
        platform = profile["platform"]
        url = profile["url"]
        slug = url.rstrip("/").split("/")[-1] or platform
        dl_dir = raw_dir / f"{platform}_{slug}"

        last_ts = tracker.get_last_ts(f"{platform}:{url.rstrip('/')}")
        if last_ts and not full_rescan:
            log.info("Resuming %s from last checkpoint", url)
        else:
            log.info("Full scan of %s", url)

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
        all_files.extend(files)
        log.info("Downloaded %d new images from %s", len(files), url)

    for fpath in all_files:
        _analyze_and_sort(fpath, out_dir, cfg,
                          invert_threshold=invert_threshold)

    tracker.close()


IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tiff", ".tif"}


def collect_images(root: Path) -> list[Path]:
    return sorted(f for f in root.rglob("*")
                  if f.is_file() and f.suffix.lower() in IMAGE_EXTS)


def process_local(cfg: dict, input_dir: str, output_dir: str | None,
                  delete: bool = False,
                  invert_threshold: float | None = None):
    out_dir = Path(output_dir) if output_dir else Path(cfg["output_dir"])
    src = Path(input_dir)
    if not src.is_dir():
        log.error("Input is not a directory: %s", src)
        return

    files = collect_images(src)
    log.info("Found %d images in %s", len(files), src)

    for fpath in files:
        _analyze_and_sort(fpath, out_dir, cfg, delete=delete,
                          invert_threshold=invert_threshold)


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
    parser.add_argument("--invert-bright", nargs="?", type=float,
                        const=0.70, default=None, metavar="THRESHOLD",
                        help="Invert colors on bright images (default threshold: 0.70)")
    args = parser.parse_args()

    cfg = load_config(args.config)
    if args.output:
        cfg["output_dir"] = args.output

    if args.input:
        process_local(cfg, args.input, args.output, delete=args.delete,
                      invert_threshold=args.invert_bright)
    else:
        run(cfg, full_rescan=args.full_rescan,
            invert_threshold=args.invert_bright)


if __name__ == "__main__":
    main()
