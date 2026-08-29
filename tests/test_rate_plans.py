import zipfile

import geopandas as gpd
import pytest
from shapely.geometry import box

from orthoswift.core.decisions import (
    ApplicationRatePlan,
    build_vra_prescription_gdf,
    export_dji_agras_prescription_zip,
    resolve_application_rate_plan,
)


def zones():
    return gpd.GeoDataFrame(
        {"zone_id": [0, 1, 2], "ndvi_mean": [0.2, 0.5, 0.8]},
        geometry=[box(500000+i*10, 0, 500010+i*10, 10) for i in range(3)],
        crs="EPSG:32633",
    )


def physical(**updates):
    values={"mode": "physical", "operation": "fertilizer", "product_name": "Urea 46-0-0",
                "rate_basis": "product", "unit": "KG_HA", "strategy": "explicit_by_zone",
                "zone_rates": {0:120,1:150,2:180}, "equipment_min_rate": 100,
                "equipment_max_rate": 200, "approved_by": "Operator"}
    values.update(updates)
    return ApplicationRatePlan(**values)


def test_relative_plan_preserves_zero_to_100_contract():
    rx, summary=resolve_application_rate_plan(zones(), None)
    assert rx.TargetRate.tolist()==[100.0,50.0,0.0]
    assert set(rx.Rate_Unit)=={"PCT"}
    assert summary["mode"]=="relative"


def test_exact_physical_plan_and_total_product():
    rx, summary=resolve_application_rate_plan(zones(), physical())
    assert rx.TargetRate.tolist()==[120.0,150.0,180.0]
    assert set(rx.Cmd_Mode)=={"physical_product_rate"}
    assert summary["estimated_total_product"]==pytest.approx(4.5)


@pytest.mark.parametrize("changes,match", [
    ({"approved_by":""}, "approved_by"),
    ({"product_name":""}, "product_name"),
    ({"unit":"PCT"}, "unit"),
    ({"zone_rates":{0:120,1:150}}, "exactly match"),
    ({"zone_rates":{0:120,1:150,2:250}}, "equipment_max_rate"),
])
def test_invalid_physical_plans_refuse(changes, match):
    with pytest.raises(ValueError, match=match):
        resolve_application_rate_plan(zones(), physical(**changes))


def test_base_rate_alone_refuses():
    with pytest.raises(ValueError, match="base_rate alone"):
        build_vra_prescription_gdf(zones(), base_rate=150, rate_unit="KG_HA")


def test_dji_agras_package_validates(tmp_path):
    prescription, _ = resolve_application_rate_plan(zones(), physical())
    result = export_dji_agras_prescription_zip(prescription, tmp_path / "controllers")

    assert result["dji_agras_vra_validation"]["valid"]
    with zipfile.ZipFile(result["dji_agras_vra_zip"]) as archive:
        names = set(archive.namelist())
    assert "DJI/Shapefile/application_boundary.shp" in names
    assert "DJI/Rx/application_rate.tif" in names
    assert all(name.startswith("DJI/") for name in names)
