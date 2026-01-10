"""Tests for session management."""

import pytest
import sys
import os
import tempfile
import shutil

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.session import (
    SessionConfig,
    FlowsheetSession,
    SessionManager,
    UnitInstance,
    Connection,
    SessionStatus,
)
from core.property_registry import PropertyPackageType


class TestSessionConfig:
    """Tests for SessionConfig dataclass."""

    def test_create_config(self):
        """Create a basic session config."""
        config = SessionConfig(
            default_property_package=PropertyPackageType.SEAWATER,
        )
        assert config.default_property_package == PropertyPackageType.SEAWATER
        assert config.property_packages == {}
        assert config.solver_options == {}

    def test_config_with_solver_options(self):
        """Config with solver options."""
        config = SessionConfig(
            default_property_package=PropertyPackageType.NACL,
            solver_options={"max_iter": 500, "tol": 1e-6},
        )
        assert config.default_property_package == PropertyPackageType.NACL
        assert config.solver_options["max_iter"] == 500
        assert config.solver_options["tol"] == 1e-6

    def test_config_serialization(self):
        """Config should be serializable to dict."""
        config = SessionConfig(
            default_property_package=PropertyPackageType.SEAWATER,
            solver_options={"max_iter": 500},
        )
        d = config.to_dict()
        # to_dict returns .value for enum (e.g., "SeawaterParameterBlock")
        assert d["default_property_package"] == PropertyPackageType.SEAWATER.value
        assert d["solver_options"]["max_iter"] == 500

    def test_config_deserialization(self):
        """Config should be deserializable from dict."""
        d = {
            "session_id": "test-id",
            "name": "test",
            "description": "",
            "default_property_package": PropertyPackageType.NACL.value,
            "property_packages": {},
            "solver": "ipopt",
            "solver_options": {"tol": 1e-6},
            "created_at": "2024-01-01T00:00:00",
            "updated_at": "2024-01-01T00:00:00",
        }
        config = SessionConfig.from_dict(d)
        assert config.default_property_package == PropertyPackageType.NACL
        assert config.solver_options["tol"] == 1e-6


