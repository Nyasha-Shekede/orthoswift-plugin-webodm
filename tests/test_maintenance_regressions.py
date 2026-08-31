import ast
import hashlib
import json
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).parents[1]


def test_worker_import_bootstrap_has_no_namespace_patching_or_bare_fallbacks():
    source = (ROOT / "orthoswift/plugin.py").read_text(encoding="utf-8")
    assert "setattr(shapely" not in source
    assert "from runner import run" not in source
    assert "site.addsitedir" not in source


def test_core_imports_do_not_silence_third_party_loggers():
    for relative in ("orthoswift/core/pipeline.py", "orthoswift/core/report.py"):
        source = (ROOT / relative).read_text(encoding="utf-8")
        tree = ast.parse(source)
        calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)]
        assert not any(
            isinstance(call.func, ast.Attribute)
            and call.func.attr == "setLevel"
            for call in calls
        )


def test_report_font_paths_point_to_bundled_assets():
    from orthoswift.core import report
    assert report.font_path.is_file()
    assert report.inter_path.is_file()
    assert report.USE_SPACE_GROTESK
    assert report.USE_INTER


def test_release_builder_outputs_self_consistent_archives(tmp_path):
    # Run the real builder, then verify every checksum using its recorded name.
    subprocess.run([sys.executable, "build_release.py"], cwd=ROOT, check=True)
    dist = ROOT / "dist"
    for checksum_path in dist.glob("*.sha256"):
        digest, filename = checksum_path.read_text(encoding="utf-8").split()
        artifact = dist / filename
        assert artifact.is_file()
        assert hashlib.sha256(artifact.read_bytes()).hexdigest() == digest
    with zipfile.ZipFile(dist / "orthoswift-webodm-plugin.zip") as archive:
        assert {name.split("/")[0] for name in archive.namelist()} == {"orthoswift"}
        assert "orthoswift/manifest.json" in archive.namelist()
        assert "orthoswift/requirements.txt" in archive.namelist()
        assert "orthoswift/CHANGELOG.md" in archive.namelist()
        assert "orthoswift/core/guide.py" not in archive.namelist()


def test_manifest_points_to_this_repository():
    manifest = json.loads((ROOT / "orthoswift/manifest.json").read_text(encoding="utf-8"))
    assert manifest["repository"] == "https://github.com/Nyasha-Shekede/orthoswift-plugin-webodm"
    assert manifest["email"]
