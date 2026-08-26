# Contributing

Thank you for contributing to the OrthoSWIFT Metashape plugin.

## Development setup

```bash
python -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate
python -m pip install -r requirements.txt
python -m pip install pytest ruff bandit
```

## Required checks

```bash
python -m pytest -q
python -m ruff check orthoswift --select E9,F
python -m bandit -q -r orthoswift -lll
```

Keep pull requests focused. Add regression tests for defects. Do not add controller-certification claims without evidence from the named display and firmware. Do not infer physical product rates from imagery without operator authorization.

## Metashape integration guidelines

- **Thread safety**: Never perform heavy raster math or blocking GIS processing on Metashape's main Python thread. Use the `QProcess` adapter pattern invoking `runner.py` in an isolated Python environment.
- **Progress reporting**: Long-running background processes must emit progress tokens to `stdout` in the format `[PROGRESS <pct>%] <message>`. `metashape.py` intercepts these tokens to update the Qt progress dialog smoothly.
- **Cross-platform paths**: Use `pathlib.Path` for all path manipulations across Windows, macOS, and Linux.
- **UI hygiene**: Adhere to the OrthoSWIFT grey & white design tokens and PySide2 widget standards.
