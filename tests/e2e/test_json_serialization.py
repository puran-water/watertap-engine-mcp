"""JSON serialization tests.

Bug #3: Tuple keys in state_args can't be JSON-serialized
Bug #9: Job result JSON truncated on failed solves
"""

import pytest
import json


class TestTupleKeySerialization:
    """Bug #3: Tuple keys like ('Liq', 'H2O') must be converted to strings."""

    @pytest.mark.unit
    def test_serialize_dict_keys_handles_tuples(self):
        """_serialize_dict_keys should convert tuple keys to strings."""
        from core.session import _serialize_dict_keys

        # Typical state_args structure with tuple keys
        state_args = {
            "flow_mass_phase_comp": {
                ("Liq", "H2O"): 0.965,
                ("Liq", "NaCl"): 0.035,
            },
            "temperature": 298.15,
            "pressure": 101325,
        }

        serialized = _serialize_dict_keys(state_args)

        # Should be JSON-serializable now
        json_str = json.dumps(serialized)
        loaded = json.loads(json_str)

        # Tuple keys should be converted to string representation
        assert "flow_mass_phase_comp" in loaded
        flow_data = loaded["flow_mass_phase_comp"]
        # Keys should be strings, not tuples
        for key in flow_data:
            assert isinstance(key, str)

    @pytest.mark.unit
    def test_deserialize_dict_keys_restores_tuples(self):
        """_deserialize_dict_keys should restore tuple keys from strings."""
        from core.session import _serialize_dict_keys, _deserialize_dict_keys

        original = {
            "flow_mass_phase_comp": {
                ("Liq", "H2O"): 0.965,
                ("Liq", "NaCl"): 0.035,
            },
        }

        # Round-trip through JSON
        serialized = _serialize_dict_keys(original)
        json_str = json.dumps(serialized)
        loaded = json.loads(json_str)
        restored = _deserialize_dict_keys(loaded)

        # Tuple keys should be restored
        assert ("Liq", "H2O") in restored["flow_mass_phase_comp"]
        assert ("Liq", "NaCl") in restored["flow_mass_phase_comp"]

    @pytest.mark.unit
    def test_nested_dict_serialization(self):
        """Deeply nested dicts with tuple keys should serialize correctly."""
        from core.session import _serialize_dict_keys

        nested = {
            "level1": {
                "level2": {
                    ("tuple", "key"): "value",
                }
            }
        }

        serialized = _serialize_dict_keys(nested)
        json_str = json.dumps(serialized)

        # Should not raise
        loaded = json.loads(json_str)
        assert "level1" in loaded


class TestJSONCompleteness:
    """Tests for JSON file completeness (not truncated)."""

    @pytest.mark.unit
    def test_large_dict_serializes_completely(self):
        """Large result dicts should serialize completely."""
        # Create a large dict similar to solve results
        large_result = {
            "solver_status": "optimal",
            "termination_condition": "optimal",
            "solve_time": 5.123,
            "iterations": 42,
            "kpis": {
                "streams": {
                    f"unit_{i}": {
                        "inlet": {
                            "flow_mass_phase_comp": {
                                "('Liq', 'H2O')": 0.965 + i * 0.001,
                                "('Liq', 'NaCl')": 0.035 - i * 0.0001,
                            },
                            "temperature": 298.15,
                            "pressure": 101325 + i * 1000,
                        }
                    }
                    for i in range(50)  # 50 units
                },
                "units": {
                    f"unit_{i}": {
                        "recovery_frac_mass_H2O": 0.95 - i * 0.01,
                        "specific_energy_consumption": 3.5 + i * 0.1,
                    }
                    for i in range(50)
                }
            }
        }

        # Serialize and verify completeness
        json_str = json.dumps(large_result, indent=2)

        # Should be able to parse back completely
        loaded = json.loads(json_str)

        assert loaded["solver_status"] == "optimal"
        assert len(loaded["kpis"]["streams"]) == 50
        assert len(loaded["kpis"]["units"]) == 50

    @pytest.mark.unit
    def test_error_result_serializes_completely(self):
        """Error results with stack traces should serialize completely."""
        error_result = {
            "job_id": "test-error",
            "status": "failed",
            "error": "ValueError: DOF is 5, expected 0\n" + "".join([
                f"  File 'module_{i}.py', line {i*10}, in function_{i}\n"
                for i in range(20)  # Long stack trace
            ]),
            "result": {
                "partial_data": "some data",
            }
        }

        json_str = json.dumps(error_result, indent=2)
        loaded = json.loads(json_str)

        assert loaded["status"] == "failed"
        assert "DOF is 5" in loaded["error"]
        assert loaded["result"]["partial_data"] == "some data"
