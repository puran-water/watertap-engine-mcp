"""Session persistence store.

This module owns the public persistence abstraction for flowsheet sessions.
It intentionally delegates serialization details to ``core.session`` so PR 2
can split ownership without changing the on-disk session format.
"""

from __future__ import annotations

from pathlib import Path

from watertap_engine_mcp.core.session import FlowsheetSession, SessionManager


class SessionStore(SessionManager):
    """Filesystem-backed store for WaterTAP flowsheet sessions."""

    def __init__(self, storage_dir: Path):
        super().__init__(storage_dir)

    def save_session(self, session: FlowsheetSession) -> None:
        """Persist a session."""

        self.save(session)

    def load_session(self, session_id: str) -> FlowsheetSession:
        """Load a session by id."""

        return self.load(session_id)

    def delete_session(self, session_id: str) -> None:
        """Delete a session by id."""

        self.delete(session_id)

