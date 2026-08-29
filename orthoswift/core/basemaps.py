"""Offline orthomosaic basemap export for field-controller review.

The MBTiles artifact is a visual companion to prescription geometry. It is not
part of any vendor prescription schema and must be imported separately where
the controller application supports custom MBTiles layers.
"""
from __future__ import annotations

import hashlib
import json
import math
import shutil
import sqlite3
import tempfile
from pathlib import Path
from typing import Optional, Sequence

import numpy as np
import rasterio
from rasterio.enums import ColorInterp, Resampling
from rasterio.shutil import copy as raster_copy
from rasterio.vrt import WarpedVRT
from rasterio.warp import calculate_default_transform, transform_bounds

_WEB_MERCATOR_INITIAL_RESOLUTION = 156543.03392804097


def _rgb_band_indexes(src: rasterio.io.DatasetReader) -> tuple[int, int, int]:
    descriptions = [str(x or "").strip().lower().replace("_", " ") for x in src.descriptions]
    aliases = {"red": {"red", "r"}, "green": {"green", "g"}, "blue": {"blue", "b"}}
    found = {}
    for name, names in aliases.items():
        for idx, description in enumerate(descriptions, start=1):
            if description in names:
                found[name] = idx
                break
    if len(found) == 3:
        return found["red"], found["green"], found["blue"]

    by_color = {str(ci).split(".")[-1].lower(): idx for idx, ci in enumerate(src.colorinterp, start=1)}
    if all(name in by_color for name in ("red", "green", "blue")):
        return by_color["red"], by_color["green"], by_color["blue"]
    if src.count >= 3:
        return 1, 2, 3
    raise ValueError("Orthomosaic needs identifiable red, green, and blue bands")


def _resampling(name: str) -> Resampling:
    key = str(name).strip().lower()
    allowed = {"nearest": Resampling.nearest, "bilinear": Resampling.bilinear,
               "cubic": Resampling.cubic, "lanczos": Resampling.lanczos}
    if key not in allowed:
        raise ValueError(f"Unsupported basemap resampling {name!r}; expected one of {sorted(allowed)}")
    return allowed[key]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_name(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in str(value).strip())
    return cleaned.strip("_") or "OrthoSWIFT_offline_orthomosaic"


def _source_cuts(src, indexes: Sequence[int], percentiles: tuple[float, float], sample_pixels: int,
                 transparent_zero_rgb: bool) -> list[tuple[float, float]]:
    lo_pct, hi_pct = percentiles
    if not (0 <= lo_pct < hi_pct <= 100):
        raise ValueError("stretch_percentiles must satisfy 0 <= low < high <= 100")
    scale = min(1.0, math.sqrt(max(1, int(sample_pixels)) / max(1, src.width * src.height)))
    out_h, out_w = max(1, int(src.height * scale)), max(1, int(src.width * scale))
    sample = src.read(indexes, out_shape=(len(indexes), out_h, out_w), masked=True,
                      resampling=Resampling.nearest).astype("float32")
    raw = np.asarray(sample.filled(np.nan), dtype="float32")
    joint_valid = ~np.any(np.ma.getmaskarray(sample), axis=0) & np.all(np.isfinite(raw), axis=0)
    if transparent_zero_rgb:
        joint_valid &= np.any(raw != 0, axis=0)
    cuts = []
    for index in range(raw.shape[0]):
        values = raw[index][joint_valid]
        if values.size == 0:
            raise ValueError("Orthomosaic RGB bands contain no jointly valid pixels")
        lo, hi = np.percentile(values, [lo_pct, hi_pct])
        if not np.isfinite(lo) or not np.isfinite(hi) or hi <= lo:
            lo, hi = float(np.nanmin(values)), float(np.nanmax(values))
        if hi <= lo:
            hi = lo + 1.0
        cuts.append((float(lo), float(hi)))
    return cuts


