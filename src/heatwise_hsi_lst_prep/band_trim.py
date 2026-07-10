"""Trim a hyperspectral GeoTIFF down to a fixed set of bands.

Two equivalent ways to specify which bands survive:
  - keep-ranges:   1-based inclusive ranges of bands to KEEP (e.g. raw 250-band
                    CHIME product -> common valid 124 bands).
  - remove-ranges: 1-based inclusive ranges of bands to DROP (e.g. an already
                    trimmed EnMAP-mimicked product -> drop atmospheric windows).

Exactly one of the two must be given; both express the same operation.
"""
from __future__ import annotations

from pathlib import Path

import rasterio


def expand_ranges(ranges: tuple[tuple[int, int], ...]) -> list[int]:
    bands: list[int] = []
    for start, end in ranges:
        if start < 1 or end < start:
            raise ValueError(f"Invalid band range: {start}-{end}")
        bands.extend(range(start, end + 1))
    return bands


def resolve_keep_bands(
    total_bands: int,
    keep_ranges: tuple[tuple[int, int], ...] | None = None,
    remove_ranges: tuple[tuple[int, int], ...] | None = None,
) -> list[int]:
    if (keep_ranges is None) == (remove_ranges is None):
        raise ValueError("Provide exactly one of keep_ranges or remove_ranges.")

    if keep_ranges is not None:
        keep = expand_ranges(keep_ranges)
        invalid = [b for b in keep if b > total_bands]
        if invalid:
            raise ValueError(f"Image has only {total_bands} bands; cannot keep {invalid[:5]}")
        return sorted(keep)

    remove = set(expand_ranges(remove_ranges))
    return [band for band in range(1, total_bands + 1) if band not in remove]


def copy_band_tags(src: rasterio.DatasetReader, dst: rasterio.DatasetWriter, keep_bands: list[int]) -> None:
    dataset_tags = src.tags()
    if "chime_band_count" in dataset_tags:
        dataset_tags["chime_band_count"] = str(len(keep_bands))
    dst.update_tags(**dataset_tags)

    for new_band, old_band in enumerate(keep_bands, start=1):
        tags = src.tags(old_band)
        if tags:
            dst.update_tags(new_band, **tags)


def trim_bands(input_path: Path, output_path: Path, keep_bands_1based: list[int]) -> None:
    input_path, output_path = Path(input_path), Path(output_path)
    with rasterio.open(input_path) as src:
        if max(keep_bands_1based) > src.count:
            raise ValueError(f"{input_path} has only {src.count} bands; cannot keep band {max(keep_bands_1based)}")

        profile = src.profile.copy()
        profile.update(count=len(keep_bands_1based))

        output_path.parent.mkdir(parents=True, exist_ok=True)

        with rasterio.open(output_path, "w", **profile) as dst:
            for new_band, old_band in enumerate(keep_bands_1based, start=1):
                dst.write(src.read(old_band), new_band)
                dst.set_band_description(new_band, src.descriptions[old_band - 1])

            copy_band_tags(src, dst, keep_bands_1based)

    print(f"[band_trim] Saved {output_path} ({len(keep_bands_1based)} bands kept)")


def parse_band_ranges(value: str) -> tuple[tuple[int, int], ...]:
    ranges = []
    for item in value.split(","):
        item = item.strip()
        if not item:
            continue
        start_text, sep, end_text = item.partition("-")
        if not sep:
            start = end = int(start_text)
        else:
            start, end = int(start_text), int(end_text)
        ranges.append((start, end))
    return tuple(ranges)


def run_band_trim(
    input_path: Path,
    output_path: Path,
    keep_ranges: str | None = None,
    remove_ranges: str | None = None,
) -> Path:
    keep = parse_band_ranges(keep_ranges) if keep_ranges else None
    remove = parse_band_ranges(remove_ranges) if remove_ranges else None
    with rasterio.open(input_path) as src:
        total_bands = src.count
    keep_bands = resolve_keep_bands(total_bands, keep_ranges=keep, remove_ranges=remove)
    trim_bands(Path(input_path), Path(output_path), keep_bands)
    return Path(output_path)
