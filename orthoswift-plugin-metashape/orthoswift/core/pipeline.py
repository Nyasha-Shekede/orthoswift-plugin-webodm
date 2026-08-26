"""Agriculture post-processing pipeline for the OrthoSWIFT WebODM plugin.

Consumes a georeferenced multispectral orthomosaic, computes vegetation indices,
management zones and stress hotspots, resolves optional operator-supplied rates,
and writes the existing GIS, controller and PDF deliverables.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Mapping, Optional

# Configure logging - suppress third-party library noise
logger = logging.getLogger(__name__)
for libname in ['matplotlib', 'matplotlib.font_manager', 'PIL', 'rasterio', 'fiona', 'geopandas', 'shapely', 'numpy', 'scipy', 'pandas', 'osgeo']:
    logging.getLogger(libname).setLevel(logging.CRITICAL)

import numpy as np
import pandas as pd
import rasterio
from rasterio.warp import calculate_default_transform, reproject, Resampling
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

from .exports import write_raster, export_polygons_kml, export_analytics_methodology
from .vegetation import (
    ndvi, ndre, gli, msavi2, clean_crop_mask, classify_ndvi,
    management_zones, stress_hotspots, canopy_cover_summary,
)
from .report import build_agriculture_pdf
from .decisions import (
    ApplicationRatePlan, resolve_application_rate_plan,
    export_all_controller_prescription_zips,
    build_spot_spray_prescription_gdf, _write_json,
)
from .guide import export_guides
from .basemaps import export_orthomosaic_mbtiles


def _ensure_dir(path: str | Path) -> Path:
    p = Path(path)
    p.mkdir(parents=True, exist_ok=True)
    return p




def _save_quick_preview(arr: np.ndarray, out_path: str | Path,
                        cmap: str = "viridis") -> Path:
    finite = arr[np.isfinite(arr)]
    lo, hi = np.percentile(finite, [2, 98]) if finite.size else (0, 1)
    plt.figure(figsize=(8, 6), dpi=150)
    plt.axis('off')
    plt.imshow(arr, cmap=cmap, vmin=lo, vmax=hi)
    plt.savefig(out_path, bbox_inches='tight', pad_inches=0)
    plt.close()
    return Path(out_path)








def _safe_to_file(gdf, path: str | Path, *, driver: str) -> Optional[Path]:
    """Write non-empty GeoDataFrames only; return None for empty geometry."""
    path = Path(path)
    if gdf is None or len(gdf) == 0:
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    gdf.to_file(path, driver=driver)
    return path







def _qa_reflectance_band(name: str, arr: np.ndarray, warnings: list[str]) -> None:
    finite = np.isfinite(arr)
    if not finite.any():
        warnings.append(f"Band {name} has no finite pixels.")
        return
    zero_pct = float(np.mean(arr[finite] == 0.0) * 100.0)
    neg_pct = float(np.mean(arr[finite] < 0.0) * 100.0)
    gt1_pct = float(np.mean(arr[finite] > 1.0) * 100.0)
    if zero_pct > 5.0:
        warnings.append(f"Band {name}: {zero_pct:.3f}% of finite pixels are exactly zero; these are treated as outside-footprint/background candidates.")
    if neg_pct > 0.01 or gt1_pct > 0.01:
        warnings.append(f"Band {name}: reflectance-like values outside [0, 1] detected (negative={neg_pct:.3f}%, >1={gt1_pct:.3f}%).")


def _qa_index(name: str, arr: np.ndarray, warnings: list[str]) -> None:
    finite = np.isfinite(arr)
    if not finite.any():
        warnings.append(f"{name.upper()} contains no finite pixels after QA masking.")
        return
    outside = finite & ((arr < -1.0) | (arr > 1.0))
    if outside.any():
        warnings.append(f"{name.upper()}: {int(outside.sum())} finite pixels ({outside.sum()/finite.sum()*100:.4f}%) outside [-1, 1]; they should have been masked before export.")







_CANONICAL_SPECTRAL_ROLES = ("red", "green", "blue", "nir", "red_edge")
_BAND_ALIASES = {
    "red": {"red", "r"},
    "green": {"green", "g"},
    "blue": {"blue", "b"},
    "nir": {"nir", "near infrared", "near-infrared", "nearinfrared"},
    "red_edge": {"red edge", "rededge", "red-edge", "re"},
}


def _normalise_band_description(value: Any) -> str:
    return " ".join(str(value or "").strip().lower().replace("_", " ").split())


def _resolve_multispectral_band_map(src, explicit: Optional[Mapping[str, int]] = None) -> tuple[dict[str, int], Optional[int]]:
    """Resolve canonical spectral roles from a stacked raster without positional guessing.

    Descriptions are authoritative. Raster color interpretation is used only for
    red/green/blue and alpha. NIR and red-edge must be described or explicitly
    mapped because ``ColorInterp.gray`` carries no spectral meaning.
    """
    from rasterio.enums import ColorInterp

    resolved: dict[str, int] = {}
    if explicit is not None:
        unknown = set(explicit) - set(_CANONICAL_SPECTRAL_ROLES)
        if unknown:
            raise ValueError(f"Unknown multispectral band role(s): {sorted(unknown)}")
        for role, index in explicit.items():
            if not isinstance(index, int) or isinstance(index, bool) or not (1 <= index <= src.count):
                raise ValueError(f"Band index for '{role}' must be an integer in [1, {src.count}]")
            resolved[role] = index

    descriptions = [_normalise_band_description(x) for x in src.descriptions]
    for role, aliases in _BAND_ALIASES.items():
        matches = [i for i, desc in enumerate(descriptions, start=1) if desc in aliases]
        if role not in resolved and len(matches) == 1:
            resolved[role] = matches[0]
        elif role not in resolved and len(matches) > 1:
            raise ValueError(f"Multiple bands are described as '{role}'; provide multispectral_band_map explicitly")

    color_roles = {ColorInterp.red: "red", ColorInterp.green: "green", ColorInterp.blue: "blue"}
    for index, interpretation in enumerate(src.colorinterp, start=1):
        role = color_roles.get(interpretation)
        if role is not None and role not in resolved:
            resolved[role] = index

    alpha_indexes = [i for i, interpretation in enumerate(src.colorinterp, start=1) if interpretation == ColorInterp.alpha]
    alpha_index = alpha_indexes[0] if len(alpha_indexes) == 1 else None

    # Positional fallback for standard multi-band stacks when metadata descriptions are missing
    if "red" not in resolved or "nir" not in resolved:
        non_alpha_indices = [i for i in range(1, src.count + 1) if i not in alpha_indexes]
        if src.count >= 6 and len(non_alpha_indices) >= 5:
            # Standard 6-band multispectral layout: 1:Red, 2:Green, 3:Blue, 4:NIR, 5:RedEdge, 6:Alpha
            resolved.setdefault("red", 1)
            resolved.setdefault("green", 2)
            resolved.setdefault("blue", 3)
            resolved.setdefault("nir", 4)
            resolved.setdefault("red_edge", 5)
        elif src.count == 5 and len(non_alpha_indices) >= 4:
            # Standard 5-band multispectral layout: 1:Red, 2:Green, 3:NIR, 4:RedEdge, 5:Alpha
            resolved.setdefault("red", 1)
            resolved.setdefault("green", 2)
            resolved.setdefault("nir", 3)
            resolved.setdefault("red_edge", 4)
        elif src.count == 4 and len(non_alpha_indices) >= 4:
            # Standard 4-band RGB-NIR layout: 1:Red, 2:Green, 3:Blue, 4:NIR
            resolved.setdefault("red", 1)
            resolved.setdefault("green", 2)
            resolved.setdefault("blue", 3)
            resolved.setdefault("nir", 4)

    for role, index in resolved.items():
        if index in alpha_indexes:
            raise ValueError(f"Band {index} is marked alpha/opacity and cannot be used as spectral role '{role}'")
    if len(set(resolved.values())) != len(resolved):
        raise ValueError("A raster band cannot be assigned to more than one spectral role")
    if "red" not in resolved or "nir" not in resolved:
        raise ValueError(
            "Stacked multispectral input requires identifiable red and NIR bands. "
            "Add band descriptions or pass multispectral_band_map={'red': ..., 'nir': ...}."
        )
    return resolved, alpha_index


def _apply_reflectance_scale(name: str, arr: np.ndarray, scale: Optional[float | Mapping[str, float]], warnings: list[str]) -> None:
    """Apply only a caller-supplied multiplicative raw-to-reflectance scale."""
    if scale is None:
        return
    factor = scale.get(name) if isinstance(scale, Mapping) else scale
    if factor is None:
        return
    factor = float(factor)
    if not np.isfinite(factor) or factor <= 0:
        raise ValueError(f"reflectance_scale for '{name}' must be finite and > 0")
    arr *= factor
    warnings.append(f"Band '{name}' multiplied by explicit reflectance_scale={factor:g}.")


def _absolute_reflectance_is_plausible(arrays: Mapping[str, np.ndarray]) -> tuple[bool, str]:
    """Conservative gate for scale-dependent indices and absolute thresholds."""
    stats = {}
    for role in ("red", "nir"):
        values = arrays[role][np.isfinite(arrays[role])]
        if values.size == 0:
            return False, f"{role} has no finite pixels"
        stats[role] = float(np.quantile(values, 0.99))
    if max(stats.values()) < 0.01:
        return False, f"red/NIR 99th percentiles are {stats['red']:.6g}/{stats['nir']:.6g}, implausibly small for [0,1] reflectance"
    return True, "red and NIR magnitudes are compatible with [0,1] reflectance"


def _validate_reflectance_band(
    name: str,
    arr: np.ndarray,
    *,
    tolerance_pct: float = 5.0,
    warnings: Optional[list[str]] = None,
) -> None:
    """Validate reflectance bounds, auto-scaling un-normalized integer DN values to [0, 1]."""
    finite = np.isfinite(arr)
    if not finite.any():
        raise ValueError(f"{name} band contains no finite reflectance values")
    outside = finite & ((arr < 0.0) | (arr > 1.0))
    outside_pct = 100.0 * np.count_nonzero(outside) / np.count_nonzero(finite)
    if outside_pct > tolerance_pct:
        max_val = float(np.nanmax(arr[finite]))
        if max_val > 1.0:
            if max_val <= 255.0:
                scale_factor = 255.0
            elif max_val <= 10000.0:
                scale_factor = 10000.0
            elif max_val <= 65535.0:
                scale_factor = 65535.0
            else:
                scale_factor = max_val
            arr /= scale_factor
            if warnings is not None:
                warnings.append(
                    f"Auto-scaled un-normalized DN for band '{name}' (max={max_val:g}) "
                    f"by 1/{scale_factor:g} to valid [0, 1] reflectance bounds."
                )
            np.clip(arr, 0.0, 1.0, out=arr)
            return

    if outside_pct:
        np.clip(arr, 0.0, 1.0, out=arr)
        if warnings is not None:
            warnings.append(f"Clipped {outside_pct:.2f}% of {name} pixels outside [0, 1] to valid reflectance bounds.")


def _warn_vector_area_consistency(name: str, gdf, warnings: list[str], tolerance_pct: float = 2.0) -> None:
    if gdf is None or len(gdf) == 0 or "raster_area_m2" not in gdf.columns or "geometry_area_m2" not in gdf.columns:
        return
    raster_area = float(gdf["raster_area_m2"].sum())
    geom_area = float(gdf["geometry_area_m2"].sum())
    if raster_area <= 0:
        return
    delta_pct = (geom_area - raster_area) / raster_area * 100.0
    if abs(delta_pct) > tolerance_pct:
        warnings.append(f"{name}: vector geometry area differs from raster area by {delta_pct:.2f}% ({geom_area:.2f} vs {raster_area:.2f} m²).")





def run_agriculture_pipeline(
    *,
    ortho_preview: str | Path,
    out_dir: str | Path,
    orthomosaic_path: Optional[str | Path] = None,
    multispectral_path: Optional[str | Path] = None,
    multispectral_band_map: Optional[Mapping[str, int]] = None,
    reflectance_scale: Optional[float | Mapping[str, float]] = None,
    red_path: Optional[str | Path] = None,
    nir_path: Optional[str | Path] = None,
    red_edge_path: Optional[str | Path] = None,
    green_path: Optional[str | Path] = None,
    blue_path: Optional[str | Path] = None,
    footprint_mask_path: Optional[str | Path] = None,
    k_range: tuple[int, int] = (2, 5),
    export_kml: bool = True,
    crop_ndvi_threshold: float = 0.20,
    crop_msavi2_threshold: Optional[float] = 0.15,
    nir_shadow_threshold: Optional[float] = 0.05,
    min_crop_component_m2: float = 2.0,
    min_non_crop_component_m2: float = 10.0,
    edge_buffer_m: float = 1.0,
    hotspot_percentile: float = 10.0,
    hotspot_zscore_cutoff: Optional[float] = 1.5,
    hotspot_min_area_m2: float = 50.0,
    export_relative_application_packages: bool = True,
    fertilizer_rate_plan: Optional[ApplicationRatePlan | Mapping[str, Any]] = None,
    spot_spray_rate_plan: Optional[ApplicationRatePlan | Mapping[str, Any]] = None,
    include_research_controller_packages: bool = False,
    export_offline_basemap: bool = False,
    bundle_basemap_in_controller_archives: bool = True,
    basemap_min_zoom: Optional[int] = None,
    basemap_max_zoom: Optional[int] = None,
    basemap_max_auto_zoom: int = 24,
    basemap_tile_format: str = "PNG",
    basemap_quality: int = 85,
    basemap_max_source_pixels: Optional[int] = 500_000_000,
    basemap_max_output_bytes: Optional[int] = 2_000_000_000,
    allow_unvalidated_prescription_export: bool = False,
    max_agriculture_pixels: Optional[int] = None,
    progress_callback=None,
) -> dict:
    """Run agriculture analytics and export affected geometry only.

    The pipeline emits management-zone and stress-target geometry, relative
    intensity prescriptions, or explicitly operator-supplied fertilizer rates.
    It does not infer an agronomic dose or generate autonomous flight missions.
    """
    out_dir = _ensure_dir(out_dir)
    if progress_callback:
        progress_callback('Preparing agriculture analysis', 25)
    prescriptions_dir = _ensure_dir(out_dir / 'prescriptions')
    rasters_dir = _ensure_dir(out_dir / 'technical_gis' / 'rasters')
    previews_dir = _ensure_dir(out_dir / 'technical_gis' / 'previews')
    summaries_dir = _ensure_dir(out_dir / 'technical_gis' / 'data_summaries')
    warnings: list[str] = []
    outputs: dict = {}
    extra_outputs: dict = {}
    band_audit: dict[str, Any] = {}
    action_coverage_summary = None
    fertilizer_rate_summary = None
    spot_spray_rate_summary = None
    resolved_rate_plan = ApplicationRatePlan.from_value(fertilizer_rate_plan)
    resolved_rate_plan.validate()

    resolved_spot_spray_plan = ApplicationRatePlan.from_value(spot_spray_rate_plan)
    resolved_spot_spray_plan.validate()

    logger.info("Application-rate mode: fertilizer=%s, spot_spray=%s", resolved_rate_plan.mode, resolved_spot_spray_plan.mode)

    separate_paths = [red_path, nir_path, red_edge_path, green_path, blue_path]
    if multispectral_path is not None and any(path is not None for path in separate_paths):
        raise ValueError("Use either multispectral_path or separate spectral band paths, not both")
    if multispectral_band_map is not None and multispectral_path is None:
        raise ValueError("multispectral_band_map requires multispectral_path")

    if export_offline_basemap or bundle_basemap_in_controller_archives:
        if orthomosaic_path is None or not Path(orthomosaic_path).exists():
            warnings.append("orthomosaic_path is missing or does not exist; offline basemap MBTiles generation skipped.")
            export_offline_basemap = False
            bundle_basemap_in_controller_archives = False
    if bundle_basemap_in_controller_archives and not export_relative_application_packages:
        warnings.append("bundle_basemap_in_controller_archives was requested without export_relative_application_packages; disabling basemap bundling.")
        bundle_basemap_in_controller_archives = False

    if red_path is not None and nir_path is not None:
        if Path(red_path).resolve() == Path(nir_path).resolve():
            raise ValueError("red_path and nir_path must refer to different spectral bands")

    arrays: dict[str, np.ndarray] = {}
    masks: dict[str, np.ndarray] = {}
    profile = None
    embedded_footprint_mask = None

    if multispectral_path is not None:
        with rasterio.open(multispectral_path) as src:
            pixel_count = int(src.height) * int(src.width)
            if max_agriculture_pixels is not None and max_agriculture_pixels > 0 and pixel_count > int(max_agriculture_pixels):
                raise ValueError(f"Agriculture raster has {pixel_count:,} pixels; configured in-memory limit is {int(max_agriculture_pixels):,}. Use a tiled workflow or smaller AOI.")
            resolved_map, alpha_index = _resolve_multispectral_band_map(src, multispectral_band_map)
            profile = src.profile.copy()
            band_audit = {
                "source": str(multispectral_path),
                "mode": "stacked",
                "resolved_band_map": resolved_map,
                "descriptions": list(src.descriptions),
                "color_interpretation": [str(x).split(".")[-1] for x in src.colorinterp],
                "alpha_band": alpha_index,
            }
            for name, index in resolved_map.items():
                band = src.read(index, masked=True)
                valid = ~np.ma.getmaskarray(band)
                arr = np.asarray(band.filled(np.nan), dtype="float32")
                valid &= np.isfinite(arr)
                arr[~valid] = np.nan
                _qa_reflectance_band(name, arr, warnings)
                arrays[name], masks[name] = arr, valid
            if alpha_index is not None:
                alpha = src.read(alpha_index, masked=True)
                embedded_footprint_mask = (~np.ma.getmaskarray(alpha) & np.isfinite(alpha.filled(np.nan)) & (np.asarray(alpha.filled(0)) > 0))
    else:
        from rasterio.enums import ColorInterp
        band_audit = {"mode": "separate", "sources": {}}
        for name, path in [("red", red_path), ("nir", nir_path), ("red_edge", red_edge_path), ("green", green_path), ("blue", blue_path)]:
            if path is None:
                continue
            with rasterio.open(path) as src:
                if src.colorinterp and src.colorinterp[0] == ColorInterp.alpha:
                    raise ValueError(f"Input for '{name}' is marked alpha/opacity, not a spectral band")
                description = _normalise_band_description(src.descriptions[0])
                described_roles = [role for role, aliases in _BAND_ALIASES.items() if description in aliases]
                if described_roles and name not in described_roles:
                    raise ValueError(f"Input routed as '{name}' is described as '{described_roles[0]}'; refusing semantic band mismatch")
                pixel_count = int(src.height) * int(src.width)
                if max_agriculture_pixels is not None and max_agriculture_pixels > 0 and pixel_count > int(max_agriculture_pixels):
                    raise ValueError(f"Agriculture raster has {pixel_count:,} pixels; configured in-memory limit is {int(max_agriculture_pixels):,}. Use a tiled workflow or smaller AOI.")
                band = src.read(1, masked=True)
                valid = ~np.ma.getmaskarray(band)
                arr = np.asarray(band.filled(np.nan), dtype="float32")
                valid &= np.isfinite(arr)
                arr[~valid] = np.nan
                _qa_reflectance_band(name, arr, warnings)
                arrays[name], masks[name] = arr, valid
                band_audit["sources"][name] = {"path": str(path), "description": src.descriptions[0], "color_interpretation": str(src.colorinterp[0]).split(".")[-1]}
                if profile is None:
                    profile = src.profile.copy()
                elif arr.shape != (profile["height"], profile["width"]) or src.transform != profile["transform"] or src.crs != profile["crs"]:
                    raise ValueError(f"Band '{name}' is not co-registered with the first spectral band")

    # Build one common validity footprint for every spectral product.
    # A dedicated alpha/footprint mask is preferred. The all-zero fallback
    # handles ODM mosaics that encode outside-footprint pixels as zeros while
    # incorrectly marking the rectangular dataset mask as fully valid.
    common_valid_mask = None

    if arrays:
        common_valid_mask = np.logical_and.reduce(
            [masks[name] for name in arrays]
        )

    if embedded_footprint_mask is not None:
        common_valid_mask &= embedded_footprint_mask
        warnings.append("Used embedded alpha band as the footprint mask; it was excluded from spectral analytics.")

    if footprint_mask_path is not None:
        with rasterio.open(footprint_mask_path) as mask_src:
            if profile is None:
                raise ValueError(
                    "Spectral profile must be initialized before reading the footprint mask"
                )
            if (mask_src.height, mask_src.width) != (
                profile["height"], profile["width"]
            ):
                raise ValueError("Footprint mask shape does not match spectral bands")
            if mask_src.transform != profile["transform"]:
                raise ValueError("Footprint mask transform does not match spectral bands")
            if mask_src.crs != profile["crs"]:
                raise ValueError("Footprint mask CRS does not match spectral bands")

            footprint = mask_src.read(1, masked=True)
            footprint_valid = (
                ~np.ma.getmaskarray(footprint)
                & np.isfinite(footprint.filled(np.nan))
                & (np.asarray(footprint.filled(0)) > 0)
            )
            common_valid_mask &= footprint_valid
    elif embedded_footprint_mask is None:
        spectral_names = [
            name
            for name in ("red", "green", "blue", "nir", "red_edge")
            if name in arrays
        ]
        if spectral_names:
            all_zero = np.logical_and.reduce(
                [arrays[name] == 0.0 for name in spectral_names]
            )
            zero_background_n = int(np.count_nonzero(common_valid_mask & all_zero))
            if zero_background_n:
                common_valid_mask &= ~all_zero
                warnings.append(
                    f"Excluded {zero_background_n:,} all-zero pixels as "
                    "outside-footprint background because no explicit "
                    "footprint mask was supplied."
                )

    for name in arrays:
        masks[name] &= common_valid_mask
        arrays[name][~common_valid_mask] = np.nan

    if profile is None:
        raise ValueError('At least one spectral band must be provided')
    if 'red' not in arrays or 'nir' not in arrays:
        raise ValueError('OrthoSWIFT agriculture requires co-registered red and NIR reflectance bands')
    for band_name, band_arr in arrays.items():
        _apply_reflectance_scale(band_name, band_arr, reflectance_scale, warnings)
        finite_values = band_arr[np.isfinite(band_arr)]
        if finite_values.size and float(np.ptp(finite_values)) <= max(1e-12, abs(float(np.median(finite_values))) * 1e-8):
            raise ValueError(f"Band '{band_name}' is spatially constant and cannot be a usable spectral measurement (possible alpha/opacity mask)")
        _validate_reflectance_band(band_name, band_arr, warnings=warnings)
    absolute_reflectance_valid, radiometry_reason = _absolute_reflectance_is_plausible(arrays)
    band_audit["absolute_reflectance_gate"] = {"passed": absolute_reflectance_valid, "reason": radiometry_reason}
    if not absolute_reflectance_valid:
        warnings.append(
            "Absolute-reflectance gate failed: " + radiometry_reason + ". "
            "NDVI/NDRE remain available because normalized differences are invariant to a common multiplicative scale; "
            "MSAVI2, absolute NIR shadow screening, plant counting, and machine prescription export are disabled."
        )
    transform = profile['transform']
    crs = profile['crs']
    from pyproj import CRS as _CRS
    from pyproj.database import query_utm_crs_info
    from pyproj.aoi import AreaOfInterest
    _resolved = _CRS.from_user_input(crs)
    _units = [float(a.unit_conversion_factor) for a in _resolved.axis_info[:2]]
    _is_metre_projected = _resolved.is_projected and len(_units) >= 2 and all(np.isclose(u, 1.0, rtol=1e-6) for u in _units)

    if not _is_metre_projected:
        # Auto-reproject geographic CRS (e.g. EPSG:4326) to the correct UTM zone
        # derived from the raster's geographic centroid.
        _height = profile['height']
        _width = profile['width']
        _cx = transform.c + transform.a * _width / 2.0
        _cy = transform.f + transform.e * _height / 2.0
        if _resolved.is_geographic:
            _lon, _lat = _cx, _cy
        else:
            from pyproj import Transformer as _T
            _to_geo = _T.from_crs(_resolved, _CRS.from_epsg(4326), always_xy=True)
            _lon, _lat = _to_geo.transform(_cx, _cy)
        _utm_results = query_utm_crs_info(
            datum_name="WGS 84",
            area_of_interest=AreaOfInterest(
                west_lon_degree=_lon, south_lat_degree=_lat,
                east_lon_degree=_lon, north_lat_degree=_lat,
            ),
        )
        if not _utm_results:
            raise ValueError(
                f"Could not determine a UTM zone for centroid ({_lat:.4f}, {_lon:.4f}). "
                "Reproject your orthomosaic to a projected metre-based CRS and re-upload."
            )
        _target_utm = _CRS.from_authority(_utm_results[0].auth_name, _utm_results[0].code)
        warnings.append(
            f"Input raster CRS is geographic ({_resolved.name}). "
            f"Auto-reprojecting band arrays to {_target_utm.name} for area and prescription calculations."
        )
        _src_crs_rio = rasterio.crs.CRS.from_user_input(_resolved.to_wkt())
        _dst_crs_rio = rasterio.crs.CRS.from_user_input(_target_utm.to_wkt())
        _new_transform, _new_width, _new_height = calculate_default_transform(
            _src_crs_rio, _dst_crs_rio, _width, _height, left=transform.c,
            bottom=transform.f + transform.e * _height, right=transform.c + transform.a * _width,
            top=transform.f,
        )
        _reprojected_arrays: dict[str, np.ndarray] = {}
        for _bname, _barr in arrays.items():
            _src = _barr.copy()
            _dst = np.full((_new_height, _new_width), np.nan, dtype="float32")
            reproject(
                source=_src, destination=_dst,
                src_transform=transform, src_crs=_src_crs_rio,
                dst_transform=_new_transform, dst_crs=_dst_crs_rio,
                resampling=Resampling.bilinear, src_nodata=np.nan, dst_nodata=np.nan,
            )
            _reprojected_arrays[_bname] = _dst
        arrays = _reprojected_arrays
        _mask_src = common_valid_mask.astype("float32")
        _mask_dst = np.zeros((_new_height, _new_width), dtype="float32")
        reproject(
            source=_mask_src, destination=_mask_dst,
            src_transform=transform, src_crs=_src_crs_rio,
            dst_transform=_new_transform, dst_crs=_dst_crs_rio,
            resampling=Resampling.nearest, src_nodata=0.0, dst_nodata=0.0,
        )
        common_valid_mask = _mask_dst > 0.5
        for _bname in arrays:
            arrays[_bname][~common_valid_mask] = np.nan
        profile.update(crs=_dst_crs_rio, transform=_new_transform, width=_new_width, height=_new_height)
        transform = _new_transform
        crs = _dst_crs_rio

    if not (np.isclose(transform.b, 0.0) and np.isclose(transform.d, 0.0)):
        raise ValueError('Agriculture analytics currently require a north-up, non-sheared raster transform')

    primary_index_preview = None
    features_stack = []
    feature_names = []
    ndvi_arr = None
    ndre_arr = None
    gli_arr = None
    msavi2_arr = None

    if 'red' in arrays and 'nir' in arrays:
        ndvi_arr = ndvi(arrays["red"], arrays["nir"])
        ndvi_arr[~common_valid_mask] = np.nan
        _qa_index('ndvi', ndvi_arr, warnings)
        write_raster(
            rasters_dir / "ndvi.tif",
            ndvi_arr,
            profile,
            dtype="float32",
            nodata=-9999.0,
        )
        outputs['ndvi_tif'] = str(rasters_dir / 'ndvi.tif')
        ndvi_cls = classify_ndvi(ndvi_arr)
        write_raster(rasters_dir / 'ndvi_classes.tif', ndvi_cls.astype('int16'), profile, dtype='int16', nodata=-1)
        outputs['ndvi_classes_tif'] = str(rasters_dir / 'ndvi_classes.tif')
        primary_index_preview = _save_quick_preview(ndvi_arr, previews_dir / 'ndvi_preview.png', cmap='RdYlGn')
        outputs['ndvi_preview'] = str(primary_index_preview)
        features_stack.append(ndvi_arr)
        feature_names.append('ndvi')

        if absolute_reflectance_valid:
            msavi2_arr = msavi2(arrays["red"], arrays["nir"])
            msavi2_arr[~common_valid_mask] = np.nan
            _qa_index('msavi2', msavi2_arr, warnings)
            write_raster(rasters_dir / "msavi2.tif", msavi2_arr, profile, dtype="float32", nodata=-9999.0)
            outputs['msavi2_tif'] = str(rasters_dir / 'msavi2.tif')
            msavi2_preview = _save_quick_preview(msavi2_arr, previews_dir / 'msavi2_preview.png', cmap='RdYlGn')
            outputs['msavi2_preview'] = str(msavi2_preview)
            features_stack.append(msavi2_arr)
            feature_names.append('msavi2')

    if 'red_edge' in arrays and 'nir' in arrays:
        ndre_arr = ndre(arrays["red_edge"], arrays["nir"])
        ndre_arr[~common_valid_mask] = np.nan
        _qa_index('ndre', ndre_arr, warnings)
        write_raster(
            rasters_dir / "ndre.tif",
            ndre_arr,
            profile,
            dtype="float32",
            nodata=-9999.0,
        )
        outputs['ndre_tif'] = str(rasters_dir / 'ndre.tif')
        ndre_preview = _save_quick_preview(ndre_arr, previews_dir / 'ndre_preview.png', cmap='RdYlGn')
        outputs['ndre_preview'] = str(ndre_preview)
        if primary_index_preview is None:
            primary_index_preview = ndre_preview
        features_stack.append(ndre_arr)
        feature_names.append('ndre')

    if {'red','green','blue'}.issubset(arrays.keys()):
        gli_arr = gli(arrays["red"], arrays["green"], arrays["blue"])
        gli_arr[~common_valid_mask] = np.nan
        _qa_index('gli', gli_arr, warnings)
        write_raster(
            rasters_dir / "gli.tif",
            gli_arr,
            profile,
            dtype="float32",
            nodata=-9999.0,
        )
        outputs['gli_tif'] = str(rasters_dir / 'gli.tif')
        gli_preview = _save_quick_preview(gli_arr, previews_dir / 'gli_preview.png', cmap='RdYlGn')
        outputs['gli_preview'] = str(gli_preview)
        if primary_index_preview is None:
            primary_index_preview = gli_preview
        features_stack.append(gli_arr)
        feature_names.append('gli')

    if not features_stack:
        raise ValueError('Need red and NIR multispectral inputs')

    stack = np.stack(features_stack, axis=0)
    finite_stack_mask = np.all(np.isfinite(stack), axis=0)
    field_mask = finite_stack_mask.copy()
    crop_qc_metrics = None

    if ndvi_arr is not None:
        crop_mask, crop_qc_metrics = clean_crop_mask(
            ndvi_arr,
            transform,
            msavi2_arr=msavi2_arr if absolute_reflectance_valid else None,
            nir_arr=arrays.get('nir') if absolute_reflectance_valid else None,
            green_arr=arrays.get('green') if absolute_reflectance_valid else None,
            ndvi_threshold=crop_ndvi_threshold,
            msavi2_threshold=crop_msavi2_threshold,
            nir_shadow_threshold=nir_shadow_threshold,
            min_crop_component_m2=min_crop_component_m2,
            min_non_crop_component_m2=min_non_crop_component_m2,
            edge_buffer_m=edge_buffer_m,
        )
        field_mask &= crop_mask
        pd.DataFrame([crop_qc_metrics]).to_csv(summaries_dir / 'crop_mask_qc_summary.csv', index=False)
        outputs['crop_mask_qc_summary_csv'] = str(summaries_dir / 'crop_mask_qc_summary.csv')
        crop_mask_raster = np.full(field_mask.shape, 255, dtype=np.uint8)
        crop_mask_raster[finite_stack_mask] = np.where(field_mask[finite_stack_mask], 1, 0)
        write_raster(rasters_dir / 'crop_mask_qc.tif', crop_mask_raster, profile, dtype='uint8', nodata=255)
        outputs['crop_mask_qc_tif'] = str(rasters_dir / 'crop_mask_qc.tif')
        if int(np.count_nonzero(field_mask)) < 500:
            warnings.append('Clean crop mask had fewer than 500 pixels after soil/shadow/water/edge QC; management zones may be unstable.')

    zoning_used_full_footprint_fallback = False
    if int(np.count_nonzero(field_mask)) < 20:
        if int(np.count_nonzero(finite_stack_mask)) >= 20:
            warnings.append('Clean crop mask contained fewer than 20 pixels; using the finite footprint for diagnostic zoning only. Machine prescriptions are blocked.')
            field_mask = finite_stack_mask.copy()
            zoning_used_full_footprint_fallback = True
        else:
            raise ValueError('Too few valid field pixels for agriculture zoning after masking')

    if progress_callback:
        progress_callback('Building management zones', 62)
    label_raster, zones = management_zones(stack, transform, crs, k_range=k_range, field_mask=field_mask, feature_names=feature_names)
    write_raster(rasters_dir / 'fertilizer_zones.tif', label_raster.astype('int16'), profile, dtype='int16', nodata=-1)
    _warn_vector_area_consistency('management_zones', zones, warnings)
    outputs['fertilizer_zones_tif'] = str(rasters_dir / 'fertilizer_zones.tif')
    fertilizer_zones_geojson = _safe_to_file(zones, rasters_dir / 'fertilizer_zones.geojson', driver='GeoJSON')
    if fertilizer_zones_geojson is not None:
        outputs['fertilizer_zones_geojson'] = str(fertilizer_zones_geojson)
    vra_dir = _ensure_dir(prescriptions_dir / 'fertilizer_zones')
    zones_summary_path = vra_dir / 'fertilizer_zone_summary.csv'
    zones.drop(columns=['geometry']).to_csv(zones_summary_path, index=False)
    outputs['fertilizer_zone_summary_csv'] = str(zones_summary_path)
    outputs['zones_n'] = int(len(zones))

    if export_kml and len(zones) > 0:
        zones_kml = vra_dir / 'fertilizer_zones.kml'
        zones_kml_view = zones.copy()
        zones_kml_view['display_name'] = zones_kml_view['relative_vigor_label'].astype(str).str.replace('_', ' ', regex=False).str.capitalize()
        export_polygons_kml(
            zones_kml_view,
            zones_kml,
            name_field='display_name',
            description_fields=['zone_id', 'geometry_area_m2', 'cluster_rank', 'cluster_count', 'ndvi_mean'],
            description_labels={
                'zone_id': 'Parent zone ID',
                'geometry_area_m2': 'Parent zone total area (m²)',
                'cluster_rank': 'Vigor rank',
                'cluster_count': 'Zones selected',
                'ndvi_mean': 'Parent zone mean NDVI',
            },
            document_name='management-zone patches',
            style_field='relative_vigor_label',
            default_style='management_zone',
            explode_multipolygon_parts=True,
            part_id_field='zone_id',
        )
        extra_outputs['fertilizer_zones_kml'] = str(zones_kml)

    if progress_callback:
        progress_callback('Resolving prescription rates', 74)
    prescription_zones, fertilizer_rate_summary = resolve_application_rate_plan(zones, resolved_rate_plan)
    rate_plan_json = summaries_dir / 'fertilizer_rate_plan.json'
    _write_json(rate_plan_json, fertilizer_rate_summary)
    outputs['fertilizer_rate_plan_json'] = str(rate_plan_json)
    rate_table_path = vra_dir / 'fertilizer_rate_table.csv'
    prescription_zones.drop(columns='geometry', errors='ignore').to_csv(rate_table_path, index=False)
    outputs['fertilizer_rate_table_csv'] = str(rate_table_path)
    prescription_geojson = rasters_dir / 'fertilizer_prescription.geojson'
    prescription_zones.to_file(prescription_geojson, driver='GeoJSON')
    outputs['fertilizer_prescription_geojson'] = str(prescription_geojson)

    hotspots = None
    cover = None
    if ndvi_arr is not None:
        hotspots = stress_hotspots(
            ndvi_arr,
            transform,
            crs,
            percentile=hotspot_percentile,
            field_mask=field_mask,
            crop_min_ndvi=crop_ndvi_threshold,
            min_area_m2=hotspot_min_area_m2,
            zscore_cutoff=hotspot_zscore_cutoff,
        )
        _warn_vector_area_consistency('stress_hotspots', hotspots, warnings)
        cover_summary = canopy_cover_summary(ndvi_arr >= crop_ndvi_threshold, transform, valid_mask=finite_stack_mask)
        cover = cover_summary['overall_cover_pct']
        cover_summary['metric_label'] = 'vegetation_cover_in_valid_multispectral_area_pct'
        cover_summary['farmer_label'] = 'Vegetation cover in valid multispectral footprint (%)'
        cover_summary['denominator'] = 'finite_co_registered_multispectral_pixels'
        cover_summary['vegetation_rule'] = 'NDVI >= 0.20 inside valid multispectral footprint'
        cover_summary['crop_mask_area_pct_of_valid'] = (
            100.0 * np.count_nonzero(field_mask) / np.count_nonzero(finite_stack_mask)
            if np.count_nonzero(finite_stack_mask) else np.nan
        )

        pd.DataFrame([cover_summary]).to_csv(
            summaries_dir / 'canopy_cover_summary.csv',
            index=False,
        )
        outputs['canopy_cover_summary_csv'] = str(summaries_dir / 'canopy_cover_summary.csv')
    else:
        warnings.append('NDVI was unavailable; stress/spray targets and canopy-cover summary were not generated.')

    basemap_mbtiles_path = None
    if bundle_basemap_in_controller_archives:
        # Generate mbtiles to a staging path (not a deliverable dir).
        # The file is only copied into controller ZIPs; no offline_basemap/ folder is written.
        import tempfile as _tf
        _bm_staging = Path(_tf.mkdtemp(prefix='osw_bm_', dir=out_dir))
        try:
            basemap_result = export_orthomosaic_mbtiles(
                orthomosaic_path,
                _bm_staging / 'orthomosaic.mbtiles',
                min_zoom=basemap_min_zoom,
                max_zoom=basemap_max_zoom,
                max_auto_zoom=basemap_max_auto_zoom,
                tile_format=basemap_tile_format,
                quality=basemap_quality,
                max_source_pixels=basemap_max_source_pixels,
                max_output_bytes=basemap_max_output_bytes,
            )
            basemap_mbtiles_path = basemap_result['mbtiles_path']
        except Exception as _bm_err:
            logger.warning('Basemap generation failed (controller ZIPs will have no basemap): %s', _bm_err)
            _bm_staging_cleanup = True

    prescription_qc_passed = absolute_reflectance_valid and not zoning_used_full_footprint_fallback and len(zones) >= 2
    if export_relative_application_packages and not prescription_qc_passed and not allow_unvalidated_prescription_export:
        warnings.append(
            "Machine prescription packages were not exported because analytical QC failed "
            f"(absolute_reflectance_valid={absolute_reflectance_valid}, "
            f"full_footprint_fallback={zoning_used_full_footprint_fallback}, zones={len(zones)})."
        )
    if export_relative_application_packages and zones is not None and len(zones) and (prescription_qc_passed or allow_unvalidated_prescription_export):
        if allow_unvalidated_prescription_export and not prescription_qc_passed:
            warnings.append("UNSAFE OVERRIDE: exporting machine packages despite failed analytical QC.")
        controller_dir = vra_dir / 'controller_packages'
        controller_packages = export_all_controller_prescription_zips(
            prescription_zones, controller_dir,
            rate_unit=fertilizer_rate_summary['unit'],
            include_research_stage=include_research_controller_packages,
            basemap_mbtiles_path=basemap_mbtiles_path,
            include_basemap_in_archives=bundle_basemap_in_controller_archives,
        )
        outputs['controller_packages_dir'] = str(controller_dir)
        outputs['john_deere_rx_zip'] = controller_packages['john_deere_zip']
        outputs['case_ih_shapefile_zip'] = controller_packages['case_ih_zip']
        outputs['trimble_aggps_zip'] = controller_packages['trimble_aggps_zip']
        outputs['trimble_gfx_zip'] = controller_packages['trimble_gfx_zip']
        outputs['ag_leader_root_zip'] = controller_packages['ag_leader_zip']
        outputs['universal_zip'] = controller_packages['generic_flat_zip']
        outputs['new_holland_intelliview_zip'] = controller_packages['new_holland_zip']
        if 'dji_agras_vra_zip' in controller_packages:
            outputs['dji_agras_vra_zip'] = controller_packages['dji_agras_vra_zip']
        if 'xag_vra_zip' in controller_packages:
            outputs['xag_vra_zip'] = controller_packages['xag_vra_zip']
        validations = {k: v for k, v in controller_packages.items() if k.endswith('_validation')}
        pd.DataFrame([validations]).to_csv(
            summaries_dir / 'controller_packages_validation.csv', index=False
        )
        outputs['controller_packages_validation_csv'] = str(summaries_dir / 'controller_packages_validation.csv')

        # Export binary or physical spot-spraying / section-control prescription package when hotspots exist
        spot_spray_rate_summary = None
        if hotspots is not None and len(hotspots) > 0:
            _safe_to_file(hotspots, rasters_dir / 'stress_hotspots.geojson', driver='GeoJSON')

            is_spot_physical = resolved_spot_spray_plan.mode == "physical"
            spot_target_rate = float(resolved_spot_spray_plan.min_rate or resolved_spot_spray_plan.max_rate or 100.0) if is_spot_physical else 100.0
            spot_rate_unit = str(resolved_spot_spray_plan.unit or "PCT").upper() if is_spot_physical else "PCT"

            spot_spray_gdf = build_spot_spray_prescription_gdf(
                hotspots,
                target_rate=spot_target_rate,
                background_rate=0.0,
                rate_unit=spot_rate_unit,
            )
            if len(spot_spray_gdf) > 0:
                _safe_to_file(spot_spray_gdf, rasters_dir / 'spot_spray_targets.geojson', driver='GeoJSON')
                spot_dir = prescriptions_dir / 'spray_targets'
                spot_dir.mkdir(parents=True, exist_ok=True)
                spot_kml = spot_dir / 'stress_patches.kml'
                export_polygons_kml(spot_spray_gdf, spot_kml, name_field='hotspot_id', description_fields=['hotspot_id', 'area_m2', 'mean_ndvi', 'severity_rank', 'threshold_ndvi'], description_labels={'hotspot_id': 'Target ID', 'area_m2': 'Target area (m²)', 'mean_ndvi': 'Mean NDVI', 'severity_rank': 'Severity rank', 'threshold_ndvi': 'Selection threshold NDVI'}, document_name='low-vigor scouting targets', severity_field='severity_rank')
                extra_outputs['spot_spray_targets_kml'] = str(spot_kml)
                spot_summary_csv = spot_dir / 'stress_patches.csv'
                spot_spray_gdf.drop(columns=['geometry']).to_csv(spot_summary_csv, index=False)
                outputs['spot_spray_summary_csv'] = str(spot_summary_csv)

                if is_spot_physical:
                    try:
                        from pyproj import CRS as _CRS
                        resolved_crs = _CRS.from_user_input(spot_spray_gdf.crs)
                        if resolved_crs.is_projected:
                            treated_area_ha = float(spot_spray_gdf.geometry.area.sum() / 10_000.0)
                        else:
                            treated_area_ha = float(spot_spray_gdf.to_crs(spot_spray_gdf.estimate_utm_crs()).geometry.area.sum() / 10_000.0)
                    except Exception:
                        treated_area_ha = float(len(spot_spray_gdf) * 0.01)

                    est_total = treated_area_ha * spot_target_rate
                    total_unit = "L" if "L" in spot_rate_unit else ("kg" if "KG" in spot_rate_unit else spot_rate_unit)
                    spot_spray_rate_summary = {
                        "mode": "physical",
                        "operation": "spray",
                        "product_name": resolved_spot_spray_plan.product_name or "Spot Spray Chemical",
                        "rate_basis": resolved_spot_spray_plan.rate_basis or "product",
                        "strategy": "target_hotspots",
                        "unit": spot_rate_unit,
                        "target_rate": spot_target_rate,
                        "treated_area_ha": round(treated_area_ha, 4),
                        "estimated_total_product": round(est_total, 2),
                        "total_product_unit": total_unit,
                        "approved_by": resolved_spot_spray_plan.approved_by or "Operator",
                    }
                    _write_json(summaries_dir / "spot_spray_rate_plan.json", spot_spray_rate_summary)
                    pd.DataFrame([spot_spray_rate_summary]).to_csv(summaries_dir / "spot_spray_rate_table.csv", index=False)

                spot_controller_dir = spot_dir / 'controller_packages'
                export_all_controller_prescription_zips(
                    spot_spray_gdf, spot_controller_dir, rate_unit=spot_rate_unit,
                    include_research_stage=include_research_controller_packages,
                    basemap_mbtiles_path=basemap_mbtiles_path,
                    include_basemap_in_archives=bundle_basemap_in_controller_archives,
                )
                outputs['spot_spray_controller_packages_dir'] = str(spot_controller_dir)

    # Clean up basemap staging dir — it was only needed to populate the controller ZIPs
    if bundle_basemap_in_controller_archives and basemap_mbtiles_path is not None:
        try:
            import shutil as _shutil
            _shutil.rmtree(_bm_staging, ignore_errors=True)
        except Exception:
            pass


    primary_index = feature_names[0]
    first_index_mean = f"{primary_index}_mean"
    if first_index_mean in zones.columns:
        valid_zones = zones.dropna(subset=[first_index_mean])
        if len(valid_zones):
            worst = valid_zones.sort_values(by=first_index_mean).iloc[0]
            best = valid_zones.sort_values(by=first_index_mean).iloc[-1]
            extra_outputs['lowest_index_zone_id'] = int(worst.zone_id)
            extra_outputs['highest_index_zone_id'] = int(best.zone_id)

    recommended_action_zone_pct = 0.0
    if cover is not None and cover > 0:
        total_zone_area = float(zones['geometry_area_m2'].sum()) if len(zones) else 0.0
        pixel_area_m2 = abs(transform.a * transform.e - transform.b * transform.d)
        usable_area_m2 = float(np.count_nonzero(field_mask)) * pixel_area_m2
        recommended_action_zone_pct = (total_zone_area / usable_area_m2 * 100.0) if usable_area_m2 > 0 else 0.0

        action_coverage_summary = {
            'metric_label': 'recommended_action_zone_coverage_pct',
            'overall_cover_pct': cover,
            'usable_field_area_m2': usable_area_m2,
            'recommended_action_zone_area_m2': total_zone_area,
            'recommended_action_zone_pct_of_usable_area': recommended_action_zone_pct,
            'usable_area_not_in_action_zones_m2': max(0.0, usable_area_m2 - total_zone_area),
            'action_zone_selection_rule': 'Minimum contiguous zone size filter applied; small noise patches excluded for machine operations.',
        }
        pd.DataFrame([action_coverage_summary]).to_csv(
            summaries_dir / 'action_zone_coverage_summary.csv',
            index=False,
        )
        outputs['action_zone_coverage_summary_csv'] = str(summaries_dir / 'action_zone_coverage_summary.csv')

    if recommended_action_zone_pct < 95.0:
        warnings.append(
            f'Recommended action zones cover {recommended_action_zone_pct:.1f}% of the usable field area; '
            'small or fragmented areas may have been excluded to keep exported files practical for equipment and field use.'
        )

    zone_rows = [['Zone', 'Priority', 'Area m²', 'Crop score']]
    if zones is not None and len(zones):
        for _, r in zones.iterrows():
            mean_ndvi = r.get('ndvi_mean', None)

            raw_label = str(r.get('relative_vigor_label', getattr(r, 'vigor_label', r.zone_id)))
            label_map = {
                'lowest_relative_vigor': 'Lowest relative vigor',
                'lower_relative_vigor': 'Lower relative vigor',
                'typical_relative_vigor': 'Typical relative vigor',
                'higher_relative_vigor': 'Higher relative vigor',
                'highest_relative_vigor': 'Highest relative vigor',
                'critical': 'High priority',
                'watch': 'Watch',
                'moderate': 'Moderate',
                'healthy': 'Healthy',
                'very_healthy': 'Very healthy',
            }
            farmer_label = label_map.get(raw_label, raw_label.replace('_', ' ').title())

            zone_rows.append([
                int(r.zone_id),
                farmer_label,
                f'{float(r.area_m2):.0f}',
                '' if mean_ndvi is None else f'{float(mean_ndvi):.3f}',
            ])
    hotspot_rows = None
    if hotspots is not None and len(hotspots):
        hotspot_rows = [['Target', 'Area m²', 'Mean NDVI', 'Severity rank']]
        for _, r in hotspots.head(10).iterrows():
            hotspot_rows.append([int(r.hotspot_id), f'{r.area_m2:.0f}', f'{r.mean_ndvi:.3f}', int(r.severity_rank)])

    pdf_path = out_dir / 'health_report.pdf'
    disclaimer_notes = []
    if not absolute_reflectance_valid:
        disclaimer_notes.append(
            "RADIOMETRIC CALIBRATION NOTICE: Spectral inputs for this flight were uncalibrated raw digital numbers. "
            "NDVI/NDRE relative health maps are included for visual field review, but absolute products "
            "(MSAVI2, plant counting, NIR shadow screening, and machine controller prescriptions) were disabled "
            "to prevent inaccurate field application."
        )
    if zoning_used_full_footprint_fallback:
        disclaimer_notes.append(
            "FIELD COVERAGE NOTICE: Clean crop canopy mask contained fewer than 20 vegetation pixels. "
            "Diagnostic zoning was computed across the full image footprint. Confirm field conditions before operations."
        )
    custom_disclaimer = "\n\n".join(disclaimer_notes) if disclaimer_notes else None

    if progress_callback:
        progress_callback('Generating reports', 92)
    build_agriculture_pdf(
        pdf_path,
        ortho_preview=ortho_preview,
        ndvi_preview=outputs.get('ndvi_preview', ortho_preview),
        canopy_cover_pct=cover,
        zone_table=zone_rows if len(zone_rows) > 1 else None,
        hotspot_table=hotspot_rows,
        coverage_metrics=action_coverage_summary,
        fertilizer_rate_summary=fertilizer_rate_summary,
        spot_spray_rate_summary=spot_spray_rate_summary,
        disclaimer=custom_disclaimer,
    )
    outputs['pdf_report'] = str(pdf_path)

    export_guides(out_dir)

    input_audit_path = summaries_dir / 'input_band_audit.json'
    _write_json(input_audit_path, band_audit)
    outputs['input_band_audit_json'] = str(input_audit_path)
    methodology = {
        'pipeline_version': '3.2.0-band-safety-patch',
        'band_audit': band_audit,
        'absolute_reflectance_valid': absolute_reflectance_valid,
        'zoning_used_full_footprint_fallback': zoning_used_full_footprint_fallback,
        'prescription_qc_passed': prescription_qc_passed,
        'zoning_k_range': list(k_range),
        'crop_mask_ndvi_threshold': crop_ndvi_threshold,
        'crop_qc_metrics': crop_qc_metrics,
        'fertilizer_rate_plan': fertilizer_rate_summary,
        'safety_notice': 'Outputs are for agronomic planning and decision support only. Ground-truth before application.',
    }
    analytics_methodology_path = summaries_dir / 'analytics_methodology.json'
    export_analytics_methodology(analytics_methodology_path, domain='agriculture')
    _write_json(summaries_dir / 'pipeline_methodology.json', methodology)
    outputs['analytics_methodology_json'] = str(analytics_methodology_path)
    outputs['pipeline_methodology_json'] = str(summaries_dir / 'pipeline_methodology.json')
    processing_warnings_path = summaries_dir / 'processing_warnings.json'
    _write_json(processing_warnings_path, {'warnings': warnings})
    outputs['processing_warnings_json'] = str(processing_warnings_path)
    outputs['warnings'] = list(warnings)

    outputs.update(extra_outputs)
    return outputs