class TestFlowsheetSession:
    """Tests for FlowsheetSession."""

    def test_create_session(self):
        """Create a new session."""
        config = SessionConfig(default_property_package=PropertyPackageType.SEAWATER)
        session = FlowsheetSession(config=config)

        assert session.config.session_id is not None
        assert len(session.config.session_id) > 0
        assert session.config == config
        assert session.units == {}
        assert session.connections == []
        assert session.feed_state is None
        assert session.status == SessionStatus.CREATED

    def test_add_unit(self):
        """Add unit to session."""
        config = SessionConfig(default_property_package=PropertyPackageType.SEAWATER)
        session = FlowsheetSession(config=config)

        unit = session.add_unit("RO1", "ReverseOsmosis0D")
        assert "RO1" in session.units
        assert isinstance(session.units["RO1"], UnitInstance)
        assert session.units["RO1"].unit_type == "ReverseOsmosis0D"
        assert session.status == SessionStatus.BUILDING

    def test_add_duplicate_unit(self):
        """Adding duplicate unit ID should raise."""
        config = SessionConfig(default_property_package=PropertyPackageType.SEAWATER)
        session = FlowsheetSession(config=config)

        session.add_unit("RO1", "ReverseOsmosis0D")
        with pytest.raises(ValueError, match="already exists"):
            session.add_unit("RO1", "Pump")

    def test_remove_unit(self):
        """Remove unit from session."""
        config = SessionConfig(default_property_package=PropertyPackageType.SEAWATER)
        session = FlowsheetSession(config=config)

        session.add_unit("RO1", "ReverseOsmosis0D")
        session.remove_unit("RO1")
        assert "RO1" not in session.units

    def test_remove_unit_not_found(self):
        """Removing non-existent unit should raise."""
        config = SessionConfig(default_property_package=PropertyPackageType.SEAWATER)
        session = FlowsheetSession(config=config)

        with pytest.raises(KeyError, match="not found"):
            session.remove_unit("nonexistent")

    def test_add_connection(self):
        """Add connection between units."""
        config = SessionConfig(default_property_package=PropertyPackageType.SEAWATER)
        session = FlowsheetSession(config=config)

        session.add_unit("pump", "Pump")
        session.add_unit("RO", "ReverseOsmosis0D")
        conn = session.add_connection("pump", "outlet", "RO", "inlet")

        assert len(session.connections) == 1
        assert isinstance(conn, Connection)
        assert conn.source_unit == "pump"
        assert conn.source_port == "outlet"
        assert conn.dest_unit == "RO"
        assert conn.dest_port == "inlet"

    def test_add_connection_invalid_source(self):
        """Adding connection with invalid source should raise."""
        config = SessionConfig(default_property_package=PropertyPackageType.SEAWATER)
        session = FlowsheetSession(config=config)

        session.add_unit("RO", "ReverseOsmosis0D")
        with pytest.raises(KeyError, match="Source unit"):
            session.add_connection("nonexistent", "outlet", "RO", "inlet")

    def test_fix_variable(self):
        """Fix variable in session."""
        config = SessionConfig(default_property_package=PropertyPackageType.SEAWATER)
        session = FlowsheetSession(config=config)

        session.add_unit("RO", "ReverseOsmosis0D")
        session.fix_variable("RO", "A_comp", 4.2e-12)

        assert "A_comp" in session.units["RO"].fixed_vars
        assert session.units["RO"].fixed_vars["A_comp"] == 4.2e-12

    def test_fix_variable_unit_not_found(self):
        """Fixing variable on non-existent unit should raise."""
        config = SessionConfig(default_property_package=PropertyPackageType.SEAWATER)
        session = FlowsheetSession(config=config)

        with pytest.raises(KeyError, match="not found"):
            session.fix_variable("nonexistent", "A_comp", 4.2e-12)

    def test_unfix_variable(self):
        """Unfix variable in session."""
        config = SessionConfig(default_property_package=PropertyPackageType.SEAWATER)
        session = FlowsheetSession(config=config)

        session.add_unit("RO", "ReverseOsmosis0D")
        session.fix_variable("RO", "A_comp", 4.2e-12)
        session.unfix_variable("RO", "A_comp")

        assert "A_comp" not in session.units["RO"].fixed_vars

    def test_unfix_variable_not_fixed(self):
        """Unfixing variable that isn't fixed should raise."""
        config = SessionConfig(default_property_package=PropertyPackageType.SEAWATER)
        session = FlowsheetSession(config=config)

        session.add_unit("RO", "ReverseOsmosis0D")
        with pytest.raises(KeyError, match="not fixed"):
            session.unfix_variable("RO", "nonexistent_var")

    def test_set_scaling_factor(self):
        """Set scaling factor for variable."""
        config = SessionConfig(default_property_package=PropertyPackageType.SEAWATER)
        session = FlowsheetSession(config=config)

        session.add_unit("RO", "ReverseOsmosis0D")
        session.set_scaling_factor("RO", "A_comp", 1e12)

        assert "A_comp" in session.units["RO"].scaling_factors
        assert session.units["RO"].scaling_factors["A_comp"] == 1e12

    def test_session_serialization(self):
        """Session should be serializable."""
        config = SessionConfig(default_property_package=PropertyPackageType.SEAWATER)
        session = FlowsheetSession(config=config)
        session.add_unit("RO", "ReverseOsmosis0D")
        session.fix_variable("RO", "A_comp", 4.2e-12)

        d = session.to_dict()
        assert "config" in d
        assert "status" in d
        assert "units" in d
        assert "connections" in d
        assert "RO" in d["units"]
        assert d["units"]["RO"]["fixed_vars"]["A_comp"] == 4.2e-12

    def test_session_deserialization(self):
        """Session should be deserializable."""
        config = SessionConfig(default_property_package=PropertyPackageType.SEAWATER)
        session = FlowsheetSession(config=config)
        session.add_unit("RO", "ReverseOsmosis0D")
        session.fix_variable("RO", "A_comp", 4.2e-12)

        d = session.to_dict()
        restored = FlowsheetSession.from_dict(d)

        assert restored.config.session_id == session.config.session_id
        assert "RO" in restored.units
        assert isinstance(restored.units["RO"], UnitInstance)
        assert restored.units["RO"].fixed_vars["A_comp"] == 4.2e-12

    def test_status_transitions(self):
        """Test session status transitions."""
        config = SessionConfig(default_property_package=PropertyPackageType.SEAWATER)
        session = FlowsheetSession(config=config)

        assert session.status == SessionStatus.CREATED

        session.add_unit("RO", "ReverseOsmosis0D")
        assert session.status == SessionStatus.BUILDING

        session.set_ready()
        assert session.status == SessionStatus.READY

        session.set_solving()
        assert session.status == SessionStatus.SOLVING

        session.set_solved({"objective": 100.0})
        assert session.status == SessionStatus.SOLVED
        assert session.results == {"objective": 100.0}

    def test_set_failed(self):
        """Test marking session as failed."""
        config = SessionConfig(default_property_package=PropertyPackageType.SEAWATER)
        session = FlowsheetSession(config=config)

        session.set_failed("Solver error: infeasible")
        assert session.status == SessionStatus.FAILED
        assert session.solve_message == "Solver error: infeasible"


