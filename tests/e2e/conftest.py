"""E2E test fixtures providing real WaterTAP models and sessions.

CODEX AUDIT: All skips removed. Tests FAIL LOUDLY if dependencies unavailable.

Codex-vetted fixture design:
- Session scope for expensive imports (watertap_available, solver_available)
- Function scope for mutable model instances to prevent cross-test contamination
"""

import pytest
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

# FAIL LOUDLY if WaterTAP/IDAES not available - NO SKIPS
import watertap
import idaes
from watertap.core.solvers import get_solver
from pyomo.environ import ConcreteModel, SolverFactory
from idaes.core import FlowsheetBlock


@pytest.fixture(scope="session")
def watertap_available():
    """Verify WaterTAP is importable (session-scoped for efficiency).

    FAIL LOUDLY: ImportError propagates if WaterTAP not installed.
    """
    from watertap.core.solvers import get_solver
    from pyomo.environ import ConcreteModel
    from idaes.core import FlowsheetBlock
    return True


@pytest.fixture(scope="session")
def solver_available():
    """Check if IPOPT is available.

    FAIL LOUDLY: AssertionError if IPOPT not available.
    """
    from pyomo.environ import SolverFactory
    solver = SolverFactory("ipopt")
    assert solver.available(), "IPOPT solver not available - tests require IPOPT"
    return solver


@pytest.fixture
def temp_storage(tmp_path):
    """Create temp directories for sessions/jobs (function-scoped)."""
    flowsheets = tmp_path / "flowsheets"
    jobs = tmp_path / "jobs"
    flowsheets.mkdir()
    jobs.mkdir()
    return {"flowsheets": flowsheets, "jobs": jobs}


@pytest.fixture
def session_manager(temp_storage):
    """SessionManager with temp directory (function-scoped)."""
    from core.session import SessionManager
    return SessionManager(temp_storage["flowsheets"])


@pytest.fixture
def seawater_pump_session(session_manager):
    """Pre-built SEAWATER session with Feed -> Pump (simplest solvable).

    Function-scoped to prevent cross-test contamination (Codex recommendation).
    """
    from core.session import FlowsheetSession, SessionConfig
    from core.property_registry import PropertyPackageType

    config = SessionConfig(
        session_id="test-seawater-pump",
        default_property_package=PropertyPackageType.SEAWATER,
    )
    session = FlowsheetSession(config=config)
    session.add_unit("Feed", "Feed", {})
    session.add_unit("Pump1", "Pump", {})
    session.add_connection("Feed", "outlet", "Pump1", "inlet")

    # Fix DOF
    session.fix_variable("Pump1", "efficiency_pump", 0.75)
    session.fix_variable("Pump1", "control_volume.properties_out[0].pressure", 500000)

    session_manager.save(session)
    return session


@pytest.fixture
def mcas_session(session_manager):
    """MCAS session with required config (function-scoped)."""
    from core.session import FlowsheetSession, SessionConfig
    from core.property_registry import PropertyPackageType

    config = SessionConfig(
        session_id="test-mcas",
        default_property_package=PropertyPackageType.MCAS,
        property_package_config={
            "solute_list": ["Na_+", "Cl_-"],
            "charge": {"Na_+": 1, "Cl_-": -1},
            "mw_data": {"Na_+": 23e-3, "Cl_-": 35.5e-3},
        }
    )
    session = FlowsheetSession(config=config)
    session.add_unit("Pump1", "Pump", {})
    session_manager.save(session)
    return session


@pytest.fixture
def zo_session(session_manager):
    """Zero-Order session (function-scoped)."""
    from core.session import FlowsheetSession, SessionConfig
    from core.property_registry import PropertyPackageType

    config = SessionConfig(
        session_id="test-zo",
        default_property_package=PropertyPackageType.ZERO_ORDER,
    )
    session = FlowsheetSession(config=config)
    session.add_unit("PumpZO", "PumpZO", {})
    session_manager.save(session)
    return session
