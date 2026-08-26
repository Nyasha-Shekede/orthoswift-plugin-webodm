# OrthoSWIFT Plugin for Agisoft Metashape

OrthoSWIFT turns a georeferenced multispectral orthomosaic into vegetation-index rasters, management zones, stress-hotspot review layers, controller prescription packages (John Deere, Case IH, Trimble, Ag Leader, New Holland, DJI Agras, XAG), and farmer-facing PDF deliverables directly inside Agisoft Metashape Professional.

## Status

This release is marked **experimental**. Prescription files are decision-support outputs, not agronomic recommendations or controller certification. Review geometry, rates, units, product labels, equipment calibration, and target-display behavior before field use.

## Requirements

- Agisoft Metashape Professional 2.2 or newer
- Python 3.10–3.12 with `venv` support for the isolated background runner environment
- A georeferenced multispectral GeoTIFF or an active Metashape chunk orthomosaic containing identifiable red and near-infrared bands
- Windows, macOS, or Linux

The plugin declares its dependencies in `requirements.txt`. The installer creates an isolated virtual environment and registers the plugin loader into Metashape's user scripts directory.

## Installation

### Automated Installer

#### Windows
Double-click `install-windows.bat` or run:
```bat
install-windows.bat
```

#### macOS
Double-click `install-macos.command` or run:
```bash
chmod +x install-macos.command
./install-macos.command
```

#### Linux
```bash
bash install-macos-linux.sh
```

### Manual CLI Installation

You can specify an explicit Python 3.10–3.12 interpreter:
```bash
python orthoswift/install.py --python /path/to/python3.11
```

To use an existing Python environment with pre-installed dependencies:
```bash
python orthoswift/install.py --python /path/to/python --skip-dependencies
```

### Uninstallation

```bash
python orthoswift/install.py --uninstall
```

## Usage

1. Launch Agisoft Metashape Professional.
2. Open a project with an aligned and orthorectified multispectral chunk, or prepare an external GeoTIFF orthomosaic.
3. From the top menu bar, select **OrthoSWIFT → Run multispectral field analysis…**.
4. The dialog detects active chunk orthomosaics automatically. Alternatively, click **Choose File** to select an external GeoTIFF.
5. (Optional) Toggle **Fertilizer rate plan** to supply operator-approved physical application rates (e.g. Urea 46-0-0 in kg/ha or lb/acre).
6. (Optional) Toggle **Spot spraying target rate** to supply a custom chemical application rate over stress/weed hotspots.
7. Click **Run analysis**.
8. Metashape displays a live progress dialog streaming processing stages (`[PROGRESS <pct>%] <message>`).
9. When complete, a summary dialog provides direct access to the results folder and `orthoswift-deliverables.zip`.

## Input contract

The input raster (or exported chunk orthomosaic) must:

- use the GeoTIFF driver;
- have a valid coordinate reference system (projected metre-based CRS or geographic WGS84, which is automatically reprojected to UTM);
- contain unique, identifiable red and near-infrared spectral bands;
- provide standard band descriptions or match standard multispectral sensor layouts (e.g., MicaSense RedEdge/Altum, DJI P4M/M3M, Sentera, RGB-NIR).

The runner rejects missing coordinate systems, duplicate band assignments, unsupported band indexes, and invalid configuration values.

### Optional physical fertilizer rates

When omitted, prescriptions contain relative `TargetRate` values from 0 to 100 percent for in-cab adjustment. Enabling physical mode accepts an operator/agronomist-supplied plan:

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

Supported units: `KG_HA`, `L_HA`, `LB_AC`, `GAL_AC`, `SEEDS_HA`. Strategies: `direct` (high vigor = high rate) and `inverse` (low vigor = high rate).

### Optional spot spraying target rate

When omitted, spot spraying prescriptions use binary section control (100% on hotspot targets, 0% off-target). Enabling custom rates encodes specific target dosages:

```json
{
  "mode": "physical",
  "operation": "spray",
  "product_name": "Roundup PowerMAX",
  "rate_basis": "product",
  "unit": "L_HA",
  "strategy": "target_hotspots",
  "min_rate": 150,
  "max_rate": 150,
  "approved_by": "Operator name"
}
```

## Outputs

Depending on the available spectral bands and quality-control gates, the results directory and `orthoswift-deliverables.zip` contain:

| Deliverable | Format | Purpose |
| :--- | :--- | :--- |
| **Fertilizer Zone Map** | Shapefile + Controller ZIPs (with offline MBTiles) | Drives variable-rate spreaders and sprayers (John Deere, Case IH, Trimble, Ag Leader, New Holland, DJI Agras, XAG). Includes full-resolution offline MBTiles basemap. |
| **Targeted Spray Map** | Shapefile + Controller ZIPs (with offline MBTiles) | Triggers sprayer sections or drones over detected weed/stress patches. |
| **Stress Hotspot Map** | GeoJSON + KML + CSV | Pinpoints lowest-performing field zones for targeted ground scouting and validation. |
| **Field Health Summary** | PDF + PNG map | Ready-to-share agronomic report combining zone maps, canopy cover statistics, and scouting targets. |
| **Technical GIS & Audit** | GeoTIFF + CSV + JSON | Analytical vegetation indices (NDVI, NDRE, GLI, MSAVI2), crop mask QC, methodology audits, and warnings. |

DJI Agras and XAG exports are structural/research-stage formats and require equipment profile verification prior to field application.

## Development

Set up a virtual environment and install development dependencies:

```bash
python -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate
python -m pip install -r requirements.txt
python -m pip install pytest ruff
```

## Architecture

- **`orthoswift/metashape.py`**: Qt dialog (PySide2), UI stylesheets, chunk raster exporter, and non-blocking `QProcess` runner watcher.
- **`orthoswift/runner.py`**: Execution adapter that parses input configs, runs `run_agriculture_pipeline`, streams stdout progress tokens, and packages deliverables.
- **`orthoswift/core/`**: Shared core processing modules (`pipeline.py`, `decisions.py`, `vegetation.py`, `exports.py`, `basemaps.py`, `report.py`, `guide.py`).
- **`orthoswift/install.py`**: Per-user script installer and `.venv` builder.

## Security

- User inputs and file selections are strictly validated and constrained to valid paths.
- Execution runs in an isolated Python subprocess without blocking Metashape's main UI thread.
- Prescription ZIP generation validates internal directory structures, geometry bounds, and field names.

Please report security concerns privately before opening public issues.

## License

MIT License. See `LICENSE`.

## Support

- Issues: https://github.com/Nyasha-Shekede/orthoswift-plugin-webodm/issues
- Website & Documentation: https://orthoswift.net

## Version

1.0.0
