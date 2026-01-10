"""Flowsheet session persistence utilities.

Extends core/session.py with file-based persistence and
multi-session management.
"""

import json
import os
import shutil
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.session import (
    FlowsheetSession,
    SessionConfig,
    UnitInstance,
    Connection,
)
from core.property_registry import PropertyPackageType


class SessionPersistence:
    """Handles session persistence to disk."""

    def __init__(self, sessions_dir: Path):
        """Initialize session persistence.

        Args:
            sessions_dir: Directory for session storage
        """
        self.sessions_dir = Path(sessions_dir)
        self.sessions_dir.mkdir(parents=True, exist_ok=True)

    def _session_path(self, session_id: str) -> Path:
        """Get path to session file."""
        return self.sessions_dir / f"{session_id}.json"

    def save(self, session: FlowsheetSession) -> None:
        """Save session to disk.

        Args:
            session: Session to save
        """
        data = self._serialize_session(session)
        path = self._session_path(session.session_id)

        with open(path, "w") as f:
            json.dump(data, f, indent=2, default=str)

    def load(self, session_id: str) -> Optional[FlowsheetSession]:
        """Load session from disk.

        Args:
            session_id: Session ID to load

        Returns:
            FlowsheetSession or None if not found
        """
        path = self._session_path(session_id)
        if not path.exists():
            return None

        with open(path) as f:
            data = json.load(f)

        return self._deserialize_session(data)

    def delete(self, session_id: str) -> bool:
        """Delete session from disk.

        Args:
            session_id: Session ID to delete

        Returns:
            True if deleted, False if not found
        """
        path = self._session_path(session_id)
        if path.exists():
            path.unlink()
            return True
        return False

    def list_sessions(self) -> List[str]:
        """List all session IDs.

        Returns:
            List of session IDs
        """
        return [
            p.stem for p in self.sessions_dir.glob("*.json")
        ]

    def get_session_summary(self, session_id: str) -> Optional[Dict[str, Any]]:
        """Get summary of session without full load.

        Args:
            session_id: Session ID

        Returns:
            Dict with session summary or None
        """
        path = self._session_path(session_id)
        if not path.exists():
            return None

        with open(path) as f:
            data = json.load(f)

        return {
            "session_id": session_id,
            "property_package": data.get("config", {}).get("property_package"),
            "unit_count": len(data.get("units", {})),
            "connection_count": len(data.get("connections", [])),
            "created_at": data.get("created_at"),
            "updated_at": data.get("updated_at"),
        }

    def _serialize_session(self, session: FlowsheetSession) -> Dict[str, Any]:
        """Serialize session to JSON-compatible dict."""
        return {
            "session_id": session.session_id,
            "created_at": session.created_at.isoformat() if session.created_at else None,
            "updated_at": datetime.now().isoformat(),
            "config": {
                "property_package": session.config.property_package.name if session.config else None,
                "solver_options": session.config.solver_options if session.config else {},
            },
            "units": {
                uid: {
                    "unit_type": u.unit_type,
                    "config": u.config,
                    "fixed_vars": u.fixed_vars,
                    "scaling_factors": u.scaling_factors,
                }
                for uid, u in session.units.items()
            },
            "connections": [
                {
                    "src_unit": c.src_unit,
                    "src_port": c.src_port,
                    "dest_unit": c.dest_unit,
                    "dest_port": c.dest_port,
                }
                for c in session.connections
            ],
            "feed_state": session.feed_state,
            "solve_status": session.solve_status,
            "solve_results": session.solve_results,
        }

    def _deserialize_session(self, data: Dict[str, Any]) -> FlowsheetSession:
        """Deserialize session from dict."""
        config_data = data.get("config", {})
        pkg_name = config_data.get("property_package")

        config = SessionConfig(
            property_package=PropertyPackageType[pkg_name] if pkg_name else PropertyPackageType.SEAWATER,
            solver_options=config_data.get("solver_options", {}),
        )

        units = {}
        for uid, u_data in data.get("units", {}).items():
            units[uid] = UnitInstance(
                unit_type=u_data["unit_type"],
                config=u_data.get("config", {}),
                fixed_vars=u_data.get("fixed_vars", {}),
                scaling_factors=u_data.get("scaling_factors", {}),
            )

        connections = []
        for c_data in data.get("connections", []):
            connections.append(Connection(
                src_unit=c_data["src_unit"],
                src_port=c_data["src_port"],
                dest_unit=c_data["dest_unit"],
                dest_port=c_data["dest_port"],
            ))

        created_at = data.get("created_at")
        if created_at and isinstance(created_at, str):
            created_at = datetime.fromisoformat(created_at)

        session = FlowsheetSession(
            session_id=data["session_id"],
            config=config,
        )
        session.units = units
        session.connections = connections
        session.feed_state = data.get("feed_state")
        session.solve_status = data.get("solve_status")
        session.solve_results = data.get("solve_results")
        session.created_at = created_at

        return session


