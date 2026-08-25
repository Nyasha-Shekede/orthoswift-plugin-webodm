"""
orthoswift_post.vegetation
==========================

Agriculture-focused analytics from orthomosaic + (optional) multispectral
indices.

Indices supported:

* NDVI = (NIR - R) / (NIR + R)         (Rouse et al., 1974; NASA: https://earthobservatory.nasa.gov/features/MeasuringVegetation/measuring_vegetation_2.php)
* NDRE = (NIR - RedEdge) / (NIR + RedEdge)  (Barnes et al., 2000)
* GLI  = (2G - R - B) / (2G + R + B)   (Louhaichi et al., 2001) — RGB-only fallback
* ExG  = 2G - R - B                     (Woebbecke et al., 1995) — RGB-only fallback

Analytics:

* Management zones (k-means, k in [2, 5]) on a stack of vegetation indices.
* Stress hotspots: bottom-X-percentile connected components of NDVI.
* Canopy cover: vegetation/non-vegetation threshold + percent cover per zone.

Citations:

    Rouse, J.W. Jr., et al., 1974. Monitoring vegetation systems in the
        Great Plains with ERTS. NASA SP-351.
    Barnes, E.M. et al., 2000. Coincident detection of crop water stress,
        nitrogen status, and canopy density using ground based multispectral
        data. Proc. 5th Intl. Conf. on Precision Agriculture.
    Louhaichi, M., Borman, M.M., Johnson, D.E., 2001. Spatially located
        platform and aerial photography for documentation of grazing impacts
        on wheat. Geocarto International 16(1): 65-70.
    Woebbecke, D.M. et al., 1995. Color indices for weed identification
        under various soil, residue, and lighting conditions. Trans. ASAE
        38(1): 259-269.
    Zone delineation by clustering: see MDPI Agriculture 12(2):231,
        https://www.mdpi.com/2077-0472/12/2/231 and PMC8779988,
        https://pmc.ncbi.nlm.nih.gov/articles/PMC8779988/.
"""
from __future__ import annotations

from typing import Optional, Sequence, Tuple

import numpy as np
from scipy import ndimage as ndi
from rasterio.transform import Affine
from rasterio import features
from shapely.geometry import shape
import geopandas as gpd
from sklearn.cluster import MiniBatchKMeans


# --------------------------------------------------------------------------
# Index math (all inputs must be float, scaled in their natural range)
# --------------------------------------------------------------------------
def _safe_div(num, den, *, denominator_epsilon: float = 1e-6, valid_range: tuple[float, float] | None = (-1.0, 1.0)):
    """Safe division for normalized vegetation-index ratios.

    Values with near-zero denominators are set to NaN. For normalized
    indices, values outside ``valid_range`` are also set to NaN rather than
    silently propagating physically meaningless ratios caused by negative or
    poorly calibrated reflectance values.
    """
    num = np.asarray(num, dtype="float32")
    den = np.asarray(den, dtype="float32")
    out = np.full(np.broadcast_shapes(num.shape, den.shape), np.nan, dtype="float32")
    valid = np.isfinite(num) & np.isfinite(den) & (np.abs(den) >= denominator_epsilon)
    np.divide(num, den, out=out, where=valid)
    if valid_range is not None:
        lo, hi = valid_range
        out[(out < lo) | (out > hi)] = np.nan
    return out


def ndvi(red: np.ndarray, nir: np.ndarray) -> np.ndarray:
    return _safe_div(nir - red, nir + red)


def ndre(red_edge: np.ndarray, nir: np.ndarray) -> np.ndarray:
    return _safe_div(nir - red_edge, nir + red_edge)


def gli(red: np.ndarray, green: np.ndarray, blue: np.ndarray) -> np.ndarray:
    return _safe_div(2 * green - red - blue, 2 * green + red + blue)




