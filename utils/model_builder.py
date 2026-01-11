"""Model Builder for WaterTAP Flowsheets.

Reconstructs a Pyomo ConcreteModel from session state, creating unit
instances and connections based on the session configuration.

NOTE: This module requires WaterTAP/IDAES to be installed. It is designed
to be used in worker.py which runs in a subprocess where heavy imports
are acceptable.
"""

import importlib
from typing import Any, Dict, List, Optional, Tuple

from core.session import FlowsheetSession, UnitInstance, Connection
from core.unit_registry import UNITS, UnitSpec
from core.property_registry import PROPERTY_PACKAGES, PropertyPackageType
from core.translator_registry import TRANSLATORS, TranslatorSpec, get_translator


class ModelBuildError(Exception):
    """Error during model building."""
    pass


class ModelBuilder:
    """Builds a Pyomo model from session state."""

    def __init__(self, session: FlowsheetSession):
        """Initialize with session.

        Args:
            session: FlowsheetSession containing flowsheet definition
        """
        self._session = session
        self._model = None
        self._flowsheet = None
        self._units = {}  # unit_id -> actual Pyomo block
        self._translators = {}  # translator_id -> actual translator block
        self._property_packages = {}  # pkg_name -> actual pkg block

    def build(self) -> Any:
        """Build the complete model from session.

        Returns:
            ConcreteModel with all units and connections

        Raises:
            ModelBuildError: If build fails
        """
        try:
            # Create base model
            self._create_base_model()

            # Create property packages
            self._create_property_packages()

            # Create units
            self._create_units()

            # Create translators
            self._create_translators()

            # Create connections (Arcs)
            self._create_connections()

            # Apply feed state
            self._apply_feed_state()

            # Apply fixed variables
            self._apply_fixed_variables()

            # Apply scaling factors
            self._apply_scaling_factors()

            return self._model

        except ImportError as e:
            raise ModelBuildError(f"Import error: {e}. Ensure WaterTAP/IDAES is installed.")
        except Exception as e:
            raise ModelBuildError(f"Build failed: {type(e).__name__}: {e}")

    def _create_base_model(self):
        """Create ConcreteModel with FlowsheetBlock."""
        from pyomo.environ import ConcreteModel
        from idaes.core import FlowsheetBlock

        self._model = ConcreteModel()
        self._model.fs = FlowsheetBlock(dynamic=False)
        self._flowsheet = self._model.fs

    def _create_property_packages(self):
        """Create property packages needed by the flowsheet.

        Handles:
        - Default property package with optional config (MCAS, ZO need config)
        - Additional packages required by translators (source/dest may differ)
        """
        # Get default package
        default_pkg_type = self._session.config.default_property_package
        pkg_spec = PROPERTY_PACKAGES.get(default_pkg_type)

        if pkg_spec is None:
            raise ModelBuildError(f"Unknown property package: {default_pkg_type}")

        # Get user-provided config for the package
        pkg_config = self._session.config.property_package_config or {}

        # Validate required config
        if pkg_spec.requires_config:
            missing = [f for f in pkg_spec.config_fields if f not in pkg_config]
            if missing and pkg_spec.database_required:
                raise ModelBuildError(
                    f"Property package {default_pkg_type.name} requires config: {missing}. "
                    f"Required fields: {pkg_spec.config_fields}"
                )
            # Note: For MCAS, missing config is an error; for ZO, we try default database

        # Import and create the property package
        try:
            module = importlib.import_module(pkg_spec.module_path)
            PkgClass = getattr(module, pkg_spec.class_name)

            # Build config kwargs for package instantiation
            config_kwargs = self._build_package_config(pkg_spec, pkg_config)

            # Create property package on flowsheet
            if config_kwargs:
                setattr(self._flowsheet, "prop_params", PkgClass(**config_kwargs))
            else:
                setattr(self._flowsheet, "prop_params", PkgClass())

            self._property_packages["default"] = getattr(self._flowsheet, "prop_params")
            self._property_packages[default_pkg_type.value] = self._property_packages["default"]

        except ImportError as e:
            raise ModelBuildError(f"Cannot import property package {pkg_spec.module_path}: {e}")
        except Exception as e:
            raise ModelBuildError(f"Cannot create property package {default_pkg_type}: {e}")

        # Create additional packages needed by translators
        self._create_translator_packages()

    def _build_package_config(self, pkg_spec: 'PropertyPackageSpec', user_config: Dict) -> Dict:
        """Build configuration kwargs for property package instantiation.

        Args:
            pkg_spec: Property package specification
            user_config: User-provided configuration

        Returns:
            Config kwargs dict for package constructor
        """
        config = {}

        if pkg_spec.pkg_type == PropertyPackageType.MCAS:
            # MCAS requires explicit solute config
            if "solute_list" in user_config:
                config["solute_list"] = user_config["solute_list"]
            if "charge" in user_config:
                config["charge"] = user_config["charge"]
            if "mw_data" in user_config:
                config["mw_data"] = user_config["mw_data"]

        elif pkg_spec.pkg_type == PropertyPackageType.ZERO_ORDER:
            # Zero-order requires database
            if "database" in user_config:
                config["database"] = user_config["database"]
            elif pkg_spec.database_required:
                # Try to create default database
                try:
                    from watertap.core.wt_database import Database
                    config["database"] = Database()
                except ImportError:
                    pass  # Will fail later with clearer error

            if "water_source" in user_config:
                config["water_source"] = user_config["water_source"]
            if "solute_list" in user_config:
                config["solute_list"] = user_config["solute_list"]

        return config

    def _create_translator_packages(self):
        """Create property packages required by translators.

        Translators (ASM↔ADM) need different packages for inlet and outlet.
        This creates those packages if they differ from the default.
        """
        for trans_id, trans_data in self._session.translators.items():
            source_pkg_type = trans_data.get("source_pkg")
            dest_pkg_type = trans_data.get("dest_pkg")

            if source_pkg_type is None or dest_pkg_type is None:
                continue

            # Convert string to enum if needed
            if isinstance(source_pkg_type, str):
                source_pkg_type = PropertyPackageType(source_pkg_type)
            if isinstance(dest_pkg_type, str):
                dest_pkg_type = PropertyPackageType(dest_pkg_type)

            # Create source package if not already created
            if source_pkg_type.value not in self._property_packages:
                self._create_additional_package(source_pkg_type, f"prop_{source_pkg_type.name.lower()}")

            # Create dest package if not already created
            if dest_pkg_type.value not in self._property_packages:
                self._create_additional_package(dest_pkg_type, f"prop_{dest_pkg_type.name.lower()}")

    def _create_additional_package(self, pkg_type: PropertyPackageType, attr_name: str):
        """Create an additional property package on the flowsheet.

        Args:
            pkg_type: Property package type to create
            attr_name: Attribute name to use on flowsheet
        """
        pkg_spec = PROPERTY_PACKAGES.get(pkg_type)
        if pkg_spec is None:
            raise ModelBuildError(f"Unknown property package: {pkg_type}")

        try:
            module = importlib.import_module(pkg_spec.module_path)
            PkgClass = getattr(module, pkg_spec.class_name)

            # Create without extra config (biological packages don't need user config)
            setattr(self._flowsheet, attr_name, PkgClass())
            self._property_packages[pkg_type.value] = getattr(self._flowsheet, attr_name)

        except ImportError as e:
            raise ModelBuildError(f"Cannot import property package {pkg_spec.module_path}: {e}")
        except Exception as e:
            raise ModelBuildError(f"Cannot create property package {pkg_type}: {e}")

    def _create_units(self):
        """Create all units from session."""
        for unit_id, unit_inst in self._session.units.items():
            self._create_unit(unit_id, unit_inst)

    def _create_unit(self, unit_id: str, unit_inst: UnitInstance):
        """Create a single unit.

        Args:
            unit_id: Unit identifier
            unit_inst: UnitInstance from session
        """
        spec = UNITS.get(unit_inst.unit_type)
        if spec is None:
            raise ModelBuildError(f"Unknown unit type: {unit_inst.unit_type}")

        try:
            # Import unit class
            module = importlib.import_module(spec.module_path)
            UnitClass = getattr(module, spec.class_name)

            # Build unit config
            config = self._build_unit_config(spec, unit_inst)

            # Create unit on flowsheet
            unit_block = UnitClass(**config)
            setattr(self._flowsheet, unit_id, unit_block)

            self._units[unit_id] = unit_block

        except ImportError as e:
            raise ModelBuildError(f"Cannot import unit {spec.module_path}: {e}")
        except Exception as e:
            raise ModelBuildError(f"Cannot create unit {unit_id} ({unit_inst.unit_type}): {e}")

    def _build_unit_config(self, spec: UnitSpec, unit_inst: UnitInstance) -> Dict:
        """Build configuration dict for unit creation.

        Args:
            spec: UnitSpec for the unit type
            unit_inst: UnitInstance with user config

        Returns:
            Configuration dict for unit constructor
        """
        config = {}

        # Property package reference
        default_pkg = self._property_packages.get("default")
        if default_pkg is not None:
            config["property_package"] = default_pkg

        # Merge user-provided config
        config.update(unit_inst.config)

        return config

    def _create_translators(self):
        """Create translator blocks from session translators.

        Translators are units that convert state variables between property packages.
        Currently only ASM↔ADM translators are supported.
        """
        for trans_id, trans_data in self._session.translators.items():
            self._create_translator(trans_id, trans_data)

    def _create_translator(self, trans_id: str, trans_data: Dict):
        """Create a single translator block.

        Translators (ASM↔ADM) convert state variables between property packages.
        Each translator needs the correct inlet and outlet property packages.

        Args:
            trans_id: Translator identifier
            trans_data: Translator configuration from session
        """
        source_pkg_type = trans_data.get("source_pkg")
        dest_pkg_type = trans_data.get("dest_pkg")

        if source_pkg_type is None or dest_pkg_type is None:
            raise ModelBuildError(f"Translator {trans_id} missing source_pkg or dest_pkg")

        # Convert string to enum if needed
        if isinstance(source_pkg_type, str):
            source_pkg_type = PropertyPackageType(source_pkg_type)
        if isinstance(dest_pkg_type, str):
            dest_pkg_type = PropertyPackageType(dest_pkg_type)

        # Get translator spec from registry
        spec = get_translator(source_pkg_type, dest_pkg_type)
        if spec is None:
            raise ModelBuildError(
                f"No translator found for {source_pkg_type.value} → {dest_pkg_type.value}"
            )

        try:
            # Import translator class
            module = importlib.import_module(spec.module_path)
            TranslatorClass = getattr(module, spec.class_name)

            # Build translator config with CORRECT inlet/outlet packages
            # Note: Translators MUST have different packages for inlet/outlet
            config = {}

            # Get inlet property package (source package type)
            inlet_pkg = self._property_packages.get(source_pkg_type.value)
            if inlet_pkg is None:
                raise ModelBuildError(
                    f"Inlet property package {source_pkg_type.value} not created for translator {trans_id}"
                )

            # Get outlet property package (dest package type)
            outlet_pkg = self._property_packages.get(dest_pkg_type.value)
            if outlet_pkg is None:
                raise ModelBuildError(
                    f"Outlet property package {dest_pkg_type.value} not created for translator {trans_id}"
                )

            config["inlet_property_package"] = inlet_pkg
            config["outlet_property_package"] = outlet_pkg

            # Add any user-provided config
            if "config" in trans_data:
                config.update(trans_data["config"])

            # Create translator on flowsheet
            translator_block = TranslatorClass(**config)
            setattr(self._flowsheet, trans_id, translator_block)

            # Store for later reference
            self._translators[trans_id] = translator_block
            # Also add to units dict so connections can find it
            self._units[trans_id] = translator_block

        except ImportError as e:
            raise ModelBuildError(f"Cannot import translator {spec.module_path}: {e}")
        except Exception as e:
            raise ModelBuildError(f"Cannot create translator {trans_id}: {e}")

    def _create_connections(self):
        """Create Arc connections between units."""
        try:
            from pyomo.network import Arc
        except ImportError:
            # Older Pyomo versions
            from pyomo.environ import Arc

        for conn in self._session.connections:
            self._create_connection(conn, Arc)

    def _create_connection(self, conn: Connection, ArcClass):
        """Create a single Arc connection.

        Args:
            conn: Connection definition
            ArcClass: Arc class from pyomo.network
        """
        src_unit = self._units.get(conn.source_unit)
        dst_unit = self._units.get(conn.dest_unit)

        if src_unit is None:
            raise ModelBuildError(f"Source unit not found: {conn.source_unit}")
        if dst_unit is None:
            raise ModelBuildError(f"Destination unit not found: {conn.dest_unit}")

        # Get ports
        src_port = getattr(src_unit, conn.source_port, None)
        dst_port = getattr(dst_unit, conn.dest_port, None)

        if src_port is None:
            raise ModelBuildError(
                f"Source port {conn.source_port} not found on {conn.source_unit}"
            )
        if dst_port is None:
            raise ModelBuildError(
                f"Destination port {conn.dest_port} not found on {conn.dest_unit}"
            )

        # Create Arc
        arc_name = f"arc_{conn.source_unit}_{conn.dest_unit}"
        arc = ArcClass(source=src_port, destination=dst_port)
        setattr(self._flowsheet, arc_name, arc)

    def _apply_feed_state(self):
        """Apply feed state from session to Feed units.

        The feed_state from session contains state_args that are already
        formatted for the default property package.
        """
        if not self._session.feed_state:
            return

        state_args = self._session.feed_state.get("state_args")
        if not state_args:
            return

        # Find Feed units and apply state
        for unit_id, unit_inst in self._session.units.items():
            if unit_inst.unit_type in ("Feed", "FeedZO"):
                unit_block = self._units.get(unit_id)
                if unit_block is None:
                    continue

                # Apply state_args to Feed unit
                try:
                    # IDAES Feed units have properties block with state variables
                    if hasattr(unit_block, 'properties'):
                        for key, value in state_args.items():
                            if hasattr(unit_block.properties[0], key):
                                var = getattr(unit_block.properties[0], key)
                                if isinstance(value, dict):
                                    # Indexed variable like flow_mass_phase_comp
                                    for idx, val in value.items():
                                        if idx in var:
                                            var[idx].fix(val)
                                elif hasattr(var, 'fix'):
                                    var.fix(value)
                except Exception:
                    # Skip if can't apply - may need unit-specific handling
                    pass

    def _apply_fixed_variables(self):
        """Apply fixed variables from session to model."""
        from pyomo.environ import value

        for unit_id, unit_inst in self._session.units.items():
            unit_block = self._units.get(unit_id)
            if unit_block is None:
                continue

            for var_path, var_value in unit_inst.fixed_vars.items():
                self._fix_variable(unit_block, var_path, var_value)

    def _fix_variable(self, unit: Any, var_path: str, value: float):
        """Fix a variable on a unit block.

        Args:
            unit: Unit block
            var_path: Variable path (e.g., "A_comp[0, H2O]" or "area")
            value: Value to fix
        """
        try:
            # Handle indexed variables like "A_comp[0, H2O]"
            if "[" in var_path:
                var_name, index_str = var_path.split("[", 1)
                index_str = index_str.rstrip("]")
                # Parse index - could be tuple or single value
                indices = [s.strip() for s in index_str.split(",")]

                var = getattr(unit, var_name, None)
                if var is None:
                    return

                # Build index tuple
                parsed_indices = []
                for idx in indices:
                    # Try int first, then float, then keep as string
                    try:
                        parsed_indices.append(int(idx))
                    except ValueError:
                        try:
                            parsed_indices.append(float(idx))
                        except ValueError:
                            parsed_indices.append(idx)

                if len(parsed_indices) == 1:
                    var[parsed_indices[0]].fix(value)
                else:
                    var[tuple(parsed_indices)].fix(value)

            else:
                # Simple variable
                var = getattr(unit, var_path, None)
                if var is not None:
                    if hasattr(var, 'fix'):
                        var.fix(value)
                    elif hasattr(var, '__iter__'):
                        # Indexed var, fix all indices
                        for v in var.values():
                            v.fix(value)

        except Exception:
            # Skip variables that can't be fixed
            pass

    def _apply_scaling_factors(self):
        """Apply scaling factors from session to model."""
        try:
            import idaes.core.util.scaling as iscale
        except ImportError:
            # No scaling if IDAES not available
            return

        for unit_id, unit_inst in self._session.units.items():
            unit_block = self._units.get(unit_id)
            if unit_block is None:
                continue

            for var_path, factor in unit_inst.scaling_factors.items():
                self._set_scaling(unit_block, var_path, factor, iscale)

    def _set_scaling(self, unit: Any, var_path: str, factor: float, iscale):
        """Set scaling factor for a variable.

        Args:
            unit: Unit block
            var_path: Variable path
            factor: Scaling factor
            iscale: IDAES scaling module
        """
        try:
            if "[" in var_path:
                var_name, index_str = var_path.split("[", 1)
                index_str = index_str.rstrip("]")
                indices = [s.strip() for s in index_str.split(",")]

                var = getattr(unit, var_name, None)
                if var is None:
                    return

                parsed_indices = []
                for idx in indices:
                    try:
                        parsed_indices.append(int(idx))
                    except ValueError:
                        try:
                            parsed_indices.append(float(idx))
                        except ValueError:
                            parsed_indices.append(idx)

                if len(parsed_indices) == 1:
                    iscale.set_scaling_factor(var[parsed_indices[0]], factor)
                else:
                    iscale.set_scaling_factor(var[tuple(parsed_indices)], factor)

            else:
                var = getattr(unit, var_path, None)
                if var is not None:
                    iscale.set_scaling_factor(var, factor)

        except Exception:
            pass

    def get_units(self) -> Dict[str, Any]:
        """Get the created unit blocks.

        Returns:
            Dict of unit_id -> unit block
        """
        return self._units

    def get_property_packages(self) -> Dict[str, Any]:
        """Get the created property packages.

        Returns:
            Dict of pkg_name -> property block
        """
        return self._property_packages

    def get_translators(self) -> Dict[str, Any]:
        """Get the created translator blocks.

        Returns:
            Dict of translator_id -> translator block
        """
        return self._translators


def build_model_from_session(session: FlowsheetSession) -> Tuple[Any, Dict[str, Any]]:
    """Convenience function to build a model from session.

    Args:
        session: FlowsheetSession to build from

    Returns:
        Tuple of (model, units_dict)

    Raises:
        ModelBuildError: If build fails
    """
    builder = ModelBuilder(session)
    model = builder.build()
    return model, builder.get_units()
