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


def analyze(img: Image.Image) -> dict:
    return {
        "colors": count_dominant_colors(img),
        "fill_rate": compute_fill_rate(img),
        "orientation": get_orientation(img),
    }
