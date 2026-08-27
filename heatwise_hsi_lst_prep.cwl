cwlVersion: v1.2

$namespaces:
  s: https://schema.org/

s:softwareVersion: 0.1.1
s:version: 0.1.1

schemas:
  - http://schema.org/version/9.0/schemaorg-current-http.rdf

$graph:

  # -------------------------------------------------------------------------
  # Main EOAP Workflow
  # -------------------------------------------------------------------------
  - class: Workflow
    id: main
    label: HEATWISE HSI/LST Preprocessing Workflow
    doc: |
      EOAP-compatible HEATWISE HSI/LST preprocessing workflow.

      The workflow runs the per-city preprocessing chain used by the
      HEATWISE LCZ mapping workflow, including hyperspectral band trimming,
      sharpening, cross-city band selection and application, optional
      cross-city PCA, and optional LST processing.

      EO input products are provided through a staged STAC catalog directory.
      The processor generates the corresponding preprocessing products and
      an output STAC catalog describing the EO products.

    requirements: []

    inputs:

      - id: config
        type: File
        label: processing configuration
        doc: |
          Run-level YAML configuration controlling the HSI/LST preprocessing
          workflow, including LST/PCA processing options, band-selection and
          PCA parameters, wavelength information, and city configuration.

      - id: input_catalog
        type: Directory
        label: input STAC catalog
        doc: |
          Directory containing a STAC catalog named catalog.json referencing
          the staged EO input products required by the preprocessing workflow,
          such as hyperspectral imagery, Sentinel-2 imagery, and LST products.

      - id: output_dir
        type: string
        label: output directory
        default: output
        doc: |
          Output directory name created inside the CWL working directory.

    steps:

      processor:
        run: "#hsi_lst_prep_processor"

        in:
          config: config
          input_catalog: input_catalog
          output_dir: output_dir

        out:
          - output

    outputs:

      output:
        type: Directory
        outputSource: processor/output


  # -------------------------------------------------------------------------
  # HSI/LST preprocessing CommandLineTool
  # -------------------------------------------------------------------------
  - class: CommandLineTool
    id: hsi_lst_prep_processor
    label: HEATWISE HSI/LST Preprocessing Processor
    doc: |
      HEATWISE HSI/LST preprocessing processor used by the LCZ mapping
      workflow.

      The processor executes the complete preprocessing chain using the
      supplied YAML configuration and the EO products referenced by the
      input STAC catalog.

    requirements:

      DockerRequirement:
        dockerPull: ghcr.io/heatwise-lcz/heatwise-hsi-lst-prep:0.1.1

      InlineJavascriptRequirement: {}

    baseCommand: python

    arguments:
      - /app/processor.py
      - run-all

    inputs:

      config:
        type: File
        label: processing configuration
        doc: |
          Run-level YAML configuration controlling the HSI/LST preprocessing
          workflow.
        inputBinding:
          prefix: --config

      input_catalog:
        type: Directory
        label: input STAC catalog
        doc: |
          Directory containing catalog.json and the associated STAC Items and
          Assets for the staged EO input products.

          The path to catalog.json inside this directory is passed to the
          processor through the --input-catalog argument.
        inputBinding:
          prefix: --input-catalog
          valueFrom: $(self.path + "/catalog.json")

      output_dir:
        type: string
        label: output directory
        default: output
        doc: |
          Output directory name created inside the CWL working directory.
        inputBinding:
          prefix: --output-dir

    outputs:

      output:
        type: Directory
        doc: |
          Complete CWL working directory containing all files produced by the
          processor, including the preprocessing products and the generated
          STAC catalog.
        outputBinding:
          glob: "."
    type: Directory
    outputBinding:
      glob: $(inputs.output_dir)
    doc: Full output directory (trimmed/sharpened intermediates, hsi_bs/hsi_pca/lst finals, band_selection.json, pca_model.*, STAC items).
