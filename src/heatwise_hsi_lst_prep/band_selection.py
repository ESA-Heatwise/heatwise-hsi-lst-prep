# -*- coding: utf-8 -*-
"""
Cross-city band selection (voting method).

For each of several cities' (trimmed, 30m, common-band) images, independently:
  Step1 spectral smoothness filter -> Step2/3 correlation clustering + medoid
  selection -> Step4 cross-city voting -> Step5 spectral spacing refinement,
  producing a final fixed-size set of band indices.

Input images must already share the same band set (i.e. the same band_trim
output), and must carry a nodata mask (default -9999).
"""
from __future__ import annotations

import json
import os
from collections import Counter

import numpy as np
from scipy.cluster.hierarchy import linkage, fcluster
from scipy.spatial.distance import squareform


def read_valid_pixels(path, nodata_default=-9999.0):
    import rasterio
    with rasterio.open(path) as src:
        img = src.read().astype("float32")
        nodata = src.nodata if src.nodata is not None else nodata_default
    b = img.shape[0]
    flat = img.reshape(b, -1)
    valid = np.all(flat != nodata, axis=0) & np.all(np.isfinite(flat), axis=0)
    pix = flat[:, valid]
    return pix, b


def sample_cols(pix, n, rng):
    N = pix.shape[1]
    idx = rng.choice(N, min(n, N), replace=False)
    return pix[:, idx]


def spectral_smoothness_filter(pix, smoothness_cut_pct):
    b = pix.shape[0]
    mean_spectrum = pix.mean(axis=1)
    curvature = [0.0]
    for i in range(1, b - 1):
        c = abs(mean_spectrum[i - 1] - 2 * mean_spectrum[i] + mean_spectrum[i + 1])
        curvature.append(c)
    curvature.append(0.0)
    curvature = np.array(curvature)
    threshold = np.percentile(curvature, smoothness_cut_pct)
    keep = np.where(curvature < threshold)[0]
    return keep.tolist(), curvature.tolist()


def correlation_clustering(pix, target_clusters, n_sample, rng):
    samp = sample_cols(pix, n_sample, rng)
    corr = np.corrcoef(samp)
    corr = np.nan_to_num(corr, nan=0.0)
    dist = 1 - corr
    np.fill_diagonal(dist, 0.0)
    dist_condensed = squareform(dist, checks=False)
    Z = linkage(dist_condensed, method="average")
    k = min(target_clusters, pix.shape[0])
    labels = fcluster(Z, k, criterion="maxclust")
    return labels, corr


def medoid_selection(labels, corr):
    selected = []
    for c in np.unique(labels):
        idx = np.where(labels == c)[0]
        subcorr = corr[np.ix_(idx, idx)]
        score = subcorr.sum(axis=1)
        medoid = idx[np.argmax(score)]
        selected.append(int(medoid))
    return sorted(selected)


def voting(city_band_lists, target_n, threshold):
    counter = Counter()
    for bands in city_band_lists:
        counter.update(bands)
    city_n = len(city_band_lists)

    selected = [b for b, c in counter.items() if c >= city_n * threshold]
    if len(selected) < target_n:
        ranked = sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))
        for b, _ in ranked:
            if b not in selected:
                selected.append(b)
            if len(selected) >= target_n:
                break
    return sorted(selected), counter


def spacing_refinement(bands, target_n):
    bands = sorted(bands)
    while len(bands) > target_n:
        gaps = np.diff(bands)
        remove_idx = int(np.argmin(gaps)) + 1
        bands.pop(remove_idx)
    return bands


def run_band_selection(
    city_to_path: dict,
    wavelength_file: str,
    keep_ranges_1based: tuple[tuple[int, int], ...],
    output_json: str,
    target_total_bands: int = 30,
    target_clusters: int = 55,
    vote_threshold: float = 0.5,
    smoothness_cut_pct: float = 95,
    n_sample: int = 20000,
    random_seed: int = 0,
) -> dict:
    rng = np.random.default_rng(random_seed)
    city_results = {}
    for city, path in city_to_path.items():
        if not os.path.exists(path):
            print(f"[skip] Missing file: {path}")
            continue
        print(f"\nProcessing {city}")
        pix, nb = read_valid_pixels(path)
        print(f"  Bands: {nb}, valid pixels: {pix.shape[1]}")

        keep, _ = spectral_smoothness_filter(pix, smoothness_cut_pct)
        pix_s = pix[keep]

        labels, corr = correlation_clustering(pix_s, target_clusters, n_sample, rng)
        medoids_local = medoid_selection(labels, corr)
        medoids = sorted(keep[i] for i in medoids_local)

        city_results[city] = medoids
        print(f"  Selected bands (0-based, {len(medoids)}): {medoids}")

    if len(city_results) == 0:
        raise SystemExit("No usable scenes; please check the paths.")

    voted, counter = voting(list(city_results.values()), target_total_bands, vote_threshold)
    print("\nVoting result (0-based):", voted)

    final_bands_0based = spacing_refinement(voted, target_total_bands)
    print(f"\nFinal {len(final_bands_0based)} bands (0-based):", final_bands_0based)

    wl_all = np.loadtxt(wavelength_file)
    keep_1based = []
    for a, b in keep_ranges_1based:
        keep_1based.extend(range(a, b + 1))
    wl = np.array([wl_all[i - 1] for i in keep_1based], dtype="float64")

    final_1based = [b + 1 for b in final_bands_0based]
    final_wl_um = [round(float(wl[b]), 4) for b in final_bands_0based]

    result = {
        "target_total_bands": target_total_bands,
        "final_bands_0based": final_bands_0based,
        "final_bands_1based": final_1based,
        "final_wavelengths_um": final_wl_um,
        "city_selected_0based": city_results,
        "vote_count": {str(k): v for k, v in sorted(counter.items())},
    }
    os.makedirs(os.path.dirname(output_json) or ".", exist_ok=True)
    with open(output_json, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)
    print("\nSaved selection to", output_json)
    return result