def msavi2(red: np.ndarray, nir: np.ndarray) -> np.ndarray:
    """Modified Soil-Adjusted Vegetation Index 2.

    Qi et al. MSAVI2 = (2*NIR + 1 - sqrt((2*NIR + 1)^2 - 8*(NIR - red))) / 2.
    Invalid square-root domains and values outside [-1, 1] are masked.
    """
    red = np.asarray(red, dtype="float32")
    nir = np.asarray(nir, dtype="float32")
    term = (2.0 * nir + 1.0) ** 2 - 8.0 * (nir - red)
    out = np.full(np.broadcast_shapes(red.shape, nir.shape), np.nan, dtype="float32")
    valid = np.isfinite(red) & np.isfinite(nir) & (term >= 0.0)
    out[valid] = (2.0 * nir[valid] + 1.0 - np.sqrt(term[valid])) / 2.0
    out[(out < -1.0) | (out > 1.0)] = np.nan
    return out


def water_index_green_gt_nir(green: np.ndarray, nir: np.ndarray, margin: float = 0.0) -> np.ndarray:
    """Simple water/standing-wetness screening mask: green reflectance > NIR.

    This is deliberately conservative and only used as an exclusion screen when
    both bands are available. It is not a hydrology classification product.
    """
    green = np.asarray(green, dtype="float32")
    nir = np.asarray(nir, dtype="float32")
    return np.isfinite(green) & np.isfinite(nir) & (green > (nir + margin))




# --------------------------------------------------------------------------
# Classification, masking
# --------------------------------------------------------------------------
def classify_ndvi(ndvi_arr: np.ndarray,
                  breaks: Sequence[float] = (0.2, 0.4, 0.6)
                  ) -> np.ndarray:
    """Classify NDVI into 4 bins by default:
       0: bare/soil/water    (NDVI < 0.2)
       1: sparse/early       (0.2 <= NDVI < 0.4)
       2: moderate           (0.4 <= NDVI < 0.6)
       3: dense              (NDVI >= 0.6)

    These breakpoints follow USGS Landsat vegetation guidance
    (https://www.usgs.gov/special-topics/landsat-vegetation-condition-products)
    and are commonly used in the literature for general crop monitoring.
    Tune per crop and growth stage in production.
    """
    out = np.full(ndvi_arr.shape, -1, dtype="int16")
    finite = np.isfinite(ndvi_arr)
    b = sorted(breaks)
    out[finite] = 0
    for i, t in enumerate(b, start=1):
        out[finite & (ndvi_arr >= t)] = i
    return out




