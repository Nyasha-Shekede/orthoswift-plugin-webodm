"""OrthoSWIFT Metashape Plugin - Agisoft Metashape Professional Integration"""
from .version import __version__
from .runner import run
from .core.pipeline import run_agriculture_pipeline

__author__ = "OrthoSWIFT"
__all__ = ["metashape", "run", "run_agriculture_pipeline", "__version__"]
