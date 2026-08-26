"""
orthoswift_post.exports
=================================

No-QGIS delivery exports for OrthoSWIFT.

Adds two deterministic exports:
    * XYZ/CSV elevation grid from a DEM
    * KML export from existing polygon GeoDataFrames

No user-defined geometry. No prescription rates. No AI.
"""
from __future__ import annotations

from pathlib import Path
from typing import Mapping, Optional, Sequence
import html
import json

import numpy as np
import rasterio
import geopandas as gpd
from shapely.geometry import Polygon, MultiPolygon




def write_raster(
    path: str | Path,
    array: np.ndarray,
    profile: dict,
    *,
    dtype: Optional[str] = None,
    nodata: Optional[float] = None,
) -> Path:
    """Write a 2D array to GeoTIFF with the given profile."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    p = profile.copy()
    p.update(
        driver="GTiff",
        height=array.shape[0],
        width=array.shape[1],
        count=1,
        dtype=dtype or array.dtype,
        compress="deflate",
    )
    if nodata is not None:
        p["nodata"] = nodata
    source = np.asanyarray(array)
    declared_nodata = p.get("nodata")
    target_dtype = np.dtype(p["dtype"])
    if np.ma.isMaskedArray(source):
        if declared_nodata is None:
            raise ValueError("Masked raster writes require a declared nodata value")
        source = source.filled(declared_nodata)
    if np.issubdtype(target_dtype, np.integer):
        nonfinite = ~np.isfinite(source)
        if nonfinite.any():
            if declared_nodata is None:
                raise ValueError("Non-finite values cannot be written to an integer raster without nodata")
            source = np.array(source, copy=True)
            source[nonfinite] = declared_nodata
    out = np.asarray(source).astype(target_dtype, copy=True)
    if np.issubdtype(out.dtype, np.floating) and declared_nodata is not None:
        out[~np.isfinite(out)] = declared_nodata
    with rasterio.open(path, "w", **p) as dst:
        dst.write(out, 1)
    return path








def _ring_to_kml_coords(ring) -> str:
    return " ".join(f"{x:.8f},{y:.8f},0" for x, y in ring.coords)


def _polygon_to_kml(poly: Polygon) -> str:
    outer = f"""
    <outerBoundaryIs>
      <LinearRing>
        <coordinates>{_ring_to_kml_coords(poly.exterior)}</coordinates>
      </LinearRing>
    </outerBoundaryIs>"""

    inners = ""
    for interior in poly.interiors:
        inners += f"""
    <innerBoundaryIs>
      <LinearRing>
        <coordinates>{_ring_to_kml_coords(interior)}</coordinates>
      </LinearRing>
    </innerBoundaryIs>"""

    return f"<Polygon>{outer}{inners}\n    </Polygon>"


def _kml_color(hex_color: str, alpha: str = "55") -> str:
    """Convert #RRGGBB to KML aabbggrr color."""
    c = str(hex_color or "#ffffff").strip().lstrip("#")
    if len(c) != 6:
        c = "ffffff"
    rr, gg, bb = c[0:2], c[2:4], c[4:6]
    return f"{alpha}{bb}{gg}{rr}"


def _format_kml_number(value: float) -> str:
    """Readable without rounding real sub-square-metre patches to zero."""
    value = float(value)
    if abs(value) < 1.0:
        return f"{value:,.4f}"
    return f"{value:,.2f}"


def _style_for_row(row, *, style_field: Optional[str], default_style: str) -> str:
    if style_field and style_field in row.index:
        value = row.get(style_field)
        if value is not None and str(value) != "nan":
            return str(value)
    return default_style


def _severity_style(value) -> str:
    """Map hotspot severity to low/medium/high visual styles."""
    text = str(value).strip().lower() if value is not None else ""
    try:
        rank = int(float(text))
    except Exception:
        rank = None
    if "high" in text or rank == 1:
        return "severity_high"
    if "medium" in text or rank == 2:
        return "severity_medium"
    if "low" in text or (rank is not None and rank >= 3):
        return "severity_low"
    return "severity_medium"


