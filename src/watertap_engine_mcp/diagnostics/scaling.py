"""Scaling diagnostics for live Pyomo models."""

from __future__ import annotations

from contextlib import redirect_stdout
from io import StringIO
from typing import Any


def scaling_report_text(model: Any) -> str:
    """Return IDAES scaling issue text for a live model."""

    import idaes.core.util.scaling as iscale

    output = StringIO()
    with redirect_stdout(output):
        iscale.report_scaling_issues(model)
    return output.getvalue()

