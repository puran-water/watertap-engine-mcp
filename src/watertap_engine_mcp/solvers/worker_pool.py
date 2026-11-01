"""Minimal worker-pool abstraction for solver jobs."""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Callable


class SolverWorkerPool:
    """Small thread-pool wrapper for future parallel solve work."""

    def __init__(self, max_workers: int = 1):
        self._executor = ThreadPoolExecutor(max_workers=max_workers)

    def submit(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Future:
        """Submit a callable to the solver worker pool."""

        return self._executor.submit(fn, *args, **kwargs)

    def shutdown(self, wait: bool = True) -> None:
        """Shut down the pool."""

        self._executor.shutdown(wait=wait)

