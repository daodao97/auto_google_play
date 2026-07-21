"""Fail-closed confirmation gate for scripts that contact external services."""

from __future__ import annotations

import sys
from collections.abc import Sequence

CONFIRM_FLAG = "--confirm-authorized-external-run"


class ExternalRunNotConfirmed(RuntimeError):
    """Raised before configuration or network access when confirmation is absent."""


def require_external_confirmation(argv: Sequence[str] | None = None) -> None:
    args = tuple(sys.argv[1:] if argv is None else argv)
    if CONFIRM_FLAG not in args:
        raise ExternalRunNotConfirmed(
            f"External requests are disabled. Re-run with {CONFIRM_FLAG} only for an authorized manual run."
        )


def confirmed_external_args(argv: Sequence[str] | None = None) -> tuple[str, ...]:
    args = tuple(sys.argv[1:] if argv is None else argv)
    require_external_confirmation(args)
    return tuple(arg for arg in args if arg != CONFIRM_FLAG)
