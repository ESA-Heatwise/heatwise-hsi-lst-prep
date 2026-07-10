# -*- coding: utf-8 -*-
"""
Cross-city PCA dimensionality reduction (unified parameters).

"Unified parameters" means: valid pixels from all cities are pooled and used
to fit ONE PCA (mean + components), then that same model transforms every
city -- so each city's PCA scores live in the same feature space and are
directly comparable.

Input: the 10m sharpened (all-band) image (the same input `apply_band_selection`
uses). nodata is masked throughout and never enters fitting/transform. Large
scenes fit on a downsampled sample and transform in row-blocks to bound memory.
"""
from __future__ import annotations

import json
import os

import numpy as np
import rasterio
from rasterio.windows import Window
from rasterio.enums import Resampling

NODATA = -9999.0


def sample_city(path: str, sample_max_dim: int, sample_per_city: int, rng, nodata: float = NODATA) -> np.ndarray:
    with rasterio.open(path) as src:
        b, H, W = src.count, src.height, src.width
        scale = max(1.0, max(H, W) / sample_max_dim)
        oh, ow = max(1, int(H / scale)), max(1, int(W / scale))
        # Nearest-neighbor downsampling to keep real pixel spectra (no averaging).
        arr = src.read(out_shape=(b, oh, ow), resampling=Resampling.nearest).astype("float32")
    flat = arr.reshape(b, -1)
    valid = np.all(flat != nodata, axis=0) & np.all(np.isfinite(flat), axis=0)
    pix = flat[:, valid].T                      # (Nvalid, b)
    if pix.shape[0] > sample_per_city:
        idx = rng.choice(pix.shape[0], sample_per_city, replace=False)
        pix = pix[idx]
    return pix


def fit_pca(samples: np.ndarray, standardize: bool, var_threshold: float, max_components: int) -> dict:
    mean = samples.mean(axis=0)
    std = samples.std(axis=0) + 1e-8 if standardize else np.ones(samples.shape[1], "float64")
    Xc = (samples - mean) / std
    U, S, Vt = np.linalg.svd(Xc, full_matrices=False)
    evr = (S ** 2) / (S ** 2).sum()
    cum = np.cumsum(evr)
    k = int(np.searchsorted(cum, var_threshold) + 1)
    k = min(max(k, 1), max_components, Vt.shape[0])
    return {
        "mean": mean.astype("float32"),
        "std": std.astype("float32"),
        "components": Vt[:k].astype("float32"),  # (k, b)
        "evr": evr[:k].astype("float64"),
        "cum": cum[:k].astype("float64"),
        "k": k,
    }


def transform_city(in_path: str, out_path: str, model: dict, block_rows: int = 128, nodata: float = NODATA) -> None:
    mean, std, comp, k = model["mean"], model["std"], model["components"], model["k"]
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with rasterio.open(in_path) as src:
        prof = src.profile.copy()
        H, W = src.height, src.width
        prof.update(count=k, dtype="float32", nodata=nodata,
                    compress="deflate", predictor=2, tiled=True,
                    blockxsize=256, blockysize=256)
        with rasterio.open(out_path, "w", **prof) as dst:
            for r0 in range(0, H, block_rows):
                rows = min(block_rows, H - r0)
                win = Window(0, r0, W, rows)
                arr = src.read(window=win).astype("float32")     # (b, rows, W)
                b = arr.shape[0]
                flat = arr.reshape(b, -1).T                       # (rows*W, b)
                valid = np.all(flat != nodata, axis=1) & np.all(np.isfinite(flat), axis=1)
                scores = np.full((flat.shape[0], k), nodata, dtype="float32")
                if valid.any():
                    Xc = (flat[valid] - mean) / std
                    scores[valid] = (Xc @ comp.T).astype("float32")
                out = scores.T.reshape(k, rows, W)
                dst.write(out, window=win)
            for i in range(k):
                dst.set_band_description(i + 1, f"PC{i + 1}")
    print(f"[pca] Saved {out_path} ({k} components)")


def save_pca_model(model: dict, output_prefix: str, standardize: bool, var_threshold: float,
                    input_files: list[str]) -> None:
    os.makedirs(os.path.dirname(output_prefix) or ".", exist_ok=True)
    np.savez(output_prefix + ".npz",
              mean=model["mean"], std=model["std"], components=model["components"],
              explained_variance_ratio=model["evr"], standardize=standardize)
    with open(output_prefix + ".json", "w", encoding="utf-8") as fp:
        json.dump({
            "standardize": standardize,
            "n_components": model["k"],
            "var_threshold": var_threshold,
            "explained_variance_ratio": [round(float(x), 6) for x in model["evr"]],
            "cumulative": [round(float(x), 6) for x in model["cum"]],
            "input_files": input_files,
        }, fp, indent=2, ensure_ascii=False)
    print(f"[pca] Saved model: {output_prefix}.npz / .json")


def load_pca_model(model_npz_path: str) -> dict:
    data = np.load(model_npz_path)
    return {
        "mean": data["mean"], "std": data["std"], "components": data["components"],
        "k": data["components"].shape[0],
    }


def run_pca_fit(
    city_to_path: dict,
    output_prefix: str,
    standardize: bool = False,
    var_threshold: float = 0.999,
    max_components: int = 30,
    sample_per_city: int = 80000,
    sample_max_dim: int = 700,
    random_seed: int = 0,
) -> dict:
    rng = np.random.default_rng(random_seed)
    pools = []
    input_files = []
    for city, path in city_to_path.items():
        if not os.path.exists(path):
            print(f"[pca] [skip] Missing: {path}")
            continue
        s = sample_city(path, sample_max_dim, sample_per_city, rng)
        print(f"[pca] Sampled {city}: {s.shape[0]} pixels")
        pools.append(s)
        input_files.append(str(path))
    if not pools:
        raise SystemExit("No usable images for PCA fitting.")
    samples = np.concatenate(pools, axis=0)
    print(f"[pca] Pooled samples: {samples.shape[0]} pixels x {samples.shape[1]} bands, fitting PCA ...")

    model = fit_pca(samples, standardize, var_threshold, max_components)
    print(f"[pca] Components kept k = {model['k']} (cumulative explained variance {model['cum'][-1]*100:.2f}%)")

    save_pca_model(model, output_prefix, standardize, var_threshold, input_files)
    return model


def run_pca_apply(input_path: str, model_npz_path: str, output_path: str, block_rows: int = 128) -> str:
    model = load_pca_model(model_npz_path)
    transform_city(input_path, output_path, model, block_rows=block_rows)
    return output_path