def clean_crop_mask(
    ndvi_arr: np.ndarray,
    transform: Affine,
    *,
    msavi2_arr: Optional[np.ndarray] = None,
    nir_arr: Optional[np.ndarray] = None,
    green_arr: Optional[np.ndarray] = None,
    ndvi_threshold: float = 0.20,
    msavi2_threshold: Optional[float] = 0.15,
    nir_shadow_threshold: Optional[float] = 0.05,
    water_green_gt_nir: bool = True,
    min_crop_component_m2: float = 2.0,
    min_non_crop_component_m2: float = 10.0,
    edge_buffer_m: float = 1.0,
) -> tuple[np.ndarray, dict]:
    """Build a conservative crop-domain mask before baseline stats/zoning.

    The mask is intentionally low-overhead: threshold arrays, remove tiny crop
    islands, exclude large contiguous non-crop holes, and optionally erode the
    outer field edge. This captures the useful QC ideas without building an
    overfit weed/soil classifier.
    """
    ndvi_arr = np.asarray(ndvi_arr, dtype="float32")
    if ndvi_arr.ndim != 2:
        raise ValueError("ndvi_arr must be a 2-D array")
    for name, optional in (("msavi2_arr", msavi2_arr), ("nir_arr", nir_arr), ("green_arr", green_arr)):
        if optional is not None and np.asarray(optional).shape != ndvi_arr.shape:
            raise ValueError(f"{name} shape must match ndvi_arr")
    if min_crop_component_m2 < 0 or min_non_crop_component_m2 < 0 or edge_buffer_m < 0:
        raise ValueError("component areas and edge_buffer_m must be non-negative")
    valid = np.isfinite(ndvi_arr)
    crop = valid & (ndvi_arr >= ndvi_threshold)

    if msavi2_arr is not None and msavi2_threshold is not None:
        m = np.asarray(msavi2_arr, dtype="float32")
        crop &= np.isfinite(m) & (m >= msavi2_threshold)

    shadow = np.zeros(crop.shape, dtype=bool)
    if nir_arr is not None and nir_shadow_threshold is not None:
        nir = np.asarray(nir_arr, dtype="float32")
        shadow = np.isfinite(nir) & (nir < nir_shadow_threshold)
        crop &= ~shadow

    water = np.zeros(crop.shape, dtype=bool)
    if water_green_gt_nir and green_arr is not None and nir_arr is not None:
        water = water_index_green_gt_nir(green_arr, nir_arr)
        crop &= ~water

    # Shadows and water/wetness screens are absolute exclusions. They must not
    # be filled back in by the small-hole/rice-grain cleanup step below.
    absolute_exclude = shadow | water

    dx, dy = abs(transform.a), abs(transform.e)
    pixel_area = abs(transform.a * transform.e - transform.b * transform.d)
    min_crop_px = max(1, int(np.ceil(min_crop_component_m2 / pixel_area)))
    min_non_crop_px = max(1, int(np.ceil(min_non_crop_component_m2 / pixel_area)))

    crop_before = int(np.count_nonzero(crop))
    crop = _remove_small_components(crop, min_crop_px)

    # Large non-crop holes inside the finite vegetation-index footprint are kept
    # excluded. Tiny non-crop holes are filled so sparse bare soil between rows
    # does not create rice-grain noise in downstream vector products.
    non_crop = valid & ~crop
    large_non_crop = _remove_small_components(non_crop, min_non_crop_px)
    crop = valid & ~large_non_crop & ~absolute_exclude

    edge_buffer_px = int(round(edge_buffer_m / max(dx, dy))) if edge_buffer_m and edge_buffer_m > 0 else 0
    if edge_buffer_px > 0 and crop.any():
        crop = ndi.binary_erosion(crop, iterations=edge_buffer_px, border_value=0)

    metrics = {
        "valid_pixels": int(np.count_nonzero(valid)),
        "crop_pixels_before_component_filter": crop_before,
        "crop_pixels_after_qc": int(np.count_nonzero(crop)),
        "shadow_pixels": int(np.count_nonzero(shadow & valid)),
        "water_pixels": int(np.count_nonzero(water & valid)),
        "large_non_crop_pixels": int(np.count_nonzero(large_non_crop)),
        "min_crop_component_m2": float(min_crop_component_m2),
        "min_non_crop_component_m2": float(min_non_crop_component_m2),
        "edge_buffer_m": float(edge_buffer_m or 0.0),
        "ndvi_threshold": float(ndvi_threshold),
        "msavi2_threshold": None if msavi2_threshold is None else float(msavi2_threshold),
        "nir_shadow_threshold": None if nir_shadow_threshold is None else float(nir_shadow_threshold),
    }
    return crop.astype(bool), metrics


# --------------------------------------------------------------------------
# Management zones (k-means, post-smoothed)
# --------------------------------------------------------------------------
def _remove_small_components(mask: np.ndarray, min_pixels: int) -> np.ndarray:
    if min_pixels <= 1:
        return mask
    labels, n = ndi.label(mask)
    if n == 0:
        return mask
    counts = np.bincount(labels.ravel())
    keep = counts >= min_pixels
    keep[0] = False
    return keep[labels]






def _polygonize_zone_mask(mask: np.ndarray, transform: Affine):
    """Polygonize a boolean mask at native raster resolution.

    Native-resolution polygonization keeps operational vector geometry aligned
    with the source label raster. Display-only simplification should happen in
    preview layers, not in machine/action deliverables.
    """
    merged = []
    mask_u8 = mask.astype("uint8")
    for geom, val in features.shapes(mask_u8, mask=mask, transform=transform):
        if val != 1:
            continue
        s = shape(geom)
        if not s.is_valid:
            s = s.buffer(0)
        if not s.is_empty:
            merged.append(s)
    if not merged:
        return None
    from shapely.ops import unary_union
    try:
        u = unary_union(merged)
    except Exception:
        u = unary_union([g.buffer(0) for g in merged])
    if not u.is_valid:
        u = u.buffer(0)
    return None if u.is_empty else u

