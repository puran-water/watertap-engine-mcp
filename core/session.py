"""Session Management for WaterTAP Flowsheets.

Provides session configuration and persistence for flowsheet building.
"""

import json
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional

from .property_registry import PropertyPackageType


def _serialize_dict_keys(obj: Any) -> Any:
    """Recursively convert tuple keys to strings for JSON serialization."""
    if isinstance(obj, dict):
        result = {}
        for k, v in obj.items():
            if isinstance(k, tuple):
                # Convert tuple to string format: "(a, b)"
                key = str(k)
            else:
                key = k
            result[key] = _serialize_dict_keys(v)
        return result
    elif isinstance(obj, list):
        return [_serialize_dict_keys(item) for item in obj]
    else:
        return obj


def _deserialize_dict_keys(obj: Any) -> Any:
    """Recursively convert string tuple keys back to tuples for Pyomo."""
    if isinstance(obj, dict):
        result = {}
        for k, v in obj.items():
            if isinstance(k, str) and k.startswith("(") and k.endswith(")"):
                # Parse tuple from string: "('Liq', 'H2O')" -> ('Liq', 'H2O')
                try:
                    import ast
                    key = ast.literal_eval(k)
                except (ValueError, SyntaxError):
                    key = k
            else:
                key = k
            result[key] = _deserialize_dict_keys(v)
        return result
    elif isinstance(obj, list):
        return [_deserialize_dict_keys(item) for item in obj]
    else:
        return obj


class SessionStatus(Enum):
    """Status of a flowsheet session."""
    CREATED = "created"
    BUILDING = "building"
    READY = "ready"
    SOLVING = "solving"
    SOLVED = "solved"
    FAILED = "failed"


@dataclass
class UnitInstance:
    """Instance of a unit in a flowsheet."""
    unit_id: str
    unit_type: str
    config: Dict[str, Any] = field(default_factory=dict)
    fixed_vars: Dict[str, float] = field(default_factory=dict)
    scaling_factors: Dict[str, float] = field(default_factory=dict)


@dataclass
class Connection:
    """Connection between two units."""
    source_unit: str
    source_port: str
    dest_unit: str
    dest_port: str
    translator_id: Optional[str] = None


@dataclass
class SessionConfig:
    """Configuration for a WaterTAP flowsheet session."""

    session_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    name: str = ""
    description: str = ""

    # Property package configuration
    default_property_package: PropertyPackageType = PropertyPackageType.SEAWATER
    property_packages: Dict[str, str] = field(default_factory=dict)

    # Property package config kwargs (for MCAS, ZO, etc. that require config)
    # Keys: solute_list, charge, mw_data (MCAS); database, water_source (ZO)
    property_package_config: Dict[str, Any] = field(default_factory=dict)

    # Solver configuration
    solver: str = "ipopt"
    solver_options: Dict[str, Any] = field(default_factory=dict)

    # Timestamps
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> Dict:
        """Convert to dictionary for serialization."""
        d = asdict(self)
        d["default_property_package"] = self.default_property_package.value
        return d

    @classmethod
    def from_dict(cls, data: Dict) -> "SessionConfig":
        """Create from dictionary."""
        data = data.copy()
        if "default_property_package" in data:
            data["default_property_package"] = PropertyPackageType(
                data["default_property_package"]
            )
        return cls(**data)


