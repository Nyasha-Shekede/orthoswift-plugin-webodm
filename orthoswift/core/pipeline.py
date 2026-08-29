"""Agriculture post-processing pipeline for the OrthoSWIFT WebODM plugin.

Consumes a georeferenced multispectral orthomosaic, computes vegetation indices,
management zones and stress hotspots, resolves optional operator-supplied rates,
and writes the existing GIS, controller and PDF deliverables.
"""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional

logger = logging.getLogger(__name__)
os.environ.setdefault("MPLBACKEND", "Agg")

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import rasterio
from rasterio.warp import Resampling, calculate_default_transform, reproject

from .basemaps import export_orthomosaic_mbtiles
from .decisions import (
    ApplicationRatePlan,
    _write_json,
    build_spot_spray_prescription_gdf,
    export_dji_agras_prescription_zip,
    resolve_application_rate_plan,
)
from .exports import export_analytics_methodology, export_polygons_kml, write_raster
from .guide import export_guides
from .report import build_agriculture_pdf
from .vegetation import (
    canopy_cover_summary,
    classify_ndvi,
    clean_crop_mask,
    gli,
    management_zones,
    msavi2,
    ndre,
    ndvi,
    stress_hotspots,
)


def _ensure_dir(path: str | Path) -> Path:
    p = Path(path)
    p.mkdir(parents=True, exist_ok=True)
    return p


def _save_quick_preview(
    arr: np.ndarray, out_path: str | Path, cmap: str = "viridis"
) -> Path:
    finite = arr[np.isfinite(arr)]
    lo, hi = np.percentile(finite, [2, 98]) if finite.size else (0, 1)
    plt.figure(figsize=(8, 6), dpi=150)
    plt.axis("off")
    plt.imshow(arr, cmap=cmap, vmin=lo, vmax=hi)
    plt.savefig(out_path, bbox_inches="tight", pad_inches=0)
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
        warnings.append(
            f"Band {name}: {zero_pct:.3f}% of finite pixels are exactly zero; these are treated as outside-footprint/background candidates."
        )
    if neg_pct > 0.01 or gt1_pct > 0.01:
        warnings.append(
            f"Band {name}: reflectance-like values outside [0, 1] detected (negative={neg_pct:.3f}%, >1={gt1_pct:.3f}%)."
        )


def _qa_index(name: str, arr: np.ndarray, warnings: list[str]) -> None:
    finite = np.isfinite(arr)
    if not finite.any():
        warnings.append(f"{name.upper()} contains no finite pixels after QA masking.")
        return
    outside = finite & ((arr < -1.0) | (arr > 1.0))
    if outside.any():
        warnings.append(
            f"{name.upper()}: {int(outside.sum())} finite pixels ({outside.sum() / finite.sum() * 100:.4f}%) outside [-1, 1]; they should have been masked before export."
        )


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


def _validated_explicit_band_map(src, explicit) -> dict[str, int]:
    if explicit is None:
        return {}
    unknown = set(explicit) - set(_CANONICAL_SPECTRAL_ROLES)
    if unknown:
        raise ValueError(f"Unknown multispectral band role(s): {sorted(unknown)}")
    resolved = {}
    for role, index in explicit.items():
        if (
            not isinstance(index, int)
            or isinstance(index, bool)
            or not 1 <= index <= src.count
        ):
            raise ValueError(
                f"Band index for '{role}' must be an integer in [1, {src.count}]"
            )
        resolved[role] = index
    return resolved


def _apply_description_band_roles(src, resolved: dict[str, int]) -> None:
    descriptions = [_normalise_band_description(value) for value in src.descriptions]
    for role, aliases in _BAND_ALIASES.items():
        if role in resolved:
            continue
        matches = [
            index
            for index, description in enumerate(descriptions, start=1)
            if description in aliases
        ]
        if len(matches) > 1:
            raise ValueError(
                f"Multiple bands are described as '{role}'; "
                "provide multispectral_band_map explicitly"
            )
        if matches:
            resolved[role] = matches[0]


def _color_interpretation_roles(src, resolved: dict[str, int]) -> list[int]:
    from rasterio.enums import ColorInterp

    color_roles = {
        ColorInterp.red: "red",
        ColorInterp.green: "green",
        ColorInterp.blue: "blue",
    }
    for index, interpretation in enumerate(src.colorinterp, start=1):
        role = color_roles.get(interpretation)
        if role is not None:
            resolved.setdefault(role, index)
    return [
        index
        for index, interpretation in enumerate(src.colorinterp, start=1)
        if interpretation == ColorInterp.alpha
    ]


def _apply_standard_band_layout(src, resolved, alpha_indexes) -> None:
    if "red" in resolved and "nir" in resolved:
        return
    non_alpha_count = src.count - len(alpha_indexes)
    layouts = (
        (
            src.count >= 6 and non_alpha_count >= 5,
            ("red", "green", "blue", "nir", "red_edge"),
        ),
        (src.count == 5 and non_alpha_count >= 4, ("red", "green", "nir", "red_edge")),
        (src.count == 4 and non_alpha_count >= 4, ("red", "green", "blue", "nir")),
    )
    for matches, roles in layouts:
        if matches:
            for index, role in enumerate(roles, start=1):
                resolved.setdefault(role, index)
            return


def _validate_resolved_band_map(resolved, alpha_indexes) -> None:
    alpha_roles = [
        (role, index) for role, index in resolved.items() if index in alpha_indexes
    ]
    if alpha_roles:
        role, index = alpha_roles[0]
        raise ValueError(
            f"Band {index} is marked alpha/opacity and cannot be used as "
            f"spectral role '{role}'"
        )
    if len(set(resolved.values())) != len(resolved):
        raise ValueError(
            "A raster band cannot be assigned to more than one spectral role"
        )
    if "red" not in resolved or "nir" not in resolved:
        raise ValueError(
            "Stacked multispectral input requires identifiable red and NIR bands. "
            "Add band descriptions or pass "
            "multispectral_band_map={'red': ..., 'nir': ...}."
        )


def _resolve_multispectral_band_map(
    src, explicit: Optional[Mapping[str, int]] = None
) -> tuple[dict[str, int], Optional[int]]:
    """Resolve canonical spectral roles from explicit and raster metadata."""
    resolved = _validated_explicit_band_map(src, explicit)
    _apply_description_band_roles(src, resolved)
    alpha_indexes = _color_interpretation_roles(src, resolved)
    _apply_standard_band_layout(src, resolved, alpha_indexes)
    _validate_resolved_band_map(resolved, alpha_indexes)
    alpha_index = alpha_indexes[0] if len(alpha_indexes) == 1 else None
    return resolved, alpha_index


def _apply_reflectance_scale(
    name: str,
    arr: np.ndarray,
    scale: Optional[float | Mapping[str, float]],
    warnings: list[str],
) -> None:
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
    warnings.append(
        f"Band '{name}' multiplied by explicit reflectance_scale={factor:g}."
    )


def _absolute_reflectance_is_plausible(
    arrays: Mapping[str, np.ndarray],
) -> tuple[bool, str]:
    """Conservative gate for scale-dependent indices and absolute thresholds."""
    stats = {}
    for role in ("red", "nir"):
        values = arrays[role][np.isfinite(arrays[role])]
        if values.size == 0:
            return False, f"{role} has no finite pixels"
        stats[role] = float(np.quantile(values, 0.99))
    if max(stats.values()) < 0.01:
        return (
            False,
            f"red/NIR 99th percentiles are {stats['red']:.6g}/{stats['nir']:.6g}, implausibly small for [0,1] reflectance",
        )
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
            warnings.append(
                f"Clipped {outside_pct:.2f}% of {name} pixels outside [0, 1] to valid reflectance bounds."
            )