def _merge_small_zone_components(
    label_raster: np.ndarray,
    valid_mask: np.ndarray,
    class_count: int,
    min_pixels: int,
) -> Tuple[np.ndarray, dict]:
    """Merge small class fragments into the nearest retained zone.

    Unlike setting small fragments to NoData, this preserves complete coverage
    of the valid analysis domain. Distance is measured in pixel space; for
    north-up rasters with nearly square pixels this is appropriate. If strongly
    anisotropic pixels are later supported, pass physical sampling distances to
    distance_transform_edt.
    """
    labels = np.asarray(label_raster, dtype="int16").copy()
    valid = np.asarray(valid_mask, dtype=bool)
    remove = np.zeros(labels.shape, dtype=bool)
    metrics = {
        "small_components_n": 0,
        "small_pixels_reassigned": 0,
    }

    for class_id in range(class_count):
        class_mask = valid & (labels == class_id)
        components, n_components = ndi.label(class_mask)
        if n_components == 0:
            continue

        counts = np.bincount(components.ravel())
        small_ids = np.flatnonzero(counts < min_pixels)
        small_ids = small_ids[small_ids != 0]
        if small_ids.size:
            class_remove = np.isin(components, small_ids)
            remove |= class_remove
            metrics["small_components_n"] += int(small_ids.size)
            metrics["small_pixels_reassigned"] += int(class_remove.sum())

    if not remove.any():
        return labels, metrics

    retained = valid & ~remove
    if not retained.any():
        raise ValueError(
            "The management-zone minimum area removes every zone component; "
            "reduce min_area_m2"
        )

    # For every removed pixel, find the closest retained pixel and copy its
    # class. This merges fragments into nearby operational zones instead of
    # converting valid field area to NoData.
    _, nearest = ndi.distance_transform_edt(
        ~retained,
        return_distances=True,
        return_indices=True,
    )
    labels[remove] = labels[nearest[0][remove], nearest[1][remove]]
    labels[~valid] = -1

    if np.any(valid & (labels < 0)):
        raise RuntimeError("Valid management-zone pixels remain unassigned")

    return labels, metrics


def _relative_zone_labels(k: int) -> list[str]:
    labels_by_k = {
        2: ["lower_relative_vigor", "higher_relative_vigor"],
        3: [
            "lower_relative_vigor",
            "typical_relative_vigor",
            "higher_relative_vigor",
        ],
        4: [
            "lowest_relative_vigor",
            "lower_relative_vigor",
            "higher_relative_vigor",
            "highest_relative_vigor",
        ],
        5: [
            "lowest_relative_vigor",
            "lower_relative_vigor",
            "typical_relative_vigor",
            "higher_relative_vigor",
            "highest_relative_vigor",
        ],
    }
    if k not in labels_by_k:
        raise ValueError(f"No relative label scheme defined for k={k}")
    return labels_by_k[k]


