"""The RAPI client's error type (its own module so mixins can share it)."""
from __future__ import annotations


class RapiError(RuntimeError):
    """A RAPI call failed; ``last_error`` is the Win32 error code if known."""

    def __init__(self, message: str, last_error: int = 0) -> None:
        super().__init__(message)
        self.last_error = last_error
