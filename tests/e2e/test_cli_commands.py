"""CLI command tests.

Codex recommendation: Split into smoke tests (fast, exit code/help) and deep tests (real solves).
"""

import pytest
import re
from io import BytesIO, TextIOWrapper
from typer.testing import CliRunner


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def cli_app():
    from cli import app
    return app


# =============================================================================
# SMOKE TESTS (fast, no solver required)
# =============================================================================

class TestCLISmoke:
    """Fast smoke tests - verify commands exist and parse correctly."""

    @pytest.mark.unit
    @pytest.mark.parametrize("cmd", [
        # Main help
        ["--help"],
        # Session commands
        ["create-session", "--help"],
        ["list-sessions"],
        ["get-session", "--help"],
        ["delete-session", "--help"],
        # Registry commands
        ["list-units"],
        ["list-property-packages"],
        ["list-translators"],
        ["get-unit-spec-cmd", "--help"],
        # Build commands
        ["create-unit", "--help"],
        ["create-feed", "--help"],
        ["connect-units", "--help"],
        # DOF commands
        ["get-dof-status", "--help"],
        ["fix-variable", "--help"],
        ["unfix-variable", "--help"],
        # Scaling commands
        ["set-scaling-factor", "--help"],
        ["calculate-scaling-factors", "--help"],
        ["report-scaling-issues", "--help"],
        # Solver commands
        ["initialize-flowsheet", "--help"],
        ["solve", "--help"],
        ["get-solve-status", "--help"],
        # Results commands
        ["get-results", "--help"],
    ])
    def test_command_exists_and_has_help(self, runner, cli_app, cmd):
        """All CLI commands should have help and not crash."""
        result = runner.invoke(cli_app, cmd)
        assert result.exit_code == 0

    @pytest.mark.unit
    def test_create_session_seawater(self, runner, cli_app):
        """Create session with SEAWATER property package."""
        result = runner.invoke(cli_app, ["create-session", "--property-package", "SEAWATER"])
        assert result.exit_code == 0
        assert "session_id" in result.stdout

    @pytest.mark.unit
    def test_create_session_nacl(self, runner, cli_app):
        """Create session with NACL property package."""
        result = runner.invoke(cli_app, ["create-session", "--property-package", "NACL"])
        assert result.exit_code == 0
        assert "session_id" in result.stdout

    @pytest.mark.unit
    def test_create_session_invalid_package(self, runner, cli_app):
        """Invalid property package should fail with clear error."""
        result = runner.invoke(cli_app, ["create-session", "--property-package", "INVALID"])
        assert result.exit_code == 1
        assert "Invalid property package" in result.stdout


class TestCLIMCASConfig:
    """Bug #8: CLI missing --config option for property_package_config."""

    @pytest.mark.unit
    def test_create_session_mcas_with_config(self, runner, cli_app):
        """MCAS with --config should work."""
        config = '{"solute_list": ["Na_+", "Cl_-"], "charge": {"Na_+": 1, "Cl_-": -1}, "mw_data": {"Na_+": 0.023, "Cl_-": 0.0355}}'
        result = runner.invoke(cli_app, [
            "create-session",
            "--property-package", "MCAS",
            "--config", config
        ])
        assert result.exit_code == 0
        assert "session_id" in result.stdout

    @pytest.mark.unit
    def test_create_session_invalid_json_config(self, runner, cli_app):
        """Invalid JSON in --config should fail with clear error."""
        result = runner.invoke(cli_app, [
            "create-session",
            "--property-package", "MCAS",
            "--config", "not valid json"
        ])
        assert result.exit_code == 1
        assert "Invalid JSON" in result.stdout

    @pytest.mark.unit
    def test_config_option_appears_in_help(self, runner, cli_app):
        """The --config option should appear in create-session help."""
        result = runner.invoke(cli_app, ["create-session", "--help"])
        assert result.exit_code == 0
        assert "--config" in result.stdout


# =============================================================================
# ENCODING TESTS (deterministic, Codex recommendation)
# =============================================================================

class TestCLIEncoding:
    """Bug #4: Unicode symbols fail on Windows cp1252 console.

    Codex recommendation: Simulate cp1252 stream to make test deterministic
    on all platforms (not just Windows).
    """

    @pytest.mark.unit
    def test_output_is_cp1252_safe_list_sessions(self, runner, cli_app):
        """CLI list-sessions output must be encodable to Windows cp1252."""
        result = runner.invoke(cli_app, ["list-sessions"])

        # Simulate writing to a cp1252 stream (like Windows console)
        cp1252_stream = TextIOWrapper(BytesIO(), encoding="cp1252", errors="strict")
        try:
            cp1252_stream.write(result.stdout)
            cp1252_stream.flush()
        except UnicodeEncodeError as e:
            pytest.fail(f"Output contains cp1252-incompatible characters: {e}")

    @pytest.mark.unit
    def test_output_is_cp1252_safe_list_units(self, runner, cli_app):
        """CLI list-units output must be encodable to Windows cp1252."""
        result = runner.invoke(cli_app, ["list-units"])

        cp1252_stream = TextIOWrapper(BytesIO(), encoding="cp1252", errors="strict")
        try:
            cp1252_stream.write(result.stdout)
            cp1252_stream.flush()
        except UnicodeEncodeError as e:
            pytest.fail(f"Output contains cp1252-incompatible characters: {e}")

    @pytest.mark.unit
    def test_error_output_is_cp1252_safe(self, runner, cli_app):
        """Error messages must also be cp1252-safe."""
        result = runner.invoke(cli_app, ["get-session", "nonexistent-id"])

        cp1252_stream = TextIOWrapper(BytesIO(), encoding="cp1252", errors="strict")
        try:
            cp1252_stream.write(result.stdout)
        except UnicodeEncodeError as e:
            pytest.fail(f"Error output contains cp1252-incompatible chars: {e}")


# =============================================================================
# WORKFLOW TESTS (deeper, may require WaterTAP)
# =============================================================================

class TestCLIWorkflow:
    """Full CLI workflow tests."""

    @pytest.mark.slow
    def test_full_workflow_create_to_dof(self, runner, cli_app):
        """Full CLI workflow: create -> units -> connect -> DOF check."""
        # 1. Create session
        result = runner.invoke(cli_app, ["create-session", "--property-package", "SEAWATER"])
        assert result.exit_code == 0

        # Extract session_id
        match = re.search(r'"session_id":\s*"([^"]+)"', result.stdout)
        assert match, f"Could not find session_id in output: {result.stdout}"
        session_id = match.group(1)

        # 2. Create units
        for unit_id, unit_type in [("Feed1", "Feed"), ("Pump1", "Pump")]:
            result = runner.invoke(cli_app, [
                "create-unit", "--session-id", session_id,
                "--unit-id", unit_id, "--unit-type", unit_type
            ])
            assert result.exit_code == 0, f"Failed to create {unit_id}: {result.stdout}"

        # 3. Connect
        result = runner.invoke(cli_app, [
            "connect-units", "--session-id", session_id,
            "--source", "Feed1.outlet", "--dest", "Pump1.inlet"
        ])
        assert result.exit_code == 0

        # 4. Get DOF
        result = runner.invoke(cli_app, ["get-dof-status", "--session-id", session_id])
        assert result.exit_code == 0
        assert "DOF" in result.stdout
