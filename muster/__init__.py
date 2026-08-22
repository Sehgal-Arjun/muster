"""Muster — a local-first personal knowledge index."""

import warnings

__version__ = "0.1.0"

# html5lib emits DataLossWarning for essentially every real marketing email
# (unclosed tags, stray attributes). It is noise: nothing downstream can act on
# it, and at sync scale it drowns the warnings that do matter. Registered here
# so it applies to library and CLI use alike.
try:
    from html5lib.constants import DataLossWarning

    warnings.filterwarnings("ignore", category=DataLossWarning)
except ImportError:  # pragma: no cover - html5lib arrives with talon
    pass
