"""High-accuracy Natural Language / PDF → Lean 4 formalization pipeline."""

__version__ = "1.0.0"

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
