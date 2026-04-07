# infogdl

Download, analyze, sort, and resize infographics from LinkedIn and Twitter — or process a local directory of images.

Images are classified by orientation (vertical/horizontal), number of dominant colors, and information density (fill rate), then sorted into a structured folder tree. Each image is cropped to its content (with a 1px border preserved), scaled to fit within 1920×1080, and compressed if oversized. No rotation is ever applied.

## Output structure

```
output/
├── horizontal/
│   ├── low_colors/
│   │   ├── sparse_fill/
│   │   ├── moderate_fill/
│   │   └── dense_fill/
│   ├── medium_colors/
│   └── high_colors/
└── vertical/
    └── ... (same structure)
```

## Setup

**Windows:**
```
setup.bat
```

**Linux / macOS:**
```bash
./setup.sh
```

Or manually:
```bash
pip install -r requirements.txt
```

Requires Python 3.10+. Scraping mode also needs Chrome + [chromedriver](https://googlechromelabs.github.io/chrome-for-testing/).

## Usage

### Process a local directory

```bash
python infogdl.py -i /path/to/images -o /path/to/output
```

Recursively finds all images (png, jpg, webp, gif, bmp, tiff) in the input directory and its subdirectories.

### Scrape from LinkedIn / Twitter

1. Log into LinkedIn and/or Twitter in Chrome or Firefox
2. Edit `config.json` with the profile URLs you want to scrape
3. Run:

```bash
python infogdl.py
```

The tool borrows your browser session cookies — no credentials are stored or requested.

### Options

| Flag | Description |
|------|-------------|
| `-i`, `--input` | Input directory of images (recursive) |
| `-o`, `--output` | Output directory (overrides config) |
| `-c`, `--config` | Config file path (default: `config.json`) |

## Configuration

`config.json` controls all parameters:

| Key | Default | Description |
|-----|---------|-------------|
| `target_width` / `target_height` | 1920 / 1080 | Max dimensions to scale into |
| `max_file_size_kb` | 500 | Compress if file exceeds this size |
| `color_bins` | low/medium/high | Thresholds for color count classification |
| `fill_bins` | sparse/moderate/dense | Thresholds for information density |
| `headless` | true | Run browser in headless mode |
| `scroll_count` | 5 | Number of page scrolls when scraping |

## How it works

1. **Scrape** — Selenium opens each profile, scrolls to load content, collects images >200px (skips avatars/icons)
2. **Analyze** — Each image is measured for dominant color count (k-means clustering), fill rate (edge density), and orientation
3. **Sort** — Placed into subfolders based on the analysis
4. **Crop** — Content bounding box detected via background color sampling; at least 1px border always preserved
5. **Resize** — Scaled to fill one dimension of the target size, maintaining aspect ratio (max 2× upscale)
6. **Compress** — PNG if small enough, otherwise JPEG at decreasing quality until under the size limit

## License

MIT