def validate_offline_basemap_mbtiles(path: str | Path) -> dict:
    """Validate MBTiles schema, metadata, zoom coverage, raster CRS, and tiles."""
    path = Path(path)
    errors: list[str] = []
    result = {"path": str(path), "valid": False, "errors": errors}
    if not path.exists() or not path.is_file():
        errors.append("MBTiles file does not exist")
        return result
    try:
        with sqlite3.connect(path) as connection:
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not {"metadata", "tiles"}.issubset(tables):
                errors.append("MBTiles must contain metadata and tiles tables")
                return result
            metadata = dict(connection.execute("SELECT name, value FROM metadata"))
            tile_count = int(connection.execute("SELECT COUNT(*) FROM tiles").fetchone()[0])
            zoom_rows = connection.execute("SELECT zoom_level, COUNT(*) FROM tiles GROUP BY zoom_level ORDER BY zoom_level").fetchall()
        result.update({"metadata": metadata, "tile_count": tile_count,
                       "tiles_by_zoom": {int(z): int(n) for z, n in zoom_rows},
                       "size_bytes": int(path.stat().st_size), "sha256": _sha256(path)})
        if tile_count <= 0:
            errors.append("MBTiles contains no tiles")
        if metadata.get("format") not in {"png", "jpg", "jpeg", "webp"}:
            errors.append("MBTiles metadata has unsupported or missing tile format")
        try:
            min_zoom, max_zoom = int(metadata["minzoom"]), int(metadata["maxzoom"])
            if min_zoom > max_zoom:
                errors.append("MBTiles minzoom exceeds maxzoom")
            if zoom_rows and (min_zoom != int(zoom_rows[0][0]) or max_zoom != int(zoom_rows[-1][0])):
                errors.append("MBTiles minzoom/maxzoom metadata does not match stored tile zoom levels")
        except Exception:
            errors.append("MBTiles minzoom/maxzoom metadata is invalid")
        try:
            bounds = [float(x) for x in metadata["bounds"].split(",")]
            if len(bounds) != 4 or not (-180 <= bounds[0] < bounds[2] <= 180 and -85.0512 <= bounds[1] < bounds[3] <= 85.0512):
                errors.append("MBTiles WGS84 bounds metadata is invalid")
            result["bounds_wgs84"] = bounds
        except Exception:
            errors.append("MBTiles bounds metadata is invalid")
        with rasterio.open(path) as src:
            result.update({"crs": None if src.crs is None else str(src.crs),
                           "width": int(src.width), "height": int(src.height), "band_count": int(src.count)})
            if src.crs is None or src.crs.to_epsg() != 3857:
                errors.append("MBTiles raster CRS must be EPSG:3857")
            if src.count not in {3, 4}:
                errors.append("MBTiles visual raster should expose RGB or RGBA bands")
    except Exception as exc:
        errors.append(f"Cannot validate MBTiles: {exc}")
    result["valid"] = len(errors) == 0
    return result


