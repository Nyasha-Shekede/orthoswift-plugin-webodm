import ast
import hashlib
import os
import shutil
import zipfile
from pathlib import Path

EXCLUDED_DIRECTORIES = {
    ".git",
    ".github",
    ".pytest_cache",
    ".venv",
    "__pycache__",
    "dist",
    "tests",
}
EXCLUDED_SUFFIXES = {".pyc", ".pyd", ".pyo"}


def _read_version(path: Path) -> str:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "__version__"
            for target in node.targets
        ):
            version = ast.literal_eval(node.value)
            if isinstance(version, str) and version.strip():
                return version
            break
    raise ValueError(f"{path} must define a non-empty string __version__")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> None:
    root = Path(__file__).resolve().parent
    dist = root / "dist"
    dist.mkdir(exist_ok=True)
    version = _read_version(root / "orthoswift" / "version.py")
    versioned_name = f"orthoswift-webodm-plugin-v{version}.zip"
    versioned_archive = dist / versioned_name

    print(f"Building WebODM plugin release: {versioned_name}")
    with zipfile.ZipFile(versioned_archive, "w", zipfile.ZIP_DEFLATED) as archive:
        package = root / "orthoswift"
        for directory, directories, files in os.walk(package):
            directories[:] = sorted(
                name for name in directories if name not in EXCLUDED_DIRECTORIES
            )
            for name in sorted(files):
                path = Path(directory) / name
                if path.suffix in EXCLUDED_SUFFIXES or name.startswith("."):
                    continue
                archive.write(path, path.relative_to(root).as_posix())
        for name in ("LICENSE", "README.md", "SECURITY.md", "CONTRIBUTING.md", "CHANGELOG.md"):
            path = root / name
            if path.is_file():
                archive.write(path, f"orthoswift/{name}")

    checksum = _sha256(versioned_archive)
    (dist / f"{versioned_name}.sha256").write_text(
        f"{checksum}  {versioned_name}\n", encoding="utf-8"
    )

    generic_name = "orthoswift-webodm-plugin.zip"
    generic_archive = dist / generic_name
    shutil.copyfile(versioned_archive, generic_archive)
    (dist / f"{generic_name}.sha256").write_text(
        f"{checksum}  {generic_name}\n", encoding="utf-8"
    )

    print(f"Artifact : {versioned_archive}")
    print(f"Generic  : {generic_archive}")
    print(f"Size     : {versioned_archive.stat().st_size / 1024:.2f} KiB")
    print(f"SHA-256  : {checksum}")


if __name__ == "__main__":
    main()
