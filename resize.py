"""Resize to fit 1920x1080 maximizing content, and compress. Never rotates."""
from PIL import Image
from pathlib import Path
import numpy as np
import io


def detect_content_bbox(img: Image.Image, border: int = 1) -> tuple[int, int, int, int]:
    """Find bounding box of non-background content, keeping at least `border` px padding.
    Detects background as the most common edge color."""
    arr = np.array(img.convert("RGB"))
    h, w = arr.shape[:2]

    # Sample edge pixels to determine background color
    edges = np.concatenate([arr[0], arr[-1], arr[:, 0], arr[:, -1]])
    # Most common color (round to nearest 8 to handle compression artifacts)
    quantized = (edges // 8) * 8
    unique, counts = np.unique(quantized.reshape(-1, 3), axis=0, return_counts=True)
    bg = unique[counts.argmax()].astype(np.float32)

    # Mask: pixels that differ from background beyond a tolerance
    diff = np.sqrt(((arr.astype(np.float32) - bg) ** 2).sum(axis=2))
    mask = diff > 30  # tolerance for compression artifacts

    if not mask.any():
        return (0, 0, w, h)

    rows = np.any(mask, axis=1)
    cols = np.any(mask, axis=0)
    y0, y1 = np.where(rows)[0][[0, -1]]
    x0, x1 = np.where(cols)[0][[0, -1]]

    # Ensure at least `border` px padding on all sides
    x0 = max(0, x0 - border)
    y0 = max(0, y0 - border)
    x1 = min(w, x1 + 1 + border)
    y1 = min(h, y1 + 1 + border)

    return (x0, y0, x1, y1)


def crop_to_content(img: Image.Image, border: int = 1) -> Image.Image:
    """Crop to content bounding box, always leaving at least `border` px around content."""
    bbox = detect_content_bbox(img, border)
    # Only crop if it actually removes something meaningful (>5% per side)
    w, h = img.size
    bw, bh = bbox[2] - bbox[0], bbox[3] - bbox[1]
    if bw < w * 0.9 or bh < h * 0.9:
        return img.crop(bbox)
    return img


def resize_to_fill_dimension(img: Image.Image, tw: int = 1920, th: int = 1080) -> Image.Image:
    """Scale so content fills as much of one target dimension as possible.
    Maintains aspect ratio. Never upscales beyond 2x. Never rotates."""
    w, h = img.size
    scale = min(tw / w, th / h, 2.0)
    new_w, new_h = int(w * scale), int(h * scale)
    if new_w == w and new_h == h:
        return img
    return img.resize((new_w, new_h), Image.LANCZOS)


def compress_if_needed(img: Image.Image, path: Path, max_kb: int = 500) -> None:
    """Save image, compressing quality iteratively if file exceeds max_kb."""
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    if buf.tell() <= max_kb * 1024:
        path.write_bytes(buf.getvalue())
        return

    for q in (95, 85, 75, 60, 45):
        buf = io.BytesIO()
        img.convert("RGB").save(buf, format="JPEG", quality=q, optimize=True)
        if buf.tell() <= max_kb * 1024:
            path = path.with_suffix(".jpg")
            path.write_bytes(buf.getvalue())
            return

    path = path.with_suffix(".jpg")
    img.convert("RGB").save(path, format="JPEG", quality=30, optimize=True)


def process(img: Image.Image, out_path: Path, tw: int = 1920, th: int = 1080, max_kb: int = 500) -> Path:
    cropped = crop_to_content(img, border=1)
    resized = resize_to_fill_dimension(cropped, tw, th)
    compress_if_needed(resized, out_path, max_kb)
    return out_path
