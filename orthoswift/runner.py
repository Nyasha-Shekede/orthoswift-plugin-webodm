"""Shared adapter runner for WebODM and Agisoft Metashape."""

from __future__ import annotations

import argparse
import json
import math
import shutil
import sys
import traceback
import zipfile
from pathlib import Path

SPECTRAL_KEYS = ("red", "green", "blue", "nir", "red_edge")


def _existing(value):
    if value in (None, ""):
        return None
    path = Path(value).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(str(path))
    return path


def _reflectance_scale(value):
    """Validate an optional positive scalar or per-band scale mapping."""
    if value in (None, ""):
        return None
    if isinstance(value, dict):
        unknown = set(value) - set(SPECTRAL_KEYS)
        if unknown:
            raise ValueError(f"Unknown reflectance_scale role(s): {sorted(unknown)}")
        clean = {}
        for role, factor in value.items():
            number = float(factor)
            if not math.isfinite(number) or number <= 0:
                raise ValueError(
                    f"reflectance_scale for '{role}' must be finite and > 0"
                )
            clean[role] = number
        return clean or None
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise ValueError("reflectance_scale must be finite and > 0")
    return number


def _as_bool(value, *, name):
    if isinstance(value, bool):
        return value
    if value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off", ""}:
            return False
    raise ValueError(f"{name} must be a boolean")


def validate_config(config):
    if not isinstance(config, dict):
        raise TypeError("Configuration must be a JSON object")
    out = Path(config["out_dir"]).expanduser().resolve()
    ortho = _existing(
        config.get("orthomosaic_path") or config.get("multispectral_path")
    )
    if not ortho:
        raise ValueError("A GeoTIFF orthomosaic is required")
    supplied_band_map = config.get("band_map")
    clean = None
    if supplied_band_map:
        clean = {
            key: int(value)
            for key, value in supplied_band_map.items()
            if key in SPECTRAL_KEYS and value not in (None, "")
        }
        if "red" not in clean or "nir" not in clean:
            raise ValueError("An advanced band-map override must include red and NIR")
        if len(set(clean.values())) != len(clean):
            raise ValueError("Each spectral role must use a different band")
        if min(clean.values()) < 1:
            raise ValueError("Band numbers are one-based")
    zones = int(config.get("zones", 3))
    if not 2 <= zones <= 8:
        raise ValueError("zones must be between 2 and 8")
    scale = _reflectance_scale(config.get("reflectance_scale"))
    offline_basemap = _as_bool(
        config.get("offline_basemap", True), name="offline_basemap"
    )
    max_pixels = config.get("max_pixels")
    if max_pixels is not None:
        max_pixels = int(max_pixels)
        if max_pixels <= 0:
            raise ValueError("max_pixels must be positive")

    import rasterio

    with rasterio.open(ortho) as dataset:
        if dataset.driver != "GTiff":
            raise ValueError(
                f"Input must be a GeoTIFF; detected driver {dataset.driver!r}"
            )
        if dataset.crs is None:
            raise ValueError("Input GeoTIFF has no coordinate reference system")
        if clean is not None:
            too_high = {
                role: index for role, index in clean.items() if index > dataset.count
            }
            if too_high:
                details = ", ".join(
                    f"{role}={index}" for role, index in too_high.items()
                )
                raise ValueError(
                    f"Band assignment exceeds the GeoTIFF band count ({dataset.count}): {details}"
                )
        from .core.pipeline import _resolve_multispectral_band_map

        detected_map, alpha_index = _resolve_multispectral_band_map(dataset, clean)
        clean = detected_map
        raster_info = {
            "path": str(ortho),
            "bands": int(dataset.count),
            "width": int(dataset.width),
            "height": int(dataset.height),
            "crs": str(dataset.crs),
            "descriptions": [description or "" for description in dataset.descriptions],
            "resolved_band_map": clean,
            "alpha_band": alpha_index,
            "band_detection": "advanced_override" if supplied_band_map else "automatic",
        }
    if ortho.is_relative_to(out):
        raise ValueError("The output directory cannot contain the input orthomosaic")
    if out.exists():
        for item in out.iterdir():
            if item.is_dir():
                shutil.rmtree(item)
            else:
                item.unlink()
    out.mkdir(parents=True, exist_ok=True)

    options = {"offline_basemap": offline_basemap, "max_pixels": max_pixels}
    return out, ortho, clean, zones, scale, raster_info, options