def _warn_vector_area_consistency(
    name: str, gdf, warnings: list[str], tolerance_pct: float = 2.0
) -> None:
    if (
        gdf is None
        or len(gdf) == 0
        or "raster_area_m2" not in gdf.columns
        or "geometry_area_m2" not in gdf.columns
    ):
        return
    raster_area = float(gdf["raster_area_m2"].sum())
    geom_area = float(gdf["geometry_area_m2"].sum())
    if raster_area <= 0:
        return
    delta_pct = (geom_area - raster_area) / raster_area * 100.0
    if abs(delta_pct) > tolerance_pct:
        warnings.append(
            f"{name}: vector geometry area differs from raster area by {delta_pct:.2f}% ({geom_area:.2f} vs {raster_area:.2f} m²)."
        )


def _check_pixel_limit(dataset, max_pixels) -> None:
    pixel_count = int(dataset.height) * int(dataset.width)
    if max_pixels is not None and max_pixels > 0 and pixel_count > int(max_pixels):
        raise ValueError(
            f"Agriculture raster has {pixel_count:,} pixels; configured "
            f"in-memory limit is {int(max_pixels):,}. Use a tiled workflow or smaller AOI."
        )


def _read_spectral_band(dataset, index, name, warnings):
    band = dataset.read(index, masked=True)
    valid = ~np.ma.getmaskarray(band)
    array = np.asarray(band.filled(np.nan), dtype="float32")
    valid &= np.isfinite(array)
    array[~valid] = np.nan
    _qa_reflectance_band(name, array, warnings)
    return array, valid


def _load_stacked_spectral_inputs(path, explicit_band_map, max_pixels, warnings):
    arrays: dict[str, np.ndarray] = {}
    masks: dict[str, np.ndarray] = {}
    with rasterio.open(path) as dataset:
        _check_pixel_limit(dataset, max_pixels)
        resolved_map, alpha_index = _resolve_multispectral_band_map(
            dataset, explicit_band_map
        )
        profile = dataset.profile.copy()
        audit = {
            "source": str(path),
            "mode": "stacked",
            "resolved_band_map": resolved_map,
            "descriptions": list(dataset.descriptions),
            "color_interpretation": [
                str(value).split(".")[-1] for value in dataset.colorinterp
            ],
            "alpha_band": alpha_index,
        }
        for name, index in resolved_map.items():
            arrays[name], masks[name] = _read_spectral_band(
                dataset, index, name, warnings
            )
        embedded_mask = None
        if alpha_index is not None:
            alpha = dataset.read(alpha_index, masked=True)
            embedded_mask = (
                ~np.ma.getmaskarray(alpha)
                & np.isfinite(alpha.filled(np.nan))
                & (np.asarray(alpha.filled(0)) > 0)
            )
    return arrays, masks, profile, embedded_mask, audit


def _validate_separate_band_metadata(dataset, name) -> None:
    from rasterio.enums import ColorInterp

    if dataset.colorinterp and dataset.colorinterp[0] == ColorInterp.alpha:
        raise ValueError(
            f"Input for '{name}' is marked alpha/opacity, not a spectral band"
        )
    description = _normalise_band_description(dataset.descriptions[0])
    described_roles = [
        role for role, aliases in _BAND_ALIASES.items() if description in aliases
    ]
    if described_roles and name not in described_roles:
        raise ValueError(
            f"Input routed as '{name}' is described as '{described_roles[0]}'; "
            "refusing semantic band mismatch"
        )


def _is_coregistered(dataset, array, profile) -> bool:
    return (
        array.shape == (profile["height"], profile["width"])
        and dataset.transform == profile["transform"]
        and dataset.crs == profile["crs"]
    )


def _load_separate_spectral_inputs(paths, max_pixels, warnings):
    arrays: dict[str, np.ndarray] = {}
    masks: dict[str, np.ndarray] = {}
    profile = None
    audit = {"mode": "separate", "sources": {}}
    for name, path in paths:
        if path is None:
            continue
        with rasterio.open(path) as dataset:
            _validate_separate_band_metadata(dataset, name)
            _check_pixel_limit(dataset, max_pixels)
            array, valid = _read_spectral_band(dataset, 1, name, warnings)
            if profile is not None and not _is_coregistered(dataset, array, profile):
                raise ValueError(
                    f"Band '{name}' is not co-registered with the first spectral band"
                )
            profile = profile or dataset.profile.copy()
            arrays[name], masks[name] = array, valid
            audit["sources"][name] = {
                "path": str(path),
                "description": dataset.descriptions[0],
                "color_interpretation": str(dataset.colorinterp[0]).split(".")[-1],
            }
    return arrays, masks, profile, None, audit


def _load_spectral_inputs(
    *,
    multispectral_path,
    multispectral_band_map,
    max_agriculture_pixels,
    red_path,
    nir_path,
    red_edge_path,
    green_path,
    blue_path,
    warnings,
):
    if multispectral_path is not None:
        return _load_stacked_spectral_inputs(
            multispectral_path,
            multispectral_band_map,
            max_agriculture_pixels,
            warnings,
        )
    paths = (
        ("red", red_path),
        ("nir", nir_path),
        ("red_edge", red_edge_path),
        ("green", green_path),
        ("blue", blue_path),
    )
    return _load_separate_spectral_inputs(paths, max_agriculture_pixels, warnings)


def _read_footprint_mask(path, profile):
    if profile is None:
        raise ValueError(
            "Spectral profile must be initialized before reading the footprint mask"
        )
    with rasterio.open(path) as dataset:
        if (dataset.height, dataset.width) != (profile["height"], profile["width"]):
            raise ValueError("Footprint mask shape does not match spectral bands")
        if dataset.transform != profile["transform"]:
            raise ValueError("Footprint mask transform does not match spectral bands")
        if dataset.crs != profile["crs"]:
            raise ValueError("Footprint mask CRS does not match spectral bands")
        footprint = dataset.read(1, masked=True)
        return (
            ~np.ma.getmaskarray(footprint)
            & np.isfinite(footprint.filled(np.nan))
            & (np.asarray(footprint.filled(0)) > 0)
        )


def _exclude_implicit_zero_background(arrays, common_mask, warnings) -> None:
    spectral_names = [
        name for name in ("red", "green", "blue", "nir", "red_edge") if name in arrays
    ]
    if not spectral_names:
        return
    all_zero = np.logical_and.reduce([arrays[name] == 0.0 for name in spectral_names])
    excluded_count = int(np.count_nonzero(common_mask & all_zero))
    if excluded_count:
        common_mask &= ~all_zero
        warnings.append(
            f"Excluded {excluded_count:,} all-zero pixels as outside-footprint "
            "background because no explicit footprint mask was supplied."
        )


