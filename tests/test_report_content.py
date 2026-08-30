from PIL import Image as PILImage
from reportlab.lib.pagesizes import A4
from reportlab.platypus import Paragraph, Table

from orthoswift.core import report


def _story_text(story):
    values = []
    for item in story:
        if isinstance(item, Paragraph):
            values.append(item.text)
        elif isinstance(item, Table):
            values.extend(str(cell) for row in item._cellvalues for cell in row)
    return "\n".join(values)


def test_prescription_report_keeps_fertilizer_and_spot_spray_separate(tmp_path, monkeypatch):
    captured = {}

    class Document:
        pagesize = A4

        def __init__(self, *args, **kwargs):
            pass

        def build(self, story, **kwargs):
            captured["story"] = story

    monkeypatch.setattr(report, "SimpleDocTemplate", Document)
    preview = tmp_path / "preview.png"
    PILImage.new("RGB", (20, 20), "green").save(preview)

    report.build_agriculture_pdf(
        tmp_path / "prescription_report.pdf",
        ortho_preview=preview,
        ndvi_preview=preview,
        zone_table=[["Zone", "Relative vigor"], [1, "Low"]],
        hotspot_table=[["Target", "Area m²"], [1, 250]],
        canopy_cover_pct=72.5,
        coverage_metrics={
            "usable_field_area_m2": 20_000,
            "recommended_action_zone_area_m2": 18_000,
        },
        fertilizer_rate_summary={
            "mode": "physical",
            "operation": "fertilizer",
            "product_name": "Urea 46-0-0",
            "strategy": "inverse",
            "unit": "KG_HA",
            "resolved_min_rate": 100,
            "resolved_max_rate": 180,
            "treated_area_ha": 1.8,
            "estimated_total_product": 252,
            "total_product_unit": "KG",
            "approved_by": "Agronomist",
        },
        spot_spray_rate_summary={
            "mode": "physical",
            "operation": "spray",
            "product_name": "Herbicide",
            "strategy": "target_hotspots",
            "unit": "L_HA",
            "target_rate": 2,
            "background_rate": 0,
            "treated_area_ha": 0.025,
            "estimated_total_product": 0.05,
            "total_product_unit": "L",
            "approved_by": "Operator",
        },
        offline_basemap_included=False,
        controller_packages_available=True,
    )

    text = _story_text(captured["story"])
    assert "Prescription Report" in text
    assert "Fertilizer Prescription Summary" in text
    assert "Spot-Spray Prescription Summary" in text
    assert "Urea 46-0-0" in text
    assert "Herbicide" in text
    assert "100–180 KG_HA" in text
    assert "2 / 0 L_HA" in text
    assert "Estimated Canopy Cover" in text
    assert "Offline MBTiles were disabled" in text
    assert "Chemical / Area Savings" not in text
    assert "Spray Report" not in text