def management_zones(
    stack: np.ndarray,
    transform: Affine,
    crs,
    *,
    k_range: Tuple[int, int] = (2, 5),
    field_mask: Optional[np.ndarray] = None,
    smoothing_iter: int = 1,
    min_area_m2: float = 100.0,
    random_state: int = 42,
    uniform_threshold: float = 0.25,
    sample_size: int = 10000,
    feature_names: Optional[Sequence[str]] = None,
) -> Tuple[np.ndarray, gpd.GeoDataFrame]:
    """K-means cluster pixels into management zones with automatic K selection.

    The returned label raster uses ``-1`` for nodata. Vector polygons are
    polygonized from the final native-resolution label raster, so geometry area,
    raster area, and attributes stay consistent for operational use.
    """
    from sklearn.metrics import silhouette_score

    if stack.ndim != 3:
        raise ValueError("stack must be (bands, H, W)")
    if (not isinstance(k_range, tuple) or len(k_range) != 2 or
            not all(isinstance(k, (int, np.integer)) for k in k_range) or
            k_range[0] < 2 or k_range[1] < k_range[0]):
        raise ValueError("k_range must be an increasing (min_k, max_k) integer tuple with min_k >= 2")
    if sample_size < k_range[1]:
        raise ValueError("sample_size must be at least max(k_range)")
    if smoothing_iter < 0 or min_area_m2 < 0:
        raise ValueError("smoothing_iter and min_area_m2 must be non-negative")
    b, H, W = stack.shape
    if feature_names is None:
        feature_names = [f"band{i}" for i in range(b)]
    if len(feature_names) != b or len(set(feature_names)) != b:
        raise ValueError("feature_names must contain one unique name per stack band")
    flat = stack.reshape(b, -1).T
    finite = np.all(np.isfinite(flat), axis=1)
    if field_mask is not None:
        if field_mask.shape != (H, W):
            raise ValueError("field_mask shape must match stack spatial shape")
        finite &= field_mask.reshape(-1).astype(bool)

    X = flat[finite]
    if X.shape[0] < k_range[0] * 10 or k_range[1] >= X.shape[0]:
        raise ValueError("Too few finite pixels for requested k_range")

    if X.shape[0] < 50:
        best_k = k_range[0]
    else:
        rng = np.random.default_rng(random_state)
        if X.shape[0] > sample_size:
            idx = rng.choice(X.shape[0], size=sample_size, replace=False)
            X_sample = X[idx]
        else:
            X_sample = X

        scores = {}
        for k in range(k_range[0], k_range[1] + 1):
            km = MiniBatchKMeans(n_clusters=k, random_state=random_state,
                                 n_init=10, batch_size=1024)
            labels = km.fit_predict(X_sample)
            if len(np.unique(labels)) < 2:
                scores[k] = -1.0
                continue
            scores[k] = float(silhouette_score(
                X_sample, labels,
                sample_size=min(5000, len(X_sample)),
                random_state=random_state,
            ))
        best_k = max(scores, key=scores.get)
        if scores[best_k] < uniform_threshold:
            best_k = k_range[0]

    zone_labels = _relative_zone_labels(best_k)

    km = MiniBatchKMeans(n_clusters=best_k, random_state=random_state,
                         n_init=10, batch_size=1024)
    labels_flat = km.fit_predict(X)

    means = np.asarray(km.cluster_centers_[:, 0], dtype="float64")
    order = np.argsort(means)
    remap = np.empty(best_k, dtype="int16")
    remap[order] = np.arange(best_k, dtype="int16")
    labels_flat = remap[labels_flat]

    label_raster = np.full(H * W, -1, dtype="int16")
    label_raster[finite] = labels_flat
    label_raster = label_raster.reshape(H, W)

    valid2d = finite.reshape(H, W)
    if smoothing_iter > 0:
        for _ in range(smoothing_iter):
            smoothed = ndi.median_filter(label_raster, size=3)
            label_raster = np.where(
                valid2d & (smoothed >= 0),
                smoothed,
                np.where(valid2d, label_raster, -1),
            ).astype("int16")

    pixel_area = abs(
        transform.a * transform.e - transform.b * transform.d
    )
    min_pixels = max(1, int(np.ceil(min_area_m2 / pixel_area)))

    label_raster, merge_metrics = _merge_small_zone_components(
        label_raster,
        valid2d,
        best_k,
        min_pixels,
    )

    polys, rows = [], []
    for c in range(best_k):
        cls_mask = label_raster == c
        if not cls_mask.any():
            continue

        geom = _polygonize_zone_mask(cls_mask, transform)
        if geom is None:
            continue

        raster_area_m2 = float(cls_mask.sum()) * pixel_area
        geometry_area_m2 = float(geom.area)
        cls_pix = cls_mask
        band_means = [float(np.nanmean(stack[bi][cls_pix])) for bi in range(b)]
        band_medians = [float(np.nanmedian(stack[bi][cls_pix])) for bi in range(b)]
        row = {
            "zone_id": int(c),
            "area_m2": raster_area_m2,
            "raster_area_m2": raster_area_m2,
            "geometry_area_m2": geometry_area_m2,
            "area_delta_pct": float((geometry_area_m2 - raster_area_m2) / raster_area_m2 * 100.0) if raster_area_m2 else 0.0,
            "small_components_merged_total": int(
                merge_metrics["small_components_n"]
            ),
            "small_pixels_reassigned_total": int(
                merge_metrics["small_pixels_reassigned"]
            ),
            "relative_vigor_label": zone_labels[c],
            "cluster_rank": int(c + 1),
            "cluster_count": int(best_k),
        }
        for bi, m in enumerate(band_means):
            row[f"band{bi}_mean"] = m
            row[f"{feature_names[bi]}_mean"] = m
        for bi, m in enumerate(band_medians):
            row[f"band{bi}_median"] = m
            row[f"{feature_names[bi]}_median"] = m
        row["primary_index"] = str(feature_names[0])
        polys.append(geom)
        rows.append(row)

    gdf = gpd.GeoDataFrame(rows, geometry=polys, crs=crs)
    if len(gdf):
        gdf = gdf[gdf["raster_area_m2"] >= min_area_m2].reset_index(drop=True)
    return label_raster, gdf


