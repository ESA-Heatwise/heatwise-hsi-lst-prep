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

```text
01_trimmed/<city>_trimmed.tif              # common-band 30m HSI
02_sharpened_10m/<city>_..._sharp10m.tif
band_selection.json                        # cross-city voted band indices
pca_model.npz / pca_model.json             # cross-city PCA model (only if process_pca)
03_hsi_final/hsi_bs/<city>_hsi_bs.tif      # final band-selected 10m HSI
03_hsi_final/hsi_pca/<city>_hsi_pca.tif    # final PCA-reduced 10m HSI (only if process_pca)
04_lst_final/<city>_lst_final.tif          # final normalized 10m LST (+ .json sidecar)
catalog.json + <city>_item.json + shared_item.json   # STAC output catalog (always written)
```


## STAC input/output

`run-all` can take its per-city inputs from a STAC catalog instead of (or in
addition to) the `cities:` list in the YAML config:

```bash
python processor.py run-all \
  --config config/example_config.yaml \
  --input-catalog examples/stac_input/catalog.json
```

The input catalog is a root `catalog.json` with one `rel: item` link per city.

Each STAC Item must contain the following assets:

- `hyperspectral_image`
- `sentinel2`

and may additionally contain:

- `lst_source`

The `lst_source` asset is read only if `process_lst: true`.

The Item `id` becomes the city name. Shared per-run settings such as band
trimming and LST processing are read from the configuration file through the
`band_trim_default:` and `lst_default:` blocks.

If `--input-catalog` is supplied, the STAC-defined city inputs take precedence
over a `cities:` list already present in the configuration.

Regardless of how inputs are supplied, `run-all` always writes an output STAC
catalog at the root of `output_dir`. The output includes:

```text
catalog.json
<city>_item.json
shared_item.json
```

The city Items reference the generated EO products, while `shared_item.json`
describes cross-city artifacts such as `band_selection.json` and, when PCA is
enabled, the PCA model files.

This allows a platform or a downstream processor to discover the generated
products without depending on the internal output directory structure.


## Sample data

`examples/stac_input/` contains a cropped Berlin sample covering the HEATWISE
sample boundary (~5.4 x 8.1 km):

```text
Berlin_CHIME.tif
Berlin_S2.tif
Berlin_LSTM.tif
Berlin_item.json
catalog.json
```

The sample products are:

- `Berlin_CHIME.tif`: 250-band CHIME-like hyperspectral input at 30 m.
- `Berlin_S2.tif`: 10-band Sentinel-2 input at 10 m, used for sharpening.
- `Berlin_LSTM.tif`: 7-band LST source at 50 m.

The extent covers all 8 labelled LCZ classes of the Berlin sample labels used
downstream, allowing the complete pipeline to produce meaningful training
patches.

`catalog.json` and `Berlin_item.json` form the STAC input catalog and use
relative links to the bundled raster assets.

For a direct local Python run from the repository root:

```bash
python processor.py run-all \
  --config examples/run_all_config.yaml \
  --input-catalog examples/stac_input/catalog.json
```

This exercises the complete preprocessing chain:

```text
trim
  -> sharpen
  -> band selection
  -> band application
  -> LST processing
  -> STAC output
```


## Run individual steps

```bash
# 1. Trim bands (keep-ranges XOR remove-ranges)
python processor.py trim-bands \
  --input raw.tif \
  --output trimmed.tif \
  --keep-ranges "6-71,188-245"

# 2. Sharpen 30m -> 10m with Sentinel-2
python processor.py sharpen \
  --hsi trimmed.tif \
  --s2 s2.tif \
  --output-dir sharpened/ \
  --method linear

# 3. Cross-city band selection (needs >=1 city, ideally several)
python processor.py select-bands \
  --inputs Athens=athens_trimmed.tif Berlin=berlin_trimmed.tif \
  --wavelength-file CHIME_v2_wavelengths_um.txt \
  --output-json band_selection.json \
  --target-bands 30

# 4. Apply the selection to a sharpened image
python processor.py apply-bands \
  --input sharpened.tif \
  --selection-json band_selection.json \
  --output hsi_bs.tif

# 5. Cross-city PCA fit (needs >=1 city, ideally several)
python processor.py pca-fit \
  --inputs Athens=athens_sharp10m.tif Berlin=berlin_sharp10m.tif \
  --output-prefix pca_model \
  --max-components 30

# 6. Apply the PCA model to a sharpened image
python processor.py pca-apply \
  --input sharpened.tif \
  --model pca_model.npz \
  --output hsi_pca.tif

# 7. LST resample + normalize
python processor.py lst \
  --input lstm.tif \
  --band 7 \
  --ref-grid hsi_bs.tif \
  --output lst_final.tif \
  --normalization zscore
```


## Notes

- `select-bands` and `pca-fit` are both inherently cross-city operations.
  Voting/fitting is performed after all participating scenes have been
  sharpened. `apply-bands` and `pca-apply` then run independently for each
  city.

- `hsi_bs` and `hsi_pca` are two independent dimensionality-reduction routes
  applied to the same sharpened 10 m HSI. Either or both may be consumed by
  the downstream `heatwise-patch-extraction` processor.

- LST normalization is configured rather than hardcoded per city. `zscore`
  computes mean and standard deviation from the current scene's valid pixels
  and records them in the output JSON sidecar. `fixed_scale` instead applies:

  ```text
  (K - kelvin_offset) / fixed_scale
  ```

