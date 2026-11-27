"""Every mail backend the module offers, in the order `jornada sync modules` lists them."""
from __future__ import annotations

from typing import Tuple

from ..accounts import AccountError
from ..registry import BackendSpec
from .apple import BACKEND as APPLE
from .gmail import BACKEND as GMAIL
from .imap import BACKEND as IMAP
from .m365 import BACKEND as M365

BACKENDS: Tuple[BackendSpec, ...] = (IMAP, GMAIL, M365, APPLE)


def backend_spec(key: str) -> BackendSpec:
    for spec in BACKENDS:
        if spec.key == key:
            return spec
    known = ", ".join(spec.key for spec in BACKENDS)
    raise AccountError(f"the mail module has no backend {key!r} (known: {known})")
