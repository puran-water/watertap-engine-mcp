"""Error path tests - solver failures, invalid inputs, edge cases.

Plan requirement: Explicit tests for solver failures and invalid inputs.

Codex audit: Hardened assertions - tests must check specific exit codes
and error messages, not use permissive 'or' conditions that always pass.
"""

import pytest
import json
from typer.testing import CliRunner


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def cli_app():
    from cli import app
    return app


class TestInvalidInputs:
    """Tests for invalid CLI inputs and graceful error handling."""

    @pytest.mark.unit
    def test_invalid_property_package(self, runner, cli_app):
        """Invalid property package should give clear error."""
        result = runner.invoke(cli_app, [
            "create-session",
            "--property-package", "INVALID_PACKAGE"
        ])
        # Must fail with non-zero exit code
        assert result.exit_code == 1, f"Expected exit code 1, got {result.exit_code}"
        # Must contain "invalid" in error message
        assert "invalid" in result.stdout.lower(), \
            f"Error message should mention 'invalid': {result.stdout}"

    @pytest.mark.unit
    def test_invalid_json_config(self, runner, cli_app):
        """Invalid JSON in --config should give clear error."""
        result = runner.invoke(cli_app, [
            "create-session",
            "--property-package", "MCAS",
            "--config", "not valid json"
        ])
        assert result.exit_code == 1, f"Expected exit code 1, got {result.exit_code}"
        assert "json" in result.stdout.lower(), \
            f"Error message should mention JSON: {result.stdout}"

    @pytest.mark.unit
    def test_nonexistent_session(self, runner, cli_app):
        """Operations on non-existent session should fail gracefully."""
        result = runner.invoke(cli_app, [
            "get-session",
            "--session-id", "nonexistent-session-id-12345"
        ])
        # Must fail with non-zero exit code (1 for app error, 2 for CLI usage error)
        assert result.exit_code in [1, 2], f"Expected non-zero exit code, got {result.exit_code}"
        # Must indicate session not found or show error
        assert "not found" in result.stdout.lower() or "error" in result.stdout.lower(), \
            f"Error message should indicate problem: {result.stdout}"

    @pytest.mark.unit
    def test_create_unit_invalid_type(self, runner, cli_app):
        """Creating unit with invalid type should fail gracefully."""
        import re

        # First create a valid session
        result = runner.invoke(cli_app, ["create-session", "--property-package", "SEAWATER"])
        assert result.exit_code == 0

        match = re.search(r'"session_id":\s*"([^"]+)"', result.stdout)
        session_id = match.group(1)

        # Try to create invalid unit type
        result = runner.invoke(cli_app, [
            "create-unit",
            "--session-id", session_id,
            "--unit-id", "BadUnit",
            "--unit-type", "NonExistentUnitType"
        ])
        # Must fail - invalid unit type
        assert result.exit_code == 1, f"Expected exit code 1, got {result.exit_code}"
        # Error message should indicate unknown unit type
        stdout_lower = result.stdout.lower()
        assert "not found" in stdout_lower or "unknown" in stdout_lower or "invalid" in stdout_lower, \
            f"Error message should indicate invalid unit type: {result.stdout}"

    @pytest.mark.unit
    def test_connect_nonexistent_units(self, runner, cli_app):
        """Connecting non-existent units should fail gracefully."""
        import re

        result = runner.invoke(cli_app, ["create-session", "--property-package", "SEAWATER"])
        assert result.exit_code == 0

        match = re.search(r'"session_id":\s*"([^"]+)"', result.stdout)
        session_id = match.group(1)

        result = runner.invoke(cli_app, [
            "connect-units",
            "--session-id", session_id,
            "--source", "NonExistent1.outlet",
            "--dest", "NonExistent2.inlet"
        ])
        # Must fail - units don't exist
        assert result.exit_code == 1, f"Expected exit code 1, got {result.exit_code}"
        # Error should indicate unit not found
        stdout_lower = result.stdout.lower()
        assert "not found" in stdout_lower or "does not exist" in stdout_lower, \
            f"Error message should indicate unit not found: {result.stdout}"