def _build_common_footprint(
    *, arrays, masks, profile, embedded_mask, footprint_mask_path, warnings
):
    if not arrays:
        raise ValueError("At least one spectral band must be provided")
    common_mask = np.logical_and.reduce([masks[name] for name in arrays])
    if embedded_mask is not None:
        common_mask &= embedded_mask
        warnings.append(
            "Used embedded alpha band as the footprint mask; it was excluded "
            "from spectral analytics."
        )
    if footprint_mask_path is not None:
        common_mask &= _read_footprint_mask(footprint_mask_path, profile)
    elif embedded_mask is None:
        _exclude_implicit_zero_background(arrays, common_mask, warnings)
    for name, array in arrays.items():
        masks[name] &= common_mask
        array[~common_mask] = np.nan
    return common_mask


def _validate_spectral_arrays(arrays, reflectance_scale, warnings, band_audit):
    if "red" not in arrays or "nir" not in arrays:
        raise ValueError(
            "OrthoSWIFT agriculture requires co-registered red and NIR reflectance bands"
        )
    for name, array in arrays.items():
        _apply_reflectance_scale(name, array, reflectance_scale, warnings)
        finite = array[np.isfinite(array)]
        tolerance = max(1e-12, abs(float(np.median(finite))) * 1e-8)
        if finite.size and float(np.ptp(finite)) <= tolerance:
            raise ValueError(
                f"Band '{name}' is spatially constant and cannot be a usable "
                "spectral measurement (possible alpha/opacity mask)"
            )
        _validate_reflectance_band(name, array, warnings=warnings)
    valid, reason = _absolute_reflectance_is_plausible(arrays)
    band_audit["absolute_reflectance_gate"] = {"passed": valid, "reason": reason}
    if not valid:
        warnings.append(
            "Absolute-reflectance gate failed: " + reason + ". "
            "NDVI/NDRE remain available because normalized differences are "
            "invariant to a common multiplicative scale; MSAVI2, absolute NIR "
            "shadow screening, plant counting, and machine prescription export "
            "are disabled."
        )
    return valid


def _apply_footprint_and_validate(
    *,
    arrays,
    masks,
    profile,
    embedded_footprint_mask,
    footprint_mask_path,
    reflectance_scale,
    warnings,
    band_audit,
):
    common_mask = _build_common_footprint(
        arrays=arrays,
        masks=masks,
        profile=profile,
        embedded_mask=embedded_footprint_mask,
        footprint_mask_path=footprint_mask_path,
        warnings=warnings,
    )
    absolute_valid = _validate_spectral_arrays(
        arrays, reflectance_scale, warnings, band_audit
    )
    return common_mask, absolute_valid


def _project_to_metric_crs(*, arrays, common_valid_mask, profile, warnings):
    transform = profile["transform"]
    crs = profile["crs"]
    from pyproj import CRS as _CRS
    from pyproj.aoi import AreaOfInterest
    from pyproj.database import query_utm_crs_info

    _resolved = _CRS.from_user_input(crs)
    _units = [float(a.unit_conversion_factor) for a in _resolved.axis_info[:2]]
    _is_metre_projected = (
        _resolved.is_projected
        and len(_units) >= 2
        and all(np.isclose(u, 1.0, rtol=1e-6) for u in _units)
    )

    if not _is_metre_projected:
        # Auto-reproject geographic CRS (e.g. EPSG:4326) to the correct UTM zone
        # derived from the raster's geographic centroid.
        _height = profile["height"]
        _width = profile["width"]
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
                west_lon_degree=_lon,
                south_lat_degree=_lat,
                east_lon_degree=_lon,
                north_lat_degree=_lat,
            ),
        )
        if not _utm_results:
            raise ValueError(
                f"Could not determine a UTM zone for centroid ({_lat:.4f}, {_lon:.4f}). "
                "Reproject your orthomosaic to a projected metre-based CRS and re-upload."
            )
        _target_utm = _CRS.from_authority(
            _utm_results[0].auth_name, _utm_results[0].code
        )
        warnings.append(
            f"Input raster CRS is geographic ({_resolved.name}). "
            f"Auto-reprojecting band arrays to {_target_utm.name} for area and prescription calculations."
        )
        _src_crs_rio = rasterio.crs.CRS.from_user_input(_resolved.to_wkt())
        _dst_crs_rio = rasterio.crs.CRS.from_user_input(_target_utm.to_wkt())
        _new_transform, _new_width, _new_height = calculate_default_transform(
            _src_crs_rio,
            _dst_crs_rio,
            _width,
            _height,
            left=transform.c,
            bottom=transform.f + transform.e * _height,
            right=transform.c + transform.a * _width,
            top=transform.f,
        )
        _reprojected_arrays: dict[str, np.ndarray] = {}
        for _bname, _barr in arrays.items():
            _src = _barr.copy()
            _dst = np.full((_new_height, _new_width), np.nan, dtype="float32")
            reproject(
                source=_src,
                destination=_dst,
                src_transform=transform,
                src_crs=_src_crs_rio,
                dst_transform=_new_transform,
                dst_crs=_dst_crs_rio,
                resampling=Resampling.bilinear,
                src_nodata=np.nan,
                dst_nodata=np.nan,
            )
            _reprojected_arrays[_bname] = _dst
        arrays = _reprojected_arrays
        _mask_src = common_valid_mask.astype("float32")
        _mask_dst = np.zeros((_new_height, _new_width), dtype="float32")
        reproject(
            source=_mask_src,
            destination=_mask_dst,
            src_transform=transform,
            src_crs=_src_crs_rio,
            dst_transform=_new_transform,
            dst_crs=_dst_crs_rio,
            resampling=Resampling.nearest,
            src_nodata=0.0,
            dst_nodata=0.0,
        )
        common_valid_mask = _mask_dst > 0.5
        for _bname in arrays:
            arrays[_bname][~common_valid_mask] = np.nan
        profile.update(
            crs=_dst_crs_rio,
            transform=_new_transform,
            width=_new_width,
            height=_new_height,
        )
        transform = _new_transform
        crs = _dst_crs_rio

    if not (np.isclose(transform.b, 0.0) and np.isclose(transform.d, 0.0)):
        raise ValueError(
            "Agriculture analytics currently require a north-up, non-sheared raster transform"
        )
    return arrays, common_valid_mask, profile, transform, crs


