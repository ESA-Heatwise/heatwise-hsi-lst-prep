# -*- coding: utf-8 -*-
"""Apply the bands chosen by band_selection.py to one (already-sharpened) image."""
from __future__ import annotations

import json
import os

import rasterio


def load_selected_bands_1based(selection_json: str):
    with open(selection_json, "r", encoding="utf-8") as f:
        sel = json.load(f)
    bands = sel["final_bands_1based"]
    wl = sel.get("final_wavelengths_um", [None] * len(bands))
    print(f"Loaded {len(bands)} selected bands from json: {bands}")
    return bands, wl


def apply_band_selection(in_path: str, out_path: str, bands_1based: list[int], wavelengths=None) -> None:
    """Extract the given 1-based bands from a multi-band TIF, band-by-band
    (low memory), preserving georeferencing."""
    wavelengths = wavelengths or [None] * len(bands_1based)
    with rasterio.open(in_path) as src:
        if max(bands_1based) > src.count:
            raise ValueError(f"{os.path.basename(in_path)} only has {src.count} bands; "
                              f"cannot select band {max(bands_1based)}")
        meta = src.meta.copy()
        meta.update(count=len(bands_1based), driver="GTiff", compress="deflate")
        descs = src.descriptions

        os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
        with rasterio.open(out_path, "w", **meta) as dst:
            for i, b in enumerate(bands_1based):
                dst.write(src.read(b), i + 1)
                desc = descs[b - 1] if descs and descs[b - 1] else (
                    f"{wavelengths[i]}um" if wavelengths[i] is not None else f"band{b}")
                dst.set_band_description(i + 1, desc)
                if wavelengths[i] is not None:
                    dst.update_tags(i + 1, wavelength_um=str(wavelengths[i]))
    print(f"[apply_band_selection] Saved {out_path} ({len(bands_1based)} bands)")


def run_apply_band_selection(input_path: str, selection_json: str, output_path: str) -> str:
    bands_1based, wavelengths = load_selected_bands_1based(selection_json)
    apply_band_selection(input_path, output_path, bands_1based, wavelengths)
    return output_path
