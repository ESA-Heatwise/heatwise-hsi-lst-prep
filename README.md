# heatwise-hsi-lst-prep

Stage 1 of the HEATWISE pipeline: turns raw per-city HSI (CHIME-like) + Sentinel-2
(+ optional LST) inputs into processed, analysis-ready rasters:

- **HSI**: drop non-common/atmospheric-window bands -> sharpen 30m -> 10m using
  Sentinel-2 -> two parallel dimensionality-reduction routes, both written
  under `03_hsi_final/`:
  - `hsi_bs/`: cross-city band-selection voting -> final band-selected 10m HSI.
  - `hsi_pca/` (optional, toggled as a whole): cross-city PCA (one unified
    model fit on all cities, so every city's scores share the same feature
    space) -> final PCA-reduced 10m HSI.
- **LST** (optional, toggled on/off as a whole): resample the LST band onto the
  `hsi_bs` grid and normalize it (z-score or fixed physical scale).

Works on one or many cities in a single run; no path is hardcoded in the source —
everything is passed via CLI flags or a YAML config.

## Install

```bash
pip install -r requirements.txt
```

## Run the full pipeline for several cities

```bash
python processor.py run-all --config config/example_config.yaml
```

Copy `config/example_config.yaml`, point it at your own data, and set
`process_lst: true/false` / `process_pca: true/false` to switch the LST and
PCA branches on or off for the whole run independently.

Output layout under `output_dir`:
```
01_trimmed/<city>_trimmed.tif              # common-band 30m HSI
02_sharpened_10m/<city>_..._sharp10m.tif
band_selection.json                        # cross-city voted band indices
pca_model.npz / pca_model.json             # cross-city PCA model (only if process_pca)
03_hsi_final/hsi_bs/<city>_hsi_bs.tif      # final band-selected 10m HSI
03_hsi_final/hsi_pca/<city>_hsi_pca.tif    # final PCA-reduced 10m HSI (only if process_pca)
04_lst_final/<city>_lst_final.tif          # final normalized 10m LST (+ .json sidecar)
catalog.json + <city>_item.json + shared_item.json   # STAC output catalogue (always written)
```

## STAC input/output

`run-all` can take its per-city inputs from a STAC catalog instead of (or in
addition to) the `cities:` list in the YAML config:

```bash
python processor.py run-all --config config/example_config.yaml \
  --input-catalog examples/stac_input/catalog.json
```

The input catalog is a root `catalog.json` with one `rel: item` link per city;
each item's `assets` must contain `hyperspectral_image` and `sentinel2`, and
may contain `lst_source` (read only if `process_lst: true`). The item `id`
becomes the city name. Shared per-run settings (`band_trim` mode/ranges, LST
band/normalization) come from the config's `band_trim_default:` /
`lst_default:` blocks, since STAC items don't currently carry per-city
overrides for these. `--input-catalog` (or the config's `input_catalog:` key)
overrides any `cities:` list already in the config.

Regardless of how inputs were supplied, `run-all` **always** writes an output
STAC catalog (`catalog.json` + one `<city>_item.json` per city + a
`shared_item.json` for cross-city artifacts like `band_selection.json` /
`pca_model.npz`) at the root of `output_dir`, so a platform or the next
chained processor can discover the products without knowing this repo's
internal folder layout.

## Sample data

`data/Berlin/` holds a small (~5 MB), fully-valid 64x64-pixel crop of the
Berlin HSI (all 250 raw bands, so `trim-bands` behaves identically to
production data) plus matching Sentinel-2 and LST crops (300 m buffer around
the HSI footprint), for quick local/CI testing without the full-size
production rasters. `examples/stac_input/` is a ready-to-use STAC input
catalog pointing at it, and `examples/run_all_config.yaml` a matching config.
Run from the repo root:

```bash
python processor.py run-all --config examples/run_all_config.yaml \
  --input-catalog examples/stac_input/catalog.json
```

This exercises the whole pipeline (trim -> sharpen -> band selection ->
apply -> LST -> STAC output) end to end in well under a minute.

## Run individual steps

```bash
# 1. Trim bands (keep-ranges XOR remove-ranges)
python processor.py trim-bands --input raw.tif --output trimmed.tif --keep-ranges "6-71,188-245"

# 2. Sharpen 30m -> 10m with Sentinel-2
python processor.py sharpen --hsi trimmed.tif --s2 s2.tif --output-dir sharpened/ --method linear

# 3. Cross-city band selection (needs >=1 city, ideally several)
python processor.py select-bands \
  --inputs Athens=athens_trimmed.tif Berlin=berlin_trimmed.tif \
  --wavelength-file CHIME_v2_wavelengths_um.txt \
  --output-json band_selection.json --target-bands 30

# 4. Apply the selection to a sharpened image
python processor.py apply-bands --input sharpened.tif --selection-json band_selection.json --output hsi_bs.tif

# 5. Cross-city PCA fit (needs >=1 city, ideally several; fit on sharpened images)
python processor.py pca-fit \
  --inputs Athens=athens_sharp10m.tif Berlin=berlin_sharp10m.tif \
  --output-prefix pca_model --max-components 30

# 6. Apply the PCA model to a sharpened image
python processor.py pca-apply --input sharpened.tif --model pca_model.npz --output hsi_pca.tif

# 7. LST resample + normalize
python processor.py lst --input lstm.tif --band 7 --ref-grid hsi_bs.tif --output lst_final.tif --normalization zscore
```

## Notes

- `select-bands` and `pca-fit` are both inherently cross-city (voting/fitting
  needs several scenes); in `run-all` each runs once after all cities have
  been sharpened, then `apply-bands`/`pca-apply` run per city. `hsi_bs` and
  `hsi_pca` are two independent, parallel reductions of the same sharpened
  10m HSI — use whichever (or both) downstream in `heatwise-patch-extraction`.
- LST normalization is a parameter, not a per-city hardcoded branch: `zscore`
  computes mean/std from the current scene's valid pixels (stored in the
  output `.json` sidecar for reproducibility); `fixed_scale` uses a fixed
  physical scale `(K - kelvin_offset) / fixed_scale` independent of scene
  statistics.
