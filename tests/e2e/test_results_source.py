"""Tests for results source indication.

Verifies that get_stream_results and get_unit_results correctly indicate
whether values are from a solved model or an unsolved model.
"""

import pytest
import sys
import os

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


class TestResultsSourceIndication:
    """Tests that results clearly indicate their source."""

    def test_get_stream_results_unsolved_has_warning(self, seawater_pump_session, session_manager):
        """Before solve, get_stream_results should show source='unsolved_model' with warning."""
        import server
        # Monkey-patch the server's session_manager for this test
        original_sm = server.session_manager
        server.session_manager = session_manager

        try:
            result = server.get_stream_results(seawater_pump_session.config.session_id)

            # Should have unsolved source indication
            assert result.get("source") == "unsolved_model"
            assert "warning" in result
            assert "unsolved" in result["warning"].lower()
        finally:
            server.session_manager = original_sm

    def test_get_unit_results_unsolved_has_warning(self, seawater_pump_session, session_manager):
        """Before solve, get_unit_results should show source='unsolved_model' with warning."""
        import server
        original_sm = server.session_manager
        server.session_manager = session_manager

        try:
            result = server.get_unit_results(seawater_pump_session.config.session_id, "Pump1")

            # Should have unsolved source indication
            assert result.get("source") == "unsolved_model"
            assert "warning" in result
            assert "unsolved" in result["warning"].lower()
        finally:
            server.session_manager = original_sm

    def test_solved_session_has_solved_source(self, seawater_pump_session, session_manager):
        """After solve, results should have source='solved' if KPIs are persisted."""
        import server
        from core.session import SessionStatus

        original_sm = server.session_manager
        server.session_manager = session_manager

        try:
            # Simulate a solved session by setting results
            seawater_pump_session.results = {
                "kpis": {
                    "streams": {
                        "Feed": {
                            "outlet": {"flow_mass": 1.0}
                        }
                    },
                    "units": {}
                }
            }
            seawater_pump_session.status = SessionStatus.SOLVED
            session_manager.save(seawater_pump_session)

            result = server.get_stream_results(seawater_pump_session.config.session_id)

            # Should have solved source indication
            assert result.get("source") == "solved"
            assert "warning" not in result
        finally:
            server.session_manager = original_sm

    def test_results_fallback_fields_present(self, seawater_pump_session, session_manager):
        """Fallback path should include all required fields."""
        import server
        original_sm = server.session_manager
        server.session_manager = session_manager

        try:
            result = server.get_stream_results(seawater_pump_session.config.session_id)

            # Required fields for fallback
            assert "session_id" in result
            assert "source" in result
            assert "streams" in result or "error" not in result
        finally:
            server.session_manager = original_sm


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