def export_polygons_kml(
    gdf: gpd.GeoDataFrame,
    out_kml: str | Path,
    *,
    name_field: Optional[str] = None,
    description_fields: Optional[Sequence[str]] = None,
    document_name: str = "vectors",
    style_field: Optional[str] = None,
    style_prefix: str = "",
    default_style: str = "management_zone",
    severity_field: Optional[str] = None,
    description_labels: Optional[Mapping[str, str]] = None,
    explode_multipolygon_parts: bool = False,
    part_id_field: Optional[str] = None,
    fill_opacity: str = "55",
    line_opacity: str = "cc",
) -> Path:
    """Export polygon or multipolygon GeoDataFrame to styled KML.

    KML is a visual review format only. This function writes clean polygon
    placemarks with transparent fills and thin outlines; it never writes
    waypoints, flight lines, or mission instructions. Geometries are reprojected
    to EPSG:4326 as required by KML.
    """
    out_kml = Path(out_kml)

    if gdf is None:
        raise ValueError("gdf cannot be None")
    if gdf.crs is None:
        raise ValueError("KML export requires gdf.crs")

    description_fields = list(description_fields or [])
    description_labels = dict(description_labels or {})
    placemarks = []

    styles = {
        "management_zone": ("#2b8cbe", "#2b8cbe"),
        "application_zone": ("#31a354", "#238b45"),
        "severity_high": ("#e31a1c", "#bd0026"),
        "severity_medium": ("#ff7f00", "#e6550d"),
        "severity_low": ("#ffd92f", "#e6ab02"),
        # Relative vigor labels (K-means clustering)
        "lowest_relative_vigor":   ("#d73027", "#b2182b"),
        "lower_relative_vigor":    ("#fc8d59", "#e34a33"),
        "typical_relative_vigor":  ("#fee08b", "#d9a900"),
        "higher_relative_vigor":   ("#91cf60", "#4dac26"),
        "highest_relative_vigor":  ("#1a9850", "#006837"),
        # Fertilizer zone gradient: red (critical) → amber → yellow → lime → green (very healthy)
        "vigor_critical":     ("#d73027", "#b2182b"),
        "vigor_watch":        ("#fc8d59", "#e34a33"),
        "vigor_moderate":     ("#fee08b", "#d9a900"),
        "vigor_healthy":      ("#91cf60", "#4dac26"),
        "vigor_very_healthy": ("#1a9850", "#006837"),
    }
    style_defs = []
    for style_id, (fill_hex, line_hex) in styles.items():
        style_defs.append(f"""
  <Style id=\"{html.escape(style_id)}\">
    <LineStyle><color>{_kml_color(line_hex, line_opacity)}</color><width>1.6</width></LineStyle>
    <PolyStyle><color>{_kml_color(fill_hex, fill_opacity)}</color><fill>1</fill><outline>1</outline></PolyStyle>
  </Style>""")

    if len(gdf) > 0:
        source_df = gdf.copy()
        kdf = gdf.to_crs("EPSG:4326")

        for idx, row in kdf.iterrows():
            geom = row.geometry
            if geom is None or geom.is_empty:
                continue

            if name_field and name_field in kdf.columns:
                name = html.escape(str(row[name_field]))
            else:
                name = f"feature_{idx}"

            desc_parts = []
            for field in description_fields:
                if field in kdf.columns:
                    value = row[field]
                    if isinstance(value, (float, np.floating)) and np.isfinite(value):
                        value = _format_kml_number(float(value))
                    label = description_labels.get(field, field)
                    desc_parts.append(
                        f"{html.escape(str(label))}: {html.escape(str(value))}"
                    )
            desc = "&#10;".join(desc_parts)

            if severity_field is not None:
                style_id = _severity_style(row[severity_field] if severity_field in kdf.columns else None)
            else:
                raw = _style_for_row(row, style_field=style_field, default_style=default_style)
                style_id = f"{style_prefix}{raw}" if style_prefix and not raw.startswith(style_prefix) else raw
                if style_id not in styles:
                    style_id = default_style if default_style in styles else "management_zone"

            if isinstance(geom, Polygon):
                wgs_parts = [geom]
            elif isinstance(geom, MultiPolygon):
                wgs_parts = list(geom.geoms)
            else:
                continue

            source_geom = source_df.loc[idx].geometry
            if isinstance(source_geom, Polygon):
                source_parts = [source_geom]
            elif isinstance(source_geom, MultiPolygon):
                source_parts = list(source_geom.geoms)
            else:
                continue

            if explode_multipolygon_parts and len(wgs_parts) > 1:
                # One KML file, one independently selectable Placemark per
                # patch. Patch measurements come from that patch's projected
                # source geometry; parent-zone attributes are explicitly scoped.
                if len(source_parts) != len(wgs_parts):
                    raise RuntimeError("Multipart geometry part count changed during KML reprojection")
                base_id = row.get(part_id_field, idx) if part_id_field else idx
                parent_area = float(source_geom.area)
                paired_parts = sorted(
                    zip(source_parts, wgs_parts),
                    key=lambda pair: (-float(pair[0].area), pair[0].bounds),
                )
                # Filter out sub-pixel / micro-sliver corner artifacts (< 1.0 m² and < 0.01% of parent zone)
                filtered_parts = [
                    pair for i, pair in enumerate(paired_parts)
                    if i == 0 or (float(pair[0].area) >= 1.0 and (parent_area <= 0 or (100.0 * float(pair[0].area) / parent_area) >= 0.01))
                ]
                for part_number, (source_part, wgs_part) in enumerate(filtered_parts, start=1):
                    patch_id = f"Z{base_id}-P{part_number:03d}"
                    patch_area = float(source_part.area)
                    patch_pct = 100.0 * patch_area / parent_area if parent_area > 0 else float("nan")
                    patch_name = f"{name} - patch {patch_id}"
                    patch_desc = "&#10;".join(filter(None, [
                        f"Patch ID: {html.escape(patch_id)}",
                        f"This patch area (m²): {_format_kml_number(patch_area)}",
                        f"Share of parent zone (%): {_format_kml_number(patch_pct)}",
                        desc,
                    ]))
                    placemarks.append(f"""
  <Placemark>
    <name>{patch_name}</name>
    <styleUrl>#{html.escape(style_id)}</styleUrl>
    <description>{patch_desc}</description>
    {_polygon_to_kml(wgs_part)}
  </Placemark>""")
            else:
                geometry_kml = (
                    _polygon_to_kml(wgs_parts[0]) if len(wgs_parts) == 1
                    else f"<MultiGeometry>{''.join(_polygon_to_kml(poly) for poly in wgs_parts)}</MultiGeometry>"
                )
                placemarks.append(f"""
<Placemark>
<name>{name}</name>
<styleUrl>#{html.escape(style_id)}</styleUrl>
<description>{desc}</description>
    {geometry_kml}
</Placemark>""")

    kml = f"""<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
<Document>
  <name>{html.escape(str(document_name))}</name>
{''.join(style_defs)}
{''.join(placemarks)}
</Document>
</kml>
"""
    out_kml.parent.mkdir(parents=True, exist_ok=True)
    out_kml.write_text(kml, encoding="utf-8")
    return out_kml

