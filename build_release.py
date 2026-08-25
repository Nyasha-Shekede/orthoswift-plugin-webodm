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

    zip_name = f"orthoswift-webodm-plugin-v{version}.zip"
    zip_path = dist_dir / zip_name

    exclude_dirs = {"__pycache__", ".pytest_cache", ".git", ".github", "tests", "dist"}
    exclude_exts = {".pyc", ".pyo", ".pyd"}

    print(f"Building WebODM plugin release package: {zip_name}")

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        # 1. Package orthoswift/ folder
        orthoswift_dir = plugin_dir / "orthoswift"
        for root, dirs, files in os.walk(orthoswift_dir):
            dirs[:] = [d for d in dirs if d not in exclude_dirs]
            for f in sorted(files):
                file_path = Path(root) / f
                if file_path.suffix not in exclude_exts and not f.startswith("."):
                    rel_path = file_path.relative_to(plugin_dir)
                    zf.write(file_path, arcname=str(rel_path).replace("\\", "/"))
                    print(f"  + {rel_path}")

        # 2. Package documentation inside orthoswift/ directory (WebODM requires exactly 1 root directory)
        for doc in ["LICENSE", "README.md", "SECURITY.md", "CONTRIBUTING.md"]:
            doc_path = plugin_dir / doc
            if doc_path.exists():
                zf.write(doc_path, arcname=f"orthoswift/{doc}")
                print(f"  + orthoswift/{doc}")

    # Generate SHA-256 Checksum
    sha256 = hashlib.sha256(zip_path.read_bytes()).hexdigest()
    sha_file = dist_dir / f"{zip_name}.sha256"
    sha_file.write_text(f"{sha256}  {zip_name}\n", encoding="utf-8")

    size_kb = zip_path.stat().st_size / 1024

    print("\n" + "=" * 60)
    print("BUILD RELEASE SUCCESSFUL!")
    print(f"Artifact : {zip_path}")
    print(f"Size     : {size_kb:.2f} KB")
    print(f"SHA-256  : {sha256}")
    print(f"Checksum : {sha_file}")
    print("=" * 60)

if __name__ == "__main__":
    main()
