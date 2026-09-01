#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
HEATWISE HSI/LST preprocessing CLI.

Subcommands:
  trim-bands      Remove/keep a fixed set of bands from a raw HSI GeoTIFF.
  sharpen         Spatially sharpen HSI (30m) using Sentinel-2 (10m) -> 10m.
  select-bands    Cross-city band-selection voting -> band_selection.json.
  apply-bands     Apply a band_selection.json to one (sharpened) image.
  pca-fit         Cross-city PCA fit -> pca_model.npz/.json.
  pca-apply       Apply a pca_model.npz to one (sharpened) image.
  lst             Resample + normalize an LST band onto a reference grid.
  run-all         Run the full per-city pipeline driven by a YAML config.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from src.heatwise_hsi_lst_prep.band_trim import run_band_trim
from src.heatwise_hsi_lst_prep.sharpen import run_sharpen
from src.heatwise_hsi_lst_prep.band_selection import run_band_selection
from src.heatwise_hsi_lst_prep.apply_band_selection import run_apply_band_selection
from src.heatwise_hsi_lst_prep.pca import run_pca_fit, run_pca_apply
from src.heatwise_hsi_lst_prep.lst_processing import run_lst_processing
from src.heatwise_hsi_lst_prep.stac_io import (
    cities_from_stac,
    write_output_catalog,
)


def cmd_trim_bands(args):
    run_band_trim(
        args.input,
        args.output,
        keep_ranges=args.keep_ranges,
        remove_ranges=args.remove_ranges,
    )


def cmd_sharpen(args):
    run_sharpen(
        [(args.hsi, args.s2)],
        args.output_dir,
        method=args.method,
    )


def cmd_select_bands(args):
    city_to_path = dict(
        item.split("=", 1)
        for item in args.inputs
    )

    keep_ranges = (
        _parse_ranges(args.keep_ranges)
        if args.keep_ranges
        else ((6, 71), (188, 245))
    )

    run_band_selection(
        city_to_path=city_to_path,
        wavelength_file=args.wavelength_file,
        keep_ranges_1based=keep_ranges,
        output_json=args.output_json,
        target_total_bands=args.target_bands,
        target_clusters=args.target_clusters,
        vote_threshold=args.vote_threshold,
        smoothness_cut_pct=args.smoothness_cut_pct,
        n_sample=args.n_sample,
    )


def cmd_apply_bands(args):
    run_apply_band_selection(
        args.input,
        args.selection_json,
        args.output,
    )


def cmd_pca_fit(args):
    city_to_path = dict(
        item.split("=", 1)
        for item in args.inputs
    )

    run_pca_fit(
        city_to_path=city_to_path,
        output_prefix=args.output_prefix,
        standardize=args.standardize,
        var_threshold=args.var_threshold,
        max_components=args.max_components,
        sample_per_city=args.sample_per_city,
        sample_max_dim=args.sample_max_dim,
    )


def cmd_pca_apply(args):
    run_pca_apply(
        args.input,
        args.model,
        args.output,
        block_rows=args.block_rows,
    )


def cmd_lst(args):
    run_lst_processing(
        input_path=args.input,
        band=args.band,
        ref_grid_path=args.ref_grid,
        output_path=args.output,
        normalization=args.normalization,
        kelvin_offset=args.kelvin_offset,
        fixed_scale=args.fixed_scale,
    )


