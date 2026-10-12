"""Fingerprint capture and diff (FORK_PLAN.md, section 6.1).

``capture.js`` collects everything a page can observe; ``CaptureServer`` hosts
it and receives the record; ``record`` names, stores, classifies and diffs
records; ``capture_with_browser`` runs the whole thing in a browser launched by
this library.
"""

from disguisebrowser.fingerprint.capture import annotate, capture_with_browser
from disguisebrowser.fingerprint.record import (
    CATEGORIES,
    SCHEMA,
    Category,
    Difference,
    Summary,
    by_category,
    classify,
    default_path,
    diff,
    diff_to_json,
    flatten,
    format_diff,
    gpu_slug,
    load_record,
    save_record,
    summarize,
)
from disguisebrowser.fingerprint.server import CaptureServer

__all__ = [
    "CATEGORIES",
    "SCHEMA",
    "CaptureServer",
    "Category",
    "Difference",
    "Summary",
    "annotate",
    "by_category",
    "capture_with_browser",
    "classify",
    "default_path",
    "diff",
    "diff_to_json",
    "flatten",
    "format_diff",
    "gpu_slug",
    "load_record",
    "save_record",
    "summarize",
]
