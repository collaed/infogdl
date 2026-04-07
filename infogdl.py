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

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)


def load_config(path: str) -> dict:
    with open(path) as f:
        return json.load(f)


def run(cfg: dict):
    out_dir = Path(cfg["output_dir"])
    raw_dir = out_dir / "_raw"
    tw, th = cfg["target_width"], cfg["target_height"]
    max_kb = cfg["max_file_size_kb"]
    color_bins = {k: v for k, v in cfg["color_bins"].items()}
    fill_bins = {k: v for k, v in cfg["fill_bins"].items()}

    all_files = []

    # 1. Scrape
    for profile in cfg["profiles"]:
        platform = profile["platform"]
        url = profile["url"]
        slug = url.rstrip("/").split("/")[-1] or platform
        dl_dir = raw_dir / f"{platform}_{slug}"
        log.info(f"Scraping {platform}: {url}")
        files = scrape_profile(
            platform, url, dl_dir,
            headless=cfg.get("headless", True),
            scroll_count=cfg.get("scroll_count", 5),
            scroll_delay=cfg.get("scroll_delay", 2.0),
        )
        all_files.extend(files)
        log.info(f"Downloaded {len(files)} images from {url}")

    # 2. Analyze, sort, resize
    for fpath in all_files:
        try:
            img = Image.open(fpath)
        except Exception as e:
            log.warning(f"Cannot open {fpath}: {e}")
            continue

        info = analyze(img)
        subfolder = sort_path(
            info["orientation"], info["colors"], info["fill_rate"],
            color_bins, fill_bins,
        )
        dest_dir = out_dir / subfolder
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest_path = dest_dir / fpath.name

        process(img, dest_path, tw, th, max_kb)
        log.info(
            f"{fpath.name} -> {subfolder}/ "
            f"(colors={info['colors']}, fill={info['fill_rate']:.2f}, "
            f"orient={info['orientation']})"
        )


IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tiff", ".tif"}


def collect_images(root: Path) -> list[Path]:
    """Recursively collect all image files under a directory."""
    return sorted(f for f in root.rglob("*") if f.is_file() and f.suffix.lower() in IMAGE_EXTS)


def process_local(cfg: dict, input_dir: str, output_dir: str | None):
    out_dir = Path(output_dir) if output_dir else Path(cfg["output_dir"])
    color_bins, fill_bins = cfg["color_bins"], cfg["fill_bins"]
    tw, th = cfg["target_width"], cfg["target_height"]
    max_kb = cfg["max_file_size_kb"]

    src = Path(input_dir)
    if not src.is_dir():
        log.error(f"Input is not a directory: {src}")
        return

    files = collect_images(src)
    log.info(f"Found {len(files)} images in {src}")

    for fpath in files:
        try:
            img = Image.open(fpath)
        except Exception as e:
            log.warning(f"Cannot open {fpath}: {e}")
            continue
        info = analyze(img)
        subfolder = sort_path(info["orientation"], info["colors"], info["fill_rate"], color_bins, fill_bins)
        dest_dir = out_dir / subfolder
        dest_dir.mkdir(parents=True, exist_ok=True)
        process(img, dest_dir / fpath.name, tw, th, max_kb)
        log.info(f"{fpath.name} -> {subfolder}/ (colors={info['colors']}, fill={info['fill_rate']:.2f})")


def main():
    parser = argparse.ArgumentParser(description="Infographic downloader & organizer")
    parser.add_argument("-c", "--config", default="config.json", help="Config file path")
    parser.add_argument("-i", "--input", help="Input directory of images (recursive)")
    parser.add_argument("-o", "--output", help="Output directory (overrides config)")
    args = parser.parse_args()

    cfg = load_config(args.config)

    if args.output:
        cfg["output_dir"] = args.output

    if args.input:
        process_local(cfg, args.input, args.output)
    else:
        run(cfg)


if __name__ == "__main__":
    main()
