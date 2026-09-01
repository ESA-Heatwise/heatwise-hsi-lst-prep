# -*- coding: utf-8 -*-
"""
Minimal STAC (SpatioTemporal Asset Catalog) input/output handling.

Input:  a catalog.json linking to one STAC Item per city. Each item's `assets`
        dict is expected to contain (asset name -> file):
            hyperspectral_image   (required)
            sentinel2             (required)
            lst_source            (optional; only needed if process_lst)
        The item id becomes the city name in the `cities:` list `run_pipeline`
        already understands, so STAC input is just another way to populate
        that list (in addition to writing it out by hand in the YAML config).

Output: after run-all finishes, a catalog.json + one <item_id>_item.json per
        city (assets: hsi_bs, hsi_pca, lst -- whichever were produced) plus a
        "shared" item for cross-city artifacts (band_selection.json,
        pca_model.npz/.json), so downstream tools/platforms can discover the
        products without knowing this repo's internal folder layout.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

STAC_VERSION = "1.1.0"

_MEDIA_TYPES = {
    ".tif": "image/tiff; application=geotiff",
    ".tiff": "image/tiff; application=geotiff",
    ".json": "application/json",
    ".npz": "application/octet-stream",
    ".yaml": "application/yaml",
    ".yml": "application/yaml",
}


def _media_type(path: Path) -> str:
    return _MEDIA_TYPES.get(path.suffix.lower(), "application/octet-stream")


def read_input_catalog(catalog_path: str | Path) -> dict[str, dict[str, Path]]:
    """Parse a STAC catalog.json and return {item_id: {asset_name: resolved_path}}."""
    catalog_path = Path(catalog_path)
    with open(catalog_path, "r", encoding="utf-8") as f:
        catalog = json.load(f)

    assets_by_item: dict[str, dict[str, Path]] = {}
    for link in catalog.get("links", []):
        if link.get("rel") != "item":
            continue
        item_path = (catalog_path.parent / link["href"]).resolve()
        with open(item_path, "r", encoding="utf-8") as f:
            item = json.load(f)
        item_id = item.get("id", item_path.stem)
        assets = {
            name: (item_path.parent / info["href"]).resolve()
            for name, info in item.get("assets", {}).items()
        }
        assets_by_item[item_id] = assets

    if not assets_by_item:
        raise ValueError(f"No STAC items found via {catalog_path} (no links with rel='item').")
    return assets_by_item


def cities_from_stac(
    catalog_path: str | Path,
    band_trim_mode: str = "keep",
    band_trim_ranges: str = "6-71,188-245",
    lst_band: int = 7,
    lst_normalization: str = "zscore",
) -> list[dict]:
    """Build the `cities:` list `run_pipeline` expects from an input STAC catalog.

    Every city gets the same band_trim/LST defaults (all HEATWISE cities processed
    so far share the same raw band count, see README); there is no per-item
    override yet since it hasn't been needed in practice.
    """
    assets_by_item = read_input_catalog(catalog_path)
    cities = []
    for city, assets in assets_by_item.items():
        missing = [a for a in ("hyperspectral_image", "sentinel2") if a not in assets]
        if missing:
            raise ValueError(f"STAC item {city!r} is missing required asset(s): {missing}")
        city_cfg = {
            "name": city,
            "hsi": str(assets["hyperspectral_image"]),
            "band_trim": {"mode": band_trim_mode, "ranges": band_trim_ranges},
            "s2": str(assets["sentinel2"]),
        }
        if "lst_source" in assets:
            city_cfg["lst"] = {
                "input": str(assets["lst_source"]),
                "band": lst_band,
                "normalization": lst_normalization,
            }
        cities.append(city_cfg)
    return cities


def write_output_catalog(
    output_dir: str | Path,
    items: dict[str, dict[str, str | Path]],
    processor_name: str = "heatwise-hsi-lst-prep",
    processor_version: str = "0.1.1",
) -> Path:
    """Write one <item_id>_item.json per entry of `items`, plus a root catalog.json
    linking to all of them. `items` = {item_id: {asset_name: file_path}}.
    Returns the path to catalog.json."""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    link_entries = []
    for item_id, assets in items.items():
        item = {
            "type": "Feature",
            "stac_version": STAC_VERSION,
            "stac_extensions": [
                "https://stac-extensions.github.io/processing/v1.2.0/schema.json"
            ],
            "id": item_id,
            "properties": {
                "datetime": datetime.now(timezone.utc).isoformat(),
                "processing:software": {
                    processor_name: processor_version
                },
            },
            "geometry": None,
            "links": [],
            "assets": {},
        }
        for asset_name, path in assets.items():
            path = Path(path)
            href = os.path.relpath(path, output_dir).replace(os.sep, "/")  # POSIX-style, even on Windows
            item["assets"][asset_name] = {
                "href": href,
                "type": _media_type(path),
                "roles": ["data"],
            }
        item_filename = f"{item_id}_item.json"
        with open(output_dir / item_filename, "w", encoding="utf-8") as f:
            json.dump(item, f, indent=2, ensure_ascii=False)
        link_entries.append({"rel": "item", "href": item_filename, "type": "application/json"})

    catalog = {
        "type": "Catalog",
        "stac_version": STAC_VERSION,
        "id": f"{processor_name}-output",
        "description": f"{processor_name} output catalogue",
        "links": link_entries,
    }
    catalog_path = output_dir / "catalog.json"
    with open(catalog_path, "w", encoding="utf-8") as f:
        json.dump(catalog, f, indent=2, ensure_ascii=False)
    print(f"[stac_io] Wrote output catalogue: {catalog_path} ({len(items)} item(s))")
    return catalog_path
