"""
orthoswift_post.exports
=================================

No-QGIS delivery exports for OrthoSWIFT.

Adds two deterministic exports:
    * XYZ/CSV elevation grid from a DEM
    * KML export from existing polygon GeoDataFrames

"""
from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Mapping, Optional, Sequence

import geopandas as gpd
import numpy as np
import rasterio
from shapely.geometry import MultiPolygon, Polygon


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
    """Write the agriculture analytics methodology and operating limitations."""
    if domain != "agriculture":
        raise ValueError("Only the agriculture methodology is available")

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    methods = {
        "schema": "orthoswift.analytics_methodology.v1",
        "domain": "agriculture",
        "deterministic": True,
        "methods": {
            "vegetation_indices": [
                "NDVI = (NIR - R) / (NIR + R)",
                "NDRE = (NIR - RE) / (NIR + RE)",
                "GLI = (2G - R - B) / (2G + R + B)",
                "MSAVI2 = (2NIR + 1 - sqrt((2NIR + 1)^2 - 8(NIR - R))) / 2",
            ],
            "management_zones": (
                "Valid vegetation-index pixels are clustered into the requested number "
                "of relative-vigor zones and converted to reviewable polygons."
            ),
            "fertilizer_prescription": (
                "Relative mode maps zone vigor to 0-100% intensity. Physical mode only "
                "spatially encodes rates supplied and approved by the operator or agronomist."
            ),
            "spot_spray_targets": (
                "Low-NDVI connected regions are exported as relative stress targets for "
                "ground scouting and optional operator-reviewed section control."
            ),
        },
        "references": {
            "MacQueen_1967": (
                "MacQueen J. Some methods for classification and analysis of multivariate "
                "observations. Proceedings of the Fifth Berkeley Symposium. 1967."
            ),
            "Rouse_1974": (
                "Rouse JW et al. Monitoring vegetation systems in the Great Plains with "
                "ERTS. NASA SP-351. 1974."
            ),
            "Serra_1982": (
                "Serra J. Image Analysis and Mathematical Morphology. Academic Press. 1982."
            ),
        },
        "limitations": [
            "Vegetation indices measure optical reflectance, not crop identity, absolute biomass, nutrient deficiency, disease, or weed species.",
            "Stress targets are relative to the analyzed flight and require ground scouting before treatment.",
            "Relative prescription rates require mapping to an approved physical rate outside OrthoSWIFT.",
            "Physical prescription rates originate from the named operator or agronomist; OrthoSWIFT does not infer agronomic dose.",
            "DJI Agras archives are structurally validated exports, not firmware or controller certification.",
            "Users must verify coordinate reference systems, geometry, units, controller interpretation, equipment calibration, labels, boundaries, field conditions, and legal compliance.",
        ],
        "artifact_location": "technical_gis/data_summaries/analytics_methodology.json",
    }
    out_path.write_text(json.dumps(methods, indent=2), encoding="utf-8")
    return out_path
