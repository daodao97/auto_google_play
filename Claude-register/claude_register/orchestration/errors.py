"""Structured flow errors that preserve existing retry decisions."""

from __future__ import annotations

import re


_SAFE_LABEL_RE = re.compile(r"^[a-z0-9][a-z0-9_:,.-]{0,119}$")


class FlowError(RuntimeError):
    """A safe error category with an explicit, caller-supplied retry decision."""

    def __init__(
        self,
        *,
        stage: str,
        category: str,
        retryable: bool,
        http_status: int | None = None,
    ) -> None:
        if not _SAFE_LABEL_RE.fullmatch(stage) or not _SAFE_LABEL_RE.fullmatch(category):
            raise ValueError("flow error labels must be stable safe identifiers")
        super().__init__(category)
        self.stage = stage
        self.category = category
        self.retryable = retryable
        self.http_status = http_status


class FlowCancelled(FlowError):
    """Signal an operator-requested stop without making it retryable."""

    def __init__(self, stage: str = "onboarding") -> None:
        super().__init__(stage=stage, category="cancelled", retryable=False)
