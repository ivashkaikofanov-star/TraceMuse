"""Local, bounded comparison of graphics with a small reference catalog."""

import json
from functools import lru_cache
from pathlib import Path
from statistics import median

from PIL import Image, ImageChops

SAMPLE_SIZE = 32


def _foreground(image: Image.Image) -> Image.Image:
    channels = image.split()
    corners = [(0, 0), (0, image.height - 1), (image.width - 1, 0), (image.width - 1, image.height - 1)]
    differences = [
        ImageChops.difference(channel, Image.new('L', image.size, int(median(channel.getpixel(point) for point in corners))))
        for channel in channels
    ]
    difference = ImageChops.lighter(ImageChops.lighter(differences[0], differences[1]), differences[2])
    return difference.point(lambda value: 255 if value > 64 else 0)


def _regions(mask: Image.Image) -> list[tuple]:
    bounds = mask.getbbox()
    if bounds is None:
        return []
    left, top, right, bottom = bounds
    regions = [bounds]
    if right - left <= (bottom - top) * 2:
        return regions
    projection = mask.crop(bounds).resize((right - left, 1), Image.Resampling.BOX).tobytes()
    min_gap = max(5, len(projection) // 40)
    runs = []
    for stop, visibility in enumerate(projection):
        if visibility >= 4:
            if runs and stop - runs[-1] < min_gap:
                runs[-1] = stop
            else:
                runs.extend([stop, stop])
    for start, stop in zip(runs[::2], runs[1::2]):
        if stop - start < 24:
            continue
        if child := mask.crop((left + start, top, left + stop + 1, bottom)).getbbox():
            regions.append((left + start + child[0], top + child[1], left + start + child[2], top + child[3]))
    return regions[:7] if len(regions) > 2 else [bounds]


def graphic_signatures(image: Image.Image, *, split: bool = False) -> list[dict]:
    if min(image.size) < 36:
        return []
    # Resize before compositing to keep temporary image allocations bounded.
    scale = min(1, 512 / max(image.size))
    size = tuple(max(1, round(dimension * scale)) for dimension in image.size)
    sample = image if size == image.size else image.resize(size, Image.Resampling.LANCZOS)
    rgba = sample.convert('RGBA')
    rgb = Image.new('RGB', sample.size, 'white')
    rgb.paste(rgba, mask=rgba.getchannel('A'))
    mask = _foreground(rgb)
    bounds = _regions(mask) if split else [mask.getbbox()]
    signatures = []
    for part, box in enumerate(bounds):
        if not box:
            continue
        crop = mask.crop(box)
        if min(crop.size) < 24:
            continue
        bitmap = crop.resize((SAMPLE_SIZE, SAMPLE_SIZE), Image.Resampling.BOX).point(lambda value: 255 if value >= 128 else 0)
        bits = int.from_bytes(bitmap.convert('1').tobytes())
        density = bits.bit_count() / (SAMPLE_SIZE * SAMPLE_SIZE)
        if not 0.12 <= density <= 0.9:
            continue
        color_counts = {'red': 0, 'blue': 0}
        pixels = rgb.crop(box).resize((SAMPLE_SIZE, SAMPLE_SIZE), Image.Resampling.BOX).tobytes()
        for r, g, b in zip(pixels[::3], pixels[1::3], pixels[2::3]):
            if r >= 100 and r >= max(g, b) + 50:
                color_counts['red'] += 1
            if b >= 65 and b >= r + 40 and g >= r + 25:
                color_counts['blue'] += 1
        signatures.append({
            'shape': f'{bits:0256x}',
            'aspect': round(crop.width / crop.height, 3),
            'colors': [name for name, count in color_counts.items() if count >= 60],
            'part': part,
        })
    return signatures


@lru_cache(maxsize=1)
def reference_catalog() -> list[dict]:
    return json.loads(Path(__file__).with_name('assets').joinpath('logo_references.json').read_text(encoding='utf-8'))


def identify_graphics(image: Image.Image, *, split: bool = False) -> list[dict]:
    matches = {}
    for signature in graphic_signatures(image, split=split):
        shape = int(signature['shape'], 16)
        scores = []
        for entry in reference_catalog():
            ref_shape = int(entry['shape'], 16)
            if not set(entry['colors']).issubset(signature['colors']):
                continue
            if not 0.83 <= signature['aspect'] / entry['aspect'] <= 1.2:
                continue
            overlap = (shape & ref_shape).bit_count() / (shape | ref_shape).bit_count()
            if overlap >= 0.85:
                scores.append((overlap, entry))
        if not scores:
            continue
        score, entry = max(scores, key=lambda candidate: candidate[0])
        if any(other['brand'] != entry['brand'] and score - overlap < 0.04 for overlap, other in scores):
            continue
        title = entry['title']
        if title not in matches or matches[title][0] < score:
            matches[title] = (score, {
                'brand': entry['brand'], 'title': title, 'url': entry['url'], 'part': signature['part'],
            })
    return [match for _, match in matches.values()]
