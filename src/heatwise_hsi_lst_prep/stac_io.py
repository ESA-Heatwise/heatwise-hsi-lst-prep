# -*- coding: utf-8 -*-
"""
Minimal STAC (SpatioTemporal Asset Catalog) input/output handling.

Input:
    A catalog.json linking to one STAC Item per city. Each Item's `assets`
    dictionary is expected to contain:

        hyperspectral_image   required
        sentinel2             required
        lst_source            optional

    The Item id becomes the city name used by the processing pipeline.

    Spatial and temporal metadata from the input Item are preserved so they
    can be propagated to the corresponding output Item.

Output:
    After run-all finishes, a catalog.json and one <item_id>_item.json per
    city are written.

    Each city Item contains the generated EO products (hsi_bs, optional
    hsi_pca, optional lst) together with the processing metadata needed to
    interpret them, such as band_selection.json and, when applicable, the
    PCA model artifacts.

    No separate non-spatial "shared" Item is created.

    Output geometry and bbox are derived from the actual generated raster
    footprint in EPSG:4326. Temporal metadata are propagated from the
    corresponding input STAC Item whenever available.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import rasterio
from rasterio.warp import transform_bounds


STAC_VERSION = "1.1.0"


_MEDIA_TYPES = {
    ".tif": "image/tiff; application=geotiff",
    ".tiff": "image/tiff; application=geotiff",
    ".json": "application/json",
    ".npz": "application/octet-stream",
    ".yaml": "application/yaml",
    ".yml": "application/yaml",
}


_METADATA_ASSETS = {
    "band_selection",
    "pca_model",
    "pca_model_metadata",
    "lst_metadata",
}


def _media_type(path: Path) -> str:
    return _MEDIA_TYPES.get(
        path.suffix.lower(),
        "application/octet-stream",
    )


def _read_input_items(
    catalog_path: str | Path,
) -> dict[str, dict]:
    """
    Read all STAC Items referenced by an input catalog.

    Returns a dictionary keyed by Item id. For each Item, resolved asset
    paths and the original spatial/temporal metadata are preserved.
    """
    catalog_path = Path(catalog_path).resolve()

    with catalog_path.open(
        "r",
        encoding="utf-8",
    ) as f:
        catalog = json.load(f)

    items: dict[str, dict] = {}

    for link in catalog.get("links", []):
        if link.get("rel") != "item":
            continue

        href = link.get("href")
        if not href:
            continue

        item_path = Path(href)

        if not item_path.is_absolute():
            item_path = (
                catalog_path.parent / item_path
            ).resolve()

        with item_path.open(
            "r",
            encoding="utf-8",
        ) as f:
            item = json.load(f)

        item_id = item.get(
            "id",
            item_path.stem,
        )

        assets = {
            name: (
                item_path.parent / info["href"]
            ).resolve()
            for name, info
            in item.get("assets", {}).items()
        }

        properties = item.get(
            "properties",
            {},
        )

        temporal_properties = {}

        if "datetime" in properties:
            temporal_properties["datetime"] = (
                properties.get("datetime")
            )

        if "start_datetime" in properties:
            temporal_properties["start_datetime"] = (
                properties.get("start_datetime")
            )

        if "end_datetime" in properties:
            temporal_properties["end_datetime"] = (
                properties.get("end_datetime")
            )

        items[item_id] = {
            "assets": assets,
            "geometry": item.get("geometry"),
            "bbox": item.get("bbox"),
            "properties": temporal_properties,
        }

    if not items:
        raise ValueError(
            f"No STAC items found via {catalog_path} "
            "(no links with rel='item')."
        )

    return items


def read_input_catalog(
    catalog_path: str | Path,
) -> dict[str, dict[str, Path]]:
    """
    Parse a STAC catalog and return resolved asset paths.

    This keeps the original public helper behaviour:

        {item_id: {asset_name: resolved_path}}

    Full Item metadata are handled internally by `_read_input_items`.
    """
    input_items = _read_input_items(
        catalog_path
    )

    return {
        item_id: item_data["assets"]
        for item_id, item_data
        in input_items.items()
    }


def cities_from_stac(
    catalog_path: str | Path,
    band_trim_mode: str = "keep",
    band_trim_ranges: str = "6-71,188-245",
    lst_band: int = 7,
    lst_normalization: str = "zscore",
) -> list[dict]:
    """
    Build the `cities:` configuration used by run_pipeline.

    Raster assets are resolved to local staged paths. Spatial and temporal
    STAC metadata are retained in `stac_metadata` so the generated output
    Item can inherit the observation time information from its input Item.
    """
    input_items = _read_input_items(
        catalog_path
    )

    cities = []

    for city, item_data in input_items.items():
        assets = item_data["assets"]

        missing = [
            asset_name
            for asset_name in (
                "hyperspectral_image",
                "sentinel2",
            )
            if asset_name not in assets
        ]

        if missing:
            raise ValueError(
                f"STAC item {city!r} is missing "
                f"required asset(s): {missing}"
            )

        city_cfg = {
            "name": city,
            "hsi": str(
                assets["hyperspectral_image"]
            ),
            "band_trim": {
                "mode": band_trim_mode,
                "ranges": band_trim_ranges,
            },
            "s2": str(
                assets["sentinel2"]
            ),
            "stac_metadata": {
                "geometry": item_data.get(
                    "geometry"
                ),
                "bbox": item_data.get(
                    "bbox"
                ),
                "properties": item_data.get(
                    "properties",
                    {},
                ),
            },
        }

        if "lst_source" in assets:
            city_cfg["lst"] = {
                "input": str(
                    assets["lst_source"]
                ),
                "band": lst_band,
                "normalization": (
                    lst_normalization
                ),
            }

        cities.append(city_cfg)

    return cities


def _raster_spatial_metadata(
    raster_path: str | Path,
) -> tuple[dict, list[float]]:
    """
    Derive an output raster footprint and bbox in EPSG:4326.

    The returned geometry is a GeoJSON Polygon corresponding to the raster
    bounding box. The bbox follows the STAC order:

        [west, south, east, north]
    """
    raster_path = Path(
        raster_path
    ).resolve()

    with rasterio.open(
        raster_path
    ) as src:
        if src.crs is None:
            raise ValueError(
                f"Raster has no CRS: {raster_path}"
            )

        west, south, east, north = (
            transform_bounds(
                src.crs,
                "EPSG:4326",
                src.bounds.left,
                src.bounds.bottom,
                src.bounds.right,
                src.bounds.top,
                densify_pts=21,
            )
        )

    bbox = [
        float(west),
        float(south),
        float(east),
        float(north),
    ]

    geometry = {
        "type": "Polygon",
        "coordinates": [
            [
                [west, south],
                [east, south],
                [east, north],
                [west, north],
                [west, south],
            ]
        ],
    }

    return geometry, bbox


def _temporal_properties(
    metadata: dict | None,
) -> dict:
    """
    Return meaningful STAC temporal properties.

    Source observation metadata are preferred. If the input Item represents
    an interval, datetime remains null and start_datetime/end_datetime are
    preserved, as required by STAC.

    A processing-time datetime is used only as a fallback when the source
    Item contains no temporal information at all.
    """
    metadata = metadata or {}

    properties = metadata.get(
        "properties",
        {},
    )

    source_datetime = properties.get(
        "datetime"
    )

    start_datetime = properties.get(
        "start_datetime"
    )

    end_datetime = properties.get(
        "end_datetime"
    )

    if source_datetime is not None:
        return {
            "datetime": source_datetime,
        }

    if (
        start_datetime is not None
        or end_datetime is not None
    ):
        result = {
            "datetime": None,
        }

        if start_datetime is not None:
            result["start_datetime"] = (
                start_datetime
            )

        if end_datetime is not None:
            result["end_datetime"] = (
                end_datetime
            )

        return result

    return {
        "datetime": (
            datetime.now(
                timezone.utc
            ).isoformat()
        )
    }


def _spatial_reference_asset(
    assets: dict[str, str | Path],
) -> Path | None:
    """
    Select a generated raster whose footprint represents the city product.

    hsi_bs is preferred because LST is resampled onto this grid and the PCA
    output is generated from the same sharpened spatial grid.
    """
    for asset_name in (
        "hsi_bs",
        "hsi_pca",
        "lst",
    ):
        if asset_name in assets:
            return Path(
                assets[asset_name]
            )

    return None


def write_output_catalog(
    output_dir: str | Path,
    items: dict[
        str,
        dict[str, str | Path],
    ],
    item_metadata: dict[
        str,
        dict,
    ] | None = None,
    processor_name: str = (
        "heatwise-hsi-lst-prep"
    ),
    processor_version: str = "0.1.1",
) -> Path:
    """
    Write one STAC Item per city and a root catalog.json.

    `items` has the form:

        {
            item_id: {
                asset_name: file_path,
                ...
            }
        }

    `item_metadata`, when supplied, contains spatial and temporal metadata
    inherited from the corresponding input STAC Item.

    Spatial metadata are preferentially derived from the actual generated
    raster footprint. Input spatial metadata are used only as a fallback.

    Returns the path to catalog.json.
    """
    output_dir = Path(
        output_dir
    )

    output_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    item_metadata = (
        item_metadata or {}
    )

    link_entries = []

    for item_id, assets in items.items():
        metadata = item_metadata.get(
            item_id,
            {},
        )

        reference_raster = (
            _spatial_reference_asset(
                assets
            )
        )

        geometry = None
        bbox = None

        if reference_raster is not None:
            try:
                geometry, bbox = (
                    _raster_spatial_metadata(
                        reference_raster
                    )
                )
            except Exception as exc:
                print(
                    "[stac_io] Warning: could not "
                    "derive spatial metadata from "
                    f"{reference_raster}: {exc}"
                )

        if geometry is None:
            geometry = metadata.get(
                "geometry"
            )

        if bbox is None:
            bbox = metadata.get(
                "bbox"
            )

        if geometry is None or bbox is None:
            raise ValueError(
                f"Unable to determine meaningful "
                f"geometry/bbox for STAC Item "
                f"{item_id!r}."
            )

        properties = (
            _temporal_properties(
                metadata
            )
        )

        properties[
            "processing:software"
        ] = {
            processor_name: (
                processor_version
            )
        }

        item = {
            "type": "Feature",
            "stac_version": STAC_VERSION,
            "stac_extensions": [
                (
                    "https://stac-extensions.github.io/"
                    "processing/v1.2.0/schema.json"
                )
            ],
            "id": item_id,
            "geometry": geometry,
            "bbox": bbox,
            "properties": properties,
            "links": [],
            "assets": {},
        }

        for asset_name, path in assets.items():
            path = Path(
                path
            )

            href = os.path.relpath(
                path,
                output_dir,
            ).replace(
                os.sep,
                "/",
            )

            roles = (
                ["metadata"]
                if asset_name
                in _METADATA_ASSETS
                else ["data"]
            )

            item["assets"][
                asset_name
            ] = {
                "href": href,
                "type": _media_type(
                    path
                ),
                "roles": roles,
            }

        item_filename = (
            f"{item_id}_item.json"
        )

        with (
            output_dir / item_filename
        ).open(
            "w",
            encoding="utf-8",
        ) as f:
            json.dump(
                item,
                f,
                indent=2,
                ensure_ascii=False,
            )

        link_entries.append(
            {
                "rel": "item",
                "href": item_filename,
                "type": (
                    "application/geo+json"
                ),
            }
        )

    catalog = {
        "type": "Catalog",
        "stac_version": STAC_VERSION,
        "id": (
            f"{processor_name}-output"
        ),
        "description": (
            f"{processor_name} "
            "output catalogue"
        ),
        "links": link_entries,
    }

    catalog_path = (
        output_dir / "catalog.json"
    )

    with catalog_path.open(
        "w",
        encoding="utf-8",
    ) as f:
        json.dump(
            catalog,
            f,
            indent=2,
            ensure_ascii=False,
        )

    print(
        "[stac_io] Wrote output catalogue: "
        f"{catalog_path} "
        f"({len(items)} item(s))"
    )

    return catalog_path
