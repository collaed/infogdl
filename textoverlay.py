"""Smart text overlay for images without text content.

Placement: finds the region with least color variation (calmest area).
Text color: contrasts with the detected background of the chosen region.
Vibe: analyzes caption tone to pick warm/cool/bold/calm styling.
Deterministic: all decisions seeded by filename hash for reproducibility.
"""
import hashlib
import json
import logging
import textwrap
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

log = logging.getLogger(__name__)

_FONT_PATHS = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "C:/Windows/Fonts/arial.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
]

# Vibe palettes: (text_color, accent_color, bg_alpha)
_VIBES = {
    "motivational": ((255, 220, 80), (255, 180, 0), 190),    # warm gold
    "analytical":   ((200, 220, 255), (100, 150, 255), 200),  # cool blue
    "urgent":       ((255, 100, 100), (255, 60, 60), 210),    # bold red
    "calm":         ((200, 255, 200), (100, 200, 120), 180),  # soft green
    "neutral":      ((240, 240, 240), (180, 180, 180), 190),  # clean white
}

_MOTIVATIONAL_WORDS = {"success", "growth", "mindset", "achieve", "dream", "goal",
                        "believe", "inspire", "power", "win", "habit", "discipline",
                        "wealth", "freedom", "courage", "passion", "purpose", "hustle"}
_ANALYTICAL_WORDS = {"data", "research", "study", "analysis", "framework", "model",
                      "strategy", "system", "process", "metric", "insight", "trend",
                      "report", "evidence", "statistics", "algorithm", "optimize"}
_URGENT_WORDS = {"breaking", "urgent", "critical", "warning", "alert", "crisis",
                  "threat", "risk", "danger", "important", "deadline", "now", "stop"}
_CALM_WORDS = {"peace", "balance", "mindful", "gratitude", "patience", "reflect",
                "breathe", "slow", "rest", "quiet", "simple", "gentle", "nature"}


def has_text(img: Image.Image, threshold: float = 0.08) -> bool:
    """Detect if image likely contains text via horizontal edge density.
    Threshold raised to avoid false positives on photos with edges."""
    gray = np.array(img.convert("L").resize((300, 300)), dtype=np.float32)
    h_edges = np.abs(np.diff(gray, axis=1))
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


def _detect_vibe(text: str, seed: int) -> str:
    """Analyze text tone to pick a visual vibe."""
    words = set(text.lower().split())
    scores = {
        "motivational": len(words & _MOTIVATIONAL_WORDS),
        "analytical": len(words & _ANALYTICAL_WORDS),
        "urgent": len(words & _URGENT_WORDS),
        "calm": len(words & _CALM_WORDS),
    }
    best = max(scores, key=scores.get)
    if scores[best] == 0:
        return "neutral"
    return best


def _find_calmest_corner(img: Image.Image) -> str:
    """Find the corner quadrant with least color variation.
    Returns 'top-left', 'top-right', 'bottom-left', or 'bottom-right'."""
    small = img.resize((100, 100))
    arr = np.array(small, dtype=np.float32)
    if arr.ndim == 2:
        arr = arr[:, :, np.newaxis]

    h, w = arr.shape[:2]
    mh, mw = h // 2, w // 2

    corners = {
        "top-left": arr[:mh, :mw],
        "top-right": arr[:mh, mw:],
        "bottom-left": arr[mh:, :mw],
        "bottom-right": arr[mh:, mw:],
    }

    best = min(corners, key=lambda k: np.var(corners[k]))
    return best


def _contrast_color(img: Image.Image, y_start: int, y_end: int) -> tuple:
    """Pick text color that contrasts with the background in the target region."""
    region = np.array(img.crop((0, y_start, img.width, y_end)).convert("RGB"))
    avg = region.mean(axis=(0, 1))
    brightness = avg[0] * 0.299 + avg[1] * 0.587 + avg[2] * 0.114
    # Return light text on dark bg, dark text on light bg
    if brightness > 128:
        return (30, 30, 30)
    return (245, 245, 245)


def overlay_text(img: Image.Image, text: str, image_path: str) -> Image.Image:
    """Overlay text with smart placement, contrast color, and tone-based vibe."""
    if not text or len(text.strip()) < 10:
        return img

    seed = int(hashlib.md5(image_path.encode()).hexdigest(), 16)
    w, h = img.size

    # Clean text: remove URLs, trailing whitespace
    import re
    text = re.sub(r'https?://\S+', '', text).strip()
    text = re.sub(r'\s+', ' ', text).strip()
    if len(text) < 10:
        return img

    # Detect vibe from text
    vibe_name = _detect_vibe(text, seed)
    vibe = _VIBES[vibe_name]
    vibe_text_color, accent_color, bg_alpha = vibe

    # Find calmest corner
    corner = _find_calmest_corner(img)
    padding = int(min(w, h) * 0.03)

    # Text goes in a narrow column (40% of width) in the chosen corner
    col_width = int(w * 0.4)

    # Auto-size font to fit all text in the column, max 80% of height
    max_text_h = int(h * 0.8)
    font_size = int(min(w, h) * 0.035)
    font_size = max(10, min(font_size, 40))

    while font_size >= 8:
        font = _get_font(font_size)
        chars_per_line = max(10, int(col_width / (font_size * 0.55)))
        lines = textwrap.wrap(text, width=chars_per_line)
        line_height = font_size + 3
        text_height = len(lines) * line_height
        if text_height <= max_text_h:
            break
        font_size -= 1

    # Corner positioning
    if "left" in corner:
        x_start = padding
    else:
        x_start = w - col_width - padding

    if "top" in corner:
        y_start = padding
    else:
        y_start = h - text_height - padding * 2

    y_start = max(padding, min(y_start, h - text_height - padding))

    # Text color: bright on dark overlay
    text_color = tuple(min(255, int(v * 0.6 + 255 * 0.4)) for v in vibe_text_color)

    # Draw semi-transparent background in corner
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw_ov = ImageDraw.Draw(overlay)
    bg_rect = [x_start - padding, y_start - padding,
               x_start + col_width + padding, y_start + text_height + padding]
    draw_ov.rectangle(bg_rect, fill=(0, 0, 0, bg_alpha))

    if img.mode != "RGBA":
        img = img.convert("RGBA")
    img = Image.alpha_composite(img, overlay)

    # Draw text lines — left-aligned in the column
    draw = ImageDraw.Draw(img)
    y = y_start
    for line in lines:
        draw.text((x_start + 1, y + 1), line, fill=(0, 0, 0, 180), font=font)
        draw.text((x_start, y), line, fill=text_color, font=font)
        y += line_height

    log.info("  📐 Layout: img=%dx%d, corner=%s, col x=%d w=%d, y=%d→%d, font=%dpx, %d lines, vibe=%s",
             w, h, corner, x_start, col_width, y_start, y_start + text_height,
             font_size, len(lines), vibe_name)

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

    log.info("📝 Overlay [%s]: %s", _detect_vibe(text, 0), image_path.name)
    result = overlay_text(img, text.strip(), str(image_path))
    result.save(image_path)
    img.close()
    return True
