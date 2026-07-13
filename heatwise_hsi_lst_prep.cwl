cwlVersion: v1.2
class: CommandLineTool

label: HEATWISE HSI/LST Preprocessing
doc: >
  EOAP-compatible HEATWISE HSI/LST preprocessing processor. Runs the full
  per-city pipeline (band trim -> sharpen -> cross-city band selection ->
  apply -> optional cross-city PCA -> optional LST) driven by a YAML config,
  optionally sourcing per-city inputs from a STAC input catalog, and always
  writes an output STAC catalog describing the products.

requirements:
  DockerRequirement:
    # Release-shaped image reference. Before publishing, build/tag this
    # image locally with the same name so local cwltool runs exercise the
    # exact tag that will later be pushed to the registry.
    dockerImageId: ghcr.io/heatwise-lcz/heatwise-hsi-lst-prep:0.1.1
    dockerPull: ghcr.io/heatwise-lcz/heatwise-hsi-lst-prep:0.1.1

baseCommand: python
arguments:
  # Absolute path: cwltool runs the container with its OWN working directory
  # (an empty per-job staging directory), not the image's Dockerfile WORKDIR,
  # so a bare relative `processor.py` is not found. Confirmed by an actual
  # cwltool run (`python: can't open file '/<job-tmp>/processor.py'`).
  - /app/processor.py
  - run-all

inputs:
  config:
    type: File
    inputBinding:
      prefix: --config
    doc: >
      Run-level YAML config (process_lst/process_pca, band_selection/pca
      params, wavelength_file, cities or STAC defaults). Any path *inside*
      this YAML (e.g. wavelength_file) must be an absolute /app/... path
      pointing into the image, not a relative one -- see the note on
      `input_catalog` below for why. examples/run_all_config_docker.yaml is
      written this way; examples/run_all_config.yaml (relative paths) is for
      local/non-Docker runs only.

  input_catalog:
    type: File?
    inputBinding:
      prefix: --input-catalog
    doc: >
      Optional STAC catalog.json listing per-city input assets
      (hyperspectral_image/sentinel2/lst_source). Overrides the config's
      `cities:` list if given. cwltool only stages the exact File given
      here -- it does NOT bring along sibling item JSON files or the
      rasters they reference (tried declaring them as `secondaryFiles`;
      confirmed by an actual cwltool run that this does not work the way a
      naive glob pattern would suggest). So for CWL use, the catalog's item
      link and every asset href must be an ABSOLUTE /app/... path pointing
      into the image (already baked in via `COPY . .`), not a relative
      path resolved next to the staged catalog.json. See
      examples/stac_input/catalog_docker.json for a working example;
      examples/stac_input/catalog.json (relative hrefs) is for local
      /non-Docker runs only.

  output_dir:
    type: string
    default: output
    inputBinding:
      prefix: --output-dir
    doc: Output directory name (created under the CWL working directory).

outputs:
  output_catalog:
    type: File
    outputBinding:
      glob: $(inputs.output_dir)/catalog.json
    doc: Root STAC catalog describing every produced item (per-city + shared).

  output_directory:
    type: Directory
    outputBinding:
      glob: $(inputs.output_dir)
    doc: Full output directory (trimmed/sharpened intermediates, hsi_bs/hsi_pca/lst finals, band_selection.json, pca_model.*, STAC items).
