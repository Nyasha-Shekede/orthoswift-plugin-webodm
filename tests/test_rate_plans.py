import zipfile
import geopandas as gpd
import pytest
from shapely.geometry import box

from orthoswift.core.decisions import (
    ApplicationRatePlan,
    build_vra_prescription_gdf,
    export_vra_shapefile_zip,
    resolve_application_rate_plan,
    validate_vra_shapefile_zip,
)


def zones():
    return gpd.GeoDataFrame(
        {"zone_id": [0, 1, 2], "ndvi_mean": [0.2, 0.5, 0.8]},
        geometry=[box(500000+i*10, 0, 500010+i*10, 10) for i in range(3)],
        crs="EPSG:32633",
    )


def physical(**updates):
    values=dict(mode="physical", operation="fertilizer", product_name="Urea 46-0-0",
                rate_basis="product", unit="KG_HA", strategy="explicit_by_zone",
                zone_rates={0:120,1:150,2:180}, equipment_min_rate=100,
                equipment_max_rate=200, approved_by="Operator")
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


def test_generic_and_deere_packages_validate(tmp_path):
    rx,_=resolve_application_rate_plan(zones(), physical())
    generic=export_vra_shapefile_zip(rx,tmp_path/'generic.zip')
    assert validate_vra_shapefile_zip(generic)["valid"]
    deere=export_vra_shapefile_zip(rx,tmp_path/'deere.zip',packaging_profile='john_deere_rx')
    result=validate_vra_shapefile_zip(deere,packaging_profile='john_deere_rx')
    assert result["valid"], result["errors"]
    with zipfile.ZipFile(deere) as archive:
        assert "Rx/fertilizer_zones.shp" in archive.namelist()
