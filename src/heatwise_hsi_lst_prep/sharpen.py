# -*- coding: utf-8 -*-
"""
Spatially sharpen / downscale a CHIME-like hyperspectral image (30 m) to 10 m
using Sentinel-2 (10 m).

Method: regression fusion + residual correction (DMS / Data Mining Sharpener idea)
  1. Resample Sentinel-2 (10 m) onto a 10 m grid that is exactly nested inside
     the CHIME grid, then 3x3-average it down to a 30 m "S2_low".
  2. At 30 m, fit a regression model with S2_low (N bands) as predictors and
     CHIME (M bands) as targets.
  3. Apply the model to the 10 m "S2_high" to predict CHIME at 10 m.
  4. Residual correction: aggregate the 10 m prediction back to 30 m, take the
     residual against the original CHIME, smoothly upsample it back to 10 m
     and add it back — this guarantees that averaging the 10 m result 3x3
     reproduces the original 30 m value (radiometric/spectral consistency).

Dependencies: rasterio, numpy, scipy, scikit-learn (optional, only for method='rf')
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import rasterio
from rasterio.warp import reproject, Resampling
from affine import Affine
from scipy.ndimage import zoom, distance_transform_edt

TARGET_RES = 10.0          # target resolution (meters)
RIDGE_LAMBDA = 1e-2        # ridge regularization strength (on standardized features)


def block_mean(arr: np.ndarray, f: int) -> np.ndarray:
    """Block-average (bands, H, W) over f x f blocks (NaN-robust), aggregating
    10m down to 30m. H, W must be divisible by f."""
    b, h, w = arr.shape
    return np.nanmean(arr.reshape(b, h // f, f, w // f, f), axis=(2, 4))


def fill_nan_nearest(arr: np.ndarray, fill_where: np.ndarray) -> np.ndarray:
    """Fill sparse NaN gaps in (bands, H, W) using nearest-neighbor values.
    Only fills where fill_where=True; all bands share the same nearest-neighbor
    index (source = pixels valid across every band)."""
    valid = np.all(np.isfinite(arr), axis=0)
    need = fill_where & ~valid
    if not need.any() or not valid.any():
        return arr
    inv = ~valid
    _, (iy, ix) = distance_transform_edt(inv, return_indices=True)
    out = arr.copy()
    for b in range(arr.shape[0]):
        out[b][need] = arr[b][iy[need], ix[need]]
    return out


def reproject_s2_to_grid(s2_path: Path, dst_crs, dst_transform, dst_w, dst_h,
                          resampling: Resampling) -> np.ndarray:
    """Reproject/resample Sentinel-2 onto the given target grid, returning
    (bands, dst_h, dst_w) float32."""
    with rasterio.open(s2_path) as src:
        out = np.full((src.count, dst_h, dst_w), np.nan, dtype="float32")
        for i in range(src.count):
            reproject(
                source=rasterio.band(src, i + 1),
                destination=out[i],
                src_transform=src.transform,
                src_crs=src.crs,
                dst_transform=dst_transform,
                dst_crs=dst_crs,
                resampling=resampling,
                src_nodata=src.nodata,
                dst_nodata=np.nan,
            )
    return out


def make_predictor(method, X_low, Y_low):
    """Fit once, return a predict(X)->(n,out) callable (memory-friendly, works
    with chunked prediction)."""
    if method == "rf":
        from sklearn.ensemble import RandomForestRegressor
        rf = RandomForestRegressor(
            n_estimators=100, max_depth=None, min_samples_leaf=4,
            n_jobs=-1, random_state=0,
        )
        rf.fit(X_low, Y_low)
        return lambda X: rf.predict(X).astype("float32")

    mu, sd = X_low.mean(0), X_low.std(0) + 1e-6
    Xl = (X_low - mu) / sd
    Xl1 = np.hstack([Xl, np.ones((Xl.shape[0], 1), dtype="float32")])
    nfeat = Xl1.shape[1]
    reg = RIDGE_LAMBDA * np.eye(nfeat, dtype="float64")
    reg[-1, -1] = 0.0
    W = np.linalg.solve(Xl1.T @ Xl1 + reg, Xl1.T @ Y_low)

    def predict(X):
        Xh = (X - mu) / sd
        Xh1 = np.hstack([Xh, np.ones((Xh.shape[0], 1), dtype="float32")])
        return (Xh1 @ W).astype("float32")
    return predict


def sharpen_one(chime_path: Path, s2_path: Path, out_dir: Path, method: str) -> Path | None:
    print(f"\n=== {chime_path.name}  <-  {s2_path.name} ===")
    with rasterio.open(chime_path) as ch:
        chime = ch.read().astype("float32")        # (bands, H, W)
        prof = ch.profile.copy()
        crs = ch.crs
        tr = ch.transform
        H, W = ch.height, ch.width
        nodata = ch.nodata if ch.nodata is not None else -9999.0
        descriptions = ch.descriptions
        band_tags = [ch.tags(i + 1) for i in range(ch.count)]
        ds_tags = ch.tags()

    f = int(round(abs(tr.a) / TARGET_RES))
    if f < 2:
        raise ValueError(f"Resolution ratio = {f}; nothing to sharpen, or config is wrong.")

    dst_transform = tr * Affine.scale(1.0 / f)
    dst_h, dst_w = H * f, W * f

    print(f"  Resampling Sentinel-2 -> {TARGET_RES:.0f}m nested grid ({dst_h}x{dst_w}) ...")
    s2_high = reproject_s2_to_grid(s2_path, crs, dst_transform, dst_w, dst_h, Resampling.bilinear)

    n_s2 = s2_high.shape[0]
    n_hs = chime.shape[0]

    chime_valid = np.all(chime != nodata, axis=0) & np.all(np.isfinite(chime), axis=0)
    foot_high = zoom(chime_valid.astype("uint8"), f, order=0).astype(bool)

    n_nan = int((foot_high & np.any(~np.isfinite(s2_high), axis=0)).sum())
    if n_nan:
        print(f"  S2 gap pixels inside footprint: {n_nan} -> nearest-neighbor fill")
        s2_high = fill_nan_nearest(s2_high, foot_high)

    s2_low = block_mean(s2_high, f)

    s2_low_valid = np.all(np.isfinite(s2_low), axis=0)
    train_mask = chime_valid & s2_low_valid
    n_train = int(train_mask.sum())
    if n_train < 50:
        print(f"  [skip] Too few valid training pixels ({n_train}); scenes may not overlap.")
        return None
    print(f"  Training pixels: {n_train} / {H * W}")

    X_low = s2_low[:, train_mask].T.astype("float32")
    Y_low = chime[:, train_mask].T.astype("float32")

    print(f"  Fitting and predicting (method={method}) ...")
    predict_fn = make_predictor(method, X_low, Y_low)

    s2_high_flat = s2_high.reshape(n_s2, -1).T
    idx_valid = np.where(np.all(np.isfinite(s2_high_flat), axis=1))[0]
    pred_img = np.zeros((n_hs, dst_h, dst_w), dtype="float32")
    pred_flat = pred_img.reshape(n_hs, -1)
    CHUNK = 400_000
    for s in range(0, idx_valid.size, CHUNK):
        ii = idx_valid[s:s + CHUNK]
        pred_flat[:, ii] = predict_fn(s2_high_flat[ii]).T
    del s2_high_flat

    pred_low = block_mean(pred_img, f)
    resid_low = np.where(train_mask[None], chime - pred_low, 0.0).astype("float32")
    del pred_low
    for i in range(n_hs):
        pred_img[i] += zoom(resid_low[i], f, order=1, mode="nearest")
    out = pred_img

    valid_high = zoom(train_mask.astype("uint8"), f, order=0).astype(bool)
    out[:, ~valid_high] = nodata

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{chime_path.stem}_sharp10m.tif"
    prof.update(
        height=dst_h, width=dst_w, transform=dst_transform,
        dtype="float32", nodata=nodata, count=n_hs,
        compress="deflate", predictor=2, tiled=True,
        blockxsize=256, blockysize=256,
    )
    with rasterio.open(out_path, "w", **prof) as dst:
        dst.write(out)
        for i in range(n_hs):
            if descriptions[i]:
                dst.set_band_description(i + 1, descriptions[i])
            if band_tags[i]:
                dst.update_tags(i + 1, **band_tags[i])
        if ds_tags:
            dst.update_tags(**ds_tags)
    print(f"  Saved: {out_path}  ({n_hs} bands, {dst_h}x{dst_w}, {TARGET_RES:.0f}m)")
    return out_path


def run_sharpen(pairs, output_dir: Path, method: str = "linear") -> list[Path]:
    """Process each pair. pairs = [(CHIME path, Sentinel-2 path), ...]; output_dir = output directory."""
    output_dir = Path(output_dir)
    print(f"Target resolution: {TARGET_RES:.0f} m | Regression method: {method}")
    outputs = []
    for chime_path, s2_path in pairs:
        chime_path, s2_path = Path(chime_path), Path(s2_path)
        if not chime_path.exists():
            print(f"[warning] Missing CHIME file: {chime_path}")
            continue
        if not s2_path.exists():
            print(f"[warning] Missing S2 file: {s2_path}")
            continue
        try:
            out_path = sharpen_one(chime_path, s2_path, output_dir, method)
            if out_path is not None:
                outputs.append(out_path)
        except Exception as e:
            print(f"[error] Failed to process {chime_path.name}: {e}")
    print("\nAll done. Results in:", output_dir)
    return outputs