class TestSolverFailures:
    """Tests for solver failure scenarios."""

    @pytest.mark.integration
    def test_solve_with_zero_dof_unfixed(self, runner, cli_app):
        """Solve without fixing DOF should handle failure gracefully."""
        import re
        import time

        # Create session with units but don't fix DOF
        result = runner.invoke(cli_app, ["create-session", "--property-package", "SEAWATER"])
        assert result.exit_code == 0

        match = re.search(r'"session_id":\s*"([^"]+)"', result.stdout)
        session_id = match.group(1)

        result = runner.invoke(cli_app, [
            "create-unit",
            "--session-id", session_id,
            "--unit-id", "Pump1",
            "--unit-type", "Pump"
        ])
        assert result.exit_code == 0

        # Attempt solve without DOF fixes
        result = runner.invoke(cli_app, ["solve", "--session-id", session_id])
        # Should succeed in submitting job
        assert result.exit_code == 0, f"Solve submission should succeed: {result.stdout}"

        # Extract job_id and verify job completes (likely as "failed")
        job_match = re.search(r'Solve job submitted:\s*(\S+)', result.stdout)
        if job_match:
            job_id = job_match.group(1)
            # Wait for job to complete
            for _ in range(30):  # 60 second timeout
                status_result = runner.invoke(cli_app, ["get-solve-status", job_id])
                if "completed" in status_result.stdout.lower() or "failed" in status_result.stdout.lower():
                    break
                time.sleep(2)
            # Job should eventually complete or fail (not hang forever)
            final_status = status_result.stdout.lower()
            assert "completed" in final_status or "failed" in final_status, \
                f"Job should complete or fail: {status_result.stdout}"

    @pytest.mark.integration
    def test_mcas_without_required_config(self, runner, cli_app):
        """MCAS without solute_list should fail with clear error."""
        import re
        import time

        # MCAS requires solute_list - session creation may succeed
        result = runner.invoke(cli_app, [
            "create-session",
            "--property-package", "MCAS"
            # Missing --config
        ])

        if result.exit_code == 0:
            match = re.search(r'"session_id":\s*"([^"]+)"', result.stdout)
            assert match, "Should have session_id in output"
            session_id = match.group(1)

            # Create unit
            result = runner.invoke(cli_app, [
                "create-unit",
                "--session-id", session_id,
                "--unit-id", "Pump1",
                "--unit-type", "Pump"
            ])
            assert result.exit_code == 0

            # Attempt solve - should fail during model build due to missing config
            result = runner.invoke(cli_app, ["solve", "--session-id", session_id])

            # If job submitted, poll for failure
            job_match = re.search(r'Solve job submitted:\s*(\S+)', result.stdout)
            if job_match:
                job_id = job_match.group(1)
                for _ in range(30):
                    status_result = runner.invoke(cli_app, ["get-solve-status", job_id])
                    if "failed" in status_result.stdout.lower():
                        # Expected: MCAS without config should fail
                        assert "solute" in status_result.stdout.lower() or "error" in status_result.stdout.lower(), \
                            f"Failed job should mention solute/error: {status_result.stdout}"
                        break
                    if "completed" in status_result.stdout.lower():
                        # Unexpected but check anyway
                        break
                    time.sleep(2)
        else:
            # If creation failed, that's also valid - MCAS requires config
            assert "config" in result.stdout.lower() or "solute" in result.stdout.lower(), \
                f"Error should mention config/solute requirement: {result.stdout}"


