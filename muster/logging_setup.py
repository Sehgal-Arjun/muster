"""Logging configuration.

The embedding stack is extremely chatty at INFO: sentence-transformers,
huggingface_hub and httpx together print about 25 lines of HTTP traffic and
model-loading detail before a search prints its first result, which buries the
output the user actually asked for. None of it is actionable in normal use, so
these libraries are pinned to WARNING and their progress bars disabled.

`muster -v` puts everything, including these, back to DEBUG.
"""

from __future__ import annotations

import logging
import os

# Everything that logs about model downloads, HTTP or tokenisation.
NOISY_LOGGERS = (
    "sentence_transformers",
    "sentence_transformers.SentenceTransformer",
    "transformers",
    "transformers.modeling_utils",
    "httpx",
    "httpcore",
    "huggingface_hub",
    "urllib3",
    "filelock",
    "PIL",
    "googleapiclient.discovery_cache",
)


def quiet_dependencies(level: int = logging.WARNING) -> None:
    for name in NOISY_LOGGERS:
        logging.getLogger(name).setLevel(level)

    # Progress bars are written straight to the stream, not through logging.
    os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
    os.environ.setdefault("TRANSFORMERS_NO_ADVISORY_WARNINGS", "1")
    try:
        from transformers.utils import logging as hf_logging

        hf_logging.set_verbosity_error()
        hf_logging.disable_progress_bar()
    except Exception:  # transformers is optional until `muster embed` is used
        pass


def configure(verbose: bool = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    quiet_dependencies(logging.DEBUG if verbose else logging.WARNING)
