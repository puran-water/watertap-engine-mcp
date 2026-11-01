"""Flowsheet model-build dispatch helpers."""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from typing import Any

from watertap_engine_mcp.utils.model_builder import ModelBuilder


def build_model(session: Any) -> Any:
    """Build a Pyomo model for a persisted flowsheet session."""

    return ModelBuilder(session).build()


def build_model_context(session: Any, *, quiet: bool = True) -> tuple[Any, Any, dict[str, Any]]:
    """Build a model and return the builder, model, and unit map."""

    builder = ModelBuilder(session)
    if quiet:
        with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            model = builder.build()
    else:
        model = builder.build()
    return builder, model, builder.get_units()