def cmd_run_all(args):
    import yaml

    with open(args.config, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    input_catalog = args.input_catalog or cfg.get("input_catalog")

    if input_catalog:
        bt_cfg = cfg.get("band_trim_default", {})
        lst_cfg = cfg.get("lst_default", {})

        cfg["cities"] = cities_from_stac(
            input_catalog,
            band_trim_mode=bt_cfg.get("mode", "keep"),
            band_trim_ranges=bt_cfg.get(
                "ranges",
                "6-71,188-245",
            ),
            lst_band=lst_cfg.get("band", 7),
            lst_normalization=lst_cfg.get(
                "normalization",
                "zscore",
            ),
        )

        print(
            f"[run-all] Loaded {len(cfg['cities'])} city/cities "
            f"from STAC catalog: {input_catalog}"
        )

    if args.output_dir:
        cfg["output_dir"] = args.output_dir

    run_pipeline(cfg)


def _parse_ranges(
    value: str,
) -> tuple[tuple[int, int], ...]:
    from src.heatwise_hsi_lst_prep.band_trim import (
        parse_band_ranges,
    )

    return parse_band_ranges(value)


def run_pipeline(cfg: dict) -> None:
    output_dir = Path(cfg["output_dir"])
    process_lst = bool(cfg.get("process_lst", False))
    process_pca = bool(cfg.get("process_pca", False))
    sharpen_method = cfg.get("sharpen_method", "linear")
    cities = cfg["cities"]

    trimmed_dir = output_dir / "01_trimmed"
    sharpened_dir = output_dir / "02_sharpened_10m"
    final_hsi_dir = output_dir / "03_hsi_final"
    hsi_bs_dir = final_hsi_dir / "hsi_bs"
    hsi_pca_dir = final_hsi_dir / "hsi_pca"
    lst_dir = output_dir / "04_lst_final"

    selection_json = output_dir / "band_selection.json"
    pca_model_prefix = output_dir / "pca_model"

    # Step 1: trim bands per city (30m, common band set)
    trimmed_paths = {}

    for city in cities:
        name = city["name"]
        bt = city["band_trim"]

        out_path = trimmed_dir / f"{name}_trimmed.tif"

        run_band_trim(
            city["hsi"],
            out_path,
            keep_ranges=(
                bt.get("ranges")
                if bt.get("mode", "keep") == "keep"
                else None
            ),
            remove_ranges=(
                bt.get("ranges")
                if bt.get("mode") == "remove"
                else None
            ),
        )

        trimmed_paths[name] = out_path

    # Step 2: sharpen 30m -> 10m per city
    sharpened_paths = {}

    for city in cities:
        name = city["name"]

        outs = run_sharpen(
            [(trimmed_paths[name], city["s2"])],
            sharpened_dir,
            method=sharpen_method,
        )

        if outs:
            sharpened_paths[name] = outs[0]

    # Step 3: cross-city band selection, computed on
    # the 30m trimmed images
    band_cfg = cfg.get("band_selection", {})

    keep_ranges = tuple(
        tuple(r)
        for r in band_cfg.get(
            "keep_ranges",
            [[6, 71], [188, 245]],
        )
    )

    run_band_selection(
        city_to_path={
            name: str(path)
            for name, path in trimmed_paths.items()
        },
        wavelength_file=cfg["wavelength_file"],
        keep_ranges_1based=keep_ranges,
        output_json=str(selection_json),
        target_total_bands=band_cfg.get(
            "target_bands",
            30,
        ),
        target_clusters=band_cfg.get(
            "target_clusters",
            55,
        ),
        vote_threshold=band_cfg.get(
            "vote_threshold",
            0.5,
        ),
        smoothness_cut_pct=band_cfg.get(
            "smoothness_cut_pct",
            95,
        ),
        n_sample=band_cfg.get(
            "n_sample",
            20000,
        ),
    )

    # Step 4: apply selected bands to each city's
    # sharpened 10m image -> hsi_bs
    final_hsi_bs_paths = {}

    for name, sharp_path in sharpened_paths.items():
        out_path = hsi_bs_dir / f"{name}_hsi_bs.tif"

        run_apply_band_selection(
            str(sharp_path),
            str(selection_json),
            str(out_path),
        )

        final_hsi_bs_paths[name] = out_path

    # Step 4b (optional): cross-city PCA, fit on all
    # cities' sharpened 10m images, then transform each
    # city -> hsi_pca.
    final_hsi_pca_paths = {}

    if process_pca:
        pca_cfg = cfg.get("pca", {})

        run_pca_fit(
            city_to_path={
                name: str(path)
                for name, path in sharpened_paths.items()
            },
            output_prefix=str(pca_model_prefix),
            standardize=pca_cfg.get(
                "standardize",
                False,
            ),
            var_threshold=pca_cfg.get(
                "var_threshold",
                0.999,
            ),
            max_components=pca_cfg.get(
                "max_components",
                30,
            ),
            sample_per_city=pca_cfg.get(
                "sample_per_city",
                80000,
            ),
            sample_max_dim=pca_cfg.get(
                "sample_max_dim",
                700,
            ),
        )

        for name, sharp_path in sharpened_paths.items():
            out_path = (
                hsi_pca_dir
                / f"{name}_hsi_pca.tif"
            )

            run_pca_apply(
                str(sharp_path),
                str(pca_model_prefix) + ".npz",
                str(out_path),
                block_rows=pca_cfg.get(
                    "block_rows",
                    128,
                ),
            )

            final_hsi_pca_paths[name] = out_path

    # Step 5 (optional): LST -> resample onto the
    # hsi_bs grid + normalize
    final_lst_paths = {}

    if process_lst:
        for city in cities:
            name = city["name"]
            lst_cfg = city.get("lst")

            if (
                not lst_cfg
                or name not in final_hsi_bs_paths
            ):
                print(
                    f"[run-all] [skip LST] {name}: "
                    "no lst configured or missing final HSI"
                )
                continue

            out_path = (
                lst_dir
                / f"{name}_lst_final.tif"
            )

            run_lst_processing(
                input_path=lst_cfg["input"],
                band=lst_cfg.get("band", 7),
                ref_grid_path=str(
                    final_hsi_bs_paths[name]
                ),
                output_path=str(out_path),
                normalization=lst_cfg.get(
                    "normalization",
                    "zscore",
                ),
                kelvin_offset=lst_cfg.get(
                    "kelvin_offset",
                    273.15,
                ),
                fixed_scale=lst_cfg.get(
                    "fixed_scale",
                    50.0,
                ),
            )

            final_lst_paths[name] = out_path

    # Step 6: write STAC output catalogue.
    #
    # Each city Item is self-contained. Shared processing
    # metadata such as band_selection.json is attached to
    # each relevant city Item rather than represented by a
    # separate non-spatial "shared" Item.

    pca_npz = Path(
        str(pca_model_prefix) + ".npz"
    )
    pca_json = Path(
        str(pca_model_prefix) + ".json"
    )

    items = {}

    for name in final_hsi_bs_paths:
        assets = {
            "hsi_bs": final_hsi_bs_paths[name],
        }

        # band_selection.json is required metadata for
        # interpreting hsi_bs. Each city Item points to the
        # same file.
        if selection_json.exists():
            assets["band_selection"] = selection_json

        if name in final_hsi_pca_paths:
            assets["hsi_pca"] = (
                final_hsi_pca_paths[name]
            )

            # PCA products depend on the fitted model.
            # Attach the same model artifacts to each city
            # Item containing an hsi_pca product.
            if pca_npz.exists():
                assets["pca_model"] = pca_npz

            if pca_json.exists():
                assets["pca_model_metadata"] = (
                    pca_json
                )

        if name in final_lst_paths:
            assets["lst"] = final_lst_paths[name]

            lst_json = (
                final_lst_paths[name]
                .with_suffix(".json")
            )

            if lst_json.exists():
                assets["lst_metadata"] = lst_json

        items[name] = assets

    item_metadata = {
        city["name"]: city.get("stac_metadata", {})
        for city in cities
    }

    write_output_catalog(
        output_dir,
        items,
        item_metadata=item_metadata,
    )

    print(
        "\n[run-all] All done. Output directory:",
        output_dir,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="HEATWISE HSI/LST preprocessing"
    )

    sub = parser.add_subparsers(
        dest="command",
        required=True,
    )

    p = sub.add_parser(
        "trim-bands",
        help="Remove/keep a fixed band range from a GeoTIFF.",
    )
    p.add_argument("--input", required=True)
    p.add_argument("--output", required=True)
    p.add_argument(
        "--keep-ranges",
        help='e.g. "6-71,188-245"',
    )
    p.add_argument(
        "--remove-ranges",
        help='e.g. "110-127,163-184"',
    )
    p.set_defaults(func=cmd_trim_bands)

    p = sub.add_parser(
        "sharpen",
        help="Sharpen a 30m HSI to 10m using Sentinel-2.",
    )
    p.add_argument("--hsi", required=True)
    p.add_argument("--s2", required=True)
    p.add_argument(
        "--output-dir",
        required=True,
    )
    p.add_argument(
        "--method",
        choices=["linear", "rf"],
        default="linear",
    )
    p.set_defaults(func=cmd_sharpen)

    p = sub.add_parser(
        "select-bands",
        help="Cross-city band-selection voting.",
    )
    p.add_argument(
        "--inputs",
        nargs="+",
        required=True,
        help='"city=path" pairs, e.g. Athens=path/to.tif',
    )
    p.add_argument(
        "--wavelength-file",
        required=True,
    )
    p.add_argument(
        "--output-json",
        required=True,
    )
    p.add_argument(
        "--keep-ranges",
        help=(
            "original band keep-ranges used for "
            'wavelength lookup, e.g. "6-71,188-245"'
        ),
    )
    p.add_argument(
        "--target-bands",
        type=int,
        default=30,
    )
    p.add_argument(
        "--target-clusters",
        type=int,
        default=55,
    )
    p.add_argument(
        "--vote-threshold",
        type=float,
        default=0.5,
    )
    p.add_argument(
        "--smoothness-cut-pct",
        type=float,
        default=95,
    )
    p.add_argument(
        "--n-sample",
        type=int,
        default=20000,
    )
    p.set_defaults(func=cmd_select_bands)

    p = sub.add_parser(
        "apply-bands",
        help="Apply a band_selection.json to one image.",
    )
    p.add_argument("--input", required=True)
    p.add_argument(
        "--selection-json",
        required=True,
    )
    p.add_argument("--output", required=True)
    p.set_defaults(func=cmd_apply_bands)

    p = sub.add_parser(
        "pca-fit",
        help="Cross-city PCA fit -> pca_model.npz/.json.",
    )
    p.add_argument(
        "--inputs",
        nargs="+",
        required=True,
        help='"city=path" pairs, e.g. Athens=path/to.tif',
    )
    p.add_argument(
        "--output-prefix",
        required=True,
        help="Writes <prefix>.npz and <prefix>.json",
    )
    p.add_argument(
        "--standardize",
        action="store_true",
        help=(
            "Correlation PCA (per-band standardize) "
            "instead of covariance PCA"
        ),
    )
    p.add_argument(
        "--var-threshold",
        type=float,
        default=0.999,
    )
    p.add_argument(
        "--max-components",
        type=int,
        default=30,
    )
    p.add_argument(
        "--sample-per-city",
        type=int,
        default=80000,
    )
    p.add_argument(
        "--sample-max-dim",
        type=int,
        default=700,
    )
    p.set_defaults(func=cmd_pca_fit)

    p = sub.add_parser(
        "pca-apply",
        help="Apply a pca_model.npz to one image.",
    )
    p.add_argument("--input", required=True)
    p.add_argument(
        "--model",
        required=True,
        help="Path to pca_model.npz",
    )
    p.add_argument("--output", required=True)
    p.add_argument(
        "--block-rows",
        type=int,
        default=128,
    )
    p.set_defaults(func=cmd_pca_apply)

    p = sub.add_parser(
        "lst",
        help=(
            "Resample + normalize an LST band "
            "onto a reference grid."
        ),
    )
    p.add_argument("--input", required=True)
    p.add_argument(
        "--band",
        type=int,
        default=7,
    )
    p.add_argument(
        "--ref-grid",
        required=True,
    )
    p.add_argument(
        "--output",
        required=True,
    )
    p.add_argument(
        "--normalization",
        choices=["zscore", "fixed_scale"],
        default="zscore",
    )
    p.add_argument(
        "--kelvin-offset",
        type=float,
        default=273.15,
    )
    p.add_argument(
        "--fixed-scale",
        type=float,
        default=50.0,
    )
    p.set_defaults(func=cmd_lst)

    p = sub.add_parser(
        "run-all",
        help="Run the full pipeline from a YAML config.",
    )
    p.add_argument(
        "--config",
        required=True,
    )
    p.add_argument(
        "--input-catalog",
        help=(
            "STAC catalog.json listing per-city input assets "
            "(hyperspectral_image/sentinel2/lst_source); "
            "overrides the config's `cities:` list if given"
        ),
    )
    p.add_argument(
        "--output-dir",
        help="Overrides the config's `output_dir` if given",
    )
    p.set_defaults(func=cmd_run_all)

    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
