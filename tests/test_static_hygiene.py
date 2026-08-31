import ast
import json
from pathlib import Path

ROOT=Path(__file__).parents[1]


def test_all_python_and_json_parse():
    for path in ROOT.rglob('*.py'):
        if '.git' not in path.parts:
            ast.parse(path.read_text(encoding='utf-8'),filename=str(path))
    for path in ROOT.rglob('*.json'):
        if '.git' not in path.parts:
            json.loads(path.read_text(encoding='utf-8'))


def test_removed_backend_features_are_absent():
    files=['orthoswift/core/pipeline.py','orthoswift/core/decisions.py']
    text='\n'.join((ROOT/name).read_text(encoding='utf-8').lower() for name in files)
    for token in ['plantcountconfig','run_multispectral_plant_count_pipeline',
                  'build_replant_prescription_gdf','interrowvegetationconfig',
                  'export_spot_spray_kmz','trafficability_gdf','catch_berm_compliance_gdf']:
        assert token.lower() not in text


def test_visible_interface_files_are_present_and_template_self_contained():
    template=(ROOT/'orthoswift/templates/index.html').read_text(encoding='utf-8')
    assert 'id="osw-form"' in template
    controller=(ROOT/'orthoswift/public/main.js').read_text(encoding='utf-8')
    assert 'fertilizer_rate_plan' in controller
    assert '<script>' not in template
    assert (ROOT/'orthoswift/public/main.js').exists()
    assert (ROOT/'orthoswift/public/style.css').exists()

def test_webodm_page_loads_plugin_assets_and_keeps_controls_operable():
    template = (ROOT / "orthoswift/templates/index.html").read_text(encoding="utf-8")
    stylesheet = (ROOT / "orthoswift/public/style.css").read_text(encoding="utf-8")
    controller = (ROOT / "orthoswift/public/main.js").read_text(encoding="utf-8")

    assert template.startswith('{% extends "app/plugins/templates/base.html" %}')
    assert "{% block content %}" in template
    assert "{% endblock %}" in template
    assert "<style" not in template
    assert 'class="osw-switch-input"' in template
    assert 'aria-controls="osw-rate-fields"' in template
    assert 'aria-controls="osw-spot-rate-fields"' in template
    assert "#osw-run {\n  display: none" not in stylesheet
    assert "rateEnabledCb.addEventListener('change', updateRateToggle)" in controller
    assert "spotRateEnabledCb.addEventListener('change', updateSpotRateToggle)" in controller


def test_offline_basemap_option_is_wired_to_both_request_formats():
    template = (ROOT / "orthoswift/templates/index.html").read_text(encoding="utf-8")
    controller = (ROOT / "orthoswift/public/main.js").read_text(encoding="utf-8")

    assert 'id="osw-offline-basemap" checked' in template
    assert "var offlineBasemap = !!(offlineBasemapInput && offlineBasemapInput.checked)" in controller
    assert "uploadData.append('offline_basemap', String(offlineBasemap))" in controller
    assert "offline_basemap: offlineBasemap" in controller
    assert "uploadData.append('offline_basemap', 'true')" not in controller
    assert "offline_basemap: true" not in controller


def test_repository_has_no_patch_artifacts_or_retired_guide():
    assert not list(ROOT.glob("*.patch"))
    assert not (ROOT / "orthoswift/core/guide.py").exists()
    assert "*.patch" in (ROOT / ".gitignore").read_text(encoding="utf-8")


def test_release_workflow_publishes_only_immutable_version_tags():
    workflow = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
    assert 'branches:' not in workflow
    assert 'tags:' in workflow
    assert 'test "$GITHUB_REF_NAME" = "v$VERSION"' in workflow
    assert "gh release delete" not in workflow
    assert "--verify-tag" in workflow


def test_methodology_matches_the_public_agriculture_scope(tmp_path):
    from orthoswift.core.exports import export_analytics_methodology

    path = export_analytics_methodology(tmp_path / "methodology.json", domain="agriculture")
    methodology = json.loads(path.read_text(encoding="utf-8"))
    assert methodology["domain"] == "agriculture"
    assert "fertilizer_prescription" in methodology["methods"]
    assert "spot_spray_targets" in methodology["methods"]
    stale = json.dumps(methodology).lower()
    assert "plant_density" not in stale
    assert "replant" not in stale
    assert "inspection, construction, and mining" not in stale
