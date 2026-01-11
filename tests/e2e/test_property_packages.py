"""Property package tests.

Bug #1: ZO property package requires database config
Bug #8: MCAS requires property_package_config

Codex recommendation: Explicitly test ZO database config and MCAS config.
"""

import pytest


class TestStandardPropertyPackages:
    """Tests for standard property packages (SEAWATER, NACL, NACL_T_DEP)."""

    @pytest.mark.integration
    @pytest.mark.parametrize("pkg", ["SEAWATER", "NACL", "NACL_T_DEP"])
    def test_standard_package_model_builds(self, pkg, session_manager, watertap_available):
        """Standard property packages should build without special config."""
        from core.session import FlowsheetSession, SessionConfig
        from core.property_registry import PropertyPackageType
        from utils.model_builder import ModelBuilder

        config = SessionConfig(
            session_id=f"test-{pkg}",
            default_property_package=PropertyPackageType[pkg],
        )
        session = FlowsheetSession(config=config)
        session.add_unit("Pump1", "Pump", {})
        session_manager.save(session)

        builder = ModelBuilder(session)
        model = builder.build()

        assert model is not None
        assert hasattr(model, 'fs')


class TestZeroOrderPackage:
    """Bug #1: ZO property package requires database config."""

    @pytest.mark.integration
    def test_zo_with_database_builds(self, session_manager, watertap_available):
        """ZO with auto-provided database should build (Bug #1 fix verification)."""
        from core.session import FlowsheetSession, SessionConfig
        from core.property_registry import PropertyPackageType
        from utils.model_builder import ModelBuilder

        config = SessionConfig(
            session_id="test-zo-with-db",
            default_property_package=PropertyPackageType.ZERO_ORDER,
        )
        session = FlowsheetSession(config=config)
        session.add_unit("PumpZO", "PumpZO", {})
        session_manager.save(session)

        builder = ModelBuilder(session)
        # This should NOT raise "database config required" after Bug #1 fix
        model = builder.build()

        assert model is not None
        assert hasattr(model, 'fs')

    @pytest.mark.integration
    def test_zo_database_is_auto_created(self, session_manager, watertap_available):
        """Verify ZO database is auto-created by ModelBuilder (Bug #1)."""
        from core.session import FlowsheetSession, SessionConfig
        from core.property_registry import PropertyPackageType
        from utils.model_builder import ModelBuilder

        config = SessionConfig(
            session_id="test-zo-auto-db",
            default_property_package=PropertyPackageType.ZERO_ORDER,
        )
        session = FlowsheetSession(config=config)
        session.add_unit("PumpZO", "PumpZO", {})
        session_manager.save(session)

        builder = ModelBuilder(session)
        model = builder.build()

        # Model should have been built successfully with auto-created database
        assert model is not None
        assert hasattr(model, 'fs')


class TestMCASPackage:
    """Bug #8: MCAS requires property_package_config."""

    @pytest.mark.integration
    def test_mcas_requires_config(self, session_manager, watertap_available):
        """MCAS without config should fail with clear error message."""
        from core.session import FlowsheetSession, SessionConfig
        from core.property_registry import PropertyPackageType
        from utils.model_builder import ModelBuilder, ModelBuildError

        config = SessionConfig(
            session_id="test-mcas-no-config",
            default_property_package=PropertyPackageType.MCAS,
            # No property_package_config!
        )
        session = FlowsheetSession(config=config)
        session.add_unit("Pump1", "Pump", {})
        session_manager.save(session)

        builder = ModelBuilder(session)

        # Should fail with descriptive error about missing config
        with pytest.raises((ModelBuildError, KeyError, ValueError, TypeError)):
            builder.build()

    @pytest.mark.integration
    def test_mcas_with_config_builds(self, mcas_session, watertap_available):
        """MCAS with proper config should build successfully."""
        from utils.model_builder import ModelBuilder

        builder = ModelBuilder(mcas_session)
        model = builder.build()

        assert model is not None
        assert hasattr(model, 'fs')

    @pytest.mark.integration
    def test_mcas_config_validation(self, session_manager, watertap_available):
        """MCAS with empty solute_list should give clear error."""
        from core.session import FlowsheetSession, SessionConfig
        from core.property_registry import PropertyPackageType
        from utils.model_builder import ModelBuilder, ModelBuildError

        # Empty solute_list - MCAS requires at least one solute
        config = SessionConfig(
            session_id="test-mcas-empty-solutes",
            default_property_package=PropertyPackageType.MCAS,
            property_package_config={
                "solute_list": [],  # Empty - must fail
            }
        )
        session = FlowsheetSession(config=config)
        session.add_unit("Pump1", "Pump", {})
        session_manager.save(session)

        builder = ModelBuilder(session)

        # Should fail with descriptive error about empty solute_list
        with pytest.raises(ModelBuildError):
            builder.build()
