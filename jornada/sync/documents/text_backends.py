"""The text-document family of the documents module: Pocket Word's .txt / .rtf files on the
device ⇄ Word on OneDrive, Google Docs or Apple Pages. The package ``__init__`` reads
``BACKENDS`` and ``DEVICE_STORE`` from here.
"""
from __future__ import annotations

from typing import Tuple

from ..registry import BackendSpec
from .gdocs import BACKEND as GDOCS
from .pages import BACKEND as PAGES
from .textcodec import device_store
from .word import BACKEND as WORD

DEVICE_STORE = device_store
BACKENDS: Tuple[BackendSpec, ...] = (WORD, GDOCS, PAGES)

__all__ = ["BACKENDS", "DEVICE_STORE", "WORD", "GDOCS", "PAGES"]
