"""High-accuracy Natural Language / PDF → Lean 4 formalization pipeline."""

__version__ = "1.0.0"

# File parsers may be installed beside the application for portable Python
# runtimes (including Windows embedded Python, which ignores PYTHONPATH).
import sys
from pathlib import Path

_local_dependencies = Path(__file__).resolve().parents[2] / ".runtime_dependencies"
if _local_dependencies.is_dir() and str(_local_dependencies) not in sys.path:
    sys.path.append(str(_local_dependencies))

from .pipeline import FormalizationPipeline
from .models import FormalizationResult, StructuredMath, MemoryEntry

import sys


def _configure_stdio() -> None:
    """Windows consoles often default to cp950; Lean code is UTF-8."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            pass


_configure_stdio()


__all__ = [
    "FormalizationPipeline",
    "FormalizationResult",
    "StructuredMath",
    "MemoryEntry",
    "__version__",
]
