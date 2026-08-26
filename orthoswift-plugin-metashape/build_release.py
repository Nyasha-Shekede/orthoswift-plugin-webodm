import os
import zipfile
import hashlib
from pathlib import Path

def main():
    plugin_dir = Path(__file__).resolve().parent
    dist_dir = plugin_dir / "dist"
    dist_dir.mkdir(exist_ok=True)

    version_file = plugin_dir / "orthoswift" / "version.py"
    version_vars = {}
    if version_file.exists():
        exec(version_file.read_text(encoding="utf-8"), version_vars)
    version = version_vars.get("__version__", "1.0.0")

    zip_name = f"orthoswift-metashape-v{version}.zip"
    zip_path = dist_dir / zip_name

    root_files = [
        "install-windows.bat",
        "install-macos.command",
        "install-macos-linux.sh",
        "requirements.txt",
        "README.md",
        "LICENSE",
        "SECURITY.md",
        "CONTRIBUTING.md",
    ]

    exclude_dirs = {"__pycache__", ".pytest_cache", ".git", ".github", "tests", "dist", ".venv"}
    exclude_exts = {".pyc", ".pyo", ".pyd"}

    print(f"Building Metashape plugin release package: {zip_name}")

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        # 1. Package root installer files & documentation
        for rf in root_files:
            file_path = plugin_dir / rf
            if file_path.is_file():
                zf.write(file_path, arcname=rf)
                print(f"  + {rf}")

        # 2. Package orthoswift/ directory tree
        orthoswift_dir = plugin_dir / "orthoswift"
        for root, dirs, files in os.walk(orthoswift_dir):
            dirs[:] = [d for d in dirs if d not in exclude_dirs]
            for f in sorted(files):
                file_path = Path(root) / f
                if file_path.suffix not in exclude_exts and not f.startswith("."):
                    rel_path = file_path.relative_to(plugin_dir)
                    zf.write(file_path, arcname=str(rel_path).replace("\\", "/"))
                    print(f"  + {rel_path}")

    # Generate SHA-256 Checksum for versioned ZIP
    sha256 = hashlib.sha256(zip_path.read_bytes()).hexdigest()
    sha_file = dist_dir / f"{zip_name}.sha256"
    sha_file.write_text(f"{sha256}  {zip_name}\n", encoding="utf-8")

    # Also create unversioned bundle for convenience (matches CI artifact name)
    generic_zip = dist_dir / "orthoswift-metashape.zip"
    generic_zip.write_bytes(zip_path.read_bytes())
    generic_sha = dist_dir / "orthoswift-metashape.zip.sha256"
    generic_sha.write_text(f"{sha256}  orthoswift-metashape.zip\n", encoding="utf-8")

    size_kb = zip_path.stat().st_size / 1024

    print("\n" + "=" * 60)
    print("METASHAPE PLUGIN BUILD RELEASE SUCCESSFUL!")
    print(f"Artifact : {zip_path}")
    print(f"Generic  : {generic_zip}")
    print(f"Version  : {version}")
    print(f"Size     : {size_kb:.2f} KB")
    print(f"SHA-256  : {sha256}")
    print(f"Checksum : {sha_file}")
    print("=" * 60)

if __name__ == "__main__":
    main()
