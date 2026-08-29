"""Deterministic agriculture prescription and controller exports.

The module converts management zones and stress-hotspot polygons into auditable
relative or operator-supplied physical-rate prescriptions. It does not infer an
agronomic dose from imagery. Vendor packages remain structural exports that
require operator verification on the target display.
"""
from __future__ import annotations

import json
import logging
import math
import tempfile
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal, Mapping, Optional

import geopandas as gpd
import numpy as np
import pandas as pd
import rasterio
from affine import Affine
from rasterio import features
from shapely.geometry import mapping
from shapely.ops import unary_union

from .basemaps import copy_basemap_companion_files
from .exports import export_polygons_kml

logger = logging.getLogger(__name__)

# General helpers




def _non_empty_polygons(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    if gdf is None:
        raise ValueError("GeoDataFrame cannot be None")
    if gdf.crs is None:
        raise ValueError("GeoDataFrame must have a CRS")
    if len(gdf) == 0:
        return gdf.copy()
    out = gdf.copy()
    out = out[out.geometry.notna() & ~out.geometry.is_empty].copy()
    if len(out) == 0:
        return out
    out["geometry"] = out.geometry.apply(lambda g: g if g.is_valid else g.buffer(0))
    out = out[out.geometry.notna() & ~out.geometry.is_empty].copy()
    return out








def _write_json(path: str | Path, obj: dict) -> Path:
    p = Path(path)
    p.write_text(json.dumps(obj, indent=2, sort_keys=True), encoding="utf-8")
    return p


def _ensure_parent(path: str | Path) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _safe_extract_zip(
    zf: zipfile.ZipFile,
    destination: Path,
    *,
    max_members: int = 256,
    max_total_uncompressed: int = 512 * 1024 * 1024,
    max_member_uncompressed: int = 256 * 1024 * 1024,
    max_compression_ratio: float = 1000.0,
) -> None:
    """Extract a bounded archive only when every regular member is safe."""
    import stat
    infos = zf.infolist()
    if len(infos) > max_members:
        raise ValueError(f"Unsafe ZIP member count: {len(infos)} > {max_members}")
    root = destination.resolve()
    seen: set[str] = set()
    total = 0
    for info in infos:
        name = info.filename.replace("\\", "/")
        key = name.casefold()
        if key in seen:
            raise ValueError(f"Unsafe duplicate ZIP member: {info.filename!r}")
        seen.add(key)
        mode = info.external_attr >> 16
        file_type = stat.S_IFMT(mode)
        if file_type == stat.S_IFLNK:
            raise ValueError(f"Unsafe ZIP symlink member: {info.filename!r}")
        # Some ZIP writers store permission bits without a Unix file type.
        if file_type not in (0, stat.S_IFREG, stat.S_IFDIR):
            raise ValueError(f"Unsafe ZIP non-regular member: {info.filename!r}")
        if name.startswith("/") or (len(name) >= 2 and name[1] == ":"):
            raise ValueError(f"Unsafe ZIP member path: {info.filename!r}")
        candidate = (root / name).resolve()
        try:
            candidate.relative_to(root)
        except ValueError as exc:
            raise ValueError(f"Unsafe ZIP member path: {info.filename!r}") from exc
        if info.file_size > max_member_uncompressed:
            raise ValueError(f"Unsafe ZIP member size: {info.filename!r}")
        total += info.file_size
        if total > max_total_uncompressed:
            raise ValueError("Unsafe ZIP total uncompressed size")
        if info.compress_size == 0:
            if info.file_size > 0:
                raise ValueError(f"Unsafe ZIP compression ratio: {info.filename!r}")
        elif info.file_size / info.compress_size > max_compression_ratio:
            raise ValueError(f"Unsafe ZIP compression ratio: {info.filename!r}")
    root.mkdir(parents=True, exist_ok=True)
    actual_total = 0
    chunk_size = 1024 * 1024
    for info in infos:
        name = info.filename.replace("\\", "/")
        target = (root / name).resolve()
        if info.is_dir() or name.endswith("/"):
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        member_total = 0
        try:
            with zf.open(info, "r") as source, target.open("wb") as destination_file:
                while True:
                    chunk = source.read(chunk_size)
                    if not chunk:
                        break
                    member_total += len(chunk)
                    actual_total += len(chunk)
                    if member_total > max_member_uncompressed:
                        raise ValueError(f"Unsafe ZIP member size: {info.filename!r}")
                    if actual_total > max_total_uncompressed:
                        raise ValueError("Unsafe ZIP total uncompressed size")
                    destination_file.write(chunk)
        except Exception:
            target.unlink(missing_ok=True)
            raise


# Operator-supplied physical application-rate plans

_ALLOWED_RATE_UNITS = {"KG_HA", "L_HA", "LB_AC", "GAL_AC", "SEEDS_HA"}
_ALLOWED_RATE_BASES = {"product", "active_ingredient", "nutrient"}
_ALLOWED_OPERATIONS = {"fertilizer", "spray", "spread", "seeding"}


@dataclass(frozen=True)
class ApplicationRatePlan:
    """Validated operator/agronomist input for a physical prescription.

    This object records a rate decision; it does not derive an agronomic dose
    from imagery. ``zone_rates`` maps the source management-zone ``zone_id`` to
    an approved physical rate. Alternatively, ``min_rate``/``max_rate`` scale
    the imagery-derived response after the operator explicitly chooses
    ``direct`` or ``inverse``.
    """

    mode: Literal["relative", "physical"] = "relative"
    operation: Optional[Literal["fertilizer", "spray", "spread", "seeding"]] = None
    product_name: Optional[str] = None
    rate_basis: Optional[Literal["product", "active_ingredient", "nutrient"]] = None
    unit: Optional[str] = None
    strategy: Literal["direct", "inverse", "explicit_by_zone"] = "inverse"
    min_rate: Optional[float] = None
    max_rate: Optional[float] = None
    zone_rates: Optional[Mapping[int, float]] = None
    equipment_min_rate: Optional[float] = None
    equipment_max_rate: Optional[float] = None
    approved_by: Optional[str] = None

    @classmethod
    def from_value(cls, value: "ApplicationRatePlan | Mapping[str, Any] | None") -> "ApplicationRatePlan":
        if value is None:
            return cls()
        if isinstance(value, cls):
            return value
        if isinstance(value, Mapping):
            return cls(**dict(value))
        raise TypeError("application rate plan must be ApplicationRatePlan, a mapping, or None")

    def validate(self) -> None:
        if self.mode not in {"relative", "physical"}:
            raise ValueError("rate plan mode must be 'relative' or 'physical'")
        if self.strategy not in {"direct", "inverse", "explicit_by_zone", "target_hotspots"}:
            raise ValueError("rate plan strategy must be direct, inverse, explicit_by_zone, or target_hotspots")
        if self.mode == "relative":
            physical_values = [self.operation, self.product_name, self.rate_basis, self.unit,
                               self.min_rate, self.max_rate, self.zone_rates,
                               self.equipment_min_rate, self.equipment_max_rate, self.approved_by]
            if any(value is not None for value in physical_values):
                raise ValueError("relative rate plans cannot contain physical-rate fields")
            if self.strategy == "explicit_by_zone":
                raise ValueError("relative rate plans cannot use explicit_by_zone")
            return
        if self.operation not in _ALLOWED_OPERATIONS:
            raise ValueError(f"physical rate plan operation must be one of {sorted(_ALLOWED_OPERATIONS)}")
        if not str(self.product_name or "").strip():
            raise ValueError("physical rate plan requires product_name")
        if self.rate_basis not in _ALLOWED_RATE_BASES:
            raise ValueError(f"physical rate plan rate_basis must be one of {sorted(_ALLOWED_RATE_BASES)}")
        unit = str(self.unit or "").upper()
        if unit not in _ALLOWED_RATE_UNITS:
            raise ValueError(f"physical rate plan unit must be one of {sorted(_ALLOWED_RATE_UNITS)}")
        if not str(self.approved_by or "").strip():
            raise ValueError("physical rate plan requires approved_by")
        bounds = (("equipment_min_rate", self.equipment_min_rate),
                  ("equipment_max_rate", self.equipment_max_rate))
        for name, value in bounds:
            if value is not None and (not np.isfinite(float(value)) or float(value) < 0):
                raise ValueError(f"{name} must be finite and non-negative")
        if (self.equipment_min_rate is not None and self.equipment_max_rate is not None
                and float(self.equipment_max_rate) < float(self.equipment_min_rate)):
            raise ValueError("equipment_max_rate must be >= equipment_min_rate")
        if self.strategy == "explicit_by_zone":
            if not self.zone_rates:
                raise ValueError("explicit_by_zone requires a non-empty zone_rates mapping")
            if self.min_rate is not None or self.max_rate is not None:
                raise ValueError("explicit_by_zone cannot also specify min_rate/max_rate")
            for zone_id, rate in self.zone_rates.items():
                try:
                    zone_number = float(zone_id)
                except (TypeError, ValueError) as exc:
                    raise ValueError("zone_rates keys must be integer zone IDs") from exc
                if not np.isfinite(zone_number) or not np.isclose(zone_number, round(zone_number)):
                    raise ValueError("zone_rates keys must be integer zone IDs")
                if not np.isfinite(float(rate)) or float(rate) < 0:
                    raise ValueError("zone_rates values must be finite and non-negative")
        else:
            if self.zone_rates is not None:
                raise ValueError("zone_rates requires strategy='explicit_by_zone'")
            if self.min_rate is None or self.max_rate is None:
                raise ValueError("direct/inverse physical plans require min_rate and max_rate")
            for name, value in (("min_rate", self.min_rate), ("max_rate", self.max_rate)):
                if not np.isfinite(float(value)) or float(value) < 0:
                    raise ValueError(f"{name} must be finite and non-negative")
            if float(self.max_rate) < float(self.min_rate):
                raise ValueError("max_rate must be >= min_rate")


def _zone_id_series(zones: gpd.GeoDataFrame) -> pd.Series:
    field = "zone_id" if "zone_id" in zones.columns else "Zone_ID" if "Zone_ID" in zones.columns else None
    if field is None:
        raise ValueError("explicit zone rates require zone_id or Zone_ID in management zones")
    values = pd.to_numeric(zones[field], errors="coerce")
    if values.isna().any() or not np.isclose(values, np.round(values)).all() or values.duplicated().any():
        raise ValueError(f"{field} must contain unique integer values")
    return values.astype(int)


def _area_hectares(gdf: gpd.GeoDataFrame) -> np.ndarray:
    from pyproj import CRS as _CRS
    from pyproj import Geod
    resolved = _CRS.from_user_input(gdf.crs)
    if resolved.is_projected:
        factors = [float(axis.unit_conversion_factor) for axis in resolved.axis_info[:2]]
        if len(factors) >= 2 and np.isclose(factors[0], factors[1]):
            return gdf.geometry.area.to_numpy(dtype=float) * factors[0] * factors[1] / 10_000.0
    wgs = gdf.to_crs("EPSG:4326") if resolved.to_epsg() != 4326 else gdf
    geod = Geod(ellps="WGS84")
    return np.asarray([abs(geod.geometry_area_perimeter(geom)[0]) / 10_000.0 for geom in wgs.geometry])


def resolve_application_rate_plan(
    zones: gpd.GeoDataFrame,
    plan: "ApplicationRatePlan | Mapping[str, Any] | None" = None,
) -> tuple[gpd.GeoDataFrame, dict]:
    """Resolve a relative or operator-approved physical plan onto zone polygons."""
    cfg = ApplicationRatePlan.from_value(plan)
    cfg.validate()
    if cfg.mode == "relative":
        rx = build_vra_prescription_gdf(zones, response=cfg.strategy)
    elif cfg.strategy == "explicit_by_zone":
        rx = build_vra_prescription_gdf(zones, response="inverse")
        zone_ids = _zone_id_series(zones)
        supplied = {int(k): float(v) for k, v in cfg.zone_rates.items()}
        missing = sorted(set(zone_ids.tolist()) - set(supplied))
        extra = sorted(set(supplied) - set(zone_ids.tolist()))
        if missing or extra:
            raise ValueError(f"zone_rates must exactly match source zone IDs; missing={missing}, extra={extra}")
        rx["Zone_ID"] = zone_ids.to_numpy()
        rx["TargetRate"] = zone_ids.map(supplied).to_numpy(dtype=float)
        rx["Rate_Unit"] = str(cfg.unit).upper()
        rx["Cmd_Mode"] = "physical_product_rate"
        rx["Rx_Mode"] = "explicit"
    else:
        rx = build_vra_prescription_gdf(
            zones, min_rate=cfg.min_rate, max_rate=cfg.max_rate,
            rate_unit=str(cfg.unit).upper(), response=cfg.strategy,
        )
    rates = pd.to_numeric(rx["TargetRate"], errors="coerce").to_numpy(dtype=float)
    if cfg.mode == "physical":
        if cfg.equipment_min_rate is not None and np.any(rates < float(cfg.equipment_min_rate)):
            raise ValueError("one or more rates are below equipment_min_rate")
        if cfg.equipment_max_rate is not None and np.any(rates > float(cfg.equipment_max_rate)):
            raise ValueError("one or more rates exceed equipment_max_rate")
    area_ha = _area_hectares(rx)
    plan_payload = asdict(cfg)
    if plan_payload.get("zone_rates") is not None:
        plan_payload["zone_rates"] = {
            str(int(k)): float(v) for k, v in plan_payload["zone_rates"].items()
        }
    summary = {
        "schema": "orthoswift.application_rate_plan.v1",
        **plan_payload,
        "unit": "PCT" if cfg.mode == "relative" else str(cfg.unit).upper(),
        "zone_count": int(len(rx)),
        "resolved_min_rate": float(np.min(rates)) if len(rates) else None,
        "resolved_max_rate": float(np.max(rates)) if len(rates) else None,
        "treated_area_ha": float(np.sum(area_ha)),
        "estimated_total_product": (float(np.sum(rates * area_ha)) if cfg.mode == "physical" and str(cfg.unit).upper() in {"KG_HA", "L_HA", "SEEDS_HA"} else None),
        "total_product_unit": ({"KG_HA": "KG", "L_HA": "L", "SEEDS_HA": "SEEDS"}.get(str(cfg.unit).upper()) if cfg.mode == "physical" else None),
        "operator_review_required": True,
        "agronomic_origin": "operator_or_agronomist_supplied" if cfg.mode == "physical" else "imagery_relative_intensity",
        "limitation": "OrthoSWIFT spatially applies supplied rates; it does not infer an agronomic dose from imagery.",
    }
    return _validate_prebuilt_prescription_gdf(rx), summary


# Agriculture: VRA prescription shapefile ZIP


def _pick_index_field(zones: gpd.GeoDataFrame, preferred: Optional[str] = None) -> str:
    if preferred:
        if preferred not in zones.columns:
            raise ValueError(f"index_field '{preferred}' not found in zones")
        return preferred
    for name in ("ndvi_mean", "msavi2_mean", "ndre_mean", "band0_mean", "mean_ndvi", "NDVI", "ndvi"):
        if name in zones.columns:
            return name
    raise ValueError(
        "No recognized vegetation-index field found for VRA rate assignment; "
        "supply index_field explicitly"
    )


def build_vra_prescription_gdf(
    zones: gpd.GeoDataFrame,
    *,
    min_rate: Optional[float] = None,
    max_rate: Optional[float] = None,
    base_rate: Optional[float] = None,
    rate_unit: str = "PCT",
    index_field: Optional[str] = None,
    response: Literal["inverse", "direct"] = "inverse",
    decimals: int = 2,
) -> gpd.GeoDataFrame:
    """Attach deterministic prescription attributes to management-zone polygons.

    Parameters
    ----------
    zones:
        Management-zone polygons with a vegetation-index mean field.
    min_rate, max_rate:
        Product-specific lower and upper target rates. Required for a true
        machine-ready target-rate file. If omitted, ``TargetRate`` is left null
        and only ``Rx_Index`` is written.
    base_rate:
        Deprecated compatibility parameter. A base rate alone is refused;
        operational physical prescriptions require explicit min/max bounds or
        exact per-zone rates in :class:`ApplicationRatePlan`.
    response:
        ``inverse`` assigns higher rates to lower index values, commonly used
        for stress-compensation spot treatment. ``direct`` assigns higher rates
        to higher index values.
    """
    if response not in {"inverse", "direct"}:
        raise ValueError("response must be 'inverse' or 'direct'")
    if not isinstance(decimals, (int, np.integer)) or decimals < 0:
        raise ValueError("decimals must be a non-negative integer")

    gdf = _non_empty_polygons(zones)
    out = gdf.copy()
    if len(out) and not out.geom_type.isin(["Polygon", "MultiPolygon"]).all():
        raise ValueError("VRA zones must contain only Polygon/MultiPolygon geometry")
    if len(out) > 1:
        try:
            pairs = out.sindex.query(out.geometry, predicate="intersects")
            for left, right in zip(pairs[0], pairs[1]):
                if left < right and out.geometry.iloc[left].intersection(out.geometry.iloc[right]).area > 0:
                    raise ValueError("VRA zone polygons overlap and create ambiguous rates")
        except ImportError:
            for left in range(len(out)):
                for right in range(left + 1, len(out)):
                    if out.geometry.iloc[left].intersection(out.geometry.iloc[right]).area > 0:
                        raise ValueError("VRA zone polygons overlap and create ambiguous rates")
    if len(out) == 0:
        # Preserve expected DBF/CSV schema even when no management zones exist.
        for col in ["Zone_ID", "Rx_Index", "TargetRate", "Rate_Unit", "Rx_Mode"]:
            out[col] = []
        return out

    idx_field = _pick_index_field(out, index_field)
    vals = pd.to_numeric(out[idx_field], errors="coerce").astype(float).to_numpy()
    finite = np.isfinite(vals)
    if finite.sum() == 0:
        raise ValueError(f"Vegetation-index field '{idx_field}' contains no finite values")
    if not finite.all():
        raise ValueError(
            f"Vegetation-index field '{idx_field}' must contain finite values for every zone"
        )
    vmin = float(np.nanmin(vals[finite]))
    vmax = float(np.nanmax(vals[finite]))
    if math.isclose(vmin, vmax):
        rx_index = np.full_like(vals, 0.5, dtype="float64")
    else:
        x = (vals - vmin) / (vmax - vmin)
        rx_index = 1.0 - x if response == "inverse" else x
    rx_index = np.clip(rx_index, 0.0, 1.0)

    if base_rate is not None and not np.isfinite(float(base_rate)):
        raise ValueError("base_rate must be finite")
    if min_rate is None and max_rate is None and base_rate is not None:
        raise ValueError(
            "base_rate alone is not an approved physical prescription; supply explicit min_rate/max_rate "
            "or use ApplicationRatePlan(strategy='explicit_by_zone')"
        )

    if (min_rate is None) ^ (max_rate is None):
        raise ValueError("min_rate and max_rate must be supplied together")
    if min_rate is not None and max_rate is not None:
        min_rate = float(min_rate)
        max_rate = float(max_rate)
        if not np.isfinite(min_rate) or not np.isfinite(max_rate):
            raise ValueError("rates must be finite")
        if min_rate < 0 or max_rate < 0:
            raise ValueError("rates must be >= 0")
        if max_rate < min_rate:
            raise ValueError("max_rate must be >= min_rate")
        target = min_rate + rx_index * (max_rate - min_rate)
        target = np.round(target, decimals)
        command_mode = "physical_product_rate"
        output_unit = str(rate_unit).upper()
    else:
        # Zero-extra-input mode: derive a machine-filled normalized intensity
        # command from photogrammetry only. This is NOT blank and NOT waiting
        # for agronomic inputs.
        target = np.round(rx_index * 100.0, decimals)
        command_mode = "normalized_percent"
        output_unit = "PCT"

    if "zone_id" in out.columns:
        zone_series = pd.to_numeric(out["zone_id"], errors="coerce")
        if zone_series.isna().any() or not np.all(np.isclose(zone_series, np.round(zone_series))):
            raise ValueError("zone_id must contain integer, non-null values")
        zone_id = zone_series.astype(int)
    elif "Zone_ID" in out.columns:
        zone_series = pd.to_numeric(out["Zone_ID"], errors="coerce")
        if zone_series.isna().any() or not np.all(np.isclose(zone_series, np.round(zone_series))):
            raise ValueError("Zone_ID must contain integer, non-null values")
        zone_id = zone_series.astype(int)
    else:
        zone_id = np.arange(1, len(out) + 1, dtype=int)
    if pd.Series(zone_id).duplicated().any():
        raise ValueError("Zone_ID values must be unique")

    out["Zone_ID"] = zone_id
    out["Rx_Index"] = np.round(rx_index, 4)
    out["TargetRate"] = target
    out["Rate_Unit"] = output_unit
    out["Cmd_Mode"] = command_mode
    out["Rx_Mode"] = response
    out["IndexFld"] = idx_field[:10]
    return out


def build_spot_spray_prescription_gdf(
    hotspots: gpd.GeoDataFrame,
    *,
    field_aoi_gdf: Optional[gpd.GeoDataFrame] = None,
    target_rate: float = 100.0,
    background_rate: float = 0.0,
    rate_unit: str = "PCT",
) -> gpd.GeoDataFrame:
    """Build a 2-zone spot-spraying / section-control prescription GDF from low-vigor scouting targets."""
    for name, value in (("target_rate", target_rate), ("background_rate", background_rate)):
        if not np.isfinite(float(value)) or float(value) < 0:
            raise ValueError(f"{name} must be finite and non-negative")
        if str(rate_unit).upper() == "PCT" and float(value) > 100:
            raise ValueError(f"{name} must be <= 100 when rate_unit=PCT")
    gdf = _non_empty_polygons(hotspots)
    if len(gdf) == 0:
        return gpd.GeoDataFrame(
            columns=["Zone_ID", "Rx_Index", "TargetRate", "Rate_Unit", "Cmd_Mode", "Rx_Mode", "IndexFld", "geometry"],
            crs=hotspots.crs if hasattr(hotspots, 'crs') else None,
        )

    out_rows = []
    # Preserve measured hotspot fields for the existing review KML/CSV. Dropping
    # them here causes labels such as feature_0 and blank area/NDVI balloons.
    for idx, (_, source_row) in enumerate(gdf.iterrows(), start=1):
        item = source_row.drop(labels="geometry", errors="ignore").to_dict()
        item.update({
            "Zone_ID": idx,
            "Rx_Index": 1.0,
            "TargetRate": float(target_rate),
            "Rate_Unit": str(rate_unit).upper(),
            "Cmd_Mode": "spot_spray_target",
            "Rx_Mode": "spot_spray",
            "IndexFld": "SPOT_SPRAY",
            "geometry": source_row.geometry,
        })
        out_rows.append(item)

    # Optional background coverage polygon
    if field_aoi_gdf is not None and len(field_aoi_gdf):
        aoi = _non_empty_polygons(field_aoi_gdf)
        if len(aoi) and not aoi.geom_type.isin(["Polygon", "MultiPolygon"]).all():
            raise ValueError("field_aoi_gdf must contain polygon geometry")
        aoi = aoi.to_crs(gdf.crs) if aoi.crs != gdf.crs else aoi
        aoi_poly = unary_union(aoi.geometry)
        hotspot_union = unary_union(gdf.geometry)
        bg_poly = aoi_poly.difference(hotspot_union)
        if not bg_poly.is_empty:
            out_rows.append({
                "Zone_ID": len(out_rows) + 1,
                "Rx_Index": 0.0,
                "TargetRate": float(background_rate),
                "Rate_Unit": str(rate_unit).upper(),
                "Cmd_Mode": "no_spray_background",
                "Rx_Mode": "spot_spray",
                "IndexFld": "SPOT_SPRAY",
                "geometry": bg_poly,
            })

    res = gpd.GeoDataFrame(out_rows, crs=gdf.crs)
    return res




def _validate_prebuilt_prescription_gdf(zones: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Validate a prescription frame that already contains command attributes."""
    out = _non_empty_polygons(zones)
    if len(out) == 0:
        return out
    if not out.geom_type.isin(["Polygon", "MultiPolygon"]).all():
        raise ValueError("Prescription zones must contain only Polygon/MultiPolygon geometry")
    required = ["Zone_ID", "Rx_Index", "TargetRate", "Rate_Unit", "Cmd_Mode", "Rx_Mode"]
    missing = [c for c in required if c not in out.columns]
    if missing:
        raise ValueError(f"Prebuilt prescription is missing fields: {missing}")
    zid = pd.to_numeric(out["Zone_ID"], errors="coerce")
    if zid.isna().any() or not np.isclose(zid, np.round(zid)).all():
        raise ValueError("Zone_ID must contain integer, non-null values")
    if zid.duplicated().any():
        raise ValueError("Zone_ID values must be unique")
    rx = pd.to_numeric(out["Rx_Index"], errors="coerce")
    rates = pd.to_numeric(out["TargetRate"], errors="coerce")
    if not np.isfinite(rx).all() or ((rx < 0) | (rx > 1)).any():
        raise ValueError("Rx_Index must be finite and in [0, 1]")
    if not np.isfinite(rates).all() or (rates < 0).any():
        raise ValueError("TargetRate must be finite and non-negative")
    pct = out["Rate_Unit"].astype(str).str.upper().eq("PCT")
    if (pct & (rates > 100)).any():
        raise ValueError("TargetRate must be <= 100 when Rate_Unit=PCT")
    out["Zone_ID"] = zid.astype(int)
    out["Rx_Index"] = rx.astype(float)
    out["TargetRate"] = rates.astype(float)
    return out


def _prescription_or_build(zones: gpd.GeoDataFrame, **kwargs) -> gpd.GeoDataFrame:
    """Use precomputed command fields without incorrectly rebuilding from an index."""
    required = {"Zone_ID", "Rx_Index", "TargetRate", "Rate_Unit", "Cmd_Mode", "Rx_Mode"}
    if required.issubset(zones.columns):
        return _validate_prebuilt_prescription_gdf(zones)
    return build_vra_prescription_gdf(zones, **kwargs)


# Controller USB-folder packaging profiles for prescription shapefile bundles.
# These profiles only control ZIP paths. They do not certify acceptance by a
# display, because target firmware, product setup, operation type, and units
# still have to be validated on the actual controller/software.
_CONTROLLER_PRESCRIPTION_PROFILES = {
    "flat": {
        "folder_prefix": "",
        "vendor": "Generic GIS / Ag Leader-style root import",
        "notes": "Shapefile sidecars are written at ZIP root.",
    },
    "john_deere_rx": {
        "folder_prefix": "Rx/",
        "vendor": "John Deere GS3 / Gen 4 / G5 prescription USB import",
        "notes": "John Deere public display help says prescription shapefiles must be in an Rx folder at USB root.",
    },
    "case_ih_shapefile": {
        "folder_prefix": "Shapefile/",
        "vendor": "Case IH AFS Pro 700/1200 shapefile import",
        "notes": "Third-party Pro 700 guides describe a Shapefile folder at USB root for shapefile prescriptions.",
    },
    "trimble_aggps": {
        "folder_prefix": "AgGPS/Prescriptions/",
        "vendor": "Trimble AgGPS / FMX / CFX prescription import",
        "notes": "Public controller-folder guides list AgGPS/Prescriptions for several Trimble displays.",
    },
    "trimble_gfx": {
        "folder_prefix": "AgData/Prescriptions/",
        "vendor": "Trimble GFX prescription import",
        "notes": "Public GFX guides list AgData/Prescriptions for shapefile prescriptions.",
    },
    "ag_leader_root": {
        "folder_prefix": "",
        "vendor": "Ag Leader InCommand / Integra / Insight shapefile import",
        "notes": "Public Ag Leader/VRA guides describe .shp import from USB/root or a root-level folder; flat ZIP is intended for extraction to USB root.",
    },
    "new_holland_intelliview": {
        "folder_prefix": "Shapefile/",
        "vendor": "New Holland IntelliView / CNH-style shapefile import",
        "notes": "Unverified CNH-style shapefile package using the same Shapefile/ root folder as the Case IH profile; requires New Holland display/software acceptance testing before certification.",
        "support_status": "unverified_cnh_compatibility",
    },
}

_DEERE_RATE_COLUMN_BY_UNIT = {
    "KG_HA": "kg_p_ha",
    "L_HA": "l_p_ha",
    "LB_AC": "lb_p_ac",
    "GAL_AC": "gal_p_ac",
    "SEEDS_HA": "seed_p_ha",
}


_CONTROLLER_PROFILE_ALIASES = {
    "generic": "flat",
    "root": "flat",
    "rx_folder": "john_deere_rx",
    "john_deere": "john_deere_rx",
    "deere": "john_deere_rx",
    "case_ih": "case_ih_shapefile",
    "caseih": "case_ih_shapefile",
    "cnh": "case_ih_shapefile",
    "pro700": "case_ih_shapefile",
    "trimble": "trimble_aggps",
    "trimble_fmx": "trimble_aggps",
    "trimble_cfx": "trimble_aggps",
    "trimble_gfx_agdata": "trimble_gfx",
    "ag_leader": "ag_leader_root",
    "agleader": "ag_leader_root",
    "incommand": "ag_leader_root",
    "new_holland": "new_holland_intelliview",
    "intelliview": "new_holland_intelliview",
    "cnh_new_holland": "new_holland_intelliview",
}


def _normalize_controller_profile(packaging_profile: str | None) -> str:
    profile = "flat" if packaging_profile is None else str(packaging_profile).strip().lower()
    profile = _CONTROLLER_PROFILE_ALIASES.get(profile, profile)
    if profile not in _CONTROLLER_PRESCRIPTION_PROFILES:
        allowed = sorted(set(_CONTROLLER_PRESCRIPTION_PROFILES) | set(_CONTROLLER_PROFILE_ALIASES))
        raise ValueError(f"Unknown packaging_profile {packaging_profile!r}; expected one of {allowed}")
    return profile




def validate_vra_shapefile_zip(path: str | Path, *, packaging_profile: Optional[str] = None) -> dict:
    """Validate an application-zone shapefile ZIP for controller-style import.

    This is a structural smoke test, not a vendor certification. It checks the
    zip members, shapefile readability, DBF-safe column names/types, required
    rate fields, projected/geographic CRS presence, and basic geometry health.
    """
    path = Path(path)
    errors: list[str] = []
    warnings: list[str] = []
    result = {
        "path": str(path),
        "valid": False,
        "errors": errors,
        "warnings": warnings,
        "feature_count": 0,
        "columns": [],
        "crs": None,
        "geometry_types": [],
        "packaging_profile": None,
        "expected_folder_prefix": None,
    }
    if not path.exists():
        errors.append("ZIP file does not exist")
        return result

    expected_prefix = None
    if packaging_profile is not None:
        profile = _normalize_controller_profile(packaging_profile)
        expected_prefix = _CONTROLLER_PRESCRIPTION_PROFILES[profile]["folder_prefix"]
        result["packaging_profile"] = profile
        result["expected_folder_prefix"] = expected_prefix

    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        try:
            with zipfile.ZipFile(path) as zf:
                names = zf.namelist()
                result["zip_members"] = names
                _safe_extract_zip(zf, td_path)
        except Exception as exc:
            errors.append(f"Cannot read ZIP archive: {exc}")
            return result

        lower_names = {n.lower(): n for n in names}
        shp_members = [n for n in names if n.lower().endswith(".shp")]
        if len(shp_members) != 1:
            errors.append(f"Expected exactly one .shp member, found {len(shp_members)}")
            return result
        shp_member = shp_members[0]
        shp_dir = str(Path(shp_member).parent).replace(".", "")
        if shp_dir and not shp_dir.endswith("/"):
            shp_dir += "/"
        stem = Path(shp_member).with_suffix("").name
        if expected_prefix is not None:
            if expected_prefix:
                if not shp_member.startswith(expected_prefix):
                    errors.append(f"Expected shapefile under {expected_prefix!r} for profile {result['packaging_profile']}, found {shp_member!r}")
            elif "/" in shp_member or "\\" in shp_member:
                warnings.append(f"Profile {result['packaging_profile']} expects shapefile at ZIP root, found {shp_member!r}")
        for ext in [".shp", ".shx", ".dbf", ".prj"]:
            expected_member = f"{shp_dir}{stem}{ext}"
            if expected_member.lower() not in lower_names:
                errors.append(f"Missing required shapefile sidecar {expected_member}")

        shp_path = td_path / shp_member
        try:
            gdf = gpd.read_file(shp_path)
        except Exception as exc:
            errors.append(f"Cannot read shapefile with GeoPandas/Fiona: {exc}")
            return result

        result["feature_count"] = int(len(gdf))
        result["columns"] = [str(c) for c in gdf.columns]
        result["crs"] = None if gdf.crs is None else str(gdf.crs)
        result["geometry_types"] = sorted([str(x) for x in gdf.geom_type.dropna().unique()])
        if len(gdf) == 0:
            errors.append("Shapefile contains zero features")
        if gdf.crs is None:
            errors.append("Missing CRS / .prj could not be parsed")
        if result.get("packaging_profile") == "john_deere_rx" and gdf.crs is not None:
            try:
                if gdf.crs.to_epsg() != 4326:
                    errors.append("John Deere shapefile prescriptions must use WGS84 / EPSG:4326")
            except Exception:
                errors.append("Could not verify John Deere prescription CRS as WGS84")
        if not bool(gdf.is_valid.all()):
            errors.append("One or more geometries are invalid")
        if not gdf.geometry.notna().all():
            errors.append("One or more geometries are null")

        required = ["Zone_ID", "TargetRate", "Rate_Unit", "Rx_Index", "Cmd_Mode", "Rx_Mode"]
        for col in required:
            if col not in gdf.columns:
                errors.append(f"Missing required DBF field {col}")
        for col in gdf.columns:
            if col != "geometry" and len(str(col)) > 10:
                errors.append(f"DBF field name exceeds 10 characters: {col}")
        if "TargetRate" in gdf.columns:
            rates = pd.to_numeric(gdf["TargetRate"], errors="coerce")
            if rates.isna().any():
                errors.append("TargetRate contains non-numeric or null values")
            else:
                if (rates < 0).any():
                    errors.append("TargetRate contains negative values")
                if (rates > 100).any() and "Rate_Unit" in gdf.columns and set(gdf["Rate_Unit"].astype(str).str.upper()) == {"PCT"}:
                    warnings.append("TargetRate exceeds 100 while Rate_Unit=PCT")
        if "Zone_ID" in gdf.columns:
            zid = pd.to_numeric(gdf["Zone_ID"], errors="coerce")
            if zid.isna().any() or not np.isclose(zid, np.round(zid)).all():
                errors.append("Zone_ID contains non-integer/null values")
            elif zid.duplicated().any():
                errors.append("Zone_ID values are duplicated")

        if len(gdf) > 500:
            warnings.append("High polygon count may be slow or unsupported on some controllers")
        if packaging_profile is None and not any('/' in n or '\\' in n for n in names):
            warnings.append("ZIP is flat. Many GIS tools accept this; John Deere and some other vendor prescription workflows require controller-specific folders such as Rx/.")

    result["valid"] = len(errors) == 0
    return result


def export_vra_shapefile_zip(
    zones: gpd.GeoDataFrame,
    out_zip: str | Path,
    *,
    min_rate: Optional[float] = None,
    max_rate: Optional[float] = None,
    base_rate: Optional[float] = None,
    rate_unit: str = "PCT",
    index_field: Optional[str] = None,
    response: Literal["inverse", "direct"] = "inverse",
    to_crs: Optional[str] = None,
    packaging_profile: str = "flat",
    basemap_mbtiles_path: Optional[str | Path] = None,
    include_basemap_in_archive: bool = False,
) -> Path:
    """Write an application-zone shapefile bundle zipped for controller import.

    The .dbf stores shapefile-safe names: ``Zone_ID``, ``TargetRate``,
    ``Rate_Unit``, ``Rx_Index``, ``Cmd_Mode``, ``Rx_Mode``. With no operator
    product rates supplied, TargetRate is a normalized 0-100 percent intensity
    field. OrthoSWIFT does not assign pesticide, herbicide, fertilizer, seed,
    or irrigation dose.
    """
    out_zip = _ensure_parent(out_zip)
    profile_name = _normalize_controller_profile(packaging_profile)
    profile_cfg = _CONTROLLER_PRESCRIPTION_PROFILES[profile_name]
    folder_prefix = profile_cfg["folder_prefix"]
    rx = _prescription_or_build(
        zones, min_rate=min_rate, max_rate=max_rate, base_rate=base_rate,
        rate_unit=rate_unit, index_field=index_field, response=response,
    )
    if to_crs is not None and len(rx):
        rx = rx.to_crs(to_crs)
    if profile_name == "john_deere_rx" and len(rx):
        # Deere's published shapefile contract requires WGS84 and recommends a
        # unit-specific numeric rate-column name to reduce display-side setup.
        rx = rx.to_crs("EPSG:4326")
        units = set(rx["Rate_Unit"].astype(str).str.upper())
        if len(units) == 1:
            deere_rate_field = _DEERE_RATE_COLUMN_BY_UNIT.get(next(iter(units)))
            if deere_rate_field:
                rx[deere_rate_field] = pd.to_numeric(rx["TargetRate"], errors="raise")
    if len(rx) == 0:
        raise ValueError("Cannot export an empty VRA shapefile package")

    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        shp_path = td_path / "fertilizer_zones.shp"
        # Keep DBF schema simple and terminal-friendly.
        export_cols = [c for c in [
            "Zone_ID", "TargetRate", "kg_p_ha", "l_p_ha", "lb_p_ac", "gal_p_ac", "seed_p_ha",
            "Rate_Unit", "Rx_Index", "Cmd_Mode", "Rx_Mode", "IndexFld", "geometry"
        ] if c in rx.columns]
        rx[export_cols].to_file(shp_path, driver="ESRI Shapefile", encoding="UTF-8")

        manifest_csv = td_path / "orthoswift_application_zone_summary.csv"
        manifest = rx.drop(columns="geometry", errors="ignore").copy()
        if "TargetRate" in manifest.columns:
            manifest["Target_Rate"] = manifest["TargetRate"]
        manifest.to_csv(manifest_csv, index=False)

        physical_mode = bool(len(rx) and set(rx["Cmd_Mode"].astype(str)) == {"physical_product_rate"})
        actual_unit = str(rx["Rate_Unit"].iloc[0]) if len(rx) else str(rate_unit)
        actual_min = float(pd.to_numeric(rx["TargetRate"]).min()) if len(rx) else None
        actual_max = float(pd.to_numeric(rx["TargetRate"]).max()) if len(rx) else None
        method = {
            "product": "OrthoSWIFT application zones",
            "packaging_profile": profile_name,
            "controller_profile": profile_cfg,
            "usb_folder_prefix": folder_prefix,
            "rate_unit": actual_unit,
            "rate_fields": {
                "dbf_target_rate": "TargetRate",
                "long_form_alias": "Target_Rate",
                "zone_id": "Zone_ID",
            },
            "min_rate": min_rate,
            "max_rate": max_rate,
            "base_rate": base_rate,
            "response": response,
            "formula": "rx_index = 1 - ((index - index_min)/(index_max - index_min)) for inverse response; if min/max rates are absent, TargetRate = 100*rx_index and Rate_Unit=PCT; if min/max rates are supplied, target_rate = min_rate + rx_index*(max_rate-min_rate)",
            "crs": str(rx.crs),
            "import_ready": True,
            "geometry_only_delivery": True,
            "operator_must_review_rates": True,
            "application_ready": False,
            "machine_ready": False,
            "command_mode": "operator_supplied_physical_rate" if physical_mode else "normalized_percent_review",
            "resolved_min_rate": actual_min,
            "resolved_max_rate": actual_max,
            "limitation": ("Physical rates were supplied by the operator/agronomist and spatially encoded by OrthoSWIFT; verify product, units, equipment calibration, boundaries, and controller interpretation before application." if physical_mode else "TargetRate is an imagery-derived relative intensity. Assign and verify physical product rates in the controller or farm-management software."),
            "farmer_facing_summary": (
                "Recommended action zones generated from drone-map analysis after removing likely shadows, "
                "bare soil, water/wet areas, map edges, and other non-crop noise. Very small or broken-up "
                "areas may be excluded to keep exported files practical for equipment and field use."
            ),
            "target_rate_plain_language": (
                f"TargetRate contains operator-approved physical rates from {actual_min:g} to {actual_max:g} {actual_unit}." if physical_mode
                else "TargetRate is a relative intensity value (0 to 100 percent)."
            ),
            "operator_responsibility_plain_language": (
                "The operator remains responsible for checking boundaries, choosing products, setting rates, "
                "confirming equipment settings, and following local rules."
            ),
            "offline_basemap": {
                "bundled": bool(include_basemap_in_archive and basemap_mbtiles_path is not None),
                "archive_folder": "Basemap/" if include_basemap_in_archive and basemap_mbtiles_path is not None else None,
                "import_note": "Import separately as a custom/offline layer where the controller app supports MBTiles; archive presence does not imply automatic import.",
            },
        }
        _write_json(td_path / "application_zone_methodology.json", method)

        if include_basemap_in_archive:
            if basemap_mbtiles_path is None:
                raise ValueError("basemap_mbtiles_path is required when include_basemap_in_archive=True")
            copy_basemap_companion_files(basemap_mbtiles_path, td_path / "Basemap")

        # Controller ZIPs must only contain prescription data and basemap.
        # CSV summaries and methodology JSON are for agronomists, not controllers.
        _CTRL_EXCLUDE = {"orthoswift_application_zone_summary.csv", "application_zone_methodology.json"}
        with zipfile.ZipFile(out_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for f in sorted(td_path.rglob("*")):
                if not f.is_file():
                    continue
                if f.name in _CTRL_EXCLUDE:
                    continue
                relative = f.relative_to(td_path)
                arcname = str(relative) if relative.parts[0] == "Basemap" else f"{folder_prefix}{f.name}"
                zf.write(f, arcname=arcname)
    return out_zip


# Agriculture: multi-controller/drone prescription packages


def _tfw_text(transform: Affine) -> str:
    """Return ESRI world-file text for a north-up affine transform."""
    return "\n".join([
        f"{transform.a:.12f}",
        f"{transform.d:.12f}",
        f"{transform.b:.12f}",
        f"{transform.e:.12f}",
        f"{transform.c + (transform.a + transform.b) / 2.0:.12f}",
        f"{transform.f + (transform.d + transform.e) / 2.0:.12f}",
        "",
    ])


def _rate_boundary_gdf(rx: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    if len(rx) == 0:
        return gpd.GeoDataFrame({"name": []}, geometry=[], crs=rx.crs)
    geom = unary_union(list(rx.geometry))
    if geom.geom_type == "GeometryCollection":
        polys = [g for g in geom.geoms if g.geom_type in {"Polygon", "MultiPolygon"}]
        geom = unary_union(polys) if polys else geom
    return gpd.GeoDataFrame({"name": ["application_boundary"]}, geometry=[geom], crs=rx.crs)


def export_dji_agras_vra_package(
    zones: gpd.GeoDataFrame,
    out_zip: str | Path,
    *,
    min_rate: Optional[float] = None,
    max_rate: Optional[float] = None,
    base_rate: Optional[float] = None,
    rate_unit: str = "PCT",
    index_field: Optional[str] = None,
    response: Literal["inverse", "direct"] = "inverse",
    to_crs: Optional[str] = None,
    resolution_m: Optional[float] = None,
    rate_field: str = "TargetRate",
    basemap_mbtiles_path: Optional[str | Path] = None,
    include_basemap_in_archive: bool = False,
) -> Path:
    """Export a DJI Agras-style VRA package with boundary shapefile + rate GeoTIFF.

    Public third-party DJI Agras VRA workflows use a DJI/ folder containing a
    Shapefile/ boundary and Rx/ GeoTIFF rate raster. This function creates that
    structural package; it is not DJI firmware certification.
    """
    out_zip = _ensure_parent(out_zip)
    rx = _prescription_or_build(
        zones,
        min_rate=min_rate,
        max_rate=max_rate,
        base_rate=base_rate,
        rate_unit=rate_unit,
        index_field=index_field,
        response=response,
    )
    if to_crs is not None and len(rx):
        rx = rx.to_crs(to_crs)
    if len(rx) == 0:
        raise ValueError("Cannot export empty DJI Agras VRA package")
    if rx.crs is None:
        raise ValueError("DJI Agras VRA export requires a CRS")
    from pyproj import CRS as _CRS
    resolved = _CRS.from_user_input(rx.crs)
    factors = [float(axis.unit_conversion_factor) for axis in resolved.axis_info[:2]]
    if (not resolved.is_projected or len(factors) < 2 or
            not all(np.isfinite(f) and np.isclose(f, 1.0, rtol=1e-6) for f in factors)):
        raise ValueError("DJI Agras rate-grid export requires a projected metre-based CRS")
    if rate_field not in rx.columns:
        raise ValueError(f"Missing rate field {rate_field!r}")
    rates = pd.to_numeric(rx[rate_field], errors="coerce")
    if rates.isna().any():
        raise ValueError(f"Rate field {rate_field!r} contains null/non-numeric values")

    minx, miny, maxx, maxy = rx.total_bounds
    if resolution_m is None:
        area = float(rx.geometry.area.sum())
        resolution_m = max(
            0.05,
            min(0.25, math.sqrt(max(area, 1.0) / 250_000.0)),
        )
    if resolution_m <= 0:
        raise ValueError("resolution_m must be > 0")
    width = max(1, int(math.ceil((maxx - minx) / resolution_m)))
    height = max(1, int(math.ceil((maxy - miny) / resolution_m)))
    transform = Affine(resolution_m, 0.0, minx, 0.0, -resolution_m, maxy)
    shapes = ((geom, float(rate)) for geom, rate in zip(rx.geometry, rates))
    raster = features.rasterize(
        shapes=shapes,
        out_shape=(height, width),
        transform=transform,
        fill=-9999.0,
        dtype="float32",
        # Assign a rate only when the raster-cell center lies within a zone.
        # all_touched=True materially expands narrow/irregular application areas.
        all_touched=False,
    )

    valid_rate_cells = raster != -9999.0
    raster_area_m2 = float(valid_rate_cells.sum()) * (
        float(resolution_m) ** 2
    )
    polygon_area_m2 = float(rx.geometry.area.sum())
    area_error_pct = (
        100.0 * (raster_area_m2 - polygon_area_m2) / polygon_area_m2
        if polygon_area_m2 > 0
        else float("nan")
    )

    if not np.isfinite(area_error_pct):
        raise ValueError("Could not calculate DJI rate-grid area error")
    if abs(area_error_pct) > 2.0:
        raise ValueError(
            "DJI rate-grid footprint differs from source polygons by "
            f"{area_error_pct:.2f}%; use a finer resolution_m"
        )

    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        shp_dir = td_path / "DJI" / "Shapefile"
        rx_dir = td_path / "DJI" / "Rx"
        shp_dir.mkdir(parents=True, exist_ok=True)
        rx_dir.mkdir(parents=True, exist_ok=True)
        boundary = _rate_boundary_gdf(rx)
        boundary.to_file(shp_dir / "application_boundary.shp", driver="ESRI Shapefile", encoding="UTF-8")
        tif_path = rx_dir / "application_rate.tif"
        with rasterio.open(
            tif_path,
            "w",
            driver="GTiff",
            height=height,
            width=width,
            count=1,
            dtype="float32",
            crs=rx.crs,
            transform=transform,
            nodata=-9999.0,
            compress="deflate",
        ) as dst:
            dst.write(raster, 1)
        (rx_dir / "application_rate.tfw").write_text(_tfw_text(transform), encoding="utf-8")
        manifest = {
            "product": "OrthoSWIFT DJI Agras VRA package",
            "support_status": "structural_export_not_firmware_certified",
            "layout": {"boundary": "DJI/Shapefile/application_boundary.shp", "rate_raster": "DJI/Rx/application_rate.tif"},
            "rate_field": rate_field,
            "rate_unit": str(rx.get("Rate_Unit", pd.Series([rate_unit])).iloc[0]) if len(rx) else rate_unit,
            "min_rate": min_rate,
            "max_rate": max_rate,
            "base_rate": base_rate,
            "response": response,
            "resolution_m": float(resolution_m),
            "polygon_area_m2": polygon_area_m2,
            "rate_raster_valid_area_m2": raster_area_m2,
            "rate_raster_area_error_pct": area_error_pct,
            "rasterization_rule": "pixel_center_within_zone",
            "crs": str(rx.crs),
            "feature_count": int(len(rx)),
            "raster_width": int(width),
            "raster_height": int(height),
            "operator_must_review_rates": True,
            "machine_ready": False,
            "limitation": "DJI Agras VRA structure is exported from OrthoSWIFT zones; validate in DJI Agras/Terra/controller software before field use.",
        }
        _write_json(td_path / "DJI" / "application_zone_methodology.json", manifest)
        if include_basemap_in_archive and basemap_mbtiles_path is not None:
            try:
                copy_basemap_companion_files(basemap_mbtiles_path, td_path / "DJI" / "Basemap")
            except (OSError, ValueError, RuntimeError) as exc:
                logger.warning("Could not add the offline basemap to the DJI package: %s", exc)
        _CTRL_EXCLUDE = {"application_zone_methodology.json"}
        with zipfile.ZipFile(out_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for f in sorted((td_path / "DJI").rglob("*")):
                if f.is_file() and f.name not in _CTRL_EXCLUDE:
                    zf.write(f, arcname=str(f.relative_to(td_path)))
    return out_zip


def validate_dji_agras_vra_package(path: str | Path) -> dict:
    path = Path(path)
    errors: list[str] = []
    warnings: list[str] = []
    result = {"path": str(path), "valid": False, "errors": errors, "warnings": warnings, "zip_members": [], "rate_raster": None, "boundary_shapefile": None}
    if not path.exists():
        errors.append("ZIP file does not exist")
        return result
    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        try:
            with zipfile.ZipFile(path) as zf:
                names = zf.namelist()
                result["zip_members"] = names
                _safe_extract_zip(zf, td_path)
        except Exception as exc:
            errors.append(f"Cannot read ZIP archive: {exc}")
            return result
        required = [
            "DJI/Shapefile/application_boundary.shp",
            "DJI/Shapefile/application_boundary.shx",
            "DJI/Shapefile/application_boundary.dbf",
            "DJI/Shapefile/application_boundary.prj",
            "DJI/Rx/application_rate.tif",
            "DJI/Rx/application_rate.tfw",
        ]
        for member in required:
            if member not in names:
                errors.append(f"Missing required DJI member {member}")
        shp_path = td_path / "DJI" / "Shapefile" / "application_boundary.shp"
        tif_path = td_path / "DJI" / "Rx" / "application_rate.tif"
        boundary_crs = None
        raster_crs = None
        if shp_path.exists():
            try:
                gdf = gpd.read_file(shp_path)
                boundary_crs = gdf.crs
                result["boundary_shapefile"] = {"feature_count": int(len(gdf)), "crs": None if gdf.crs is None else str(gdf.crs), "geometry_types": sorted(gdf.geom_type.dropna().unique().astype(str).tolist())}
                if len(gdf) == 0:
                    errors.append("DJI boundary shapefile contains zero features")
                if gdf.crs is None:
                    errors.append("DJI boundary shapefile CRS is missing")
            except Exception as exc:
                errors.append(f"Cannot read DJI boundary shapefile: {exc}")
        if tif_path.exists():
            try:
                with rasterio.open(tif_path) as src:
                    raster_crs = src.crs
                    arr = src.read(1, masked=True)
                    finite_count = int(np.ma.count(arr))
                    result["rate_raster"] = {"width": int(src.width), "height": int(src.height), "crs": None if src.crs is None else str(src.crs), "nodata": src.nodata, "finite_count": finite_count, "min": None if finite_count == 0 else float(arr.min()), "max": None if finite_count == 0 else float(arr.max())}
                    if src.crs is None:
                        errors.append("DJI rate GeoTIFF CRS is missing")
                    if finite_count == 0:
                        errors.append("DJI rate GeoTIFF contains no valid rate pixels")
            except Exception as exc:
                errors.append(f"Cannot read DJI rate GeoTIFF: {exc}")
        if boundary_crs is not None and raster_crs is not None and boundary_crs != raster_crs:
            errors.append("DJI boundary and rate-raster CRS do not match")
        if shp_path.exists() and tif_path.exists():
            try:
                boundary = gpd.read_file(shp_path)
                with rasterio.open(tif_path) as src:
                    rate = src.read(1, masked=True)
                    pixel_area = abs(
                        src.transform.a * src.transform.e
                        - src.transform.b * src.transform.d
                    )
                    raster_area = float(np.ma.count(rate)) * pixel_area

                polygon_area = float(boundary.geometry.area.sum())
                area_error_pct = (
                    100.0 * (raster_area - polygon_area) / polygon_area
                    if polygon_area > 0
                    else float("nan")
                )
                result["footprint_area_check"] = {
                    "polygon_area_m2": polygon_area,
                    "rate_raster_valid_area_m2": raster_area,
                    "area_error_pct": area_error_pct,
                    "tolerance_pct": 2.0,
                }

                if (
                    not np.isfinite(area_error_pct)
                    or abs(area_error_pct) > 2.0
                ):
                    errors.append(
                        "DJI rate-raster footprint differs from boundary "
                        f"by {area_error_pct:.2f}%"
                    )
            except Exception as exc:
                errors.append(f"Cannot validate DJI footprint area: {exc}")
    result["valid"] = len(errors) == 0
    return result


def export_xag_vra_package(
    zones: gpd.GeoDataFrame,
    out_zip: str | Path,
    *,
    min_rate: Optional[float] = None,
    max_rate: Optional[float] = None,
    base_rate: Optional[float] = None,
    rate_unit: str = "PCT",
    index_field: Optional[str] = None,
    response: Literal["inverse", "direct"] = "inverse",
    to_crs: Optional[str] = "EPSG:4326",
    basemap_mbtiles_path: Optional[str | Path] = None,
    include_basemap_in_archive: bool = False,
) -> Path:
    """Export a best-effort XAG-style research package: KML + JSON rate zones.

    Public PIX4D documentation says XAG VRA export uses KML + JSON. The exact
    vendor JSON schema is not public here, so this package is explicitly marked
    research-stage and not flight-certified.
    """
    out_zip = _ensure_parent(out_zip)
    rx = _prescription_or_build(zones, min_rate=min_rate, max_rate=max_rate, base_rate=base_rate, rate_unit=rate_unit, index_field=index_field, response=response)
    if len(rx) == 0:
        raise ValueError("Cannot export empty XAG VRA package")
    if to_crs is not None:
        rx = rx.to_crs(to_crs)
    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        xag_dir = td_path / "XAG"
        xag_dir.mkdir(parents=True, exist_ok=True)
        export_polygons_kml(rx, xag_dir / "fertilizer_zones.kml", name_field="Zone_ID", description_fields=["Zone_ID", "TargetRate", "Rate_Unit", "Rx_Index"], document_name="XAG VRA rate zones")
        features_out = []
        for _, row in rx.iterrows():
            features_out.append({
                "type": "Feature",
                "properties": {k: (None if pd.isna(v) else v.item() if hasattr(v, "item") else v) for k, v in row.drop(labels="geometry", errors="ignore").items()},
                "geometry": mapping(row.geometry),
            })
        payload = {
            "schema": "orthoswift.xag_vra.research.v1",
            "support_status": "research_stage_not_vendor_certified",
            "vendor_basis": "Public PIX4D documentation reports XAG VRA packages as KML + JSON; exact XAG JSON schema must be verified with XAG/Pix4D before operational use.",
            "type": "FeatureCollection",
            "rate_field": "TargetRate",
            "rate_unit_field": "Rate_Unit",
            "operator_must_review_rates": True,
            "machine_ready": False,
            "features": features_out,
        }
        (xag_dir / "fertilizer_zones.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
        _write_json(xag_dir / "application_zone_methodology.json", {"product": "OrthoSWIFT XAG VRA research package", "machine_ready": False, "operator_must_review_rates": True, "files": ["XAG/fertilizer_zones.kml", "XAG/fertilizer_zones.json"]})
        if include_basemap_in_archive and basemap_mbtiles_path is not None:
            copy_basemap_companion_files(basemap_mbtiles_path, td_path / "XAG" / "Basemap")
        _CTRL_EXCLUDE = {"application_zone_methodology.json"}
        with zipfile.ZipFile(out_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for f in sorted(td_path.rglob("*")):
                if f.is_file() and f.name not in _CTRL_EXCLUDE:
                    zf.write(f, arcname=str(f.relative_to(td_path)))
    return out_zip


def validate_xag_vra_package(path: str | Path) -> dict:
    path = Path(path)
    errors: list[str] = []
    result = {"path": str(path), "valid": False, "errors": errors, "zip_members": [], "feature_count": 0, "support_status": "research_stage_not_vendor_certified"}
    if not path.exists():
        errors.append("ZIP file does not exist")
        return result
    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        try:
            with zipfile.ZipFile(path) as zf:
                names = zf.namelist(); result["zip_members"] = names; _safe_extract_zip(zf, td_path)
        except Exception as exc:
            errors.append(f"Cannot read ZIP archive: {exc}"); return result
        for member in ["XAG/fertilizer_zones.kml", "XAG/fertilizer_zones.json"]:
            if member not in names:
                errors.append(f"Missing required XAG research member {member}")
        json_path = td_path / "XAG" / "fertilizer_zones.json"
        if json_path.exists():
            try:
                payload = json.loads(json_path.read_text(encoding="utf-8"))
                result["feature_count"] = int(len(payload.get("features", [])))
                if result["feature_count"] == 0:
                    errors.append("XAG JSON contains zero features")
            except Exception as exc:
                errors.append(f"Cannot parse XAG JSON: {exc}")
    result["valid"] = len(errors) == 0
    return result


def export_all_controller_prescription_zips(
    zones: gpd.GeoDataFrame,
    out_dir: str | Path,
    *,
    min_rate: Optional[float] = None,
    max_rate: Optional[float] = None,
    base_rate: Optional[float] = None,
    rate_unit: str = "PCT",
    index_field: Optional[str] = None,
    response: Literal["inverse", "direct"] = "inverse",
    include_research_stage: bool = True,
    basemap_mbtiles_path: Optional[str | Path] = None,
    include_basemap_in_archives: bool = False,
) -> dict:
    """Export one package per controller/drone family into controller_packages/."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    outputs: dict = {}
    _bm = basemap_mbtiles_path
    _inc_bm = include_basemap_in_archives
    shapefile_profiles = {
        "generic_flat": ("flat", "universal.zip"),
        "john_deere": ("john_deere_rx", "john_deere.zip"),
        "case_ih": ("case_ih_shapefile", "case_ih.zip"),
        "trimble_aggps": ("trimble_aggps", "trimble_aggps.zip"),
        "trimble_gfx": ("trimble_gfx", "trimble_gfx.zip"),
        "ag_leader": ("ag_leader_root", "ag_leader.zip"),
        "new_holland": ("new_holland_intelliview", "new_holland.zip"),
    }
    for label, (profile, filename) in shapefile_profiles.items():
        zpath = out_dir / filename
        export_vra_shapefile_zip(
            zones, zpath,
            min_rate=min_rate, max_rate=max_rate, base_rate=base_rate,
            rate_unit=rate_unit, index_field=index_field, response=response,
            packaging_profile=profile,
            basemap_mbtiles_path=_bm, include_basemap_in_archive=_inc_bm,
        )
        outputs[f"{label}_zip"] = str(zpath)
        outputs[f"{label}_validation"] = validate_vra_shapefile_zip(zpath, packaging_profile=profile)
    if include_research_stage:
        dji_zip = out_dir / "dji_agras.zip"
        export_dji_agras_vra_package(
            zones, dji_zip,
            min_rate=min_rate, max_rate=max_rate, base_rate=base_rate,
            rate_unit=rate_unit, index_field=index_field, response=response,
            basemap_mbtiles_path=_bm, include_basemap_in_archive=_inc_bm,
        )
        outputs["dji_agras_vra_zip"] = str(dji_zip)
        outputs["dji_agras_vra_validation"] = validate_dji_agras_vra_package(dji_zip)
        xag_zip = out_dir / "xag.zip"
        export_xag_vra_package(
            zones, xag_zip,
            min_rate=min_rate, max_rate=max_rate, base_rate=base_rate,
            rate_unit=rate_unit, index_field=index_field, response=response,
            basemap_mbtiles_path=_bm, include_basemap_in_archive=_inc_bm,
        )
        outputs["xag_vra_zip"] = str(xag_zip)
        outputs["xag_vra_validation"] = validate_xag_vra_package(xag_zip)
    return outputs


# Agriculture: DJI-style spot-spray KMZ/WPML












# Inspection: ledgers, cut/fill, trafficability, berms































