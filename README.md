# OrthoSWIFT Plugin for WebODM

OrthoSWIFT turns a georeferenced multispectral orthomosaic into vegetation-index rasters, management zones, stress-hotspot review layers, controller prescription packages, and farmer-facing PDF deliverables inside WebODM.

## Status

This release is marked **experimental**. Prescription files are decision-support outputs, not agronomic recommendations or controller certification. Review geometry, rates, units, product labels, equipment calibration, and target-display behavior before field use.

## Requirements

- WebODM 2.5.0 or newer, as declared in `orthoswift/manifest.json`
- A georeferenced multispectral GeoTIFF containing identifiable red and near-infrared bands
- Internet access while enabling the plugin if Python dependencies are not already cached

The plugin declares its Python dependencies in `orthoswift/requirements.txt`; WebODM installs plugin dependencies when the plugin is enabled.

## Installation

### Install the release ZIP

1. Download `orthoswift-plugin-webodm.zip` from the GitHub release.
2. In WebODM, open **Administration → Plugins**.
3. Select **Load Plugin (.zip)** and upload the ZIP.
4. Enable **OrthoSWIFT**. WebODM installs the declared Python dependencies.
5. Restart the WebODM application and worker services if your deployment does not reload plugins automatically.

Do not install this by restarting NodeODM alone. The plugin runs in WebODM and its worker process.

### Install from source for development

Clone the repository, then copy or link the `orthoswift/` directory into WebODM's `plugins/` directory. Enable it from **Administration → Plugins**.

```bash
git clone https://github.com/orthoswift/orthoswift-plugin-webodm.git
```

## Usage

1. Open the OrthoSWIFT page from the WebODM side menu.
2. Select a multispectral orthomosaic GeoTIFF or a WebODM `all.zip` archive containing one.
3. Optionally enter an operator-approved physical fertilizer-rate plan.
4. Run the analysis and download `orthoswift-deliverables.zip`.

The existing interface is self-contained in `orthoswift/templates/index.html`.

## Input contract

The selected GeoTIFF must:

- use the GeoTIFF driver;
- have a coordinate reference system;
- contain unique, identifiable red and near-infrared bands;
- use band descriptions or a supported standard multispectral layout.

The runner rejects missing coordinate systems, duplicate band assignments, unsupported band indexes, and invalid configuration values.

### Optional physical fertilizer rates

When omitted, prescriptions contain relative `TargetRate` values from 0 to 100 percent. Physical mode accepts an operator/agronomist-supplied plan such as:

```json
{
  "mode": "physical",
  "operation": "fertilizer",
  "product_name": "Urea 46-0-0",
  "rate_basis": "product",
  "unit": "KG_HA",
  "strategy": "direct",
  "min_rate": 100,
  "max_rate": 180,
  "approved_by": "Operator name"
}
```

Supported units are `KG_HA`, `L_HA`, `LB_AC`, `GAL_AC`, and `SEEDS_HA`. OrthoSWIFT spatially encodes supplied rates; it does not infer an agronomic dose from imagery.

## Outputs

Depending on the available bands and quality-control gates, the archive contains:

- NDVI, NDRE, GLI, MSAVI2, classification, and management-zone GeoTIFFs;
- management-zone and stress-hotspot GeoJSON/KML/CSV layers;
- generic and controller-oriented prescription ZIPs;
- optional offline orthomosaic MBTiles;
- `health_report.pdf` and `setup_guide.pdf`;
- rate-plan, input-band, methodology, validation, and warning audit files.

DJI and XAG exports are structural/research-stage formats and require target-system verification.

## Development

Create an environment, install plugin and test dependencies, then run:

```bash
python -m pip install -r orthoswift/requirements.txt
python -m pip install pytest
python -m pytest -q
```

The tests cover rate-plan validation, controller package generation, runner configuration, a real synthetic multispectral end-to-end job, upload limits, release metadata, static syntax, and removal of unsupported backend features.

## Repository scope

The public plugin contains the agriculture pipeline used by the visible WebODM interface. Unreachable plant-counting, reseeding, inter-row weed, DSM/DTM, inspection, flight-mission, and disabled-stub code has been removed. The user-facing template, stylesheet, and active agriculture report/guide wording are retained.

## Publishing and review

Publish the source repository and create a tagged GitHub release containing `orthoswift-plugin-webodm.zip`. For WebODM review, send the repository URL and release ZIP to the maintainer, together with:

- supported WebODM version;
- installation steps;
- test command and passing result;
- a small non-sensitive sample dataset or reproducible fixture;
- screenshots and a short description of expected outputs;
- known limitations and controller-certification status.

If requesting inclusion in WebODM itself, open a pull request against the WebODM repository after maintainer feedback. The official plugin guide allows independent GitHub/ZIP distribution and describes core-plugin inclusion through a WebODM pull request.

## Security

- Uploaded filenames are reduced to a basename.
- Uploads and extracted orthomosaics are size-bounded.
- ZIP members are streamed to fixed paths instead of extracting user-controlled paths.
- Prescription ZIP validation checks archive paths, member sizes, compression ratios, geometry, CRS, and required fields.

Please report vulnerabilities privately before opening a public issue.

## License

MIT License. See `LICENSE`.

## Support

Issues: https://github.com/orthoswift/orthoswift-plugin-webodm/issues

## Version

4.5.3