@dataclass
class FlowsheetSession:
    """Complete flowsheet session state."""

    config: SessionConfig
    status: SessionStatus = SessionStatus.CREATED

    # Flowsheet structure
    units: Dict[str, UnitInstance] = field(default_factory=dict)
    connections: List[Connection] = field(default_factory=list)
    translators: Dict[str, Dict] = field(default_factory=dict)

    # Feed state
    feed_state: Optional[Dict] = None

    # Results
    solve_status: Optional[str] = None
    solve_message: Optional[str] = None
    results: Optional[Dict] = None

    # DOF tracking
    dof_status: Dict[str, int] = field(default_factory=dict)
    total_dof: int = 0

    def add_unit(
        self,
        unit_id: str,
        unit_type: str,
        config: Optional[Dict] = None,
    ) -> UnitInstance:
        """Add a unit to the flowsheet.

        Args:
            unit_id: Unique identifier for the unit
            unit_type: Type of unit (e.g., "ReverseOsmosis0D")
            config: Unit configuration options

        Returns:
            Created UnitInstance

        Raises:
            ValueError: If unit_id already exists
        """
        if unit_id in self.units:
            raise ValueError(f"Unit '{unit_id}' already exists")

        unit = UnitInstance(
            unit_id=unit_id,
            unit_type=unit_type,
            config=config or {},
        )
        self.units[unit_id] = unit
        self.status = SessionStatus.BUILDING
        self._update_timestamp()
        return unit

    def remove_unit(self, unit_id: str) -> None:
        """Remove a unit from the flowsheet.

        Args:
            unit_id: Unit to remove

        Raises:
            KeyError: If unit not found
        """
        if unit_id not in self.units:
            raise KeyError(f"Unit '{unit_id}' not found")

        # Remove unit
        del self.units[unit_id]

        # Remove associated connections
        self.connections = [
            c for c in self.connections
            if c.source_unit != unit_id and c.dest_unit != unit_id
        ]

        self._update_timestamp()

    def add_connection(
        self,
        source_unit: str,
        source_port: str,
        dest_unit: str,
        dest_port: str,
        translator_id: Optional[str] = None,
    ) -> Connection:
        """Add a connection between units.

        Args:
            source_unit: Source unit ID
            source_port: Source port name
            dest_unit: Destination unit ID
            dest_port: Destination port name
            translator_id: Translator ID if needed

        Returns:
            Created Connection

        Raises:
            KeyError: If source or dest unit not found
        """
        if source_unit not in self.units and source_unit != "Feed":
            raise KeyError(f"Source unit '{source_unit}' not found")
        if dest_unit not in self.units:
            raise KeyError(f"Destination unit '{dest_unit}' not found")

        conn = Connection(
            source_unit=source_unit,
            source_port=source_port,
            dest_unit=dest_unit,
            dest_port=dest_port,
            translator_id=translator_id,
        )
        self.connections.append(conn)
        self._update_timestamp()
        return conn

    def fix_variable(
        self,
        unit_id: str,
        var_name: str,
        value: float,
    ) -> None:
        """Fix a variable value on a unit.

        Args:
            unit_id: Unit ID
            var_name: Variable name
            value: Value to fix

        Raises:
            KeyError: If unit not found
        """
        if unit_id not in self.units:
            raise KeyError(f"Unit '{unit_id}' not found")

        self.units[unit_id].fixed_vars[var_name] = value
        self._update_timestamp()

    def unfix_variable(self, unit_id: str, var_name: str) -> None:
        """Unfix a variable on a unit.

        Args:
            unit_id: Unit ID
            var_name: Variable name

        Raises:
            KeyError: If unit or variable not found
        """
        if unit_id not in self.units:
            raise KeyError(f"Unit '{unit_id}' not found")
        if var_name not in self.units[unit_id].fixed_vars:
            raise KeyError(f"Variable '{var_name}' not fixed on unit '{unit_id}'")

        del self.units[unit_id].fixed_vars[var_name]
        self._update_timestamp()

    def set_scaling_factor(
        self,
        unit_id: str,
        var_name: str,
        factor: float,
    ) -> None:
        """Set scaling factor for a variable.

        Args:
            unit_id: Unit ID
            var_name: Variable name
            factor: Scaling factor

        Raises:
            KeyError: If unit not found
        """
        if unit_id not in self.units:
            raise KeyError(f"Unit '{unit_id}' not found")

        self.units[unit_id].scaling_factors[var_name] = factor
        self._update_timestamp()

    def update_dof_status(self, dof_by_unit: Dict[str, int], total: int) -> None:
        """Update DOF status after analysis.

        Args:
            dof_by_unit: DOF count per unit
            total: Total DOF for flowsheet
        """
        self.dof_status = dof_by_unit
        self.total_dof = total
        self._update_timestamp()

    def set_ready(self) -> None:
        """Mark session as ready for solving."""
        self.status = SessionStatus.READY
        self._update_timestamp()

    def set_solving(self) -> None:
        """Mark session as currently solving."""
        self.status = SessionStatus.SOLVING
        self._update_timestamp()

    def set_solved(self, results: Dict) -> None:
        """Mark session as successfully solved.

        Args:
            results: Solver results
        """
        self.status = SessionStatus.SOLVED
        self.solve_status = "optimal"
        self.results = results
        self._update_timestamp()

    def set_failed(self, message: str) -> None:
        """Mark session as failed.

        Args:
            message: Failure message
        """
        self.status = SessionStatus.FAILED
        self.solve_status = "failed"
        self.solve_message = message
        self._update_timestamp()

    def _update_timestamp(self) -> None:
        """Update the updated_at timestamp."""
        self.config.updated_at = datetime.now(timezone.utc).isoformat()

    def to_dict(self) -> Dict:
        """Convert to dictionary for serialization."""
        return {
            "config": self.config.to_dict(),
            "status": self.status.value,
            "units": {k: asdict(v) for k, v in self.units.items()},
            "connections": [asdict(c) for c in self.connections],
            "translators": self.translators,
            # Serialize dicts with tuple keys (e.g., state_args with ('Liq', 'H2O'))
            "feed_state": _serialize_dict_keys(self.feed_state),
            "solve_status": self.solve_status,
            "solve_message": self.solve_message,
            "results": _serialize_dict_keys(self.results),
            "dof_status": self.dof_status,
            "total_dof": self.total_dof,
        }

    @classmethod
    def from_dict(cls, data: Dict) -> "FlowsheetSession":
        """Create from dictionary."""
        config = SessionConfig.from_dict(data["config"])
        status = SessionStatus(data["status"])

        units = {
            k: UnitInstance(**v) for k, v in data.get("units", {}).items()
        }
        connections = [
            Connection(**c) for c in data.get("connections", [])
        ]

        return cls(
            config=config,
            status=status,
            units=units,
            connections=connections,
            translators=data.get("translators", {}),
            # Deserialize tuple keys back from strings
            feed_state=_deserialize_dict_keys(data.get("feed_state")),
            solve_status=data.get("solve_status"),
            solve_message=data.get("solve_message"),
            results=_deserialize_dict_keys(data.get("results")),
            dof_status=data.get("dof_status", {}),
            total_dof=data.get("total_dof", 0),
        )