- `band_trim` supports two input styles for specifying which bands survive:
  `keep-ranges` (list the bands to keep) or `remove-ranges` (list the bands to
  drop). Both operate on the raw band count of whatever file you point at —
  neither implies a specific upstream product. **All cities that will be
  pooled together in `select-bands`/`pca-fit` must end up with the same band
  count and the same keep/remove ranges**, otherwise band index `i` means a
  different wavelength in each city and cross-city voting/PCA will silently
  mix incompatible bands (or crash on array-shape mismatches once band counts
  differ, as in `pca-fit`). Check each raw file's band count first
  (`rasterio.open(path).count`) before deciding per-city `band_trim` settings.

## Docker

The image is built under its release-shaped name (registry namespace +
versioned tag, matching the CWL's `dockerPull`), so local tests exercise the
exact tag that will later be pushed to the registry:

```bash
docker build -t ghcr.io/heatwise-lcz/heatwise-hsi-lst-prep:0.1.0 .

docker run --rm \
  -v "$(pwd)/examples:/app/examples" \
  -v "$(pwd)/data:/app/data" \
  -v /path/to/host/output:/app/output \
  ghcr.io/heatwise-lcz/heatwise-hsi-lst-prep:0.1.0 \
  run-all --config examples/run_all_config.yaml \
          --input-catalog examples/stac_input/catalog.json \
          --output-dir /app/output
```

The base image is plain `python:3.11-slim` (rasterio's pip wheels bundle
GDAL; this repo doesn't need geopandas/fiona's system GDAL linkage) --
see the Dockerfile for details.

> The image has since been built and exercised repeatedly through `cwltool`
> runs (both standalone and as the `prep` step of `heatwise-lcz-pipeline`).

## CWL

`heatwise_hsi_lst_prep.cwl` describes the same `run-all` interface for CWL
runners (inputs: `config` File, optional `input_catalog` File, `output_dir`
string; outputs: `output_catalog` File + `output_directory` Directory).
`examples/job.yaml` is a ready-to-use job order for the bundled sample data:

```bash
cd examples && cwltool ../heatwise_hsi_lst_prep.cwl job.yaml
```

**Two variants of the example config/catalog exist, for two different
execution modes** -- this tripped up the first real `cwltool` run, so it's
worth being explicit:

| | local / plain `python processor.py` | Docker / CWL (`cwltool`) |
|---|---|---|
| config | `examples/run_all_config.yaml` | `examples/run_all_config_docker.yaml` |
| catalog | `examples/stac_input/catalog.json` (+ `Berlin_item.json`) | `examples/stac_input/catalog_docker.json` (+ `Berlin_item_docker.json`) |
| paths inside them | relative (`./data/...`) | absolute (`/app/data/...`, `/app/examples/...`) |

The reason: `cwltool` runs the container with **its own empty per-job working
directory**, not the image's Dockerfile `WORKDIR /app` -- confirmed by an
actual `cwltool` run failing with `python: can't open file '/<job-tmp>/processor.py'`
when the CWL invoked a bare relative `processor.py`. So the CWL's
`arguments` now reference `/app/processor.py` directly, and any path
*inside* the config/catalog content must also be an absolute `/app/...`
path pointing into the image (baked in via `COPY . .`), not a path relative
to wherever `cwltool` happens to stage things.

This also killed the original plan of using `secondaryFiles` to bring the
STAC item JSON and rasters along with a relative-href `catalog.json`:
`cwltool` only stages the exact File given for an input, confirmed by an
actual run where only `catalog.json` got mounted into the container, not its
sibling `Berlin_item.json` or the `.tif` assets it pointed at. The
`catalog_docker.json`/`Berlin_item_docker.json` pair sidesteps this
entirely by using absolute hrefs, so `cwltool` never needs to stage anything
beyond the one small `catalog_docker.json` file itself.

`examples/job.yaml` is wired to the Docker/CWL variants; edit `--config`/
`--input-catalog` directly if you want to test the local variants without
Docker.

> **Rebuild the image before testing this** if you already built it before
> `run_all_config_docker.yaml`/`catalog_docker.json`/`Berlin_item_docker.json`
> existed -- they need to be baked in via `COPY . .`:
> `docker build -t ghcr.io/heatwise-lcz/heatwise-hsi-lst-prep:0.1.0 .`