def _compute_and_export_indices(
    *,
    arrays,
    common_valid_mask,
    absolute_reflectance_valid,
    profile,
    rasters_dir,
    previews_dir,
    outputs,
    warnings,
):
    primary_index_preview = None
    features_stack = []
    feature_names = []
    ndvi_arr = None
    ndre_arr = None
    gli_arr = None
    msavi2_arr = None

    if "red" in arrays and "nir" in arrays:
        ndvi_arr = ndvi(arrays["red"], arrays["nir"])
        ndvi_arr[~common_valid_mask] = np.nan
        _qa_index("ndvi", ndvi_arr, warnings)
        write_raster(
            rasters_dir / "ndvi.tif",
            ndvi_arr,
            profile,
            dtype="float32",
            nodata=-9999.0,
        )
        outputs["ndvi_tif"] = str(rasters_dir / "ndvi.tif")
        ndvi_cls = classify_ndvi(ndvi_arr)
        write_raster(
            rasters_dir / "ndvi_classes.tif",
            ndvi_cls.astype("int16"),
            profile,
            dtype="int16",
            nodata=-1,
        )
        outputs["ndvi_classes_tif"] = str(rasters_dir / "ndvi_classes.tif")
        primary_index_preview = _save_quick_preview(
            ndvi_arr, previews_dir / "ndvi_preview.png", cmap="RdYlGn"
        )
        outputs["ndvi_preview"] = str(primary_index_preview)
        features_stack.append(ndvi_arr)
        feature_names.append("ndvi")

        if absolute_reflectance_valid:
            msavi2_arr = msavi2(arrays["red"], arrays["nir"])
            msavi2_arr[~common_valid_mask] = np.nan
            _qa_index("msavi2", msavi2_arr, warnings)
            write_raster(
                rasters_dir / "msavi2.tif",
                msavi2_arr,
                profile,
                dtype="float32",
                nodata=-9999.0,
            )
            outputs["msavi2_tif"] = str(rasters_dir / "msavi2.tif")
            msavi2_preview = _save_quick_preview(
                msavi2_arr, previews_dir / "msavi2_preview.png", cmap="RdYlGn"
            )
            outputs["msavi2_preview"] = str(msavi2_preview)
            features_stack.append(msavi2_arr)
            feature_names.append("msavi2")

    if "red_edge" in arrays and "nir" in arrays:
        ndre_arr = ndre(arrays["red_edge"], arrays["nir"])
        ndre_arr[~common_valid_mask] = np.nan
        _qa_index("ndre", ndre_arr, warnings)
        write_raster(
            rasters_dir / "ndre.tif",
            ndre_arr,
            profile,
            dtype="float32",
            nodata=-9999.0,
        )
        outputs["ndre_tif"] = str(rasters_dir / "ndre.tif")
        ndre_preview = _save_quick_preview(
            ndre_arr, previews_dir / "ndre_preview.png", cmap="RdYlGn"
        )
        outputs["ndre_preview"] = str(ndre_preview)
        if primary_index_preview is None:
            primary_index_preview = ndre_preview
        features_stack.append(ndre_arr)
        feature_names.append("ndre")

    if {"red", "green", "blue"}.issubset(arrays.keys()):
        gli_arr = gli(arrays["red"], arrays["green"], arrays["blue"])
        gli_arr[~common_valid_mask] = np.nan
        _qa_index("gli", gli_arr, warnings)
        write_raster(
            rasters_dir / "gli.tif",
            gli_arr,
            profile,
            dtype="float32",
            nodata=-9999.0,
        )
        outputs["gli_tif"] = str(rasters_dir / "gli.tif")
        gli_preview = _save_quick_preview(
            gli_arr, previews_dir / "gli_preview.png", cmap="RdYlGn"
        )
        outputs["gli_preview"] = str(gli_preview)
        if primary_index_preview is None:
            primary_index_preview = gli_preview
        features_stack.append(gli_arr)
        feature_names.append("gli")

    if not features_stack:
        raise ValueError("Need red and NIR multispectral inputs")
    return features_stack, feature_names, ndvi_arr, msavi2_arr


def _build_and_export_zones(
    *,
    features_stack,
    feature_names,
    ndvi_arr,
    msavi2_arr,
    arrays,
    absolute_reflectance_valid,
    transform,
    crs,
    profile,
    k_range,
    crop_ndvi_threshold,
    crop_msavi2_threshold,
    nir_shadow_threshold,
    min_crop_component_m2,
    min_non_crop_component_m2,
    edge_buffer_m,
    export_kml,
    prescriptions_dir,
    rasters_dir,
    summaries_dir,
    outputs,
    extra_outputs,
    warnings,
    resolved_rate_plan,
    progress_callback,
):
    stack = np.stack(features_stack, axis=0)
    finite_stack_mask = np.all(np.isfinite(stack), axis=0)
    field_mask = finite_stack_mask.copy()
    crop_qc_metrics = None

    if ndvi_arr is not None:
        crop_mask, crop_qc_metrics = clean_crop_mask(
            ndvi_arr,
            transform,
            msavi2_arr=msavi2_arr if absolute_reflectance_valid else None,
            nir_arr=arrays.get("nir") if absolute_reflectance_valid else None,
            green_arr=arrays.get("green") if absolute_reflectance_valid else None,
            ndvi_threshold=crop_ndvi_threshold,
            msavi2_threshold=crop_msavi2_threshold,
            nir_shadow_threshold=nir_shadow_threshold,
            min_crop_component_m2=min_crop_component_m2,
            min_non_crop_component_m2=min_non_crop_component_m2,
            edge_buffer_m=edge_buffer_m,
        )
        field_mask &= crop_mask
        pd.DataFrame([crop_qc_metrics]).to_csv(
            summaries_dir / "crop_mask_qc_summary.csv", index=False
        )
        outputs["crop_mask_qc_summary_csv"] = str(
            summaries_dir / "crop_mask_qc_summary.csv"
        )
        crop_mask_raster = np.full(field_mask.shape, 255, dtype=np.uint8)
        crop_mask_raster[finite_stack_mask] = np.where(
            field_mask[finite_stack_mask], 1, 0
        )
        write_raster(
            rasters_dir / "crop_mask_qc.tif",
            crop_mask_raster,
            profile,
            dtype="uint8",
            nodata=255,
        )
        outputs["crop_mask_qc_tif"] = str(rasters_dir / "crop_mask_qc.tif")
        if int(np.count_nonzero(field_mask)) < 500:
            warnings.append(
                "Clean crop mask had fewer than 500 pixels after soil/shadow/water/edge QC; management zones may be unstable."
            )

    zoning_used_full_footprint_fallback = False
    if int(np.count_nonzero(field_mask)) < 20:
        if int(np.count_nonzero(finite_stack_mask)) >= 20:
            warnings.append(
                "Clean crop mask contained fewer than 20 pixels; using the finite footprint for diagnostic zoning only. Machine prescriptions are blocked."
            )
            field_mask = finite_stack_mask.copy()
            zoning_used_full_footprint_fallback = True
        else:
            raise ValueError(
                "Too few valid field pixels for agriculture zoning after masking"
            )

    if progress_callback:
        progress_callback("Building management zones", 62)
    label_raster, zones = management_zones(
        stack,
        transform,
        crs,
        k_range=k_range,
        field_mask=field_mask,
        feature_names=feature_names,
    )
    write_raster(
        rasters_dir / "fertilizer_zones.tif",
        label_raster.astype("int16"),
        profile,
        dtype="int16",
        nodata=-1,
    )
    _warn_vector_area_consistency("management_zones", zones, warnings)
    outputs["fertilizer_zones_tif"] = str(rasters_dir / "fertilizer_zones.tif")
    fertilizer_zones_geojson = _safe_to_file(
        zones, rasters_dir / "fertilizer_zones.geojson", driver="GeoJSON"
    )
    if fertilizer_zones_geojson is not None:
        outputs["fertilizer_zones_geojson"] = str(fertilizer_zones_geojson)
    vra_dir = _ensure_dir(prescriptions_dir / "fertilizer_zones")
    zones_summary_path = vra_dir / "fertilizer_zone_summary.csv"
    zones.drop(columns=["geometry"]).to_csv(zones_summary_path, index=False)
    outputs["fertilizer_zone_summary_csv"] = str(zones_summary_path)
    outputs["zones_n"] = int(len(zones))

    if export_kml and len(zones) > 0:
        zones_kml = vra_dir / "fertilizer_zones.kml"
        zones_kml_view = zones.copy()
        zones_kml_view["display_name"] = (
            zones_kml_view["relative_vigor_label"]
            .astype(str)
            .str.replace("_", " ", regex=False)
            .str.capitalize()
        )
        export_polygons_kml(
            zones_kml_view,
            zones_kml,
            name_field="display_name",
            description_fields=[
                "zone_id",
                "geometry_area_m2",
                "cluster_rank",
                "cluster_count",
                "ndvi_mean",
            ],
            description_labels={
                "zone_id": "Parent zone ID",
                "geometry_area_m2": "Parent zone total area (m²)",
                "cluster_rank": "Vigor rank",
                "cluster_count": "Zones selected",
                "ndvi_mean": "Parent zone mean NDVI",
            },
            document_name="management-zone patches",
            style_field="relative_vigor_label",
            default_style="management_zone",
            explode_multipolygon_parts=True,
            part_id_field="zone_id",
        )
        extra_outputs["fertilizer_zones_kml"] = str(zones_kml)

    if progress_callback:
        progress_callback("Resolving prescription rates", 74)
    prescription_zones, fertilizer_rate_summary = resolve_application_rate_plan(
        zones, resolved_rate_plan
    )
    rate_plan_json = summaries_dir / "fertilizer_rate_plan.json"
    _write_json(rate_plan_json, fertilizer_rate_summary)
    outputs["fertilizer_rate_plan_json"] = str(rate_plan_json)
    rate_table_path = vra_dir / "fertilizer_rate_table.csv"
    prescription_zones.drop(columns="geometry", errors="ignore").to_csv(
        rate_table_path, index=False
    )
    outputs["fertilizer_rate_table_csv"] = str(rate_table_path)
    prescription_geojson = rasters_dir / "fertilizer_prescription.geojson"
    prescription_zones.to_file(prescription_geojson, driver="GeoJSON")
    outputs["fertilizer_prescription_geojson"] = str(prescription_geojson)
    return (
        crop_qc_metrics,
        field_mask,
        finite_stack_mask,
        zones,
        fertilizer_rate_summary,
        prescription_zones,
        vra_dir,
        zoning_used_full_footprint_fallback,
    )


