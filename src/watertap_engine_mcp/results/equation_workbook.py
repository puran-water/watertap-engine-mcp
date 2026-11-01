from __future__ import annotations
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any
_requested_models = ContextVar('requested_equation_workbook_models', default=None)

def retain_requested_model(model, *, state=None):
    models = _requested_models.get()
    if models is not None:
        from watertap_engine_mcp.results.model_state import capture_model_state
        models.append(RetainedModel(model, state if state is not None else capture_model_state(model)))
