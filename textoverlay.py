"""Text overlay for images without text content.

Detects if an image has minimal text, and if so, overlays the post caption
from the sidecar metadata. Uses deterministic parameters (seeded by image
filename hash) so the same image always gets the same overlay style.
"""
import hashlib
import json
import logging
import textwrap
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

log = logging.getLogger(__name__)

# Font candidates — tried in order, first found wins
_FONT_PATHS = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "C:/Windows/Fonts/arial.ttf",
    "C:/Windows/Fonts/segoeui.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
]

# Style presets — picked deterministically per image
_STYLES = [
    {"position": "bottom", "bg_alpha": 180, "text_color": (255, 255, 255),
     "font_scale": 1.0, "padding": 0.05},
    {"position": "top", "bg_alpha": 200, "text_color": (255, 255, 255),
     "font_scale": 0.9, "padding": 0.04},
    {"position": "bottom", "bg_alpha": 220, "text_color": (240, 240, 240),
     "font_scale": 1.1, "padding": 0.06},
    {"position": "center", "bg_alpha": 160, "text_color": (255, 255, 255),
     "font_scale": 1.2, "padding": 0.08},
    {"position": "bottom", "bg_alpha": 200, "text_color": (255, 220, 100),
     "font_scale": 1.0, "padding": 0.05},
]


def has_text(img: Image.Image, threshold: float = 0.02) -> bool:
    """Detect if image likely contains text using edge density variance.
    Text-heavy images have high-frequency horizontal edge patterns."""
    gray = np.array(img.convert("L").resize((300, 300)), dtype=np.float32)

    # Horizontal edges (text creates strong horizontal patterns)
    h_edges = np.abs(np.diff(gray, axis=1))
    # Vertical edges
    v_edges = np.abs(np.diff(gray, axis=0))

    # Text has lots of small, regular edge clusters
    # Compute the ratio of "busy" rows (rows with many edge transitions)
    row_activity = (h_edges > 20).sum(axis=1) / h_edges.shape[1]
    busy_rows = (row_activity > 0.1).sum() / len(row_activity)

    return busy_rows > threshold


def _get_font(size: int) -> ImageFont.FreeTypeFont:
    for path in _FONT_PATHS:
        try:
            return ImageFont.truetype(path, size)
        except (OSError, IOError):
            continue
    return ImageFont.load_default()


def _deterministic_style(image_path: str) -> dict:
    """Pick a style based on hash of filename — always same result."""
    h = int(hashlib.md5(image_path.encode()).hexdigest(), 16)
    return _STYLES[h % len(_STYLES)]


def overlay_text(img: Image.Image, text: str, image_path: str) -> Image.Image:
    """Overlay text on image using deterministic style based on filename."""
    if not text or len(text.strip()) < 10:
        return img

    style = _deterministic_style(image_path)
    w, h = img.size

    # Font size relative to image
    base_size = int(min(w, h) * 0.04 * style["font_scale"])
    base_size = max(14, min(base_size, 48))
    font = _get_font(base_size)

    # Wrap text
    chars_per_line = max(20, int(w / (base_size * 0.55)))
    lines = textwrap.wrap(text, width=chars_per_line)
    if len(lines) > 6:
        lines = lines[:5] + ["..."]

    # Calculate text block size
    draw = ImageDraw.Draw(img)
    line_height = base_size + 4
    text_height = len(lines) * line_height
    padding = int(min(w, h) * style["padding"])

    # Position
    if style["position"] == "top":
        y_start = padding
    elif style["position"] == "center":
        y_start = (h - text_height) // 2
    else:  # bottom
        y_start = h - text_height - padding * 2

    # Draw semi-transparent background
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    overlay_draw = ImageDraw.Draw(overlay)
    bg_rect = [0, y_start - padding, w, y_start + text_height + padding]
    overlay_draw.rectangle(bg_rect, fill=(0, 0, 0, style["bg_alpha"]))

    # Composite background
    if img.mode != "RGBA":
        img = img.convert("RGBA")
    img = Image.alpha_composite(img, overlay)

    # Draw text
    draw = ImageDraw.Draw(img)
    y = y_start
    for line in lines:
        # Center text
        bbox = draw.textbbox((0, 0), line, font=font)
        tw = bbox[2] - bbox[0]
        x = (w - tw) // 2
        # Shadow
        draw.text((x + 1, y + 1), line, fill=(0, 0, 0, 200), font=font)
        # Main text
        draw.text((x, y), line, fill=style["text_color"], font=font)
        y += line_height

    return img.convert("RGB")


def process_overlay(image_path: Path, sidecar_path: Path | None = None) -> bool:
    """Check if image needs text overlay, apply if so. Returns True if modified."""
    try:
        img = Image.open(image_path)
    except Exception:
        return False

    if has_text(img):
        img.close()
        return False

    # Get text from sidecar
    if sidecar_path is None:
        sidecar_path = image_path.with_suffix(".json")
    if not sidecar_path.exists():
        img.close()
        return False

    try:
        meta = json.loads(sidecar_path.read_text())
    except Exception:
        img.close()
        return False

    text = meta.get("text") or meta.get("caption") or ""
    if len(text.strip()) < 10:
        img.close()
        return False

    log.info("📝 Adding text overlay to %s", image_path.name)
    result = overlay_text(img, text.strip(), str(image_path))
    result.save(image_path)
    img.close()
    return True