def _find_and_export_hotspots(
    *,
    ndvi_arr,
    transform,
    crs,
    hotspot_percentile,
    field_mask,
    crop_ndvi_threshold,
    hotspot_min_area_m2,
    hotspot_zscore_cutoff,
    finite_stack_mask,
    summaries_dir,
    outputs,
    warnings,
):
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
        _warn_vector_area_consistency("stress_hotspots", hotspots, warnings)
        cover_summary = canopy_cover_summary(
            ndvi_arr >= crop_ndvi_threshold, transform, valid_mask=finite_stack_mask
        )
        cover = cover_summary["overall_cover_pct"]
        cover_summary["metric_label"] = (
            "vegetation_cover_in_valid_multispectral_area_pct"
        )
        cover_summary["farmer_label"] = (
            "Vegetation cover in valid multispectral footprint (%)"
        )
        cover_summary["denominator"] = "finite_co_registered_multispectral_pixels"
        cover_summary["vegetation_rule"] = (
            "NDVI >= 0.20 inside valid multispectral footprint"
        )
        cover_summary["crop_mask_area_pct_of_valid"] = (
            100.0 * np.count_nonzero(field_mask) / np.count_nonzero(finite_stack_mask)
            if np.count_nonzero(finite_stack_mask)
            else np.nan
        )

        pd.DataFrame([cover_summary]).to_csv(
            summaries_dir / "canopy_cover_summary.csv",
            index=False,
        )
        outputs["canopy_cover_summary_csv"] = str(
            summaries_dir / "canopy_cover_summary.csv"
        )
    else:
        warnings.append(
            "NDVI was unavailable; stress/spray targets and canopy-cover summary were not generated."
        )
    return hotspots, cover


def _stage_controller_basemap(
    *,
    enabled,
    orthomosaic_path,
    out_dir,
    min_zoom,
    max_zoom,
    max_auto_zoom,
    tile_format,
    quality,
    max_source_pixels,
    max_output_bytes,
):
    if not enabled:
        return None, None
    staging_dir = Path(tempfile.mkdtemp(prefix="osw_bm_", dir=out_dir))
    try:
        result = export_orthomosaic_mbtiles(
            orthomosaic_path,
            staging_dir / "orthomosaic.mbtiles",
            min_zoom=min_zoom,
            max_zoom=max_zoom,
            max_auto_zoom=max_auto_zoom,
            tile_format=tile_format,
            quality=quality,
            max_source_pixels=max_source_pixels,
            max_output_bytes=max_output_bytes,
        )
        return result["mbtiles_path"], staging_dir
    except Exception as exc:
        logger.warning(
            "Basemap generation failed (controller ZIPs will have no basemap): %s",
            exc,
        )
        return None, staging_dir


def _prescription_export_decision(
    *,
    absolute_reflectance_valid,
    zoning_fallback,
    zone_count,
    export_requested,
    unsafe_override,
    warnings,
):
    qc_passed = absolute_reflectance_valid and not zoning_fallback and zone_count >= 2
    if export_requested and not qc_passed and not unsafe_override:
        warnings.append(
            "Machine prescription packages were not exported because analytical QC failed "
            f"(absolute_reflectance_valid={absolute_reflectance_valid}, "
            f"full_footprint_fallback={zoning_fallback}, zones={zone_count})."
        )
    if export_requested and unsafe_override and not qc_passed:
        warnings.append(
            "UNSAFE OVERRIDE: exporting machine packages despite failed analytical QC."
        )
    return qc_passed, export_requested and zone_count > 0 and (
        qc_passed or unsafe_override
    )


def _export_dji_agras_controller(
    *, prescription_zones, controller_dir, rate_unit, basemap_path,
    include_basemap, summaries_dir, outputs,
):
    package = export_dji_agras_prescription_zip(
        prescription_zones,
        controller_dir,
        rate_unit=rate_unit,
        basemap_mbtiles_path=basemap_path,
        include_basemap_in_archive=include_basemap,
    )
    outputs["controller_packages_dir"] = str(controller_dir)
    outputs["dji_agras_vra_zip"] = package["dji_agras_vra_zip"]
    validation_path = summaries_dir / "dji_agras_validation.json"
    _write_json(validation_path, package["dji_agras_vra_validation"])
    outputs["dji_agras_validation_json"] = str(validation_path)


def _spot_treated_area_hectares(spot_spray_gdf) -> float:
    from pyproj import CRS

    crs = CRS.from_user_input(spot_spray_gdf.crs)
    projected = spot_spray_gdf
    if not crs.is_projected:
        target_crs = spot_spray_gdf.estimate_utm_crs()
        if target_crs is None:
            raise ValueError("Could not determine a projected CRS for spot-spray area")
        projected = spot_spray_gdf.to_crs(target_crs)
    return float(projected.geometry.area.sum() / 10_000.0)


def _physical_spot_summary(
    *,
    spot_spray_gdf,
    target_rate,
    rate_unit,
    rate_plan,
    summaries_dir,
):
    treated_area_ha = _spot_treated_area_hectares(spot_spray_gdf)
    total_unit = "L" if "L" in rate_unit else ("kg" if "KG" in rate_unit else rate_unit)
    summary = {
        "mode": "physical",
        "operation": "spray",
        "product_name": rate_plan.product_name or "Spot Spray Chemical",
        "rate_basis": rate_plan.rate_basis or "product",
        "strategy": "target_hotspots",
        "unit": rate_unit,
        "target_rate": target_rate,
        "treated_area_ha": round(treated_area_ha, 4),
        "estimated_total_product": round(treated_area_ha * target_rate, 2),
        "total_product_unit": total_unit,
        "approved_by": rate_plan.approved_by or "Operator",
    }
    _write_json(summaries_dir / "spot_spray_rate_plan.json", summary)
    pd.DataFrame([summary]).to_csv(
        summaries_dir / "spot_spray_rate_table.csv", index=False
    )
    return summary


