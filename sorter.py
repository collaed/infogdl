"""Sort downloaded images into subfolders based on analysis."""
from pathlib import Path


def bin_value(value: float, bins: dict[str, list]) -> str:
    for label, (lo, hi) in bins.items():
        if lo <= value < hi or (value == hi and hi == max(b[1] for b in bins.values())):
            return label
    return list(bins.keys())[-1]


def sort_path(orientation: str, colors: int, fill_rate: float,
              color_bins: dict, fill_bins: dict) -> str:
    """Return relative subfolder path: orientation/colors_X/fill_Y"""
    color_label = bin_value(colors, color_bins)
    fill_label = bin_value(fill_rate, fill_bins)
    return f"{orientation}/{color_label}_colors/{fill_label}_fill"