# --------------------------------------------------------------------------
# Stress hotspots
# --------------------------------------------------------------------------
def stress_hotspots(
    ndvi_arr: np.ndarray,
    transform: Affine,
    crs,
    *,
    percentile: float = 10.0,
    field_mask: Optional[np.ndarray] = None,
    crop_min_ndvi: float = 0.20,
    stress_min_ndvi: float = 0.20,
    min_area_m2: float = 50.0,
    smoothing_px: int = 1,
    zscore_cutoff: Optional[float] = 1.5,
    min_threshold_ndvi: Optional[float] = None,
    max_threshold_ndvi: Optional[float] = None,
) -> gpd.GeoDataFrame:
    """Low-NDVI crop stress candidates after clean crop-domain masking.

    The hotspot threshold is calculated only from the supplied crop-domain mask.
    By default, the effective cutoff is the stricter of a lower percentile and
    a robust median/MAD z-score threshold. This reduces baseline corruption from
    bare soil, roads, shadows, water, and severe non-crop gaps.
    """
    if not (0.0 <= percentile <= 100.0):
        raise ValueError("percentile must be in [0, 100]")
    if min_area_m2 < 0:
        raise ValueError("min_area_m2 must be non-negative")
    if smoothing_px < 0:
        raise ValueError("smoothing_px must be non-negative")
    z = np.where(np.isfinite(ndvi_arr), ndvi_arr.astype("float32", copy=False), np.nan)
    if field_mask is not None and np.asarray(field_mask).shape != z.shape:
        raise ValueError("field_mask shape must match ndvi_arr")

    crop_mask = np.isfinite(z) & (z >= crop_min_ndvi)
    if field_mask is not None:
        crop_mask &= field_mask.astype(bool)

    finite_crop = z[crop_mask]
    if finite_crop.size < 100:
        return gpd.GeoDataFrame({"geometry": []}, geometry="geometry", crs=crs)

    percentile_thr = float(np.percentile(finite_crop, percentile))
    median = float(np.median(finite_crop))
    mad = float(np.median(np.abs(finite_crop - median)))
    # MAD=0 is common in quantized/piecewise-uniform maps. Falling back to the
    # global standard deviation can move the cutoff below every low-vigor mode
    # and silently suppress obvious patches, so use the percentile in that case.
    robust_sigma = 1.4826 * mad
    zscore_thr = median - float(zscore_cutoff) * robust_sigma if zscore_cutoff is not None and robust_sigma > 0 else percentile_thr
    thr = min(percentile_thr, zscore_thr)
    # A robust-z cutoff below the admissible stress range makes the candidate
    # mask empty by construction. Quantized and multimodal crop maps commonly
    # produce this case; retain the explicitly requested percentile cutoff.
    if thr < stress_min_ndvi <= percentile_thr:
        thr = percentile_thr
    if min_threshold_ndvi is not None:
        thr = max(thr, float(min_threshold_ndvi))
    if max_threshold_ndvi is not None:
        thr = min(thr, float(max_threshold_ndvi))

    mask = crop_mask & (z <= thr) & (z >= stress_min_ndvi)

    if smoothing_px > 0:
        mask = ndi.binary_opening(mask, iterations=smoothing_px)
        mask = ndi.binary_closing(mask, iterations=smoothing_px)

    pixel_area = abs(transform.a * transform.e - transform.b * transform.d)
    min_pixels = max(1, int(np.ceil(min_area_m2 / pixel_area)))
    labels, n = ndi.label(mask)
    if n == 0:
        return gpd.GeoDataFrame({"geometry": []}, geometry="geometry", crs=crs)

    counts = np.bincount(labels.ravel(), minlength=n + 1)
    keep = counts >= min_pixels
    keep[0] = False
    filtered_mask = keep[labels]
    if not filtered_mask.any():
        return gpd.GeoDataFrame({"geometry": []}, geometry="geometry", crs=crs)

    from shapely.geometry import Polygon, MultiPolygon, GeometryCollection, mapping
    from rasterio.features import geometry_mask

    polys, rows = [], []
    hotspot_id = 1
    mask_u8 = filtered_mask.astype("uint8")
    for geom, val in features.shapes(mask_u8, mask=filtered_mask, transform=transform):
        if val != 1:
            continue
        s = shape(geom)
        if not s.is_valid:
            s = s.buffer(0)

        parts = []
        if isinstance(s, Polygon):
            parts = [s]
        elif isinstance(s, MultiPolygon):
            parts = list(s.geoms)
        elif isinstance(s, GeometryCollection):
            parts = [g for g in s.geoms if isinstance(g, Polygon)]

        for part in parts:
            if part.is_empty:
                continue
            geom_mask = geometry_mask([mapping(part)], out_shape=ndvi_arr.shape, transform=transform, invert=True)
            pix_mask = geom_mask & filtered_mask
            area_m2 = float(np.count_nonzero(pix_mask)) * pixel_area
            if area_m2 < min_area_m2:
                continue
            vals = z[pix_mask & np.isfinite(z)]
            if vals.size == 0:
                continue
            mean_ndvi = float(np.nanmean(vals))
            median_ndvi = float(np.nanmedian(vals))
            rows.append({
                "hotspot_id": int(hotspot_id),
                "area_m2": round(area_m2, 2),
                "raster_area_m2": round(area_m2, 2),
                "geometry_area_m2": round(float(part.area), 2),
                "area_delta_pct": round(float((float(part.area) - area_m2) / area_m2 * 100.0), 3) if area_m2 else 0.0,
                "mean_ndvi": round(mean_ndvi, 3),
                "median_ndvi": round(median_ndvi, 3),
                "threshold_ndvi": round(float(thr), 3),
                "percentile_threshold_ndvi": round(float(percentile_thr), 3),
                "robust_z_threshold_ndvi": round(float(zscore_thr), 3),
                "baseline_median_ndvi": round(float(median), 3),
                "baseline_robust_sigma": round(float(robust_sigma), 3),
                "crop_min_ndvi": round(float(crop_min_ndvi), 3),
                "stress_min_ndvi": round(float(stress_min_ndvi), 3),
                "screening_layer": True,
                "Area (sq m)": round(area_m2, 2),
                "Mean NDVI": round(mean_ndvi, 3),
                "Median NDVI": round(median_ndvi, 3),
                "Threshold NDVI": round(float(thr), 3),
            })
            polys.append(part)
            hotspot_id += 1

    gdf = gpd.GeoDataFrame(rows, geometry=polys, crs=crs)
    if len(gdf):
        gdf = gdf.sort_values("mean_ndvi").reset_index(drop=True)
        gdf["severity_rank"] = np.arange(1, len(gdf) + 1)
        gdf["Severity Rank"] = gdf["severity_rank"].astype(int)
    return gdf