class TestSessionManager:
    """Tests for SessionManager persistence."""

    @pytest.fixture
    def temp_dir(self):
        """Create temporary directory for session storage."""
        d = tempfile.mkdtemp()
        yield d
        shutil.rmtree(d)

    def test_create_manager(self, temp_dir):
        """Create session manager."""
        from pathlib import Path
        manager = SessionManager(storage_dir=temp_dir)
        assert manager.storage_dir == Path(temp_dir)

    def test_save_and_load_session(self, temp_dir):
        """Save and load session through manager."""
        manager = SessionManager(storage_dir=temp_dir)

        config = SessionConfig(default_property_package=PropertyPackageType.SEAWATER)
        session = FlowsheetSession(config=config)
        session.add_unit("RO", "ReverseOsmosis0D")

        manager.save(session)

        loaded = manager.load(session.config.session_id)
        assert loaded.config.session_id == session.config.session_id
        assert "RO" in loaded.units

    def test_list_sessions(self, temp_dir):
        """List all sessions."""
        manager = SessionManager(storage_dir=temp_dir)

        # Create and save two sessions
        config1 = SessionConfig(default_property_package=PropertyPackageType.SEAWATER)
        session1 = FlowsheetSession(config=config1)
        manager.save(session1)

        config2 = SessionConfig(default_property_package=PropertyPackageType.NACL)
        session2 = FlowsheetSession(config=config2)
        manager.save(session2)

        sessions = manager.list_sessions()
        assert len(sessions) == 2
        session_ids = [s["session_id"] for s in sessions]
        assert session1.config.session_id in session_ids
        assert session2.config.session_id in session_ids

    def test_delete_session(self, temp_dir):
        """Delete session."""
        manager = SessionManager(storage_dir=temp_dir)

        config = SessionConfig(default_property_package=PropertyPackageType.SEAWATER)
        session = FlowsheetSession(config=config)
        manager.save(session)

        session_id = session.config.session_id
        assert manager.exists(session_id)

        manager.delete(session_id)
        assert not manager.exists(session_id)

    def test_delete_nonexistent_session(self, temp_dir):
        """Deleting non-existent session should raise."""
        manager = SessionManager(storage_dir=temp_dir)

        with pytest.raises(FileNotFoundError):
            manager.delete("nonexistent-id")

    def test_load_nonexistent_session(self, temp_dir):
        """Loading non-existent session should raise."""
        manager = SessionManager(storage_dir=temp_dir)

        with pytest.raises(FileNotFoundError):
            manager.load("nonexistent-id")

    def test_session_persistence(self, temp_dir):
        """Session should persist to disk."""
        manager = SessionManager(storage_dir=temp_dir)

        config = SessionConfig(default_property_package=PropertyPackageType.SEAWATER)
        session = FlowsheetSession(config=config)
        session.add_unit("RO", "ReverseOsmosis0D")
        session.fix_variable("RO", "A_comp", 4.2e-12)
        manager.save(session)

        # Create new manager, load session
        manager2 = SessionManager(storage_dir=temp_dir)
        loaded = manager2.load(session.config.session_id)

        assert loaded is not None
        assert "RO" in loaded.units
        assert loaded.units["RO"].fixed_vars["A_comp"] == 4.2e-12

    def test_update_session(self, temp_dir):
        """Update session should persist changes."""
        manager = SessionManager(storage_dir=temp_dir)

        config = SessionConfig(default_property_package=PropertyPackageType.SEAWATER)
        session = FlowsheetSession(config=config)

        session.add_unit("pump", "Pump")
        manager.save(session)

        session.add_unit("RO", "ReverseOsmosis0D")
        manager.save(session)

        loaded = manager.load(session.config.session_id)
        assert "pump" in loaded.units
        assert "RO" in loaded.units

    def test_exists(self, temp_dir):
        """Test exists method."""
        manager = SessionManager(storage_dir=temp_dir)

        config = SessionConfig(default_property_package=PropertyPackageType.SEAWATER)
        session = FlowsheetSession(config=config)

        assert not manager.exists(session.config.session_id)

        manager.save(session)
        assert manager.exists(session.config.session_id)


class TestUnitInstance:
    """Tests for UnitInstance dataclass."""

    def test_create_unit_instance(self):
        """Create a basic unit instance."""
        unit = UnitInstance(
            unit_id="RO1",
            unit_type="ReverseOsmosis0D",
        )
        assert unit.unit_id == "RO1"
        assert unit.unit_type == "ReverseOsmosis0D"
        assert unit.config == {}
        assert unit.fixed_vars == {}
        assert unit.scaling_factors == {}

    def test_unit_instance_with_config(self):
        """Unit instance with config and fixed vars."""
        unit = UnitInstance(
            unit_id="pump1",
            unit_type="Pump",
            config={"has_phase_equilibrium": False},
            fixed_vars={"efficiency_pump": 0.8},
            scaling_factors={"work": 1e-3},
        )
        assert unit.config["has_phase_equilibrium"] is False
        assert unit.fixed_vars["efficiency_pump"] == 0.8
        assert unit.scaling_factors["work"] == 1e-3


class TestConnection:
    """Tests for Connection dataclass."""

    def test_create_connection(self):
        """Create a basic connection."""
        conn = Connection(
            source_unit="pump",
            source_port="outlet",
            dest_unit="RO",
            dest_port="inlet",
        )
        assert conn.source_unit == "pump"
        assert conn.source_port == "outlet"
        assert conn.dest_unit == "RO"
        assert conn.dest_port == "inlet"
        assert conn.translator_id is None

    def test_connection_with_translator(self):
        """Connection with translator."""
        conn = Connection(
            source_unit="asm_reactor",
            source_port="outlet",
            dest_unit="adm_digester",
            dest_port="inlet",
            translator_id="translator_asm1_adm1",
        )
        assert conn.translator_id == "translator_asm1_adm1"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