class SessionManager:
    """Manager for flowsheet sessions with persistence."""

    def __init__(self, base_dir: Optional[Path] = None):
        """Initialize session manager.

        Args:
            base_dir: Base directory for session storage
        """
        if base_dir is None:
            base_dir = Path(__file__).parent.parent / "jobs" / "sessions"

        self.persistence = SessionPersistence(base_dir)
        self._active_sessions: Dict[str, FlowsheetSession] = {}

    def create_session(
        self,
        property_package: PropertyPackageType = PropertyPackageType.SEAWATER,
        solver_options: Optional[Dict[str, Any]] = None,
    ) -> FlowsheetSession:
        """Create a new session.

        Args:
            property_package: Default property package
            solver_options: Solver options

        Returns:
            New FlowsheetSession
        """
        config = SessionConfig(
            property_package=property_package,
            solver_options=solver_options or {},
        )

        session = FlowsheetSession(config=config)
        self._active_sessions[session.session_id] = session
        self.persistence.save(session)

        return session

    def get_session(self, session_id: str) -> Optional[FlowsheetSession]:
        """Get a session by ID.

        Args:
            session_id: Session ID

        Returns:
            FlowsheetSession or None
        """
        # Check active sessions first
        if session_id in self._active_sessions:
            return self._active_sessions[session_id]

        # Load from persistence
        session = self.persistence.load(session_id)
        if session:
            self._active_sessions[session_id] = session

        return session

    def save_session(self, session: FlowsheetSession) -> None:
        """Save session changes.

        Args:
            session: Session to save
        """
        self.persistence.save(session)

    def delete_session(self, session_id: str) -> bool:
        """Delete a session.

        Args:
            session_id: Session ID

        Returns:
            True if deleted
        """
        if session_id in self._active_sessions:
            del self._active_sessions[session_id]

        return self.persistence.delete(session_id)

    def list_sessions(self) -> List[Dict[str, Any]]:
        """List all sessions with summaries.

        Returns:
            List of session summaries
        """
        summaries = []
        for session_id in self.persistence.list_sessions():
            summary = self.persistence.get_session_summary(session_id)
            if summary:
                summaries.append(summary)

        return sorted(summaries, key=lambda s: s.get("created_at", ""), reverse=True)

    def add_unit(
        self,
        session_id: str,
        unit_id: str,
        unit_type: str,
        config: Optional[Dict[str, Any]] = None,
    ) -> Optional[UnitInstance]:
        """Add a unit to a session.

        Args:
            session_id: Session ID
            unit_id: Unit identifier
            unit_type: Type of unit
            config: Unit configuration

        Returns:
            UnitInstance or None if session not found
        """
        session = self.get_session(session_id)
        if not session:
            return None

        unit = UnitInstance(
            unit_type=unit_type,
            config=config or {},
        )
        session.units[unit_id] = unit
        self.save_session(session)

        return unit

    def remove_unit(self, session_id: str, unit_id: str) -> bool:
        """Remove a unit from a session.

        Args:
            session_id: Session ID
            unit_id: Unit to remove

        Returns:
            True if removed
        """
        session = self.get_session(session_id)
        if not session or unit_id not in session.units:
            return False

        del session.units[unit_id]

        # Also remove connections involving this unit
        session.connections = [
            c for c in session.connections
            if c.src_unit != unit_id and c.dest_unit != unit_id
        ]

        self.save_session(session)
        return True

    def add_connection(
        self,
        session_id: str,
        src_unit: str,
        src_port: str,
        dest_unit: str,
        dest_port: str,
    ) -> Optional[Connection]:
        """Add a connection to a session.

        Args:
            session_id: Session ID
            src_unit: Source unit ID
            src_port: Source port name
            dest_unit: Destination unit ID
            dest_port: Destination port name

        Returns:
            Connection or None
        """
        session = self.get_session(session_id)
        if not session:
            return None

        connection = Connection(
            src_unit=src_unit,
            src_port=src_port,
            dest_unit=dest_unit,
            dest_port=dest_port,
        )
        session.connections.append(connection)
        self.save_session(session)

        return connection

    def fix_variable(
        self,
        session_id: str,
        unit_id: str,
        var_name: str,
        value: float,
    ) -> bool:
        """Fix a variable in a session.

        Args:
            session_id: Session ID
            unit_id: Unit ID
            var_name: Variable name
            value: Value to fix

        Returns:
            True if successful
        """
        session = self.get_session(session_id)
        if not session or unit_id not in session.units:
            return False

        session.units[unit_id].fixed_vars[var_name] = value
        self.save_session(session)

        return True

    def set_scaling_factor(
        self,
        session_id: str,
        unit_id: str,
        var_name: str,
        factor: float,
    ) -> bool:
        """Set a scaling factor in a session.

        Args:
            session_id: Session ID
            unit_id: Unit ID
            var_name: Variable name
            factor: Scaling factor

        Returns:
            True if successful
        """
        session = self.get_session(session_id)
        if not session or unit_id not in session.units:
            return False

        session.units[unit_id].scaling_factors[var_name] = factor
        self.save_session(session)

        return True