def _export_spot_controllers(
    *,
    hotspots,
    rate_plan,
    rasters_dir,
    prescriptions_dir,
    summaries_dir,
    basemap_path,
    include_basemap,
    outputs,
    extra_outputs,
):
    if hotspots is None or len(hotspots) == 0:
        return None
    _safe_to_file(hotspots, rasters_dir / "stress_hotspots.geojson", driver="GeoJSON")
    physical = rate_plan.mode == "physical"
    target_rate = (
        float(rate_plan.min_rate or rate_plan.max_rate or 100.0) if physical else 100.0
    )
    rate_unit = str(rate_plan.unit or "PCT").upper() if physical else "PCT"
    prescription = build_spot_spray_prescription_gdf(
        hotspots, target_rate=target_rate, background_rate=0.0, rate_unit=rate_unit
    )
    if len(prescription) == 0:
        return None

    _safe_to_file(
        prescription, rasters_dir / "spot_spray_targets.geojson", driver="GeoJSON"
    )
    spot_dir = _ensure_dir(prescriptions_dir / "spray_targets")
    spot_kml = spot_dir / "stress_patches.kml"
    export_polygons_kml(
        prescription,
        spot_kml,
        name_field="hotspot_id",
        description_fields=[
            "hotspot_id",
            "area_m2",
            "mean_ndvi",
            "severity_rank",
            "threshold_ndvi",
        ],
        description_labels={
            "hotspot_id": "Target ID",
            "area_m2": "Target area (m²)",
            "mean_ndvi": "Mean NDVI",
            "severity_rank": "Severity rank",
            "threshold_ndvi": "Selection threshold NDVI",
        },
        document_name="low-vigor scouting targets",
        severity_field="severity_rank",
    )
    extra_outputs["spot_spray_targets_kml"] = str(spot_kml)
    summary_path = spot_dir / "stress_patches.csv"
    prescription.drop(columns=["geometry"]).to_csv(summary_path, index=False)
    outputs["spot_spray_summary_csv"] = str(summary_path)

    summary = None
    if physical:
        summary = _physical_spot_summary(
            spot_spray_gdf=prescription,
            target_rate=target_rate,
            rate_unit=rate_unit,
            rate_plan=rate_plan,
            summaries_dir=summaries_dir,
        )
    controller_dir = spot_dir
    package = export_dji_agras_prescription_zip(
        prescription,
        controller_dir,
        rate_unit=rate_unit,
        basemap_mbtiles_path=basemap_path,
        include_basemap_in_archive=include_basemap,
    )
    outputs["spot_spray_controller_packages_dir"] = str(controller_dir)
    outputs["spot_spray_dji_agras_vra_zip"] = package["dji_agras_vra_zip"]
    validation_path = summaries_dir / "spot_spray_dji_agras_validation.json"
    _write_json(validation_path, package["dji_agras_vra_validation"])
    outputs["spot_spray_dji_agras_validation_json"] = str(validation_path)
    return summary


def _export_controller_packages(
    *,
    bundle_basemap_in_controller_archives,
    orthomosaic_path,
    out_dir,
    basemap_min_zoom,
    basemap_max_zoom,
    basemap_max_auto_zoom,
    basemap_tile_format,
    basemap_quality,
    basemap_max_source_pixels,
    basemap_max_output_bytes,
    absolute_reflectance_valid,
    zoning_used_full_footprint_fallback,
    zones,
    export_relative_application_packages,
    allow_unvalidated_prescription_export,
    vra_dir,
    prescription_zones,
    fertilizer_rate_summary,
    hotspots,
    resolved_spot_spray_plan,
    rasters_dir,
    prescriptions_dir,
    summaries_dir,
    outputs,
    extra_outputs,
    warnings,
    spot_spray_rate_summary=None,
):
    basemap_path, staging_dir = _stage_controller_basemap(
        enabled=bundle_basemap_in_controller_archives,
        orthomosaic_path=orthomosaic_path,
        out_dir=out_dir,
        min_zoom=basemap_min_zoom,
        max_zoom=basemap_max_zoom,
        max_auto_zoom=basemap_max_auto_zoom,
        tile_format=basemap_tile_format,
        quality=basemap_quality,
        max_source_pixels=basemap_max_source_pixels,
        max_output_bytes=basemap_max_output_bytes,
    )
    try:
        qc_passed, should_export = _prescription_export_decision(
            absolute_reflectance_valid=absolute_reflectance_valid,
            zoning_fallback=zoning_used_full_footprint_fallback,
            zone_count=len(zones),
            export_requested=export_relative_application_packages,
            unsafe_override=allow_unvalidated_prescription_export,
            warnings=warnings,
        )
        if should_export:
            _export_dji_agras_controller(
                prescription_zones=prescription_zones,
                controller_dir=vra_dir,
                rate_unit=fertilizer_rate_summary["unit"],
                basemap_path=basemap_path,
                include_basemap=bundle_basemap_in_controller_archives,
                summaries_dir=summaries_dir,
                outputs=outputs,
            )
            spot_spray_rate_summary = _export_spot_controllers(
                hotspots=hotspots,
                rate_plan=resolved_spot_spray_plan,
                rasters_dir=rasters_dir,
                prescriptions_dir=prescriptions_dir,
                summaries_dir=summaries_dir,
                basemap_path=basemap_path,
                include_basemap=bundle_basemap_in_controller_archives,
                outputs=outputs,
                extra_outputs=extra_outputs,
            )
        return qc_passed, spot_spray_rate_summary
    finally:
        if staging_dir is not None:
            shutil.rmtree(staging_dir, ignore_errors=True)


