#!/usr/bin/env python3
"""Regenerate custom_components/comfort_band/brand/icon.png and icon@2x.png.

Usage: uv run --with pillow python scripts/make_brand_icon.py custom_components/comfort_band/brand
(also writes preview.png there; delete it before committing)
"""

import math
import sys
from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(sys.argv[1])
SS = 2048  # supersampled canvas, downscaled for anti-aliasing


def lerp(a, b, t):
    return tuple(round(a[i] + (b[i] - a[i]) * t) for i in range(3))


WARM = (255, 138, 61)  # top: too warm
COOL = (47, 128, 237)  # bottom: too cool
MID = (38, 166, 154)  # inside the band: comfortable

# Vertical gradient warm -> teal -> cool
grad = Image.new("RGB", (SS, SS))
px = grad.load()
for y in range(SS):
    t = y / (SS - 1)
    c = lerp(WARM, MID, t / 0.5) if t < 0.5 else lerp(MID, COOL, (t - 0.5) / 0.5)
    for x in range(SS):
        px[x, y] = c

# Rounded-square mask filling the canvas (trimmed: no padding)
mask = Image.new("L", (SS, SS), 0)
ImageDraw.Draw(mask).rounded_rectangle([0, 0, SS - 1, SS - 1], radius=int(SS * 0.22), fill=255)
img = Image.new("RGBA", (SS, SS), (0, 0, 0, 0))
img.paste(grad, (0, 0), mask)

fill_layer = Image.new("RGBA", (SS, SS), (0, 0, 0, 0))
fd = ImageDraw.Draw(fill_layer)
line_layer = Image.new("RGBA", (SS, SS), (0, 0, 0, 0))
d = ImageDraw.Draw(line_layer)
band_top, band_bot = int(SS * 0.36), int(SS * 0.64)
margin = int(SS * 0.12)
# The comfort band: a soft white fill between two white edges
fd.rectangle([margin, band_top, SS - margin, band_bot], fill=(255, 255, 255, 70))
lw = int(SS * 0.035)
for y in (band_top, band_bot):
    d.rounded_rectangle(
        [margin, y - lw // 2, SS - margin, y + lw // 2], radius=lw // 2, fill=(255, 255, 255, 235)
    )

# Temperature line: a gentle wave that stays inside the band
amp = (band_bot - band_top) * 0.28
cy = (band_top + band_bot) / 2
pts = []
for i in range(400):
    x = margin + (SS - 2 * margin) * i / 399
    pts.append((x, cy + amp * math.sin(2 * math.pi * 1.5 * i / 399)))
wl = int(SS * 0.05)
for p in pts:  # stamp discs along the path: a smooth, round-capped thick line
    d.ellipse(
        [p[0] - wl / 2, p[1] - wl / 2, p[0] + wl / 2, p[1] + wl / 2], fill=(255, 255, 255, 255)
    )

img = Image.alpha_composite(img, fill_layer)
img = Image.alpha_composite(img, line_layer)
OUT.mkdir(parents=True, exist_ok=True)
img.resize((512, 512), Image.LANCZOS).save(OUT / "icon@2x.png", optimize=True)
img.resize((256, 256), Image.LANCZOS).save(OUT / "icon.png", optimize=True)
# Preview on light and dark backgrounds, at a few sizes
prev = Image.new("RGB", (512 + 256 + 64 + 48, 560), (255, 255, 255))
dark = Image.new("RGB", (prev.width, 560), (28, 28, 30))
for bg in (prev, dark):
    x = 0
    for size in (512, 256, 64, 32):
        bg.paste(
            img.resize((size, size), Image.LANCZOS), (x, 0), img.resize((size, size), Image.LANCZOS)
        )
        x += size + 8
both = Image.new("RGB", (prev.width, 1120))
both.paste(prev, (0, 0))
both.paste(dark, (0, 560))
both.save(OUT / "preview.png")
print("written", sorted(p.name for p in OUT.iterdir()))
