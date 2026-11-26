"""Nonblank/dark-theme checks for the native application's rendered pages."""

from pathlib import Path

from PIL import Image, ImageStat

directory = Path(__file__).resolve().parents[2] / "12_Logs/ControlCenter-QA"
files = sorted(directory.glob("*/*.png"))
assert len(files) >= 11, "Native page screenshots are missing"
for path in files:
    with Image.open(path) as source:
        image = source.convert("RGB")
        assert image.width >= 600 and image.height >= 450, path.name
        assert sum(ImageStat.Stat(image).var) > 50, f"Blank rendering: {path.name}"
        histogram = image.convert("L").histogram()
        dark_fraction = sum(histogram[:100]) / (image.width * image.height)
        assert dark_fraction > 0.6, f"Unexpected light rendering: {path.name}"
    print("PASS", path.name)
print(f"{len(files)} native screenshots passed pixel checks.")
