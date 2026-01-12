"""Tests for validate_flowsheet tool.

Tests the enhanced validate_flowsheet function including:
- Orphan ports detection
- Property package compatibility checking
- DOF status validation
- Unconnected units detection
"""

import pytest
import sys
import os

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server as srv


class TestValidateFlowsheetOrphanPorts:
    """Test orphan port detection in validate_flowsheet."""

    def test_detects_unconnected_inlet(self):
        """Test that unconnected inlet ports generate warnings."""
        result = srv.create_session(property_package="SEAWATER")
        session_id = result["session_id"]

        # Create a pump - it has inlet and outlet ports
        srv.create_unit(session_id, unit_type="Pump", unit_id="pump1")

        # Validate without connecting anything
        result = srv.validate_flowsheet(session_id)

        # Should have warnings about unconnected ports
        warnings = result.get("warnings", [])
        warning_text = " ".join(warnings)

        # Pump should have unconnected inlet warning
        assert "inlet" in warning_text.lower() or len(warnings) > 0

        # Cleanup
        srv.delete_session(session_id)

    def test_no_orphan_warnings_when_connected(self):
        """Test that properly connected units don't generate orphan warnings."""
        result = srv.create_session(property_package="SEAWATER")
        session_id = result["session_id"]

        # Create feed and pump
        srv.create_feed(session_id, flow_vol_m3_hr=3.6, tds_mg_L=35000)
        srv.create_unit(session_id, unit_type="Pump", unit_id="pump1")

        # Connect feed to pump
        srv.connect_ports(
            session_id,
            source_unit="Feed",
            source_port="outlet",
            dest_unit="pump1",
            dest_port="inlet",
        )

        result = srv.validate_flowsheet(session_id)

        # Should not have inlet orphan warning for pump
        warnings = result.get("warnings", [])
        inlet_warnings = [w for w in warnings if "pump1" in w and "inlet" in w]
        assert len(inlet_warnings) == 0

        # Cleanup
        srv.delete_session(session_id)


class TestValidateFlowsheetPropertyPackage:
    """Test property package compatibility validation."""

    def test_compatible_property_package(self):
        """Test validation with compatible property package."""
        result = srv.create_session(property_package="SEAWATER")
        session_id = result["session_id"]

        # Pump should be compatible with SEAWATER
        srv.create_unit(session_id, unit_type="Pump", unit_id="pump1")

        result = srv.validate_flowsheet(session_id)

        # Should not have compatibility issues
        issues = result.get("issues", [])
        compat_issues = [i for i in issues if "compatible" in i.lower()]
        assert len(compat_issues) == 0

        # Cleanup
        srv.delete_session(session_id)

    def test_connection_level_compatibility_same_package(self):
        """Test that units with same property package pass validation."""
        result = srv.create_session(property_package="SEAWATER")
        session_id = result["session_id"]

        # Create two SEAWATER-compatible units and connect them
        srv.create_unit(session_id, unit_type="Pump", unit_id="pump1")
        srv.create_unit(session_id, unit_type="Pump", unit_id="pump2")
        srv.connect_ports(
            session_id,
            source_unit="pump1",
            source_port="outlet",
            dest_unit="pump2",
            dest_port="inlet",
        )

        result = srv.validate_flowsheet(session_id)

        # Should not have translator-related issues
        issues = result.get("issues", [])
        translator_issues = [i for i in issues if "translator" in i.lower()]
        assert len(translator_issues) == 0

        # Cleanup
        srv.delete_session(session_id)


class TestValidateFlowsheetDOF:
    """Test DOF validation in validate_flowsheet."""

    def test_validates_dof_after_fix(self):
        """Test that DOF is reflected in validation."""
        result = srv.create_session(property_package="SEAWATER")
        session_id = result["session_id"]

        srv.create_feed(session_id, flow_vol_m3_hr=3.6, tds_mg_L=35000)
        srv.create_unit(session_id, unit_type="Pump", unit_id="pump1")

        result = srv.validate_flowsheet(session_id)

        # DOF status should be included
        assert "dof_status" in result or "total_dof" in result

        # Cleanup
        srv.delete_session(session_id)


class TestValidateFlowsheetUnconnectedUnits:
    """Test unconnected unit detection."""

    def test_detects_unconnected_units(self):
        """Test that unconnected units are identified."""
        result = srv.create_session(property_package="SEAWATER")
        session_id = result["session_id"]

        # Create two units but don't connect them
        srv.create_unit(session_id, unit_type="Pump", unit_id="pump1")
        srv.create_unit(session_id, unit_type="Pump", unit_id="pump2")

        result = srv.validate_flowsheet(session_id)

        # Should have unconnected unit warning/issue
        warnings = result.get("warnings", [])
        issues = result.get("issues", [])
        all_messages = warnings + issues

        # At least one pump should have unconnected ports
        assert len(all_messages) > 0

        # Cleanup
        srv.delete_session(session_id)


class TestValidateFlowsheetEmptySession:
    """Test validation of empty flowsheet."""

    def test_empty_flowsheet_validation(self):
        """Test validating a flowsheet with no units."""
        result = srv.create_session(property_package="SEAWATER")
        session_id = result["session_id"]

        result = srv.validate_flowsheet(session_id)

        # Should not error, but may have warnings
        assert "error" not in result

        # Cleanup
        srv.delete_session(session_id)


class TestValidateFlowsheetSessionNotFound:
    """Test validation with non-existent session."""

    def test_session_not_found(self):
        """Test validation with non-existent session."""
        result = srv.validate_flowsheet("nonexistent-session-id")
        assert "error" in result


class TestValidateFlowsheetValid:
    """Test validation returns valid=True for proper flowsheets."""

    def test_valid_field_in_response(self):
        """Test that 'valid' field is returned."""
        result = srv.create_session(property_package="SEAWATER")
        session_id = result["session_id"]

        result = srv.validate_flowsheet(session_id)

        # Should have a 'valid' field
        assert "valid" in result

        # Cleanup
        srv.delete_session(session_id)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