def export_analytics_methodology(out_path: str | Path, *, domain: str) -> Path:
    """Write a concise machine-readable methodology/citation manifest for analytics with legal disclaimers."""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    
    refs = {
        "Horn_1981": "Horn BKP. Hill shading and the reflectance map. Proceedings of the IEEE. 1981;69(1):14-47.",
        "MacQueen_1967": "MacQueen J. Some methods for classification and analysis of multivariate observations. Proc 5th Berkeley Symp.",
        "Rouse_1974": "Rouse JW et al. Monitoring vegetation systems in the Great Plains with ERTS. NASA SP-351. 1974.",
        "Serra_1982": "Serra J. Image Analysis and Mathematical Morphology. Academic Press. 1982.",
    }
    
    common_disclaimers = {
        "general": "OrthoSWIFT generates machine-readable decision-support files for agriculture, inspection, construction, and mining workflows. These files are derived deterministically from analytic layers and exported in open formats for review, import testing, and operator-controlled execution. They are NOT autonomous approvals, legal certifications, engineering designs, agronomic prescriptions by a licensed professional, flight authorizations, or substitutes for field verification.",
        "quality_boundaries": "Decision-support outputs can degrade or become unsafe when imported blindly into external equipment. Users must verify coordinate systems, geometry alignment, units, controller import behavior, machine calibration, field conditions, and regulatory compliance before operational use. OrthoSWIFT outputs are deterministic and auditable, but deterministic does not mean certified, safe, legal, or agronomically correct for every site or machine.",
        "methodology_json": "Every job that produces decision-support outputs includes this methodology.json artifact in the decisions/ delivery folder. This file documents the parameters, equations, command semantics, and references used to generate the outputs.",
    }
    
    methods = {
        "domain": "agriculture_analytics",
        "deterministic": True,
        "disclaimer": common_disclaimers["general"],
        "equations": [
            "NDVI = (NIR - R) / (NIR + R)",
            "NDRE = (NIR - RE) / (NIR + RE)",
            "GLI = (2G - R - B) / (2G + R + B)",
            "canopy_cover = (vegetation_pixels / valid_pixels) * 100",
            "plant_mask = (NDVI >= max(configured_floor, Otsu_threshold)) AND (MSAVI2 >= configured_floor)",
            "plant_density_per_ha = accepted_connected_components / observed_valid_area_ha",
            "row_periodicity = abs(mean(exp(i * 2*pi*cross_row_coordinate / expected_row_spacing)))",
            "in_row_gap_candidate = interior adjacent-object gap >= configured_ratio * expected_inrow_spacing",
            "stand_shortfall_status = SCOUT_LOW_STAND when local valid coverage passes and observed row-associated density is below configured target percentage",
            "vra_index = 1 - ((index_i - index_min) / (index_max - index_min))",
            "TargetRate = 100 * vra_index (relative mode)",
            "TargetRate = min_rate + vra_index * (max_rate - min_rate) or exact operator-supplied zone rate (physical mode)",
        ],
        "decision_outputs": {
            "vra_prescription": {
                "description": "Variable-rate application files support either an imagery-derived relative 0-100% intensity or an explicit operator/agronomist-supplied physical rate plan. Physical mode records product, rate basis, unit, strategy or exact zone rates, equipment bounds, and approver; it never infers dose from imagery.",
                "warning": "Relative mode requires downstream mapping to approved physical rates. Physical mode only spatially encodes supplied rates; users must verify product, units, controller interpretation, equipment calibration, field boundaries, labels, and legal compliance before application.",
            },
            "stand_screening": {
                "description": "Optional early-season multispectral screening. Counts isolated NDVI/MSAVI2 vegetation components inside an operator-supplied AOI after configured resolution, coverage, separability, canopy-cover, physical-size, and optional row-periodicity gates.",
                "warning": "Counts vegetation objects, not crop identity. Weeds may pass; touching plants may merge; fragmented plants may split. Validate crop/stage/sensor protocols against independent manual counts before stand or replant decisions.",
            },
            "stand_action_candidates": {
                "description": "Optional conservative layers derived only after a row model passes: interior in-row gap candidates, off-row vegetation candidates, and local stand-shortfall scouting zones.",
                "warning": "These are not definitive planter skips, weed diagnoses, or replant prescriptions. Fully missing rows and row-end gaps are not detected. Curved/multiple-orientation rows require a different validated model.",
            },
            "spot_spray_mission": {
                "description": "Optional/export-disabled by default. Agriculture pipeline exports review geometry only (stress-hotspot polygons as GeoJSON) unless a separate operator-reviewed mission export workflow is explicitly enabled and validated. The underlying hotspot geometry remains available for downstream tooling.",
                "warning": "Mission file generation is intentionally gated. Any derived flight plan MUST be independently reviewed and validated in the target flight-planning environment before flight. Aircraft firmware, payload behaviour, local airspace, obstacles, weather, pilot certification, and chemical-application legality remain solely the operator's responsibility. OrthoSWIFT does not generate or endorse autonomous spray missions.",
            },
        },
        "references": refs,
        "limitations": [
            "Vegetation indices measure optical reflectance, not absolute biomass or nutrient concentration.",
            "Stress hotspots are relative to the flight extent and require ground scouting for diagnosis.",
            "Stand-screening thresholds and physical size bounds are protocol-specific and are not universal defaults.",
            "Stand-shortfall zones are scouting priorities only; imagery alone does not determine replanting.",
        ],
        "quality_boundaries": common_disclaimers["quality_boundaries"],
        "methodology_json": common_disclaimers["methodology_json"],
    }
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(methods, f, indent=2)
        
    return out_path