- `band_trim` supports two alternative input styles:
  - `keep-ranges`: list the bands to retain.
  - `remove-ranges`: list the bands to discard.

  Both operate on the raw band count of the supplied HSI.

- All cities pooled together in `select-bands` or `pca-fit` must have the same
  resulting band count and compatible keep/remove ranges. Otherwise the same
  band index can represent different wavelengths in different cities.

Before defining band-trimming parameters, the raw band count can be inspected
with:

```python
rasterio.open(path).count
```


## Docker

The processor image is built under the same registry name and version used by
the CWL `DockerRequirement`:

```bash
docker build \
  -t ghcr.io/heatwise-lcz/heatwise-hsi-lst-prep:0.1.1 \
  .
```

A direct Docker run can use the bundled example data and configuration:

```bash
docker run --rm \
  -v "$(pwd)/examples:/app/examples" \
  -v "$(pwd)/data:/app/data" \
  -v /path/to/host/output:/app/output \
  ghcr.io/heatwise-lcz/heatwise-hsi-lst-prep:0.1.1 \
  python /app/processor.py run-all \
    --config /app/examples/run_all_config_docker.yaml \
    --input-catalog /app/examples/stac_input/catalog.json \
    --output-dir /app/output
```

The repository is copied into `/app` when the image is built. Auxiliary
processor resources that are part of the software package can therefore use
paths inside the image, for example:

```text
/app/data/CHIME_v2_wavelengths_um.txt
```

EO input products are handled separately through the staged STAC input when
the processor is executed as a CWL Application Package.


## CWL / EO Application Package

`heatwise_hsi_lst_prep.cwl` packages the preprocessing processor as a CWL v1.2
EO Application Package.

The CWL document contains:

- a top-level `Workflow` with ID `main`, used as the application entry point;
- a `CommandLineTool` with ID `hsi_lst_prep_processor`, which executes the
  preprocessing code inside the versioned Docker image.

The workflow inputs are:

### `config`

```text
type: File
```

Run-level YAML configuration controlling the processing chain, including
LST/PCA options, band-selection parameters, wavelength information and shared
processing parameters.

### `input_catalog`

```text
type: Directory
```

A staged EO input directory containing:

```text
catalog.json
STAC Item JSON files
EO raster assets referenced by the Items
```

For example:

```text
stac_input/
├── catalog.json
├── Berlin_item.json
├── Berlin_CHIME.tif
├── Berlin_S2.tif
└── Berlin_LSTM.tif
```

The STAC catalog and Items use relative links to their associated files.

The complete directory is staged by CWL, rather than staging `catalog.json` as
an isolated `File`. The processor is then passed:

```text
<input_catalog>/catalog.json
```

through its `--input-catalog` argument.

This means that Docker-specific STAC catalogs containing absolute `/app/...`
asset paths are not required for CWL execution.

Legacy files such as:

```text
catalog_docker.json
Berlin_item_docker.json
```

may remain in the example directory for historical reference, but they are not
used by the current CWL job.

### `output_dir`

```text
type: string
default: "."
```

Products are written directly into the CWL working directory by default.

The `CommandLineTool` exposes the complete working directory as one CWL
`Directory` output using:

```yaml
outputBinding:
  glob: "."
```

This allows all generated files required for stage-out to be collected
together, including the STAC catalog and its referenced products.

A typical CWL output contains:

```text
01_trimmed/
├── Berlin_trimmed.tif

02_sharpened_10m/
├── Berlin_trimmed_sharp10m.tif

03_hsi_final/
└── hsi_bs/
    └── Berlin_hsi_bs.tif

04_lst_final/
├── Berlin_lst_final.tif
└── Berlin_lst_final.json

band_selection.json
catalog.json
Berlin_item.json
shared_item.json
```

If PCA processing is enabled, the corresponding PCA products and model files
are included as well.


## CWL example

`examples/job.yaml` provides a ready-to-use CWL job for the bundled Berlin
sample:

```yaml
config:
  class: File
  path: run_all_config_docker.yaml

input_catalog:
  class: Directory
  path: stac_input

output_dir: "."
```

Run the example from the repository root with:

```bash
cwltool \
  --outdir cwl-output \
  heatwise_hsi_lst_prep.cwl \
  examples/job.yaml
```

The CWL processor invokes:

```text
python /app/processor.py run-all
```

inside the Docker container and passes the staged STAC catalog as:

```text
--input-catalog <staged-input-directory>/catalog.json
```


## EOAP validation

The bundled Berlin example has been tested end-to-end as an EO Application
Package.

The validation covers:

```text
CWL document validation
        ↓
Input STAC validation
        ↓
Docker image build
        ↓
CWL execution
        ↓
Output STAC validation
        ↓
Generated-product inspection
```

The current example successfully:

- validates `heatwise_hsi_lst_prep.cwl` with `cwltool`;
- validates the input `catalog.json` and its STAC Item with PySTAC;
- builds the processor Docker image;
- executes the Berlin sample through the CWL Workflow;
- generates the preprocessing products and output STAC catalog;
- validates the generated output STAC catalog and Items with PySTAC.


## Automated validation

The repository contains a GitHub Actions workflow at:

```text
.github/workflows/validate-cwl.yml
```

The workflow installs `cwltool` and PySTAC validation dependencies and performs:

1. CWL validation.
2. Input STAC validation.
3. Processor Docker image build.
4. End-to-end execution of `examples/job.yaml`.
5. Output STAC validation.
6. Listing of the generated products for inspection.

The workflow can also be launched manually through GitHub Actions using
`workflow_dispatch`.