def make_preview(ortho, out_path, band_map):
    import matplotlib.pyplot as plt
    import numpy as np
    import rasterio

    with rasterio.open(ortho) as src:
        ids = (
            [band_map.get(role) for role in ("red", "green", "blue")]
            if band_map
            else []
        )
        ids = ids if all(ids) else list(range(1, min(src.count, 3) + 1))
        arr = src.read(
            ids, out_shape=(len(ids), min(src.height, 1600), min(src.width, 1600))
        ).astype("float32")
    if arr.shape[0] == 1:
        arr = np.repeat(arr, 3, axis=0)
    elif arr.shape[0] == 2:
        arr = np.concatenate([arr, arr[1:2]], axis=0)
    rgb = np.moveaxis(arr[:3], 0, -1)
    finite = np.isfinite(rgb)
    lo, hi = np.nanpercentile(rgb[finite], [2, 98]) if finite.any() else (0, 1)
    rgb = np.clip((rgb - lo) / (hi - lo if hi > lo else 1), 0, 1)
    plt.imsave(out_path, rgb)


def run(config, progress_callback=None):
    out, ortho, band_map, zones, scale, raster_info, options = validate_config(config)
    if progress_callback:
        progress_callback("Rendering orthomosaic preview", 22)
    previews_dir = out / "technical_gis" / "previews"
    previews_dir.mkdir(parents=True, exist_ok=True)
    preview = previews_dir / "orthomosaic_preview.png"
    make_preview(ortho, preview, band_map)
    from .core.pipeline import run_agriculture_pipeline

    result = run_agriculture_pipeline(
        ortho_preview=preview,
        out_dir=out,
        orthomosaic_path=ortho,
        multispectral_path=ortho,
        multispectral_band_map=band_map,
        reflectance_scale=scale,
        k_range=(zones, zones),
        hotspot_percentile=float(config.get("hotspot_percentile", 10)),
        hotspot_min_area_m2=float(config.get("hotspot_min_area_m2", 50)),
        export_kml=True,
        export_offline_basemap=options["offline_basemap"],
        export_relative_application_packages=True,
        fertilizer_rate_plan=config.get("fertilizer_rate_plan"),
        spot_spray_rate_plan=config.get("spot_spray_rate_plan"),
        bundle_basemap_in_controller_archives=options["offline_basemap"],
        allow_unvalidated_prescription_export=_as_bool(
            config.get("unsafe_override", False), name="unsafe_override"
        ),
        max_agriculture_pixels=options["max_pixels"],
        progress_callback=progress_callback,
    )
    summaries_dir = out / "technical_gis" / "data_summaries"
    summaries_dir.mkdir(parents=True, exist_ok=True)
    manifest = summaries_dir / "adapter_run.json"
    manifest.write_text(
        json.dumps(
            {
                "adapter_config": {
                    "zones": zones,
                    "offline_basemap": options["offline_basemap"],
                    "fertilizer_rate_plan": config.get("fertilizer_rate_plan"),
                    "spot_spray_rate_plan": config.get("spot_spray_rate_plan"),
                    "host": config.get("host"),
                },
                "input_raster": raster_info,
                "controller_package_requested": "dji_agras",
                "core_outputs": result,
            },
            indent=2,
            default=str,
        ),
        encoding="utf-8",
    )
    if progress_callback:
        progress_callback("Packaging deliverables ZIP archive", 98)
    archive = out.parent / "orthoswift-deliverables.zip"
    with zipfile.ZipFile(
        archive, "w", zipfile.ZIP_DEFLATED, allowZip64=True
    ) as output_zip:
        for path in sorted(out.rglob("*")):
            if path.is_file():
                output_zip.write(path, path.relative_to(out.parent))
    if progress_callback:
        progress_callback("Complete. Deliverables ready", 100)
    return {
        "output_dir": str(out),
        "archive": str(archive),
        "core_outputs": result,
        "input_raster": raster_info,
    }


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("config")
    args = parser.parse_args(argv)
    try:
        config = json.loads(Path(args.config).read_text(encoding="utf-8"))

        def _cli_progress(message, pct):
            print(f"[PROGRESS {pct}%] {message}", flush=True)

        res = run(config, progress_callback=_cli_progress)
        print(json.dumps(res, indent=2), flush=True)
        return 0
    except Exception as exc:
        print(
            json.dumps({"error": str(exc), "traceback": traceback.format_exc()}),
            file=sys.stderr,
            flush=True,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