def export_orthomosaic_mbtiles(
    orthomosaic_path: str | Path,
    out_mbtiles: str | Path,
    *,
    min_zoom: Optional[int] = None,
    max_zoom: Optional[int] = None,
    max_auto_zoom: int = 24,
    tile_format: str = "PNG",
    quality: int = 85,
    resampling: str = "bilinear",
    stretch_percentiles: tuple[float, float] = (2.0, 98.0),
    sample_pixels: int = 1_000_000,
    layer_name: str = "OrthoSWIFT current orthomosaic",
    transparent_zero_rgb: bool = True,
    max_source_pixels: Optional[int] = 500_000_000,
    max_output_bytes: Optional[int] = 2_000_000_000,
) -> dict:
    """Create a controller-friendly EPSG:3857 MBTiles visual basemap.

    Conversion is block-streamed through a temporary byte RGBA GeoTIFF, then a
    WarpedVRT, so peak memory does not scale with the full orthomosaic size.
    """
    source_path, out_path = Path(orthomosaic_path), Path(out_mbtiles)
    if not source_path.exists():
        raise ValueError(f"orthomosaic_path does not exist: {source_path}")
    if not (0 <= int(max_auto_zoom) <= 24):
        raise ValueError("max_auto_zoom must be in [0, 24]")
    fmt = str(tile_format).upper()
    if fmt not in {"PNG", "JPEG", "WEBP"}:
        raise ValueError("tile_format must be PNG, JPEG, or WEBP")
    if not (1 <= int(quality) <= 100):
        raise ValueError("quality must be in [1, 100]")
    resample = _resampling(resampling)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    with rasterio.open(source_path) as src:
        if src.crs is None:
            raise ValueError("Orthomosaic must have a CRS")
        if src.width <= 0 or src.height <= 0:
            raise ValueError("Orthomosaic has invalid dimensions")
        source_pixels = int(src.width) * int(src.height)
        if max_source_pixels is not None and source_pixels > int(max_source_pixels):
            raise ValueError(
                f"Orthomosaic has {source_pixels:,} pixels; configured basemap limit is "
                f"{int(max_source_pixels):,}. Use a smaller AOI or raise max_source_pixels explicitly."
            )
        rgb_indexes = _rgb_band_indexes(src)
        source_crs = str(src.crs)
        source_bounds_wgs84 = transform_bounds(src.crs, "EPSG:4326", *src.bounds, densify_pts=21)
        natural_transform, _, _ = calculate_default_transform(src.crs, "EPSG:3857", src.width, src.height, *src.bounds)
        natural_resolution = max(abs(natural_transform.a), abs(natural_transform.e))
        auto_zoom = int(round(math.log2(_WEB_MERCATOR_INITIAL_RESOLUTION / natural_resolution)))
        resolved_max_zoom = min(int(max_auto_zoom), max(0, auto_zoom)) if max_zoom is None else int(max_zoom)
        resolved_min_zoom = max(0, resolved_max_zoom - 5) if min_zoom is None else int(min_zoom)
        if not (0 <= resolved_min_zoom <= resolved_max_zoom <= 24):
            raise ValueError("Zooms must satisfy 0 <= min_zoom <= max_zoom <= 24")
        target_resolution = _WEB_MERCATOR_INITIAL_RESOLUTION / (2 ** resolved_max_zoom)
        target_transform, target_width, target_height = calculate_default_transform(
            src.crs, "EPSG:3857", src.width, src.height, *src.bounds, resolution=target_resolution)
        cuts = _source_cuts(src, rgb_indexes, stretch_percentiles, sample_pixels, transparent_zero_rgb)
        source_dtypes = [src.dtypes[index - 1] for index in rgb_indexes]

        with tempfile.TemporaryDirectory() as temp_dir:
            rgba_path = Path(temp_dir) / "orthomosaic_rgba.tif"
            output_band_count = 3 if fmt == "JPEG" else 4
            profile = src.profile.copy()
            profile.update(driver="GTiff", count=output_band_count, dtype="uint8", nodata=None, tiled=True,
                           blockxsize=256, blockysize=256, compress="deflate",
                           photometric="RGB", BIGTIFF="IF_SAFER")
            with rasterio.open(rgba_path, "w", **profile) as dst:
                dst.colorinterp = (
                    (ColorInterp.red, ColorInterp.green, ColorInterp.blue)
                    if output_band_count == 3 else
                    (ColorInterp.red, ColorInterp.green, ColorInterp.blue, ColorInterp.alpha)
                )
                for _, window in src.block_windows(rgb_indexes[0]):
                    data = src.read(rgb_indexes, window=window).astype("float32")
                    valid = (src.dataset_mask(window=window) > 0) & np.all(np.isfinite(data), axis=0)
                    if transparent_zero_rgb:
                        valid &= np.any(data != 0, axis=0)
                    rgba = np.empty((4, int(window.height), int(window.width)), dtype="uint8")
                    for index, (lo, hi) in enumerate(cuts):
                        rgba[index] = np.clip((data[index] - lo) * 255.0 / (hi - lo), 0, 255).astype("uint8")
                    rgba[3] = np.where(valid, 255, 0).astype("uint8")
                    rgba[:3, ~valid] = 0
                    dst.write(rgba[:output_band_count], window=window)

            if out_path.exists():
                out_path.unlink()
            with rasterio.open(rgba_path) as rgba_src:
                with WarpedVRT(rgba_src, crs="EPSG:3857", transform=target_transform,
                               width=target_width, height=target_height,
                               resampling=resample, add_alpha=False) as warped:
                    options = {"TILE_FORMAT": fmt, "ZOOM_LEVEL_STRATEGY": "AUTO",
                               "RESAMPLING": str(resampling).upper()}
                    if fmt in {"JPEG", "WEBP"}:
                        options["QUALITY"] = str(int(quality))
                    raster_copy(warped, out_path, driver="MBTiles", **options)

            overview_factors = [2 ** level for level in range(1, resolved_max_zoom - resolved_min_zoom + 1)]
            if overview_factors:
                with rasterio.open(out_path, "r+") as mbtiles:
                    mbtiles.build_overviews(overview_factors, Resampling.average)

    with sqlite3.connect(out_path) as connection:
        stored_zoom = connection.execute(
            "SELECT MIN(zoom_level), MAX(zoom_level) FROM tiles"
        ).fetchone()
        if stored_zoom is None or stored_zoom[0] is None or stored_zoom[1] is None:
            raise RuntimeError("Generated MBTiles contains no stored zoom levels")
        effective_min_zoom, effective_max_zoom = int(stored_zoom[0]), int(stored_zoom[1])
        metadata_updates = {
            "name": _safe_name(layer_name),
            "description": str(layer_name),
            "type": "overlay",
            "bounds": ",".join(f"{value:.10f}" for value in source_bounds_wgs84),
            "minzoom": str(effective_min_zoom),
            "maxzoom": str(effective_max_zoom),
            "orthoswift_role": "offline_visual_basemap",
        }
        for key, value in metadata_updates.items():
            connection.execute("INSERT OR REPLACE INTO metadata(name, value) VALUES (?, ?)", (key, value))
        connection.commit()

    if max_output_bytes is not None and out_path.stat().st_size > int(max_output_bytes):
        size = out_path.stat().st_size
        out_path.unlink(missing_ok=True)
        raise ValueError(
            f"Generated MBTiles would be {size:,} bytes, exceeding max_output_bytes={int(max_output_bytes):,}. "
            "Lower max_zoom, crop the AOI, or use JPEG/WEBP tiles."
        )
    validation = validate_offline_basemap_mbtiles(out_path)
    if not validation["valid"]:
        raise RuntimeError("Generated MBTiles failed validation: " + "; ".join(validation["errors"]))
    manifest = {
        "product": "OrthoSWIFT offline orthomosaic basemap",
        "role": "visual_context_only",
        "source_path": str(source_path),
        "source_crs": source_crs,
        "source_rgb_bands": list(rgb_indexes),
        "source_dtypes": source_dtypes,
        "stretch_percentiles": list(stretch_percentiles),
        "transparent_zero_rgb": bool(transparent_zero_rgb and fmt != "JPEG"),
        "jpeg_transparency_note": "JPEG tiles do not support alpha; outside-footprint pixels render black." if fmt == "JPEG" else None,
        "tile_format": fmt,
        "resampling": str(resampling).lower(),
        "requested_min_zoom": resolved_min_zoom,
        "requested_max_zoom": resolved_max_zoom,
        "min_zoom": effective_min_zoom,
        "max_zoom": effective_max_zoom,
        "bounds_wgs84": list(source_bounds_wgs84),
        "mbtiles": validation,
        "compatibility": {
            "dji_pilot_2_enterprise": "documented_manual_custom_layer_import",
            "dji_agras_app": "not_verified_in_public_t40_t50_manuals",
        },
        "operator_note": "Import orthomosaic.mbtiles separately as a custom/offline map layer where supported. Do not treat presence inside a prescription ZIP as automatic controller ingestion.",
        "gdal_cli_equivalent": [
            "gdalwarp -t_srs EPSG:3857 -r bilinear -dstalpha input_orthomosaic.tif warped_rgba.tif",
            "gdal_translate -of MBTILES -co TILE_FORMAT=PNG -co ZOOM_LEVEL_STRATEGY=AUTO warped_rgba.tif orthomosaic.mbtiles",
            "gdaladdo -r average orthomosaic.mbtiles 2 4 8 16 32",
        ],
    }
    manifest_path = out_path.with_suffix(".manifest.json")
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    readme_path = out_path.with_name(out_path.stem + "_README.txt")
    readme_path.write_text(
        "OrthoSWIFT offline orthomosaic basemap\n\n"
        "This MBTiles file provides current visual field context beneath prescription vectors.\n"
        "DJI Pilot 2 documented workflow: Profile > Map Settings > MapTiler > Custom Layer.\n"
        "Import this file separately; controller support varies by model, app, and firmware.\n"
        "DJI Agras App support is not established by the public T40/T50 manuals reviewed.\n"
        "The prescription geometry remains authoritative. Verify alignment before flight/application.\n",
        encoding="utf-8",
    )
    return {"mbtiles_path": str(out_path), "manifest_path": str(manifest_path),
            "readme_path": str(readme_path), "validation": validation}