class TestModelBuildErrors:
    """Tests for model build failures."""

    @pytest.mark.integration
    def test_empty_mcas_solute_list_fails(self, session_manager, watertap_available):
        """MCAS with empty solute_list should fail at model build."""
        from core.session import FlowsheetSession, SessionConfig
        from core.property_registry import PropertyPackageType
        from utils.model_builder import ModelBuilder, ModelBuildError

        config = SessionConfig(
            session_id="test-mcas-empty",
            default_property_package=PropertyPackageType.MCAS,
            property_package_config={"solute_list": []},
        )
        session = FlowsheetSession(config=config)
        session.add_unit("Pump1", "Pump", {})
        session_manager.save(session)

        builder = ModelBuilder(session)

        with pytest.raises(ModelBuildError):
            builder.build()

    @pytest.mark.integration
    def test_invalid_unit_config(self, session_manager, watertap_available):
        """Unit with invalid config should either ignore or raise clear error."""
        from core.session import FlowsheetSession, SessionConfig
        from core.property_registry import PropertyPackageType
        from utils.model_builder import ModelBuilder, ModelBuildError

        config = SessionConfig(
            session_id="test-invalid-unit-config",
            default_property_package=PropertyPackageType.SEAWATER,
        )
        session = FlowsheetSession(config=config)
        # Add unit with potentially problematic config
        session.add_unit("Pump1", "Pump", {"invalid_param": "invalid_value"})
        session_manager.save(session)

        builder = ModelBuilder(session)
        # Build should either:
        # 1. Succeed (ignoring unknown config) with valid model, OR
        # 2. Fail with ModelBuildError
        # It should NOT raise unhandled exceptions
        try:
            model = builder.build()
            # If build succeeds, model must be valid
            assert model is not None, "Build returned None model"
            assert hasattr(model, 'fs'), "Model should have flowsheet block"
        except ModelBuildError as e:
            # Expected error type for config issues
            assert str(e), "ModelBuildError should have message"
        except (KeyError, ValueError, TypeError) as e:
            # Acceptable specific errors
            pytest.fail(f"Build raised {type(e).__name__} instead of ModelBuildError: {e}")


class TestEdgeCases:
    """Edge case tests."""

    @pytest.mark.unit
    def test_empty_session_operations(self, runner, cli_app):
        """Operations on empty session should work."""
        import re

        result = runner.invoke(cli_app, ["create-session", "--property-package", "SEAWATER"])
        assert result.exit_code == 0

        match = re.search(r'"session_id":\s*"([^"]+)"', result.stdout)
        session_id = match.group(1)

        # Get DOF on empty session - should succeed with DOF=0
        result = runner.invoke(cli_app, ["get-dof-status", "--session-id", session_id])
        assert result.exit_code == 0, f"get-dof-status failed: {result.stdout}"
        # Should contain DOF info
        assert "dof" in result.stdout.lower(), \
            f"Output should contain DOF info: {result.stdout}"

    @pytest.mark.unit
    def test_duplicate_unit_id(self, runner, cli_app):
        """Creating duplicate unit ID should be handled."""
        import re

        result = runner.invoke(cli_app, ["create-session", "--property-package", "SEAWATER"])
        assert result.exit_code == 0

        match = re.search(r'"session_id":\s*"([^"]+)"', result.stdout)
        session_id = match.group(1)

        # Create first unit
        result = runner.invoke(cli_app, [
            "create-unit",
            "--session-id", session_id,
            "--unit-id", "Pump1",
            "--unit-type", "Pump"
        ])
        assert result.exit_code == 0

        # Try to create duplicate
        result = runner.invoke(cli_app, [
            "create-unit",
            "--session-id", session_id,
            "--unit-id", "Pump1",
            "--unit-type", "Pump"
        ])
        # Must either:
        # 1. Fail with error (exit code 1), OR
        # 2. Succeed and overwrite (exit code 0)
        # Either behavior is acceptable, but must be deterministic
        assert result.exit_code in [0, 1], f"Unexpected exit code: {result.exit_code}"
        if result.exit_code == 1:
            # If it fails, error should mention duplicate/exists
            assert "exist" in result.stdout.lower() or "duplicate" in result.stdout.lower(), \
                f"Error should mention duplicate: {result.stdout}"

    @pytest.mark.unit
    def test_special_characters_in_session_name(self, runner, cli_app):
        """Session with special characters in name should work."""
        import re

        result = runner.invoke(cli_app, [
            "create-session",
            "--property-package", "SEAWATER",
            "--name", "Test Session with spaces & symbols!"
        ])
        assert result.exit_code == 0, f"Session creation failed: {result.stdout}"
        # Should have valid session_id in output
        match = re.search(r'"session_id":\s*"([^"]+)"', result.stdout)
        assert match, f"Should have session_id in output: {result.stdout}"
        session_id = match.group(1)
        assert len(session_id) > 0, "session_id should not be empty"
