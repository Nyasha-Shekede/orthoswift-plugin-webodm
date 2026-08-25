# Contributing

Thank you for improving the OrthoSWIFT WebODM plugin.

## Development setup

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install -r orthoswift/requirements.txt
python -m pip install pytest ruff bandit
```

## Required checks

```bash
python -m pytest -q
python -m ruff check orthoswift tests --select E9,F
python -m bandit -q -r orthoswift -x tests -lll
```

Keep pull requests focused. Add regression tests for defects. Do not add controller-certification claims without evidence from the named display and firmware. Do not infer physical product rates from imagery.

## WebODM integration

Follow the official [WebODM plugin development guide](https://docs.webodm.org/plugin-development-guide/). Long-running worker functions must keep their imports inside the function and accept only JSON-serializable arguments.