def _build_report_and_audit(
    *,
    feature_names,
    zones,
    cover,
    transform,
    field_mask,
    summaries_dir,
    outputs,
    extra_outputs,
    hotspots,
    out_dir,
    ortho_preview,
    absolute_reflectance_valid,
    zoning_used_full_footprint_fallback,
    fertilizer_rate_summary,
    spot_spray_rate_summary,
    band_audit,
    prescription_qc_passed,
    k_range,
    crop_ndvi_threshold,
    crop_qc_metrics,
    warnings,
    progress_callback,
):
    action_coverage_summary = None
    primary_index = feature_names[0]
    first_index_mean = f"{primary_index}_mean"
    if first_index_mean in zones.columns:
        valid_zones = zones.dropna(subset=[first_index_mean])
        if len(valid_zones):
            worst = valid_zones.sort_values(by=first_index_mean).iloc[0]
            best = valid_zones.sort_values(by=first_index_mean).iloc[-1]
            extra_outputs["lowest_index_zone_id"] = int(worst.zone_id)
            extra_outputs["highest_index_zone_id"] = int(best.zone_id)

    recommended_action_zone_pct = 0.0
    if cover is not None and cover > 0:
        total_zone_area = float(zones["geometry_area_m2"].sum()) if len(zones) else 0.0
        pixel_area_m2 = abs(transform.a * transform.e - transform.b * transform.d)
        usable_area_m2 = float(np.count_nonzero(field_mask)) * pixel_area_m2
        recommended_action_zone_pct = (
            (total_zone_area / usable_area_m2 * 100.0) if usable_area_m2 > 0 else 0.0
        )

        action_coverage_summary = {
            "metric_label": "recommended_action_zone_coverage_pct",
            "overall_cover_pct": cover,
            "usable_field_area_m2": usable_area_m2,
            "recommended_action_zone_area_m2": total_zone_area,
            "recommended_action_zone_pct_of_usable_area": recommended_action_zone_pct,
            "usable_area_not_in_action_zones_m2": max(
                0.0, usable_area_m2 - total_zone_area
            ),
            "action_zone_selection_rule": "Minimum contiguous zone size filter applied; small noise patches excluded for machine operations.",
        }
        pd.DataFrame([action_coverage_summary]).to_csv(
            summaries_dir / "action_zone_coverage_summary.csv",
            index=False,
        )
        outputs["action_zone_coverage_summary_csv"] = str(
            summaries_dir / "action_zone_coverage_summary.csv"
        )

    if recommended_action_zone_pct < 95.0:
        warnings.append(
            f"Recommended action zones cover {recommended_action_zone_pct:.1f}% of the usable field area; "
            "small or fragmented areas may have been excluded to keep exported files practical for equipment and field use."
        )

    zone_rows = [["Zone", "Priority", "Area m²", "Crop score"]]
    if zones is not None and len(zones):
        for _, r in zones.iterrows():
            mean_ndvi = r.get("ndvi_mean", None)

            raw_label = str(
                r.get("relative_vigor_label", getattr(r, "vigor_label", r.zone_id))
            )
            label_map = {
                "lowest_relative_vigor": "Lowest relative vigor",
                "lower_relative_vigor": "Lower relative vigor",
                "typical_relative_vigor": "Typical relative vigor",
                "higher_relative_vigor": "Higher relative vigor",
                "highest_relative_vigor": "Highest relative vigor",
                "critical": "High priority",
                "watch": "Watch",
                "moderate": "Moderate",
                "healthy": "Healthy",
                "very_healthy": "Very healthy",
            }
            farmer_label = label_map.get(raw_label, raw_label.replace("_", " ").title())

            zone_rows.append(
                [
                    int(r.zone_id),
                    farmer_label,
                    f"{float(r.area_m2):.0f}",
                    "" if mean_ndvi is None else f"{float(mean_ndvi):.3f}",
                ]
            )
    hotspot_rows = None
    if hotspots is not None and len(hotspots):
        hotspot_rows = [["Target", "Area m²", "Mean NDVI", "Severity rank"]]
        for _, r in hotspots.head(10).iterrows():
            hotspot_rows.append(
                [
                    int(r.hotspot_id),
                    f"{r.area_m2:.0f}",
                    f"{r.mean_ndvi:.3f}",
                    int(r.severity_rank),
                ]
            )

    pdf_path = out_dir / "spray_report.pdf"
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
        progress_callback("Generating reports", 92)
    build_agriculture_pdf(
        pdf_path,
        ortho_preview=ortho_preview,
        ndvi_preview=outputs.get("ndvi_preview", ortho_preview),
        canopy_cover_pct=cover,
        zone_table=zone_rows if len(zone_rows) > 1 else None,
        hotspot_table=hotspot_rows,
        coverage_metrics=action_coverage_summary,
        fertilizer_rate_summary=fertilizer_rate_summary,
        spot_spray_rate_summary=spot_spray_rate_summary,
        disclaimer=custom_disclaimer,
    )
    outputs["pdf_report"] = str(pdf_path)

    export_guides(out_dir)

    input_audit_path = summaries_dir / "input_band_audit.json"
    _write_json(input_audit_path, band_audit)
    outputs["input_band_audit_json"] = str(input_audit_path)
    methodology = {
        "pipeline_version": "3.2.0-band-safety-patch",
        "band_audit": band_audit,
        "absolute_reflectance_valid": absolute_reflectance_valid,
        "zoning_used_full_footprint_fallback": zoning_used_full_footprint_fallback,
        "prescription_qc_passed": prescription_qc_passed,
        "zoning_k_range": list(k_range),
        "crop_mask_ndvi_threshold": crop_ndvi_threshold,
        "crop_qc_metrics": crop_qc_metrics,
        "fertilizer_rate_plan": fertilizer_rate_summary,
        "safety_notice": "Outputs are for agronomic planning and decision support only. Ground-truth before application.",
    }
    analytics_methodology_path = summaries_dir / "analytics_methodology.json"
    export_analytics_methodology(analytics_methodology_path, domain="agriculture")
    _write_json(summaries_dir / "pipeline_methodology.json", methodology)
    outputs["analytics_methodology_json"] = str(analytics_methodology_path)
    outputs["pipeline_methodology_json"] = str(
        summaries_dir / "pipeline_methodology.json"
    )
    processing_warnings_path = summaries_dir / "processing_warnings.json"
    _write_json(processing_warnings_path, {"warnings": warnings})
    outputs["processing_warnings_json"] = str(processing_warnings_path)
    outputs["warnings"] = list(warnings)

    outputs.update(extra_outputs)
    return outputs


@dataclass
class _RunSetup:
    out_dir: Path
    prescriptions_dir: Path
    rasters_dir: Path
    previews_dir: Path
    summaries_dir: Path
    warnings: list[str]
    outputs: dict
    extra_outputs: dict
    fertilizer_plan: ApplicationRatePlan
    spot_spray_plan: ApplicationRatePlan
    bundle_basemap: bool