class SessionManager:
    """Manager for persisting and loading flowsheet sessions."""

    def __init__(self, storage_dir: Path):
        """Initialize session manager.

        Args:
            storage_dir: Directory for session storage
        """
        self.storage_dir = Path(storage_dir)
        self.storage_dir.mkdir(parents=True, exist_ok=True)

    def _session_path(self, session_id: str) -> Path:
        """Get path for session file."""
        return self.storage_dir / f"{session_id}.json"

    def save(self, session: FlowsheetSession) -> None:
        """Save session to disk.

        Args:
            session: Session to save
        """
        path = self._session_path(session.config.session_id)
        with open(path, "w") as f:
            json.dump(session.to_dict(), f, indent=2)

    def load(self, session_id: str) -> FlowsheetSession:
        """Load session from disk.

        Args:
            session_id: Session ID to load

        Returns:
            Loaded FlowsheetSession

        Raises:
            FileNotFoundError: If session not found
        """
        path = self._session_path(session_id)
        if not path.exists():
            raise FileNotFoundError(f"Session '{session_id}' not found")

        with open(path) as f:
            data = json.load(f)
        return FlowsheetSession.from_dict(data)

    def delete(self, session_id: str) -> None:
        """Delete a session.

        Args:
            session_id: Session to delete

        Raises:
            FileNotFoundError: If session not found
        """
        path = self._session_path(session_id)
        if not path.exists():
            raise FileNotFoundError(f"Session '{session_id}' not found")
        path.unlink()

    def list_sessions(self) -> List[Dict]:
        """List all sessions.

        Returns:
            List of session summaries (id, name, status, created, updated)
        """
        sessions = []
        for path in self.storage_dir.glob("*.json"):
            try:
                with open(path) as f:
                    data = json.load(f)
                sessions.append({
                    "session_id": data["config"]["session_id"],
                    "name": data["config"].get("name", ""),
                    "status": data["status"],
                    "created_at": data["config"]["created_at"],
                    "updated_at": data["config"]["updated_at"],
                })
            except (json.JSONDecodeError, KeyError):
                continue
        return sorted(sessions, key=lambda x: x["updated_at"], reverse=True)

    def exists(self, session_id: str) -> bool:
        """Check if session exists.

        Args:
            session_id: Session ID to check

        Returns:
            True if session exists
        """
        return self._session_path(session_id).exists()
