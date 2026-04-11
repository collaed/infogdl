"""Image analysis: color counting, fill rate, orientation."""
import numpy as np
from PIL import Image
from sklearn.cluster import MiniBatchKMeans


def count_dominant_colors(img: Image.Image, max_k: int = 20, threshold: float = 0.02) -> int:
    """Count distinct dominant colors using k-means clustering.
    Colors making up less than `threshold` of pixels are ignored."""
    small = img.copy()
    small.thumbnail((150, 150))
    pixels = np.array(small.convert("RGB")).reshape(-1, 3).astype(np.float32)

    k = min(max_k, len(pixels))
    km = MiniBatchKMeans(n_clusters=k, n_init=1, random_state=0).fit(pixels)
    _, counts = np.unique(km.labels_, return_counts=True)
    proportions = counts / counts.sum()
    return int((proportions >= threshold).sum())


def compute_fill_rate(img: Image.Image) -> float:
    """Estimate how much of the image is 'content' vs background.
    Uses edge density as a proxy for information density."""
    gray = np.array(img.convert("L"), dtype=np.float32)
    # Sobel-like gradient magnitude
    gx = np.abs(np.diff(gray, axis=1))
    gy = np.abs(np.diff(gray, axis=0))
    # Normalize to 0-1
    edge_map = np.zeros(gray.shape)
    edge_map[:, 1:] += gx
    edge_map[1:, :] += gy
    edge_map = edge_map / (edge_map.max() + 1e-9)
    # Fraction of pixels above a low threshold = "filled"
    return float((edge_map > 0.05).sum() / edge_map.size)


def get_orientation(img: Image.Image) -> str:
    w, h = img.size
    return "horizontal" if w >= h else "vertical"


def is_infographic(img: Image.Image, max_colors: int = 15,
                   max_gradient_bands: int = 4) -> tuple[bool, dict]:
    """Detect if an image is likely an infographic vs a photo.

    Infographics have:
    - Limited distinct colors (flat design, not continuous tones)
    - Few gradient bands (smooth transitions are limited)
    - High color uniformity within regions

    Photos have:
    - Full palette (thousands of subtle color variations)
    - Many gradient bands (sky, skin, shadows)
    - Low uniformity (every pixel slightly different)

    Returns (is_infographic, details_dict).
    """
    small = img.copy()
    small.thumbnail((150, 150))
    pixels = np.array(small.convert("RGB")).reshape(-1, 3).astype(np.float32)

    # 1. Count dominant colors
    k = min(20, len(pixels))
    km = MiniBatchKMeans(n_clusters=k, n_init=1, random_state=0).fit(pixels)
    _, counts = np.unique(km.labels_, return_counts=True)
    proportions = counts / counts.sum()
    n_colors = int((proportions >= 0.02).sum())

    # 2. Count gradient bands — how many smooth color transitions exist
    # Sample vertical and horizontal lines, count distinct color steps
    arr = np.array(small.convert("RGB"), dtype=np.float32)
    h, w = arr.shape[:2]

    gradient_count = 0
    for line in [arr[h // 4, :], arr[h // 2, :], arr[3 * h // 4, :],
                 arr[:, w // 4], arr[:, w // 2], arr[:, 3 * w // 4]]:
        # Count color transitions (jumps > 15 in any channel)
        diffs = np.max(np.abs(np.diff(line, axis=0)), axis=1)
        transitions = (diffs > 15).sum()
        # Gradients = smooth areas between transitions
        smooth_runs = len(line) - transitions
        if smooth_runs > len(line) * 0.3:
            gradient_count += 1

    # 3. Color uniformity — what % of pixels are close to a cluster center
    distances = np.min(np.linalg.norm(
        pixels[:, np.newaxis] - km.cluster_centers_[np.newaxis, :], axis=2), axis=1)
    uniformity = float((distances < 25).sum() / len(distances))

    is_info = (n_colors <= max_colors and
               gradient_count <= max_gradient_bands and
               uniformity > 0.6)

    details = {
        "distinct_colors": n_colors,
        "gradient_bands": gradient_count,
        "uniformity": round(uniformity, 3),
        "is_infographic": is_info,
    }
    return is_info, details


def analyze(img: Image.Image) -> dict:
    is_info, info_details = is_infographic(img)
    return {
        "colors": count_dominant_colors(img),
        "fill_rate": compute_fill_rate(img),
        "orientation": get_orientation(img),
        "is_infographic": is_info,
        **info_details,
    }