def canopy_cover_summary(veg_mask: np.ndarray, transform: Affine,
                         zones: Optional[gpd.GeoDataFrame] = None,
                         valid_mask: Optional[np.ndarray] = None
                         ) -> dict:
    """Whole-field and optionally per-zone canopy cover (%).
    
    veg_mask is True for vegetation. valid_mask should be True only for pixels
    inside the valid field/raster domain. If valid_mask is omitted, all pixels
    are treated as valid for backward compatibility.
    """
    veg_mask = np.asarray(veg_mask, dtype=bool)
    if veg_mask.ndim != 2:
        raise ValueError("veg_mask must be a 2-D array")
    if valid_mask is not None and np.asarray(valid_mask).shape != veg_mask.shape:
        raise ValueError("valid_mask shape must match veg_mask")
    pixel_area = abs(transform.a * transform.e - transform.b * transform.d)
    if valid_mask is None:
        valid = np.ones_like(veg_mask, dtype=bool)
    else:
        valid = valid_mask.astype(bool)
    
    total = float(np.sum(valid)) * pixel_area
    veg = float(np.sum(veg_mask & valid)) * pixel_area
    overall = (veg / total * 100.0) if total > 0 else float("nan")
    out = {"overall_cover_pct": overall, "total_area_m2": total}
    if zones is not None and len(zones) > 0:
        out["zones"] = None
    return out