def _prepare_pipeline_run(
    *,
    out_dir,
    orthomosaic_path,
    multispectral_path,
    multispectral_band_map,
    red_path,
    nir_path,
    red_edge_path,
    green_path,
    blue_path,
    fertilizer_rate_plan,
    spot_spray_rate_plan,
    export_offline_basemap,
    bundle_basemap_in_controller_archives,
    export_relative_application_packages,
    progress_callback,
):
    out_dir = _ensure_dir(out_dir)
    if progress_callback:
        progress_callback("Preparing agriculture analysis", 25)
    warnings: list[str] = []
    fertilizer_plan = ApplicationRatePlan.from_value(fertilizer_rate_plan)
    fertilizer_plan.validate()
    spot_spray_plan = ApplicationRatePlan.from_value(spot_spray_rate_plan)
    spot_spray_plan.validate()
    logger.info(
        "Application-rate mode: fertilizer=%s, spot_spray=%s",
        fertilizer_plan.mode,
        spot_spray_plan.mode,
    )

    separate_paths = [red_path, nir_path, red_edge_path, green_path, blue_path]
    if multispectral_path is not None and any(
        path is not None for path in separate_paths
    ):
        raise ValueError(
            "Use either multispectral_path or separate spectral band paths, not both"
        )
    if multispectral_band_map is not None and multispectral_path is None:
        raise ValueError("multispectral_band_map requires multispectral_path")
    if export_offline_basemap or bundle_basemap_in_controller_archives:
        if orthomosaic_path is None or not Path(orthomosaic_path).exists():
            warnings.append(
                "orthomosaic_path is missing or does not exist; offline basemap MBTiles generation skipped."
            )
            bundle_basemap_in_controller_archives = False
    if (
        bundle_basemap_in_controller_archives
        and not export_relative_application_packages
    ):
        warnings.append(
            "bundle_basemap_in_controller_archives was requested without "
            "export_relative_application_packages; disabling basemap bundling."
        )
        bundle_basemap_in_controller_archives = False
    if red_path is not None and nir_path is not None:
        if Path(red_path).resolve() == Path(nir_path).resolve():
            raise ValueError(
                "red_path and nir_path must refer to different spectral bands"
            )

    return _RunSetup(
        out_dir=out_dir,
        prescriptions_dir=_ensure_dir(out_dir / "prescriptions"),
        rasters_dir=_ensure_dir(out_dir / "technical_gis" / "rasters"),
        previews_dir=_ensure_dir(out_dir / "technical_gis" / "previews"),
        summaries_dir=_ensure_dir(out_dir / "technical_gis" / "data_summaries"),
        warnings=warnings,
        outputs={},
        extra_outputs={},
        fertilizer_plan=fertilizer_plan,
        spot_spray_plan=spot_spray_plan,
        bundle_basemap=bundle_basemap_in_controller_archives,
    )


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
    """Run the staged agriculture analysis and return its deliverable manifest."""
    setup = _prepare_pipeline_run(
        out_dir=out_dir,
        orthomosaic_path=orthomosaic_path,
        multispectral_path=multispectral_path,
        multispectral_band_map=multispectral_band_map,
        red_path=red_path,
        nir_path=nir_path,
        red_edge_path=red_edge_path,
        green_path=green_path,
        blue_path=blue_path,
        fertilizer_rate_plan=fertilizer_rate_plan,
        spot_spray_rate_plan=spot_spray_rate_plan,
        export_offline_basemap=export_offline_basemap,
        bundle_basemap_in_controller_archives=bundle_basemap_in_controller_archives,
        export_relative_application_packages=export_relative_application_packages,
        progress_callback=progress_callback,
    )
    out_dir = setup.out_dir
    prescriptions_dir = setup.prescriptions_dir
    rasters_dir = setup.rasters_dir
    previews_dir = setup.previews_dir
    summaries_dir = setup.summaries_dir
    warnings = setup.warnings
    outputs = setup.outputs
    extra_outputs = setup.extra_outputs
    resolved_rate_plan = setup.fertilizer_plan
    resolved_spot_spray_plan = setup.spot_spray_plan
    bundle_basemap_in_controller_archives = setup.bundle_basemap

    arrays, masks, profile, embedded_mask, band_audit = _load_spectral_inputs(
        multispectral_path=multispectral_path,
        multispectral_band_map=multispectral_band_map,
        max_agriculture_pixels=max_agriculture_pixels,
        red_path=red_path,
        nir_path=nir_path,
        red_edge_path=red_edge_path,
        green_path=green_path,
        blue_path=blue_path,
        warnings=warnings,
    )
    common_mask, absolute_valid = _apply_footprint_and_validate(
        arrays=arrays,
        masks=masks,
        profile=profile,
        embedded_footprint_mask=embedded_mask,
        footprint_mask_path=footprint_mask_path,
        reflectance_scale=reflectance_scale,
        warnings=warnings,
        band_audit=band_audit,
    )
    arrays, common_mask, profile, transform, crs = _project_to_metric_crs(
        arrays=arrays,
        common_valid_mask=common_mask,
        profile=profile,
        warnings=warnings,
    )
    features, feature_names, ndvi_arr, msavi2_arr = _compute_and_export_indices(
        arrays=arrays,
        common_valid_mask=common_mask,
        absolute_reflectance_valid=absolute_valid,
        profile=profile,
        rasters_dir=rasters_dir,
        previews_dir=previews_dir,
        outputs=outputs,
        warnings=warnings,
    )
    (
        crop_qc_metrics,
        field_mask,
        finite_stack_mask,
        zones,
        fertilizer_rate_summary,
        prescription_zones,
        vra_dir,
        zoning_fallback,
    ) = _build_and_export_zones(
        features_stack=features,
        feature_names=feature_names,
        ndvi_arr=ndvi_arr,
        msavi2_arr=msavi2_arr,
        arrays=arrays,
        absolute_reflectance_valid=absolute_valid,
        transform=transform,
        crs=crs,
        profile=profile,
        k_range=k_range,
        crop_ndvi_threshold=crop_ndvi_threshold,
        crop_msavi2_threshold=crop_msavi2_threshold,
        nir_shadow_threshold=nir_shadow_threshold,
        min_crop_component_m2=min_crop_component_m2,
        min_non_crop_component_m2=min_non_crop_component_m2,
        edge_buffer_m=edge_buffer_m,
        export_kml=export_kml,
        prescriptions_dir=prescriptions_dir,
        rasters_dir=rasters_dir,
        summaries_dir=summaries_dir,
        outputs=outputs,
        extra_outputs=extra_outputs,
        warnings=warnings,
        resolved_rate_plan=resolved_rate_plan,
        progress_callback=progress_callback,
    )
    hotspots, cover = _find_and_export_hotspots(
        ndvi_arr=ndvi_arr,
        transform=transform,
        crs=crs,
        hotspot_percentile=hotspot_percentile,
        field_mask=field_mask,
        crop_ndvi_threshold=crop_ndvi_threshold,
        hotspot_min_area_m2=hotspot_min_area_m2,
        hotspot_zscore_cutoff=hotspot_zscore_cutoff,
        finite_stack_mask=finite_stack_mask,
        summaries_dir=summaries_dir,
        outputs=outputs,
        warnings=warnings,
    )
    prescription_qc_passed, spot_summary = _export_controller_packages(
        bundle_basemap_in_controller_archives=bundle_basemap_in_controller_archives,
        orthomosaic_path=orthomosaic_path,
        out_dir=out_dir,
        basemap_min_zoom=basemap_min_zoom,
        basemap_max_zoom=basemap_max_zoom,
        basemap_max_auto_zoom=basemap_max_auto_zoom,
        basemap_tile_format=basemap_tile_format,
        basemap_quality=basemap_quality,
        basemap_max_source_pixels=basemap_max_source_pixels,
        basemap_max_output_bytes=basemap_max_output_bytes,
        absolute_reflectance_valid=absolute_valid,
        zoning_used_full_footprint_fallback=zoning_fallback,
        zones=zones,
        export_relative_application_packages=export_relative_application_packages,
        allow_unvalidated_prescription_export=allow_unvalidated_prescription_export,
        vra_dir=vra_dir,
        prescription_zones=prescription_zones,
        fertilizer_rate_summary=fertilizer_rate_summary,
        hotspots=hotspots,
        resolved_spot_spray_plan=resolved_spot_spray_plan,
        rasters_dir=rasters_dir,
        prescriptions_dir=prescriptions_dir,
        summaries_dir=summaries_dir,
        outputs=outputs,
        extra_outputs=extra_outputs,
        warnings=warnings,
    )
    return _build_report_and_audit(
        feature_names=feature_names,
        zones=zones,
        cover=cover,
        transform=transform,
        field_mask=field_mask,
        summaries_dir=summaries_dir,
        outputs=outputs,
        extra_outputs=extra_outputs,
        hotspots=hotspots,
        out_dir=out_dir,
        ortho_preview=ortho_preview,
        absolute_reflectance_valid=absolute_valid,
        zoning_used_full_footprint_fallback=zoning_fallback,
        fertilizer_rate_summary=fertilizer_rate_summary,
        spot_spray_rate_summary=spot_summary,
        band_audit=band_audit,
        prescription_qc_passed=prescription_qc_passed,
        k_range=k_range,
        crop_ndvi_threshold=crop_ndvi_threshold,
        crop_qc_metrics=crop_qc_metrics,
        warnings=warnings,
        progress_callback=progress_callback,
    )