def copy_basemap_companion_files(
    basemap_path: str | Path,
    destination: str | Path,
    *,
    max_archive_member_bytes: int = 250_000_000,
) -> list[Path]:
    """Copy a prebuilt MBTiles file and sidecars into a package staging dir.

    The size guard stays below the package validator's 256 MiB per-member
    extraction bound. Larger basemaps remain valid as standalone companions but
    must not be embedded in prescription archives.
    """
    source, destination = Path(basemap_path), Path(destination)
    validation = validate_offline_basemap_mbtiles(source)
    if not validation["valid"]:
        raise ValueError("Invalid basemap_mbtiles_path: " + "; ".join(validation["errors"]))
    if source.stat().st_size > int(max_archive_member_bytes):
        raise ValueError(
            f"Basemap is {source.stat().st_size:,} bytes and exceeds the "
            f"{int(max_archive_member_bytes):,}-byte prescription-archive member limit. "
            "Ship it as the standalone offline_basemap/orthomosaic.mbtiles artifact instead."
        )
    destination.mkdir(parents=True, exist_ok=True)
    copied = []
    # Only copy the MBTiles file itself — no README, no sidecar manifest inside controller ZIPs
    targets = [(source, destination / "orthomosaic.mbtiles")]
    for src, dst in targets:
        shutil.copy2(src, dst)
        copied.append(dst)
    return copied
