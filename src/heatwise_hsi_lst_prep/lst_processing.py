# -*- coding: utf-8 -*-
"""
Resample an LST band (e.g. one band of an ECOSTRESS/LSTM image) onto the
reference (HSI) 10m grid and normalize it, writing out a raster.

Two normalization modes (selected via a parameter, not hardcoded per city):
  - zscore:      (x - mean) / std, mean/std computed from this scene's valid
                 pixels and saved to the sidecar json so it can be recovered.
  - fixed_scale: (K - kelvin_offset) / fixed_scale, independent of scene
                 statistics, so absolute temperature stays comparable across
                 scenes.

Output: <output>.tif (float32, nodata=-9999) + <output>.json (normalization
parameters, for downstream reproducibility / inversion).
"""
from __future__ import annotations

import json
import os

import numpy as np
import rasterio
from rasterio.warp import reproject, Resampling

NODATA = -9999.0


def _reproject_band_to_grid(src_path, band, ref_transform, ref_crs, ref_h, ref_w, resampling):
    out = np.full((ref_h, ref_w), NODATA, dtype="float32")
    with rasterio.open(src_path) as src:
        print(f"[lst] Source: {src.count} bands, {src.res}, {src.crs}, nodata={src.nodata}")
        reproject(
            source=rasterio.band(src, band), destination=out,
            src_transform=src.transform, src_crs=src.crs,
            dst_transform=ref_transform, dst_crs=ref_crs,
            resampling=resampling, src_nodata=src.nodata, dst_nodata=NODATA,
        )
    return out


def normalize_zscore(lst, valid_mask):
    mean = float(lst[valid_mask].mean())
    std = float(lst[valid_mask].std())
    out = np.full(lst.shape, NODATA, dtype="float32")
    out[valid_mask] = ((lst[valid_mask] - mean) / (std + 1e-8)).astype("float32")
    params = {"method": "zscore", "mean": mean, "std": std}
    return out, params


def normalize_fixed_scale(lst, valid_mask, kelvin_offset=273.15, fixed_scale=50.0):
    out = np.full(lst.shape, NODATA, dtype="float32")
    out[valid_mask] = ((lst[valid_mask] - kelvin_offset) / fixed_scale).astype("float32")
    params = {"method": "fixed_scale", "kelvin_offset": kelvin_offset, "fixed_scale": fixed_scale}
    return out, params


def run_lst_processing(
    input_path: str,
    band: int,
    ref_grid_path: str,
    output_path: str,
    normalization: str = "zscore",
    kelvin_offset: float = 273.15,
    fixed_scale: float = 50.0,
) -> str:
    with rasterio.open(ref_grid_path) as ref:
        H, W = ref.height, ref.width
        tr, crs = ref.transform, ref.crs
        prof = ref.profile.copy()

    lst_raw = _reproject_band_to_grid(input_path, band, tr, crs, H, W, Resampling.bilinear)
    valid_mask = (lst_raw != NODATA) & np.isfinite(lst_raw)
    print(f"[lst] Resampled onto reference grid {H}x{W}; valid fraction {100 * valid_mask.mean():.1f}%")
    if not valid_mask.any():
        raise SystemExit(
            f"[lst] No valid/overlapping pixels between {input_path!r} and the reference grid "
            f"{ref_grid_path!r}. This usually means the LST source doesn't actually cover this "
            f"city's extent (wrong file for this city) or the band index is wrong."
        )

    if normalization == "zscore":
        lst_norm, params = normalize_zscore(lst_raw, valid_mask)
    elif normalization == "fixed_scale":
        lst_norm, params = normalize_fixed_scale(lst_raw, valid_mask, kelvin_offset, fixed_scale)
    else:
        raise ValueError(f"Unknown normalization: {normalization!r} (use 'zscore' or 'fixed_scale')")

    vv = lst_norm[valid_mask]
    print(f"[lst] Normalized ({normalization}): min/med/max = {vv.min():.3f}/{np.median(vv):.3f}/{vv.max():.3f}")

    output_path = str(output_path)
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    prof.update(count=1, dtype="float32", nodata=NODATA,
                compress="deflate", tiled=True, blockxsize=256, blockysize=256)
    with rasterio.open(output_path, "w", **prof) as dst:
        dst.write(lst_norm, 1)
        dst.update_tags(lst_source=f"{os.path.basename(input_path)} band{band}, bilinear -> ref grid")

    sidecar = {
        "input": str(input_path),
        "band": band,
        "ref_grid": str(ref_grid_path),
        "valid_fraction": float(valid_mask.mean()),
        **params,
    }
    json_path = os.path.splitext(output_path)[0] + ".json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(sidecar, f, indent=2, ensure_ascii=False)

    print(f"[lst] Saved: {output_path}  (+ {json_path})")
    return output_path
