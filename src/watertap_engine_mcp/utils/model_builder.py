"""Model Builder for WaterTAP Flowsheets.

Reconstructs a Pyomo ConcreteModel from session state, creating unit
instances and connections based on the session configuration.

NOTE: This module requires WaterTAP/IDAES to be installed. It is designed
to be used in worker.py which runs in a subprocess where heavy imports
are acceptable.
"""
import importlib
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple, Union
from watertap_engine_mcp.core.session import FlowsheetSession, UnitInstance, Connection
from watertap_engine_mcp.core.unit_registry import UNITS, AutoFixSpec, UnitProfile, UnitSpec, compile_engineering_inputs, unit_supports_costing_package, normalize_profile_package_config, resolve_unit_support
from watertap_engine_mcp.core.property_registry import PROPERTY_PACKAGES, REACTION_PACKAGE_VARIANTS, PropertyPackageType, build_property_package, is_asm2d_property_package_type
from watertap_engine_mcp.core.translator_registry import TRANSLATORS, TranslatorSpec, get_translator

class ModelBuildError(Exception):
    """Error during model building."""
    pass
ERR_TARGET_INCOMPATIBLE = 'TARGET_PACKAGE_INCOMPATIBLE'

class TargetCompatibilityError(ModelBuildError):
    """Pre-build gate failure: a process-control target's required components or
    units/actuators are absent from the property package / topology.

    Raised at the top of :meth:`ModelBuilder._apply_process_constraints` BEFORE
    the matching ``_add_*_target_constraint`` builder runs, so the engine returns
    a STRUCTURED remediation (which property package to use / which unit to add)
    instead of failing deep in constraint-build accessing a missing component
    (e.g. ``_add_chemical_p_target_constraint`` reaching for ``S_PO4``) or — worse
    — silently pinning a metric expression that evaluates to a wrong number.

    Subclasses :class:`ModelBuildError` so every existing caller that catches
    ``ModelBuildError`` (worker, dispatch, solve_for) still sees it, and carries
    the A7-style ``(code, message, details)`` triple plus a :meth:`to_error`
    bridge into the canonical ``api.errors`` envelope.
    """
    code = ERR_TARGET_INCOMPATIBLE

    def __init__(self, message: str, *, target: str, remediation: str, missing_components: Optional[List[str]]=None, missing_units: Optional[List[str]]=None, details: Optional[Dict[str, Any]]=None):
        super().__init__(message)
        self.message = message
        self.target = target
        self.remediation = remediation
        self.missing_components = list(missing_components or [])
        self.missing_units = list(missing_units or [])
        self.details: Dict[str, Any] = {'target': target, 'missing_components': self.missing_components, 'missing_units': self.missing_units, 'remediation': remediation, **(details or {})}

    def to_error(self, correlation_id: Optional[str]=None) -> Any:
        """Convert to the canonical redacted ``api.errors`` envelope."""
        from watertap_engine_mcp.api.errors import make_error
        return make_error(self.code, self.message, details=self.details, correlation_id=correlation_id)
_TARGET_REQUIREMENTS: Dict[str, Dict[str, Any]] = {'chemical_p_target': {'label': 'effluent total-P <- metal-salt dose', 'component_groups': [('S_PO4',), ('X_MeP',)], 'unit': 'chemical_p_dose', 'component_remediation': "ModifiedASM2D carries S_PO4 but has no X_MeP metal-phosphate solid state, so the ASM2D metal-salt P-precipitation surrogate cannot close phosphorus on it. Use property package 'ASM2D' for chemical_p_target, or drop the target and rely on ModifiedASM2D biological P removal (PAO/X_PP/X_PHA).", 'unit_remediation': "Add a ChemicalPhosphorusRemovalASM2D unit (the metal_dose actuator) to the topology, or name an existing one via the constraint 'dose_unit'."}, 'alkalinity_target': {'label': 'effluent alkalinity <- caustic dose', 'component_groups': [('S_ALK', 'S_IC')], 'unit': 'caustic_dose', 'component_remediation': 'The property package carries no alkalinity coordinate (S_ALK on ASM2D / S_IC on ModifiedASM2D). Use an ASM2D / ModifiedASM2D package.', 'unit_remediation': "Add an alkalinity dose unit (an AlkalinityDoseASM2D caustic_dose actuator) to the topology, or name one via the constraint 'dose_unit'."}, 'do_target': {'label': 'aerobic outlet DO <- aeration KLa', 'component_groups': [('S_O2',)], 'unit': 'kla', 'component_remediation': 'The property package carries no dissolved-oxygen component (S_O2); DO control needs an aerobic ASM property package.', 'unit_remediation': "Add or identify an aerobic unit exposing a KLa aeration actuator (name it via the constraint 'actuator_unit'/'kla_unit')."}, 'no3_target': {'label': 'effluent NO3 <- supplemental-carbon dose', 'component_groups': [('S_NO3',)], 'unit': 'carbon_dose', 'component_remediation': 'The property package carries no nitrate component (S_NO3); denitrification control needs an aerobic/anoxic ASM property package.', 'unit_remediation': "Add a SupplementalCarbonDoseASM2D unit (the carbon_dose actuator) to the topology, or name an existing one via the constraint 'dose_unit'."}, 'srt': {'label': 'sludge retention time <- WAS split', 'component_groups': [], 'unit': 'was_splitter', 'unit_remediation': 'SRT control requires a dedicated WAS sludge-splitter actuator (a split_fraction on the named waste port). Add a recuperation / bleed Separator so the SRT closure can free its waste split.'}, 'adm1_temperature_target': {'label': 'digester outlet temperature <- heat duty', 'component_groups': [], 'unit': 'adm1_digester_heat', 'unit_remediation': "temperature_target requires an ADM1 digester (a liquid_phase control volume exposing a heat duty). Name the digester unit via 'target_unit'."}, 'adm1_ph_target': {'label': 'digester pH / strong-ion alkalinity <- base dose', 'component_groups': [], 'unit': 'adm1_base_dose', 'unit_remediation': "adm1_ph_target requires an ADM1BaseDose unit (a caustic_dose driving the mADM1 strong-ion cations pool). Add one ahead of the digester, or name it via 'dose_unit'."}, 'adm1_metal_target': {'label': 'digester soluble-P (S_IP) <- struvite Mg dose', 'component_groups': [('S_IP',)], 'unit': 'adm1_mg_dose', 'component_remediation': "Soluble inorganic phosphorus S_IP exists in ModifiedADM1 only (classic ADM1 has no S_IP state). Use property package 'MODIFIED_ADM1'.", 'unit_remediation': "adm1_metal_target requires an ADM1StruvitePRemoval unit (the mg_dose actuator). Add one, or name an existing one via 'dose_unit'."}}
_TARGET_CANONICAL: Dict[str, str] = {'srt': 'srt', 'temperature_target': 'adm1_temperature_target', 'adm1_temperature_target': 'adm1_temperature_target', 'target_temperature': 'adm1_temperature_target', 'adm1_ph_target': 'adm1_ph_target', 'adm1_base_dose': 'adm1_ph_target', 'adm1_ph_alkalinity_target': 'adm1_ph_target', 'adm1_caustic_target': 'adm1_ph_target', 'adm1_metal_target': 'adm1_metal_target', 'adm1_struvite_target': 'adm1_metal_target', 'adm1_soluble_p_target': 'adm1_metal_target', 'do_target': 'do_target', 'target_do': 'do_target', 'dissolved_oxygen_target': 'do_target', 'chemical_p_target': 'chemical_p_target', 'target_effluent_p': 'chemical_p_target', 'chemical_p': 'chemical_p_target', 'no3_target': 'no3_target', 'target_effluent_no3': 'no3_target', 'denitrification_target': 'no3_target', 'supplemental_carbon': 'no3_target', 'alkalinity_target': 'alkalinity_target', 'target_alkalinity': 'alkalinity_target', 'ph_target': 'alkalinity_target', 'caustic_target': 'alkalinity_target'}

class ModelBuilder:
    """Builds a Pyomo model from session state."""

    def __init__(self, session: FlowsheetSession, *, include_costing: bool=True):
        """Initialize with session.

        Args:
            session: FlowsheetSession containing flowsheet definition
            include_costing: Whether to attach costing blocks during build
        """
        self._session = session
        self._include_costing = include_costing
        self._model = None
        self._flowsheet = None
        self._units = {}
        self._translators = {}
        self._property_packages = {}
        self._reaction_packages = {}
        self._reaction_config = None
        self._unit_profiles: Dict[str, Optional[UnitProfile]] = {}
        self._free_recycle_ratio_objective_terms = []
        self._costing_report = {'enabled': False, 'package': None, 'requested_units': [], 'supported_units': [], 'skipped_units': [], 'warnings': [], 'product_stream': None}

    def build(self) -> Any:
        """Build the complete model from session.

        Returns:
            ConcreteModel with all units and connections

        Raises:
            ModelBuildError: If build fails
        """
        try:
            self._apply_supported_contracts()
            self._create_base_model()
            self._create_property_packages()
            self._apply_global_parameter_fixes()
            self._create_units()
            self._apply_unit_post_build_controls()
            self._create_translators()
            self._create_connections()
            self._expand_arcs()
            self._apply_special_unit_couplings()
            self._apply_process_constraints()
            self._apply_feed_state()
            self._apply_zero_order_parameters()
            self._apply_profile_auto_fixes()
            self._apply_engineering_inputs()
            self._apply_fixed_variables()
            self._apply_reaction_parameter_fixes()
            self._apply_reaction_config()
            self._apply_asm2d_recycle_pressure_reference()
            self._seed_adm1_vapor_pressure_branch_guard()
            self._free_target_control_actuators()
            self._apply_split_fraction_bounds()
            self._apply_initial_guesses()
            self._apply_profile_property_scaling_defaults()
            self._apply_profile_unit_scaling_defaults()
            self._apply_scaling_factors()
            self._apply_activated_sludge_scaling_defaults()
            if self._include_costing:
                self._create_costing()
            self._record_modified_asm2d_kinetics_read_back()
            return self._model
        except TargetCompatibilityError:
            raise
        except ImportError as e:
            raise ModelBuildError(f'Import error: {e}. Ensure WaterTAP/IDAES is installed.')
        except Exception as e:
            raise ModelBuildError(f'Build failed: {type(e).__name__}: {e}')

    def _apply_supported_contracts(self) -> None:
        """Normalize unit configs and validate any enforced support profiles."""
        normalized_package_config = dict(self._session.config.property_package_config or {})
        self._unit_profiles = {}
        for unit_id, unit_inst in self._session.units.items():
            spec = UNITS.get(unit_inst.unit_type)
            if spec is None:
                raise ModelBuildError(f'Unknown unit type: {unit_inst.unit_type}')
            try:
                normalized_config, _, profile = resolve_unit_support(spec, self._session.config.default_property_package, unit_inst.config, self._session.config.property_package_config)
                if profile is not None:
                    normalized_package_config = normalize_profile_package_config(profile, normalized_package_config)
            except ValueError as exc:
                raise ModelBuildError(f'Unsupported public contract for {unit_id} ({unit_inst.unit_type}): {exc}') from exc
            unit_inst.config = normalized_config
            self._unit_profiles[unit_id] = profile
        self._session.config.property_package_config = normalized_package_config

    def _create_base_model(self):
        """Create ConcreteModel with FlowsheetBlock."""
        from pyomo.environ import ConcreteModel
        from idaes.core import FlowsheetBlock
        self._model = ConcreteModel()
        self._model.fs = FlowsheetBlock(dynamic=False)
        self._flowsheet = self._model.fs

    def _apply_profile_auto_fixes(self) -> None:
        """Apply profile-declared automatic fixes after unit creation."""
        for unit_id, profile in self._unit_profiles.items():
            if profile is None or not profile.auto_fixed_variables:
                continue
            unit_block = self._units.get(unit_id)
            if unit_block is None:
                continue
            for auto_fix in profile.auto_fixed_variables:
                from watertap_engine_mcp.core.rating_geometry import GEOMETRY_VARIABLES, GEOMETRY_UNITS, RatingGeometryError
                if auto_fix.name.split('[')[0] in GEOMETRY_VARIABLES:
                    supplied = self._session.units[unit_id].fixed_vars
                    if auto_fix.name not in supplied:
                        geometry_unit = GEOMETRY_UNITS[auto_fix.name.split('[')[0]]
                        raise RatingGeometryError(f'{unit_id} ({self._session.units[unit_id].unit_type}): stage (b) must supply {auto_fix.name} [{geometry_unit}]; profile defaults cannot specify rating geometry.')
                    continue
                self._apply_auto_fix(unit_block, auto_fix)

    def _apply_auto_fix(self, unit_block: Any, auto_fix: AutoFixSpec) -> None:
        """Apply one automatic fix against a live unit block."""
        import sys
        try:
            var, index = self._resolve_variable_path(unit_block, auto_fix.name)
            if var is None:
                raise AttributeError(f'Variable path not found: {auto_fix.name}')
            target = var[index] if index is not None else var
            if auto_fix.use_existing_value:
                target.fix()
            elif auto_fix.value is not None:
                target.fix(auto_fix.value)
            else:
                target.fix()
        except Exception as exc:
            print(f'Warning: Cannot auto-fix {auto_fix.name}: {exc}', file=sys.stderr)

    def _apply_engineering_inputs(self) -> None:
        """Apply persisted engineering inputs as internal variable fixes."""
        for unit_id, unit_inst in self._session.units.items():
            if not unit_inst.engineering_inputs:
                continue
            unit_block = self._units.get(unit_id)
            spec = UNITS.get(unit_inst.unit_type)
            if unit_block is None or spec is None:
                continue
            try:
                compiled = compile_engineering_inputs(spec, self._session.config.default_property_package, unit_inst.config, unit_inst.engineering_inputs)
            except ValueError as exc:
                raise ModelBuildError(f'Invalid engineering inputs for {unit_id} ({unit_inst.unit_type}): {exc}') from exc
            for variable_name, value in compiled.items():
                self._fix_variable(unit_block, variable_name, value)

    def _apply_profile_property_scaling_defaults(self) -> None:
        """Apply profile-declared property-package scaling defaults."""
        for profile in self._unit_profiles.values():
            if profile is None or not profile.property_default_scaling:
                continue
            pkg_type = profile.property_package or self._session.config.default_property_package
            pkg_block = self._property_packages.get(pkg_type.value)
            if pkg_block is None:
                continue
            for scaling in profile.property_default_scaling:
                try:
                    pkg_block.set_default_scaling(scaling.name, scaling.factor, index=scaling.index)
                except Exception:
                    continue

    def _apply_profile_unit_scaling_defaults(self) -> None:
        """Apply registry-declared unit scaling defaults for supported profiles."""
        try:
            import idaes.core.util.scaling as iscale
        except ImportError:
            return
        for unit_id, unit_inst in self._session.units.items():
            unit_block = self._units.get(unit_id)
            spec = UNITS.get(unit_inst.unit_type)
            if unit_block is None or spec is None:
                continue
            profile = self._unit_profiles.get(unit_id)
            scaling_items = {}
            if spec.default_scaling:
                scaling_items.update(spec.default_scaling)
            if profile is not None and profile.default_scaling:
                scaling_items.update(profile.default_scaling)
            for var_path, factor in scaling_items.items():
                self._set_scaling(unit_block, var_path, factor, iscale)

    def _apply_initial_guesses(self) -> None:
        """Set post-fix initial guesses for units that need stable nonlinear seeds."""
        from pyomo.environ import value, units as pyunits
        for unit_id, unit_inst in self._session.units.items():
            unit_block = self._units.get(unit_id)
            if unit_block is None:
                continue
            if unit_inst.unit_type == 'Evaporator':
                try:
                    vapor_temperature = unit_block.properties_vapor[0].temperature
                    if vapor_temperature.ub is None or vapor_temperature.ub > 348.15:
                        vapor_temperature.setub(348.15)
                except Exception:
                    pass
            if unit_inst.unit_type == 'Compressor':
                try:
                    outlet_temperature = unit_block.control_volume.properties_out[0].temperature
                    if outlet_temperature.ub is None or outlet_temperature.ub > 450:
                        outlet_temperature.setub(450)
                except Exception:
                    pass
            if unit_inst.unit_type == 'SteamHeater0D':
                self._seed_steam_heater_hot_side_guess(unit_id, unit_block)
            if unit_inst.unit_type != 'Ultraviolet0D':
                continue
            try:
                uv_dose = pyunits.convert(unit_block.uv_intensity * unit_block.exposure_time, to_units=unit_block.uv_dose.get_units())
                unit_block.uv_dose.set_value(value(uv_dose))
            except Exception:
                continue

    def _seed_steam_heater_hot_side_guess(self, unit_id: str, unit_block: Any) -> None:
        """Seed the free steam-flow guess used by the upstream SteamHeater init path.

        Upstream SteamHeater0D examples leave the hot-side vapor flow unfixed and
        only provide a numeric seed. Our process surface represents that steam
        side through an upstream WATER Feed block, so we mirror the upstream
        guess on both the source port and the unit inlet before initialization.
        """
        hot_port = getattr(unit_block, 'hot_side_inlet', None)
        if hot_port is None or not hasattr(hot_port, 'flow_mass_phase_comp'):
            return
        try:
            hot_vapor = hot_port.flow_mass_phase_comp[0, 'Vap', 'H2O']
            if not hot_vapor.fixed:
                hot_vapor.set_value(1.0)
        except Exception:
            pass
        for conn in self._session.connections:
            if conn.dest_unit != unit_id or conn.dest_port != 'hot_side_inlet':
                continue
            src_unit = self._units.get(conn.source_unit)
            if src_unit is None:
                continue
            src_port = getattr(src_unit, conn.source_port, None)
            if src_port is None or not hasattr(src_port, 'flow_mass_phase_comp'):
                continue
            try:
                src_vapor = src_port.flow_mass_phase_comp[0, 'Vap', 'H2O']
                if not src_vapor.fixed:
                    src_vapor.set_value(1.0)
            except Exception:
                continue

    def _create_property_packages(self):
        """Create property packages needed by the flowsheet.

        Handles:
        - Default property package with optional config (MCAS, ZO need config)
        - Additional packages required by translators (source/dest may differ)
        """
        default_pkg_type = self._session.config.default_property_package
        pkg_spec = PROPERTY_PACKAGES.get(default_pkg_type)
        if pkg_spec is None:
            raise ModelBuildError(f'Unknown property package: {default_pkg_type}')
        pkg_config = self._session.config.property_package_config or {}
        try:
            config_kwargs = self._build_package_config(pkg_spec, pkg_config)
            if pkg_spec.requires_config and pkg_spec.pkg_type == PropertyPackageType.MCAS:
                if 'solute_list' not in config_kwargs:
                    raise ModelBuildError(f"MCAS property package requires 'solute_list' in config. Set property_package_config with solute definitions.")
            build_property_package(self._flowsheet, 'prop_params', default_pkg_type, config_kwargs)
            self._property_packages['default'] = getattr(self._flowsheet, 'prop_params')
            self._property_packages[default_pkg_type.value] = self._property_packages['default']
            self._apply_common_package_scaling_defaults(self._property_packages['default'], default_pkg_type)
            self._create_reaction_package_if_required(default_pkg_type, self._property_packages['default'], 'rxn_params')
        except ImportError as e:
            raise ModelBuildError(f'Cannot import property package {pkg_spec.module_path}: {e}')
        except Exception as e:
            raise ModelBuildError(f'Cannot create property package {default_pkg_type}: {e}')
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
            for field_name in ('diffusivity_data', 'elec_mobility_data', 'molar_volume_data'):
                field_data = user_config.get(field_name)
                if isinstance(field_data, dict):
                    user_config[field_name] = {key if isinstance(key, tuple) else ('Liq', key): value for key, value in field_data.items()}
            activity_model = user_config.get('activity_coefficient_model')
            density_calculation = user_config.get('density_calculation')
            material_flow_basis = user_config.get('material_flow_basis')
            if isinstance(activity_model, str) or isinstance(density_calculation, str) or isinstance(material_flow_basis, str):
                from watertap.property_models.multicomp_aq_sol_prop_pack import ActivityCoefficientModel, DensityCalculation, MaterialFlowBasis
                if isinstance(activity_model, str):
                    try:
                        user_config['activity_coefficient_model'] = ActivityCoefficientModel[activity_model.lower()]
                    except KeyError as exc:
                        raise ModelBuildError(f'Unknown MCAS activity_coefficient_model: {activity_model}') from exc
                if isinstance(density_calculation, str):
                    try:
                        user_config['density_calculation'] = DensityCalculation[density_calculation.lower()]
                    except KeyError as exc:
                        raise ModelBuildError(f'Unknown MCAS density_calculation: {density_calculation}') from exc
                if isinstance(material_flow_basis, str):
                    try:
                        user_config['material_flow_basis'] = MaterialFlowBasis[material_flow_basis.lower()]
                    except KeyError as exc:
                        raise ModelBuildError(f'Unknown MCAS material_flow_basis: {material_flow_basis}') from exc
            config.update({key: value for key, value in user_config.items() if value is not None})
        elif pkg_spec.pkg_type == PropertyPackageType.ZERO_ORDER:
            if 'database' in user_config:
                config['database'] = user_config['database']
            elif pkg_spec.database_required:
                try:
                    from watertap.core.wt_database import Database
                    config['database'] = Database()
                except ImportError:
                    pass
            if 'water_source' in user_config:
                config['water_source'] = user_config['water_source']
            if 'solute_list' in user_config:
                config['solute_list'] = user_config['solute_list']
        elif pkg_spec.pkg_type == PropertyPackageType.NATURAL_GAS_PR:
            from idaes.models_extra.power_generation.properties.natural_gas_PR import EosType, get_prop
            components = user_config.get('components')
            if isinstance(components, str):
                components = [c.strip() for c in components.split(',') if c.strip()]
            if not components:
                components = ['CH4', 'CO2', 'N2']
            eos_name = str(user_config.get('eos', 'PR')).upper()
            eos = EosType.PR if eos_name == 'PR' else EosType.IDEAL
            config.update(get_prop(components, ['Vap'], eos=eos))
        return config

    def _create_translator_packages(self):
        """Create property packages required by translators.

        Translators may require inlet/outlet packages that differ from the
        session default. This creates those packages if they are not already
        present on the flowsheet.
        """
        for trans_id, trans_data in self._session.translators.items():
            source_pkg_type = trans_data.get('source_pkg')
            dest_pkg_type = trans_data.get('dest_pkg')
            if source_pkg_type is None or dest_pkg_type is None:
                continue
            if isinstance(source_pkg_type, str):
                source_pkg_type = PropertyPackageType(source_pkg_type)
            if isinstance(dest_pkg_type, str):
                dest_pkg_type = PropertyPackageType(dest_pkg_type)
            if source_pkg_type.value not in self._property_packages:
                self._create_additional_package(source_pkg_type, f'prop_{source_pkg_type.name.lower()}')
            if dest_pkg_type.value not in self._property_packages:
                self._create_additional_package(dest_pkg_type, f'prop_{dest_pkg_type.name.lower()}')

    def _create_additional_package(self, pkg_type: PropertyPackageType, attr_name: str):
        """Create an additional property package on the flowsheet.

        Args:
            pkg_type: Property package type to create
            attr_name: Attribute name to use on flowsheet
        """
        pkg_spec = PROPERTY_PACKAGES.get(pkg_type)
        if pkg_spec is None:
            raise ModelBuildError(f'Unknown property package: {pkg_type}')
        try:
            config_kwargs = self._build_package_config(pkg_spec, self._additional_package_config(pkg_type))
            build_property_package(self._flowsheet, attr_name, pkg_type, config_kwargs)
            self._property_packages[pkg_type.value] = getattr(self._flowsheet, attr_name)
            self._apply_common_package_scaling_defaults(self._property_packages[pkg_type.value], pkg_type)
            self._create_reaction_package_if_required(pkg_type, self._property_packages[pkg_type.value], f'rxn_{pkg_type.name.lower()}')
        except ImportError as e:
            raise ModelBuildError(f'Cannot import property package {pkg_spec.module_path}: {e}')
        except Exception as e:
            raise ModelBuildError(f'Cannot create property package {pkg_type}: {e}')

    def _create_reaction_package_if_required(self, pkg_type: PropertyPackageType, property_package: Any, attr_name: str) -> None:
        """Create the reaction package paired with an ASM/ADM property package."""
        pkg_spec = PROPERTY_PACKAGES.get(pkg_type)
        if pkg_spec is None or not pkg_spec.requires_reaction_package or (not pkg_spec.reaction_module_path) or (not pkg_spec.reaction_class_name):
            return
        if pkg_type.value in self._reaction_packages:
            return
        options: Dict[str, Any] = {}
        if pkg_type == PropertyPackageType.MODIFIED_ASM2D:
            from watertap_engine_mcp.core.modified_asm2d_reaction_config import reaction_package_options
            options = reaction_package_options(self._session_reaction_config())
        try:
            module = importlib.import_module(pkg_spec.reaction_module_path)
            ReactionClass = getattr(module, pkg_spec.reaction_class_name)
            setattr(self._flowsheet, attr_name, ReactionClass(property_package=property_package, **options))
            self._reaction_packages[pkg_type.value] = getattr(self._flowsheet, attr_name)
        except ImportError as e:
            raise ModelBuildError(f'Cannot import reaction package {pkg_spec.reaction_module_path}: {e}')
        except Exception as e:
            raise ModelBuildError(f'Cannot create reaction package {pkg_type}: {e}')

    def _apply_global_parameter_fixes(self) -> None:
        'Public engineering calculation interface.'
        from watertap_engine_mcp.core.modified_asm2d_kinetics import fix_parameters
        metadata = self._session.config.runtime_metadata or {}
        property_fixes = metadata.get('property_parameter_fixes')
        if isinstance(property_fixes, dict) and property_fixes:
            fix_parameters(self._unique_parameter_blocks(self._property_packages), property_fixes, where="the session's property packages", label='property')

    def _apply_reaction_parameter_fixes(self) -> None:
        'Fix the declared reaction parameters, refusing a name no package can take; then the house ModifiedASM2d set.'
        from watertap_engine_mcp.core.modified_asm2d_kinetics import MODIFIED_ASM2D_KINETICS_KEY, ROUTES_RULING_ID, fix_reaction_parameters, house_kinetics, house_parameter_names
        metadata = self._session.config.runtime_metadata
        metadata.pop(MODIFIED_ASM2D_KINETICS_KEY, None)
        reaction_fixes = metadata.get('reaction_parameter_fixes')
        reaction_fixes = dict(reaction_fixes) if isinstance(reaction_fixes, dict) else {}
        if reaction_fixes:
            fix_reaction_parameters(self._unique_parameter_blocks(self._reaction_packages), reaction_fixes, where="the session's reaction packages")
        block = self._reaction_packages.get(PropertyPackageType.MODIFIED_ASM2D.value)
        if block is None:
            return
        route = f'ModelBuilder session ({metadata.get('origin_tool') or 'create_unit'})'
        reactors = sorted((unit_id for unit_id, unit in self._units.items() if getattr(getattr(unit, 'config', None), 'reaction_package', None) is block))
        names = house_parameter_names()
        supplied = {name: value for name, value in reaction_fixes.items() if name in names}
        if not reactors:
            metadata[MODIFIED_ASM2D_KINETICS_KEY] = {'ruling': ROUTES_RULING_ID, 'route': route, 'reactors': [], 'house_defaults': {}, 'caller_supplied': supplied, 'note': 'no unit rates this ModifiedASM2d reaction package: no kinetic set is applied'}
            return
        temperature_k, source = self._modified_asm2d_feed_temperature()
        fixes, record = house_kinetics(reaction_fixes=supplied, temperature_k=temperature_k, temperature_source_clause_ref=source, route=route, ruling=ROUTES_RULING_ID)
        fix_reaction_parameters([block], fixes, where='the ModifiedASM2d reaction package')
        metadata[MODIFIED_ASM2D_KINETICS_KEY] = {**record, 'reactors': reactors}

    def _modified_asm2d_feed_temperature(self) -> Tuple[Optional[float], str]:
        """The one temperature this session's ModifiedASM2d feeds fix, or None and why."""
        import pyomo.environ as pyo
        package = self._property_packages.get(PropertyPackageType.MODIFIED_ASM2D.value)
        stated: Dict[str, float] = {}
        for unit_id, unit in self._units.items():
            unit_inst = self._session.units.get(unit_id)
            if unit_inst is None or unit_inst.unit_type != 'Feed':
                continue
            if getattr(getattr(unit, 'config', None), 'property_package', None) is not package:
                continue
            try:
                temperature = unit.properties[0].temperature
            except Exception:
                continue
            if temperature.fixed and temperature.value is not None:
                stated[unit_id] = float(pyo.value(temperature))
        if not stated:
            return (None, 'no ModifiedASM2d feed of this session fixes a temperature')
        if len({round(value, 6) for value in stated.values()}) > 1:
            return (None, f'its ModifiedASM2d feeds fix different temperatures {stated} K')
        value = next(iter(stated.values()))
        return (value, f'session feed state temperature {value - 273.15:g} C ({', '.join(sorted(stated))})')

    def _session_reaction_config(self) -> Any:
        """The session's stated ModifiedASM2d model form (`REACTION_CONFIG_TARGET` of its runtime metadata), typed."""
        from watertap_engine_mcp.core.modified_asm2d_reaction_config import REACTION_CONFIG_TARGET, typed_reaction_config
        if self._reaction_config is None:
            self._reaction_config = typed_reaction_config((self._session.config.runtime_metadata or {}).get(REACTION_CONFIG_TARGET))
        return self._reaction_config

    def _apply_reaction_config(self) -> None:
        'Public engineering calculation interface.'
        from watertap_engine_mcp.core.modified_asm2d_reaction_config import BUILT_REACTION_CONFIG_KEY, REACTION_CONFIG_TARGET, SESSION_ROUTE_RULING_ID, ReactionConfigRefused, apply_reaction_config
        metadata = self._session.config.runtime_metadata
        metadata.pop(BUILT_REACTION_CONFIG_KEY, None)
        config = self._session_reaction_config()
        block = self._reaction_packages.get(PropertyPackageType.MODIFIED_ASM2D.value)
        reactors = sorted(((unit_id, unit) for unit_id, unit in self._units.items() if block is not None and getattr(getattr(unit, 'config', None), 'reaction_package', None) is block))
        if not reactors:
            if config.is_variant:
                raise ReactionConfigRefused(f'reaction_config_refused: {REACTION_CONFIG_TARGET} states a ModifiedASM2d model-form variant, and no unit of this session rates a ModifiedASM2d reaction package to carry it ({SESSION_ROUTE_RULING_ID} G3)')
            return
        record = apply_reaction_config(block, [unit for _, unit in reactors], config)
        metadata[BUILT_REACTION_CONFIG_KEY] = {**record, 'reactors': [unit_id for unit_id, _ in reactors]}

    def _record_modified_asm2d_kinetics_read_back(self) -> None:
        """Record the house names read back off the built ModifiedASM2d reaction package (G7)."""
        from watertap_engine_mcp.core.modified_asm2d_kinetics import MODIFIED_ASM2D_KINETICS_KEY, built_reaction_parameters
        block = self._reaction_packages.get(PropertyPackageType.MODIFIED_ASM2D.value)
        record = self._session.config.runtime_metadata.get(MODIFIED_ASM2D_KINETICS_KEY)
        if block is not None and isinstance(record, dict):
            record['built_reaction_parameters'] = built_reaction_parameters(block)

    @staticmethod
    def _unique_parameter_blocks(blocks: Dict[str, Any]) -> List[Any]:
        seen: Set[int] = set()
        unique: List[Any] = []
        for block in blocks.values():
            marker = id(block)
            if marker in seen:
                continue
            seen.add(marker)
            unique.append(block)
        return unique

    def _apply_common_package_scaling_defaults(self, pkg_block: Any, pkg_type: PropertyPackageType) -> None:
        """Apply common property-package scaling defaults."""
        scaling_entries: Dict[PropertyPackageType, List[Tuple[Any, float]]] = {PropertyPackageType.SEAWATER: [(('Liq', 'H2O'), 1), (('Liq', 'TDS'), 100.0)], PropertyPackageType.NACL: [(('Liq', 'H2O'), 1), (('Liq', 'NaCl'), 100.0)], PropertyPackageType.NACL_T_DEP: [(('Liq', 'H2O'), 1), (('Liq', 'NaCl'), 100.0)], PropertyPackageType.WATER: [(('Liq', 'H2O'), 1), (('Vap', 'H2O'), 1)]}
        for index, factor in scaling_entries.get(pkg_type, []):
            try:
                pkg_block.set_default_scaling('flow_mass_phase_comp', factor, index=index)
            except Exception:
                continue
        if is_asm2d_property_package_type(pkg_type):
            try:
                from watertap_engine_mcp.solver.scaler import DEFAULT_ASM2D_SCALING_FACTORS
                asm2d_entries = DEFAULT_ASM2D_SCALING_FACTORS
            except Exception:
                asm2d_entries = {'S_O2': 1000.0, 'S_F': 1000.0, 'S_A': 1000.0, 'S_NH4': 100.0, 'S_NO3': 100.0, 'S_PO4': 1000.0, 'S_IC': 1000.0, 'S_K': 100.0, 'S_Mg': 100.0, 'S_I': 100.0, 'S_N2': 100.0, 'X_I': 10.0, 'X_S': 100.0, 'X_H': 10.0, 'X_PAO': 10.0, 'X_PP': 100.0, 'X_PHA': 100.0, 'X_AUT': 10.0, 'X_MeOH': 10000000.0, 'X_MeP': 10000000.0, 'X_TSS': 1.0}
            package_entries = [('flow_vol', 100.0, None), ('temperature', 0.1, None), ('pressure', 0.0001, None), ('alkalinity', 1000.0, None)]
            package_entries.extend((('conc_mass_comp', factor, component) for component, factor in asm2d_entries.items()))
            for name, factor, index in package_entries:
                try:
                    pkg_block.set_default_scaling(name, factor, index=index)
                except Exception:
                    continue

    def _additional_package_config(self, pkg_type: PropertyPackageType) -> Dict[str, Any]:
        """Return config for an additional translator package.

        Most additional packages can reuse the session package config. A small
        number of curated generic translators need a minimal package config even
        when the session default package is different.
        """
        config = dict(self._session.config.property_package_config or {})
        for trans_data in self._session.translators.values():
            source_pkg_type = trans_data.get('source_pkg')
            dest_pkg_type = trans_data.get('dest_pkg')
            if isinstance(source_pkg_type, str):
                source_pkg_type = PropertyPackageType(source_pkg_type)
            if isinstance(dest_pkg_type, str):
                dest_pkg_type = PropertyPackageType(dest_pkg_type)
            spec = get_translator(source_pkg_type, dest_pkg_type)
            if spec is None or not spec.generic_rule:
                continue
            if pkg_type == PropertyPackageType.ZERO_ORDER and spec.generic_rule in {'zero_order_to_seawater_tds', 'seawater_to_zero_order_tds'}:
                config.setdefault('water_source', 'municipal')
                config.setdefault('solute_list', ['tds'])
        return config

    def _create_units(self):
        """Create all units from session."""
        for unit_id, unit_inst in self._session.units.items():
            self._create_unit(unit_id, unit_inst)

    def _apply_unit_post_build_controls(self) -> None:
        """Apply opt-in controls that need a constructed Pyomo unit block."""
        for unit_id, unit_inst in self._session.units.items():
            inlet_indexes = unit_inst.config.get('deactivate_pressure_equality_inlets')
            if not inlet_indexes:
                continue
            unit_block = self._units.get(unit_id)
            constraints = getattr(unit_block, 'pressure_equality_constraints', None)
            if constraints is None:
                continue
            for inlet_index in inlet_indexes:
                for time_index in (0, 0.0):
                    try:
                        constraints[time_index, int(inlet_index)].deactivate()
                        break
                    except Exception:
                        continue

    def _create_unit(self, unit_id: str, unit_inst: UnitInstance):
        """Create a single unit.

        Args:
            unit_id: Unit identifier
            unit_inst: UnitInstance from session
        """
        spec = UNITS.get(unit_inst.unit_type)
        if spec is None:
            raise ModelBuildError(f'Unknown unit type: {unit_inst.unit_type}')
        try:
            module = importlib.import_module(spec.module_path)
            UnitClass = getattr(module, spec.class_name)
            config = self._build_unit_config(spec, unit_inst)
            if spec.unit_type == 'AD':
                from watertap_engine_mcp.chemistry.modified_adm1_sfe import ad_unit_class
                UnitClass = ad_unit_class(config, UnitClass)
            unit_block = UnitClass(**config)
            setattr(self._flowsheet, unit_id, unit_block)
            if spec.unit_type == 'SurrogateCrystallizer':
                self._attach_surrogate_crystallizer_defaults(unit_block)
                try:
                    unit_block.properties_out_liq[0].pressure.setlb(1000)
                except Exception:
                    pass
            if spec.unit_type == 'CSTR_Injection':
                self._fix_non_oxygen_injection_terms(unit_block)
            if spec.unit_type == 'GasMembraneStage1D':
                self._apply_gas_membrane_inputs(unit_block, unit_inst)
            if spec.unit_type == 'GasScrewCompressor':
                self._apply_gas_compressor_inputs(unit_block, unit_inst)
            if spec.unit_type == 'GasHeater':
                self._apply_gas_heater_inputs(unit_block, unit_inst)
            self._units[unit_id] = unit_block
        except ImportError as e:
            raise ModelBuildError(f'Cannot import unit {spec.module_path}: {e}')
        except Exception as e:
            raise ModelBuildError(f'Cannot create unit {unit_id} ({unit_inst.unit_type}): {e}')

    def _attach_surrogate_crystallizer_defaults(self, unit_block: Any) -> None:
        """Attach the curated upstream surrogate defaults to a surrogate crystallizer."""
        if hasattr(unit_block, 'surrogate_block_vapor_pressure'):
            return
        from idaes.core.surrogate.pysmo_surrogate import PysmoSurrogate
        from idaes.core.surrogate.surrogate_block import SurrogateBlock
        from pyomo.environ import Constraint, Var, units as pyunits
        from watertap.unit_models import surrogate_crystallizer as surrogate_module
        surrogate_dir = Path(surrogate_module.__file__).resolve().parent.parent / 'data' / 'surrogate_defaults' / 'surrogate_crystallizer_defaults'
        if not surrogate_dir.exists():
            raise ModelBuildError(f'SurrogateCrystallizer defaults not found: {surrogate_dir}')
        surrogate_outputs = [('Vapor Pressure (atm)', 'Vapor_Pressure', 'surrogate_vapor_pressure', 'surrogate_block_vapor_pressure', pyunits.atm, None), ('Calcite_g', 'Calcite_g', 'surrogate_calcite_g', 'surrogate_block_calcite', pyunits.g / pyunits.s, 'Calcite_g'), ('Anhydrite_g', 'Anhydrite_g', 'surrogate_anhydrite_g', 'surrogate_block_anhydrite', pyunits.g / pyunits.s, 'Anhydrite_g'), ('Glauberite_g', 'Glauberite_g', 'surrogate_glauberite_g', 'surrogate_block_glauberite', pyunits.g / pyunits.s, 'Glauberite_g'), ('Halite_g', 'Halite_g', 'surrogate_halite_g', 'surrogate_block_halite', pyunits.g / pyunits.s, 'Halite_g')]
        if not set(unit_block.solids_list).issubset({solid_name for *_, solid_name in surrogate_outputs if solid_name is not None}):
            raise ModelBuildError('Curated SurrogateCrystallizer profile requires the default solids_ions_dict')
        surrogate_inputs = [unit_block.temperature_operating, unit_block.evaporation_percent]
        for _, file_stem, var_name, block_name, units, _ in surrogate_outputs:
            unit_block.add_component(block_name, SurrogateBlock(concrete=True))
            unit_block.add_component(var_name, Var(initialize=1.0, units=units))
            surrogate = PysmoSurrogate.load_from_file(str(surrogate_dir / f'{file_stem}.json'))
            getattr(unit_block, block_name).build_model(surrogate, input_vars=surrogate_inputs, output_vars=getattr(unit_block, var_name))

        def _solid_mass_rule(b, solid_name):
            var_name = f'surrogate_{solid_name[:-2].lower()}_g'
            return b.flow_mass_sol_comp_apparent[solid_name] == pyunits.convert(getattr(b, var_name), to_units=pyunits.kg / pyunits.s)
        unit_block.surrogate_mass_conversion = Constraint(unit_block.solids_list, rule=_solid_mass_rule)
        unit_block.surrogate_pressure_conversion = Constraint(expr=unit_block.pressure_operating == pyunits.convert(unit_block.surrogate_vapor_pressure, to_units=pyunits.Pa))

    def _build_unit_config(self, spec: UnitSpec, unit_inst: UnitInstance) -> Dict:
        """Build configuration dict for unit creation.

        Args:
            spec: UnitSpec for the unit type
            unit_inst: UnitInstance with user config

        Returns:
            Configuration dict for unit constructor
        """
        config = {}
        user_config = dict(unit_inst.config)
        is_database_zero_order = unit_inst.unit_type.endswith('ZO') and spec.module_path.startswith('watertap.unit_models.zero_order.') and (unit_inst.unit_type != 'FeedZO')
        if is_database_zero_order:
            for key in ('database', 'process_subtype'):
                if key in unit_inst.zo_config and key not in user_config:
                    user_config[key] = unit_inst.zo_config[key]
            if 'database' not in user_config or user_config.get('database') == 'default':
                from watertap.core.wt_database import Database
                user_config['database'] = Database()
            elif isinstance(user_config.get('database'), str):
                from watertap.core.wt_database import Database
                user_config['database'] = Database(user_config['database'])
        if unit_inst.unit_type == 'Clarifier':
            user_config.setdefault('outlet_list', ['underflow', 'effluent'])
        if spec.unit_type == 'Mixer':
            user_config.pop('deactivate_pressure_equality_inlets', None)
            momentum = user_config.get('momentum_mixing_type')
            if isinstance(momentum, str):
                from idaes.models.unit_models.mixer import MomentumMixingType as _MMT
                stripped = momentum.split('.')[-1]
                try:
                    user_config['momentum_mixing_type'] = _MMT[stripped]
                except KeyError as exc:
                    raise ValueError(f"Unknown MomentumMixingType '{momentum}' for unit {unit_inst.unit_id}; valid values: {[m.name for m in _MMT]}") from exc
        if spec.unit_type == 'ElectrodialysisBipolar1D':
            user_config = self._coerce_bped_config(user_config)
        if spec.unit_type == 'Evaporator':
            config['property_package_feed'] = self._resolve_property_package(user_config.pop('property_package_feed', None) or user_config.get('property_package') or self._session.config.default_property_package)
            config['property_package_vapor'] = self._resolve_property_package(user_config.pop('property_package_vapor', None) or PropertyPackageType.WATER)
            user_config.pop('property_package', None)
        elif spec.unit_type in {'SteamHeater0D', 'MembraneDistillation0D', 'MembraneDistillation1D'}:
            user_config.pop('property_package', None)
        elif spec.unit_type == 'SurrogateCrystallizer':
            config['property_package'] = self._resolve_property_package(user_config.pop('property_package', None) or self._session.config.default_property_package)
            config['vapor_property_package'] = self._resolve_property_package(user_config.pop('vapor_property_package', None) or PropertyPackageType.WATER)
            solids = user_config.pop('solids_ions_dict', None)
            if solids is not None:
                config['solids_ions_dict'] = self._coerce_solids_ions_dict(solids)
        elif spec.unit_type == 'AD':
            liquid_ref = user_config.pop('liquid_property_package', None) or user_config.pop('property_package', None) or self._session.config.default_property_package
            config['liquid_property_package'] = self._resolve_property_package(liquid_ref)
            config['vapor_property_package'] = self._resolve_property_package(user_config.pop('vapor_property_package', None) or PropertyPackageType.ADM1_VAPOR)
            config['reaction_package'] = self._resolve_reaction_package(user_config.pop('reaction_package', None), liquid_ref)
        elif spec.unit_type in {'CSTR', 'CSTR_Injection', 'AerationTank'}:
            package_ref = user_config.pop('property_package', None) or self._session.config.default_property_package
            config['property_package'] = self._resolve_property_package(package_ref)
            config['reaction_package'] = self._resolve_reaction_package(user_config.pop('reaction_package', None), package_ref)
        elif spec.unit_type == 'GasMembraneStage1D':
            from idaes.models_extra.co2_capture_and_utilization.unit_models.membrane_1d import MembraneFlowPattern
            gas_pkg = self._resolve_property_package(user_config.pop('property_package', None) or self._session.config.default_property_package)
            config['feed_side'] = {'property_package': gas_pkg}
            config['sweep_side'] = {'property_package': gas_pkg}
            config['sweep_flow'] = bool(user_config.pop('sweep_flow', False))
            config['finite_elements'] = int(user_config.pop('finite_elements', 5))
            flow_type = user_config.pop('flow_type', 'COUNTERCURRENT')
            if isinstance(flow_type, str):
                try:
                    flow_type = MembraneFlowPattern[flow_type.split('.')[-1]]
                except KeyError as exc:
                    raise ValueError(f"Unknown membrane flow_type '{flow_type}'; valid values: {[p.name for p in MembraneFlowPattern]}") from exc
            config['flow_type'] = flow_type
            if not hasattr(self, '_pending_gas_inputs'):
                self._pending_gas_inputs = {}
            self._pending_gas_inputs[unit_inst.unit_id] = {'permeance': user_config.pop('permeance', None) or user_config.pop('permeance_gpu', None), 'area_cm2': user_config.pop('area_cm2', None) or user_config.pop('membrane_area_cm2', None), 'length_cm': user_config.pop('length_cm', None), 'permeate_pressure_bar': user_config.pop('permeate_pressure_bar', None)}
        elif spec.unit_type == 'GasScrewCompressor':
            gas_pkg = self._resolve_property_package(user_config.pop('property_package', None) or self._session.config.default_property_package)
            if gas_pkg is not None:
                config['property_package'] = gas_pkg
            if not hasattr(self, '_pending_gas_compressor'):
                self._pending_gas_compressor = {}
            self._pending_gas_compressor[unit_inst.unit_id] = {k: user_config.pop(k, None) for k in ('swept_vol_m3_s', 'capacity_slide', 'volume_ratio_Vi', 'k_isentropic', 'cp_mol', 'eta_vol', 'eta_mech', 'eta_motor', 'f_oil', 'discharge_pressure_bar', 'saturated_capacity')}
        elif spec.unit_type == 'GasHeater':
            gas_pkg = self._resolve_property_package(user_config.pop('property_package', None) or self._session.config.default_property_package)
            if gas_pkg is not None:
                config['property_package'] = gas_pkg
            config['has_pressure_change'] = bool(user_config.pop('has_pressure_change', False))
            if not hasattr(self, '_pending_gas_heater'):
                self._pending_gas_heater = {}
            self._pending_gas_heater[unit_inst.unit_id] = {k: user_config.pop(k, None) for k in ('outlet_temperature_C', 'heat_duty_W', 'delta_p_bar')}
        else:
            property_package = self._resolve_property_package(user_config.pop('property_package', None) or self._session.config.default_property_package)
            if property_package is not None:
                config['property_package'] = property_package
        config.update(user_config)
        return self._resolve_nested_package_refs(config)
    _SURROGATE_CRYSTALLIZER_SOLID_MW_G_MOL = {'Calcite_g': 100.09, 'Anhydrite_g': 136.14, 'Glauberite_g': 278.2, 'Halite_g': 58.44}

    def _coerce_solids_ions_dict(self, solids: Dict[str, Any]) -> Dict[str, Any]:
        """Upconvert the registry's JSON-safe solids map to watertap>=1.7 schema.

        watertap 1.7 restructured SurrogateCrystallizer's ``solids_ions_dict``
        from ``{solid: {ion: stoich}}`` to
        ``{solid: {"precipitation_stoichiometric": {...}, "mw": <pint qty>}}``.
        The registry stays declarative (plain stoich map); the mw is attached
        here from the curated table so profile matching keeps comparing plain
        JSON values.
        """
        from pyomo.environ import units as pyunits
        coerced: Dict[str, Any] = {}
        for solid, entry in solids.items():
            if isinstance(entry, dict) and 'precipitation_stoichiometric' in entry:
                new_entry = dict(entry)
                mw = new_entry.get('mw')
                if isinstance(mw, (int, float)):
                    new_entry['mw'] = mw * pyunits.g / pyunits.mol
            else:
                mw_g_mol = self._SURROGATE_CRYSTALLIZER_SOLID_MW_G_MOL.get(solid)
                if mw_g_mol is None:
                    raise ValueError(f"""No molecular weight on record for crystallizer solid '{solid}'; supply the watertap>=1.7 entry form {{"precipitation_stoichiometric": {{...}}, "mw": <g/mol>}}""")
                new_entry = {'precipitation_stoichiometric': dict(entry), 'mw': mw_g_mol * pyunits.g / pyunits.mol}
            coerced[solid] = new_entry
        return coerced

    def _coerce_bped_config(self, config: Dict[str, Any]) -> Dict[str, Any]:
        """Convert JSON-safe bipolar ED literals into the live enum objects."""
        resolved = dict(config)
        from watertap.unit_models.electrodialysis_bipolar_1D import ElectricalOperationMode, FrictionFactorMethod, HydraulicDiameterMethod, LimitingCurrentDensitybpmMethod, PressureDropMethod
        enum_map = {'operation_mode': (ElectricalOperationMode, 'bipolar ED operation mode'), 'limiting_current_density_method_bpm': (LimitingCurrentDensitybpmMethod, 'bipolar ED limiting current method'), 'pressure_drop_method': (PressureDropMethod, 'bipolar ED pressure drop method'), 'friction_factor_method': (FrictionFactorMethod, 'bipolar ED friction factor method'), 'hydraulic_diameter_method': (HydraulicDiameterMethod, 'bipolar ED hydraulic diameter method')}
        for key, (enum_cls, label) in enum_map.items():
            value = resolved.get(key)
            if not isinstance(value, str):
                continue
            try:
                resolved[key] = enum_cls[value]
            except KeyError:
                for member in enum_cls:
                    if member.name.lower() == value.lower():
                        resolved[key] = member
                        break
                else:
                    raise ModelBuildError(f'Unsupported {label} literal: {value}')
        return resolved

    def _resolve_nested_package_refs(self, value: Any) -> Any:
        """Recursively resolve property-package references embedded in config payloads."""
        if isinstance(value, dict):
            if set(value) == {'value', 'units'}:
                return self._coerce_unit_value(value['value'], value['units'])
            resolved = {}
            for key, nested_value in value.items():
                if key.startswith('property_package') and (not isinstance(nested_value, dict)):
                    resolved[key] = self._resolve_property_package(nested_value)
                elif key.startswith('reaction_package') and (not isinstance(nested_value, dict)):
                    resolved[key] = self._resolve_reaction_package(nested_value)
                else:
                    resolved[key] = self._coerce_special_config_value(key, self._resolve_nested_package_refs(nested_value))
            return resolved
        if isinstance(value, list):
            if len(value) == 2 and isinstance(value[1], str) and (value[1] in {'g/mol', 'kg/mol', 'kg/L', 'kg/m^3'}):
                from pyomo.environ import units as pyunits
                unit = self._coerce_unit_value(1, value[1]) / 1
                return (value[0], unit)
            return [self._resolve_nested_package_refs(item) for item in value]
        if isinstance(value, tuple):
            return tuple((self._resolve_nested_package_refs(item) for item in value))
        return value

    def _coerce_unit_value(self, raw_value: Any, unit_name: str) -> Any:
        """Convert a serialized value/unit pair into a Pyomo quantity."""
        from pyomo.environ import units as pyunits
        unit_lookup = {'g/mol': pyunits.g / pyunits.mol, 'kg/mol': pyunits.kg / pyunits.mol, 'kg/L': pyunits.kg / pyunits.L, 'kg/m^3': pyunits.kg / pyunits.m ** 3}
        unit = unit_lookup.get(unit_name)
        if unit is None:
            raise ModelBuildError(f'Unsupported serialized unit literal: {unit_name}')
        return raw_value * unit

    def _coerce_special_config_value(self, key: str, value: Any) -> Any:
        """Resolve JSON-safe registry literals into live WaterTAP/IDAES objects."""
        if not isinstance(value, str):
            return value

        def _resolve_enum(enum_cls: Any, literal: str, *, label: str) -> Any:
            try:
                return enum_cls[literal]
            except KeyError:
                for member in enum_cls:
                    if member.name.lower() == literal.lower():
                        return member
                raise ModelBuildError(f'Unsupported {label} literal: {literal}')
        if key == 'delta_temperature_callback' and value == 'delta_temperature_chen_callback':
            from watertap.unit_models.mvc.components.lmtd_chen_callback import delta_temperature_chen_callback
            return delta_temperature_chen_callback
        if key == 'flow_pattern' and value == 'countercurrent':
            from idaes.models.unit_models.heat_exchanger import HeatExchangerFlowPattern
            return HeatExchangerFlowPattern.countercurrent
        if key == 'mode':
            raise ModelBuildError("SteamHeater0D 'mode' config was removed in watertap 1.7; the unit always condenses. Drop the 'mode' key.")
        if key == 'energy_balance_type':
            from idaes.core import EnergyBalanceType
            return _resolve_enum(EnergyBalanceType, value, label='energy balance type')
        if key == 'split_basis':
            from idaes.models.unit_models.separator import SplittingType
            return _resolve_enum(SplittingType, value, label='separator split basis')
        if key == 'MD_configuration_Type':
            from watertap.unit_models.MD.membrane_distillation_base import MDconfigurationType
            return _resolve_enum(MDconfigurationType, value, label='membrane distillation configuration')
        return value

    def attach_costing(self) -> Dict[str, Any]:
        """Attach costing blocks to an already-built model."""
        if self._model is None or self._flowsheet is None:
            raise ModelBuildError('Build the model before attaching costing')
        if hasattr(self._flowsheet, 'costing'):
            self.initialize_costing_values()
            return self.get_costing_report()
        self._create_costing()
        self.initialize_costing_values()
        return self.get_costing_report()

    def _initialize_costing_block_from_constraints(self, block: Any) -> None:
        """Initialize costing variables directly from same-named constraints."""
        from pyomo.environ import Constraint, units as pyunits
        from pyomo.util.calc_var_value import calculate_variable_from_constraint
        for _ in range(3):
            for constraint in block.component_objects(Constraint, descend_into=False):
                name = constraint.local_name
                if not name.endswith('_constraint'):
                    continue
                target_name = name[:-len('_constraint')]
                target = getattr(block, target_name, None)
                if target is None:
                    continue
                try:
                    if constraint.is_indexed():
                        if not hasattr(target, 'is_indexed') or not target.is_indexed():
                            continue
                        for index in constraint:
                            try:
                                calculate_variable_from_constraint(target[index], constraint[index])
                            except Exception:
                                continue
                    else:
                        calculate_variable_from_constraint(target, constraint)
                except Exception:
                    continue

    def initialize_costing_values(self) -> None:
        """Initialize attached costing blocks from their defining constraints."""
        costing_block = getattr(self._flowsheet, 'costing', None)
        if costing_block is None:
            return
        for unit_block in self._units.values():
            unit_costing = getattr(unit_block, 'costing', None)
            if unit_costing is None:
                continue
            self._initialize_costing_block_from_constraints(unit_costing)
        if hasattr(costing_block, 'initialize'):
            try:
                costing_block.initialize()
            except Exception:
                pass
        elif hasattr(costing_block, 'initialize_build'):
            try:
                costing_block.initialize_build()
            except Exception:
                pass
        self._initialize_costing_block_from_constraints(costing_block)

    def _resolve_property_package(self, package_ref: Any) -> Any:
        """Resolve a package enum/name to a live property package block."""
        if package_ref is None:
            return None
        if hasattr(package_ref, 'build_state_block'):
            return package_ref
        pkg_type = package_ref
        if isinstance(package_ref, str):
            try:
                pkg_type = PropertyPackageType[package_ref.upper()]
            except KeyError:
                pkg_type = next((candidate for candidate in PropertyPackageType if candidate.value == package_ref), None)
        if not isinstance(pkg_type, PropertyPackageType):
            raise ModelBuildError(f'Unknown property package reference: {package_ref}')
        pkg_block = self._property_packages.get(pkg_type.value)
        if pkg_block is not None:
            return pkg_block
        attr_name = f'prop_{pkg_type.name.lower()}'
        self._create_additional_package(pkg_type, attr_name)
        return self._property_packages[pkg_type.value]

    def _resolve_reaction_package(self, reaction_ref: Any=None, property_package_ref: Any=None) -> Any:
        """Resolve an ASM/ADM reaction-package reference to a live block."""
        if reaction_ref is not None and hasattr(reaction_ref, 'build_reaction_block'):
            return reaction_ref
        variant = REACTION_PACKAGE_VARIANTS.get(reaction_ref.strip().upper()) if isinstance(reaction_ref, str) else None
        if variant is not None:
            key = f'variant:{variant.name}'
            rxn_block = self._reaction_packages.get(key)
            if rxn_block is not None:
                return rxn_block
            prop_block = self._resolve_property_package(variant.pairs_with)
            paired = PROPERTY_PACKAGES.get(variant.pairs_with)
            if paired is not None and paired.reaction_module_path == variant.module_path and (paired.reaction_class_name == variant.class_name):
                self._create_reaction_package_if_required(variant.pairs_with, prop_block, f'rxn_{variant.pairs_with.name.lower()}')
                own = self._reaction_packages.get(variant.pairs_with.value)
                if own is not None:
                    self._reaction_packages[key] = own
                    return own
            try:
                ReactionClass = getattr(importlib.import_module(variant.module_path), variant.class_name)
            except (ImportError, AttributeError) as exc:
                raise ModelBuildError(f'Reaction package variant {variant.name} failed to import: {exc}') from exc
            attr_name = f'rxn_{variant.name.lower()}'
            setattr(self._flowsheet, attr_name, ReactionClass(property_package=prop_block))
            self._reaction_packages[key] = getattr(self._flowsheet, attr_name)
            return self._reaction_packages[key]
        pkg_type = None
        if reaction_ref is not None:
            pkg_type = self._coerce_reaction_package_ref(reaction_ref)
        elif property_package_ref is not None:
            pkg_type = self._coerce_reaction_package_ref(property_package_ref)
        else:
            pkg_type = self._session.config.default_property_package
        if not isinstance(pkg_type, PropertyPackageType):
            raise ModelBuildError(f'Unknown reaction package reference: {reaction_ref}')
        pkg_spec = PROPERTY_PACKAGES.get(pkg_type)
        if pkg_spec is None or not pkg_spec.requires_reaction_package:
            raise ModelBuildError(f'Property package {pkg_type.name} has no reaction package')
        rxn_block = self._reaction_packages.get(pkg_type.value)
        if rxn_block is not None:
            return rxn_block
        prop_block = self._resolve_property_package(pkg_type)
        attr_name = 'rxn_params' if pkg_type == self._session.config.default_property_package else f'rxn_{pkg_type.name.lower()}'
        self._create_reaction_package_if_required(pkg_type, prop_block, attr_name)
        rxn_block = self._reaction_packages.get(pkg_type.value)
        if rxn_block is None:
            raise ModelBuildError(f'Reaction package for {pkg_type.name} was not created')
        return rxn_block

    def _coerce_reaction_package_ref(self, package_ref: Any) -> Any:
        """Normalize a reaction/property package reference to PropertyPackageType."""
        if isinstance(package_ref, PropertyPackageType):
            return package_ref
        if isinstance(package_ref, str):
            try:
                return PropertyPackageType[package_ref.upper()]
            except KeyError:
                for candidate in PropertyPackageType:
                    if candidate.value == package_ref:
                        return candidate
        if hasattr(package_ref, 'build_state_block'):
            for pkg_name, block in self._property_packages.items():
                if block is package_ref:
                    return self._coerce_reaction_package_ref(pkg_name)
        return package_ref

    def _fix_non_oxygen_injection_terms(self, unit_block: Any) -> None:
        """Fix non-oxygen mass-transfer terms to zero for aerated CSTR_Injection."""
        try:
            if not unit_block.config.has_aeration:
                return
            component_list = list(unit_block.config.property_package.component_list)
            oxygen = 'S_O' if 'S_O' in component_list else 'S_O2'
            for index in unit_block.injection:
                *_, comp = index
                if comp != oxygen:
                    unit_block.injection[index].fix(0)
        except Exception as exc:
            raise ModelBuildError(f'Failed to fix non-oxygen CSTR_Injection mass-transfer terms: {exc}') from exc

    def _apply_gas_membrane_inputs(self, unit_block: Any, unit_inst: Any) -> None:
        """Fix GasMembraneStage1D design/operating inputs stashed by _build_unit_config.

        The stash (self._pending_gas_inputs[unit_id]) carries:
          - permeance: {component: GPU} (literal vendor GPU)
          - area_cm2: total active area [cm2]
          - length_cm: membrane/fiber length [cm]
          - permeate_pressure_bar: absolute permeate (vent/recycle) pressure [bar]
        The element indices are resolved here (after build), so all finite elements
        receive the same permeance.
        """
        try:
            ei = dict(getattr(self, '_pending_gas_inputs', {}).get(unit_inst.unit_id, {}))
            permeance = ei.get('permeance') or {}
            if not isinstance(permeance, dict) or not permeance:
                raise ModelBuildError("GasMembraneStage1D requires engineering_inputs['permeance'] as {component: GPU}")
            crossover = list(set(unit_block.mscontactor.feed_side.component_list) & set(unit_block.mscontactor.sweep_side.component_list))
            default_perm = min((float(v) for v in permeance.values()))
            for comp in crossover:
                val = float(permeance.get(comp, default_perm))
                for t in unit_block.flowsheet().time:
                    for s in unit_block.mscontactor.elements:
                        unit_block.permeance[t, s, comp].fix(val)
            area = ei.get('area_cm2') or ei.get('membrane_area_cm2')
            if area is None:
                raise ModelBuildError("GasMembraneStage1D requires engineering_inputs['area_cm2']")
            unit_block.area.fix(float(area))
            from watertap_engine_mcp.core.rating_geometry import require_number
            length = require_number(ei.get('length_cm'), 'length_cm', 'cm', 'GasMembraneStage1D')
            unit_block.length.fix(length)
            p_perm_bar = ei.get('permeate_pressure_bar')
            if p_perm_bar is None:
                raise ModelBuildError("GasMembraneStage1D requires engineering_inputs['permeate_pressure_bar']")
            for t in unit_block.flowsheet().time:
                unit_block.sweep_side_outlet.pressure[t].fix(float(p_perm_bar) * 100000.0)
        except ModelBuildError:
            raise
        except Exception as exc:
            raise ModelBuildError(f'Failed to apply GasMembraneStage1D engineering inputs: {exc}') from exc

    def _apply_gas_compressor_inputs(self, unit_block: Any, unit_inst: Any) -> None:
        """Apply OilFloodedScrewCompressor (GasScrewCompressor) design inputs stashed at
        config: the semi-empirical screw parameters (set_screw_params) + the discharge-
        pressure design spec. The saturated-slide capacity closure (activate_saturated_
        capacity, which holds the suction + frees the discharge + pins mixed flow to
        displacement) is recorded in self._gas_saturated_capacity and applied by the
        gas_membrane recycle recipe AFTER the design-mode forward-pass init — not here."""
        try:
            ei = dict(getattr(self, '_pending_gas_compressor', {}).get(unit_inst.unit_id, {}))
            sp: Dict[str, float] = {}
            if ei.get('swept_vol_m3_s') is not None:
                sp['swept_vol'] = float(ei['swept_vol_m3_s'])
            for key in ('capacity_slide', 'volume_ratio_Vi', 'k_isentropic', 'cp_mol', 'eta_vol', 'eta_mech', 'eta_motor', 'f_oil'):
                if ei.get(key) is not None:
                    sp[key] = float(ei[key])
            if sp:
                unit_block.set_screw_params(**sp)
            dp = ei.get('discharge_pressure_bar')
            if dp is None:
                raise ModelBuildError("GasScrewCompressor requires config['discharge_pressure_bar']")
            for t in unit_block.flowsheet().time:
                unit_block.outlet.pressure[t].fix(float(dp) * 100000.0)
            if not hasattr(self, '_gas_saturated_capacity'):
                self._gas_saturated_capacity = {}
            self._gas_saturated_capacity[unit_inst.unit_id] = bool(ei.get('saturated_capacity'))
            unit_block._gas_saturated_capacity_requested = bool(ei.get('saturated_capacity'))
        except ModelBuildError:
            raise
        except Exception as exc:
            raise ModelBuildError(f'Failed to apply GasScrewCompressor inputs: {exc}') from exc

    def _apply_gas_heater_inputs(self, unit_block: Any, unit_inst: Any) -> None:
        """Apply GasHeater (IDAES Heater) design inputs: fix the outlet temperature
        (preferred -> solve heat_duty) OR a fixed heat_duty; + an optional pressure drop
        (requires has_pressure_change=True)."""
        try:
            ei = dict(getattr(self, '_pending_gas_heater', {}).get(unit_inst.unit_id, {}))
            t_out_C = ei.get('outlet_temperature_C')
            q_W = ei.get('heat_duty_W')
            if t_out_C is None and q_W is None:
                raise ModelBuildError("GasHeater requires config 'outlet_temperature_C' or 'heat_duty_W'")
            dpb = ei.get('delta_p_bar')
            for t in unit_block.flowsheet().time:
                if t_out_C is not None:
                    unit_block.outlet.temperature[t].fix(float(t_out_C) + 273.15)
                else:
                    unit_block.heat_duty[t].fix(float(q_W))
                if dpb is not None and hasattr(unit_block, 'deltaP'):
                    unit_block.deltaP[t].fix(float(dpb) * 100000.0)
        except ModelBuildError:
            raise
        except Exception as exc:
            raise ModelBuildError(f'Failed to apply GasHeater inputs: {exc}') from exc

    def _create_translators(self):
        """Create translator blocks from session translators.

        Translators are units that convert state variables between property
        packages. They may be named WaterTAP translator unit models or curated
        generic IDAES Translator blocks copied from upstream flowsheets.
        """
        for trans_id, trans_data in self._session.translators.items():
            self._create_translator(trans_id, trans_data)

    def _create_translator(self, trans_id: str, trans_data: Dict):
        """Create a single translator block.

        Translators convert state variables between property packages. Each
        translator needs the correct inlet and outlet property packages.

        Args:
            trans_id: Translator identifier
            trans_data: Translator configuration from session
        """
        source_pkg_type = trans_data.get('source_pkg')
        dest_pkg_type = trans_data.get('dest_pkg')
        if source_pkg_type is None or dest_pkg_type is None:
            raise ModelBuildError(f'Translator {trans_id} missing source_pkg or dest_pkg')
        if isinstance(source_pkg_type, str):
            source_pkg_type = PropertyPackageType(source_pkg_type)
        if isinstance(dest_pkg_type, str):
            dest_pkg_type = PropertyPackageType(dest_pkg_type)
        spec = get_translator(source_pkg_type, dest_pkg_type)
        if spec is None:
            raise ModelBuildError(f'No translator found for {source_pkg_type.value} → {dest_pkg_type.value}')
        try:
            module = importlib.import_module(spec.module_path)
            TranslatorClass = getattr(module, spec.class_name)
            config = {}
            inlet_pkg = self._property_packages.get(source_pkg_type.value)
            if inlet_pkg is None:
                raise ModelBuildError(f'Inlet property package {source_pkg_type.value} not created for translator {trans_id}')
            outlet_pkg = self._property_packages.get(dest_pkg_type.value)
            if outlet_pkg is None:
                raise ModelBuildError(f'Outlet property package {dest_pkg_type.value} not created for translator {trans_id}')
            config['inlet_property_package'] = inlet_pkg
            config['outlet_property_package'] = outlet_pkg
            if spec.requires_reaction_packages:
                config['inlet_reaction_package'] = self._resolve_reaction_package(None, source_pkg_type)
                config['outlet_reaction_package'] = self._resolve_reaction_package(None, dest_pkg_type)
            if 'config' in trans_data:
                config.update(trans_data['config'])
            translator_block = TranslatorClass(**config)
            setattr(self._flowsheet, trans_id, translator_block)
            translator_block = getattr(self._flowsheet, trans_id)
            if spec.generic_rule:
                self._configure_generic_translator(translator_block, spec)
            self._translators[trans_id] = translator_block
            self._units[trans_id] = translator_block
        except ImportError as e:
            raise ModelBuildError(f'Cannot import translator {spec.module_path}: {e}')
        except Exception as e:
            raise ModelBuildError(f'Cannot create translator {trans_id}: {e}')

    def _configure_generic_translator(self, translator_block: Any, spec: TranslatorSpec) -> None:
        """Apply curated upstream constraint/fix rules to a generic translator."""
        from pyomo.environ import Constraint, units as pyunits
        rule = spec.generic_rule
        if rule == 'water_to_seawater_distillate':

            @translator_block.Constraint()
            def eq_flow_mass_h2o(blk):
                return blk.properties_in[0].flow_mass_phase_comp['Liq', 'H2O'] == blk.properties_out[0].flow_mass_phase_comp['Liq', 'H2O']

            @translator_block.Constraint()
            def eq_temperature(blk):
                return blk.properties_in[0].temperature == blk.properties_out[0].temperature

            @translator_block.Constraint()
            def eq_pressure(blk):
                return blk.properties_in[0].pressure == blk.properties_out[0].pressure
            translator_block.properties_out[0].flow_mass_phase_comp['Liq', 'TDS'].fix(1e-05)
            return
        if rule == 'zero_order_to_seawater_tds':

            @translator_block.Constraint(['H2O', 'tds'])
            def eq_flow_mass_comp(blk, j):
                mapped = 'TDS' if j == 'tds' else j
                return blk.properties_in[0].flow_mass_comp[j] == blk.properties_out[0].flow_mass_phase_comp['Liq', mapped]
            return
        if rule == 'seawater_to_zero_order_tds':

            @translator_block.Constraint(['H2O', 'TDS'])
            def eq_flow_mass_comp(blk, j):
                mapped = 'tds' if j == 'TDS' else j
                return blk.properties_in[0].flow_mass_phase_comp['Liq', j] == blk.properties_out[0].flow_mass_comp[mapped]
            return
        if rule == 'mcas_to_seawater_tds':
            solutes = tuple(translator_block.properties_in[0].params.solute_set)

            @translator_block.Constraint()
            def eq_flow_mass_h2o(blk):
                return blk.properties_in[0].flow_mass_phase_comp['Liq', 'H2O'] == blk.properties_out[0].flow_mass_phase_comp['Liq', 'H2O']

            @translator_block.Constraint()
            def eq_flow_mass_tds(blk):
                return sum((blk.properties_in[0].flow_mass_phase_comp['Liq', solute] for solute in solutes)) == blk.properties_out[0].flow_mass_phase_comp['Liq', 'TDS']

            @translator_block.Constraint()
            def eq_temperature(blk):
                return blk.properties_in[0].temperature == blk.properties_out[0].temperature

            @translator_block.Constraint()
            def eq_pressure(blk):
                return blk.properties_in[0].pressure == blk.properties_out[0].pressure
            return
        if rule == 'nacl_to_cryst_nacl':

            @translator_block.Constraint(['H2O', 'NaCl'])
            def eq_liq_flow_mass_comp(blk, j):
                return blk.properties_in[0].flow_mass_phase_comp['Liq', j] == blk.properties_out[0].flow_mass_phase_comp['Liq', j]

            @translator_block.Constraint()
            def eq_temperature(blk):
                return blk.properties_in[0].temperature == blk.properties_out[0].temperature

            @translator_block.Constraint()
            def eq_pressure(blk):
                return blk.properties_in[0].pressure == blk.properties_out[0].pressure
            translator_block.properties_out[0].flow_mass_phase_comp['Vap', 'H2O'].fix(1e-08)
            translator_block.properties_out[0].flow_mass_phase_comp['Sol', 'NaCl'].fix(1e-08)
            return
        raise ModelBuildError(f'Unknown generic translator rule: {rule}')

    def _create_connections(self):
        """Create Arc connections between units."""
        try:
            from pyomo.network import Arc
        except ImportError:
            from pyomo.environ import Arc
        for conn in self._session.connections:
            self._create_connection(conn, Arc)

    def _create_connection(self, conn: Connection, ArcClass):
        """Create Arc(s) for a connection, routing through translator if specified.

        Args:
            conn: Connection definition
            ArcClass: Arc class from pyomo.network
        """
        src_unit = self._units.get(conn.source_unit)
        dst_unit = self._units.get(conn.dest_unit)
        if src_unit is None:
            raise ModelBuildError(f'Source unit not found: {conn.source_unit}')
        if dst_unit is None:
            raise ModelBuildError(f'Destination unit not found: {conn.dest_unit}')
        src_port = getattr(src_unit, conn.source_port, None)
        dst_port = getattr(dst_unit, conn.dest_port, None)
        if src_port is None:
            raise ModelBuildError(f'Source port {conn.source_port} not found on {conn.source_unit}')
        if dst_port is None:
            raise ModelBuildError(f'Destination port {conn.dest_port} not found on {conn.dest_unit}')
        if conn.translator_id and conn.translator_id in self._translators:
            translator = self._translators[conn.translator_id]
            trans_inlet = getattr(translator, 'inlet', None)
            trans_outlet = getattr(translator, 'outlet', None)
            if trans_inlet is None or trans_outlet is None:
                raise ModelBuildError(f'Translator {conn.translator_id} missing inlet/outlet ports')
            arc1_name = f'arc_{conn.source_unit}_to_{conn.translator_id}'
            arc1 = ArcClass(source=src_port, destination=trans_inlet)
            setattr(self._flowsheet, arc1_name, arc1)
            arc2_name = f'arc_{conn.translator_id}_to_{conn.dest_unit}'
            arc2 = ArcClass(source=trans_outlet, destination=dst_port)
            setattr(self._flowsheet, arc2_name, arc2)
        else:
            arc_name = f'arc_{conn.source_unit}_{conn.dest_unit}'
            arc = ArcClass(source=src_port, destination=dst_port)
            setattr(self._flowsheet, arc_name, arc)

    def _expand_arcs(self):
        """Expand arcs for Pyomo network.

        Arcs must be expanded before solving or initialization.
        This transforms Arc constraints into explicit equality constraints.
        """
        try:
            from pyomo.environ import TransformationFactory
            TransformationFactory('network.expand_arcs').apply_to(self._model)
        except Exception as e:
            import sys
            print(f'Note: Arc expansion: {e}', file=sys.stderr)

    def _apply_special_unit_couplings(self) -> None:
        """Apply curated post-connection unit couplings.

        Some mixed-package WaterTAP units require an explicit coupling call in
        addition to arc connections. MVC evaporators are the main case.

        When ``thermal_couplings`` is set on the session (e.g. by a template),
        only those explicit pairs are wired and the MVC heuristic fallback is
        suppressed.  This prevents wrong auto-couplings in multi-evaporator
        topologies such as MEE.
        """
        explicit = self._session.thermal_couplings
        if explicit:
            for evap_id, cond_id in explicit.items():
                evaporator = self._units.get(evap_id)
                condenser = self._units.get(cond_id)
                if evaporator is None or condenser is None or (not hasattr(evaporator, 'connect_to_condenser')):
                    continue
                evaporator.connect_to_condenser(condenser)
            return
        for evap_id, unit_inst in self._session.units.items():
            if unit_inst.unit_type != 'Evaporator':
                continue
            condenser_id = self._find_mvc_condenser(evap_id)
            if condenser_id is None:
                continue
            evaporator = self._units.get(evap_id)
            condenser = self._units.get(condenser_id)
            if evaporator is None or condenser is None or (not hasattr(evaporator, 'connect_to_condenser')):
                continue
            evaporator.connect_to_condenser(condenser)

    def _find_mvc_condenser(self, evaporator_id: str) -> Optional[str]:
        """Find the condenser coupled to one evaporator via the session graph."""
        downstream = [conn.dest_unit for conn in self._session.connections if conn.source_unit == evaporator_id and conn.source_port == 'outlet_vapor']
        for unit_id in downstream:
            unit_inst = self._session.units.get(unit_id)
            if unit_inst is None:
                continue
            if unit_inst.unit_type == 'Condenser':
                return unit_id
            if unit_inst.unit_type != 'Compressor':
                continue
            for conn in self._session.connections:
                if conn.source_unit != unit_id:
                    continue
                next_inst = self._session.units.get(conn.dest_unit)
                if next_inst is not None and next_inst.unit_type == 'Condenser':
                    return conn.dest_unit
        return None

    def _apply_process_constraints(self) -> None:
        """Apply flowsheet-level algebraic process constraints from the session."""
        self._free_recycle_ratio_objective_terms = []
        self._target_control_actuator_claims = {}
        self._target_control_freed_actuators = []
        self._chemical_p_target_freed_doses = []
        try:
            self._session.config.runtime_metadata['chemical_p_target_specs'] = []
            self._session.config.runtime_metadata['alkalinity_target_specs'] = []
            self._session.config.runtime_metadata['alkalinity_min_dose_actuators'] = []
            self._session.config.runtime_metadata['do_target_specs'] = []
            self._session.config.runtime_metadata['srt_target_specs'] = []
            self._session.config.runtime_metadata['no3_target_specs'] = []
            self._session.config.runtime_metadata['adm1_temperature_target_specs'] = []
            self._session.config.runtime_metadata['adm1_ph_target_specs'] = []
            self._session.config.runtime_metadata['adm1_metal_target_specs'] = []
        except Exception:
            pass
        for index, constraint_spec in enumerate(self._session.process_constraints):
            constraint_type = (constraint_spec.get('type') or constraint_spec.get('constraint_type') or '').lower()
            self._assert_target_compatibility(constraint_type, constraint_spec)
            if constraint_type == 'srt':
                self._add_srt_constraint(index, constraint_spec)
                continue
            if constraint_type in ('temperature_target', 'adm1_temperature_target', 'target_temperature'):
                self._add_adm1_temperature_target_constraint(index, constraint_spec)
                continue
            if constraint_type in ('adm1_ph_target', 'adm1_base_dose', 'adm1_ph_alkalinity_target', 'adm1_caustic_target'):
                self._add_adm1_ph_alkalinity_target_constraint(index, constraint_spec)
                continue
            if constraint_type in ('adm1_metal_target', 'adm1_struvite_target', 'adm1_soluble_p_target'):
                self._add_adm1_metal_target_constraint(index, constraint_spec)
                continue
            if constraint_type in ('do_target', 'target_do', 'dissolved_oxygen_target'):
                self._add_do_target_constraint(index, constraint_spec)
                continue
            if constraint_type == 'mlss_bounds':
                self._apply_mlss_bounds(index, constraint_spec)
                continue
            if constraint_type == 'recycle_ratio':
                self._add_recycle_ratio_constraint(index, constraint_spec)
                continue
            if constraint_type in ('chemical_p_target', 'target_effluent_p', 'chemical_p'):
                self._add_chemical_p_target_constraint(index, constraint_spec)
                continue
            if constraint_type in ('no3_target', 'target_effluent_no3', 'denitrification_target', 'supplemental_carbon'):
                self._add_no3_target_constraint(index, constraint_spec)
                continue
            if constraint_type in ('alkalinity_target', 'target_alkalinity', 'ph_target', 'caustic_target'):
                self._add_alkalinity_target_constraint(index, constraint_spec)
                continue
            if constraint_type in ('raw_var_target', 'raw_var', 'variable_target'):
                self._add_raw_var_target_constraint(index, constraint_spec)
                continue
            raise ModelBuildError(f'Unsupported process constraint type: {constraint_spec.get('type')}')
        self._add_recycle_ratio_regularization_objective()

    def _assert_target_compatibility(self, constraint_type: str, constraint_spec: Dict[str, Any]) -> None:
        """Pre-build gate: a target's required components + unit/actuator exist.

        Looks the (canonicalized) target up in :data:`_TARGET_REQUIREMENTS`,
        verifies every required component "OR group" is carried by some liquid
        property package in the model AND the required topology unit/actuator is
        present, and raises :class:`TargetCompatibilityError` (naming the missing
        component(s)/unit(s) + remediation) on a mismatch. Targets with no entry
        (mlss_bounds, recycle_ratio, raw_var_target) are not gated. Read-only: it
        never mutates the model, so it is safe to run ahead of the builder.
        """
        canonical = _TARGET_CANONICAL.get(constraint_type)
        if canonical is None:
            return
        req = _TARGET_REQUIREMENTS.get(canonical)
        if req is None:
            return
        available = self._gate_available_components()
        groups = [tuple(group) for group in req.get('component_groups', [])]
        if canonical == 'do_target':
            groups = [(str(constraint_spec.get('component', 'S_O2')),)]
        elif canonical == 'no3_target':
            groups = [(str(constraint_spec.get('component', 'S_NO3')),)]
        missing_components: List[str] = []
        for group in groups:
            if not available & set(group):
                missing_components.append('/'.join(group))
        present, unit_label = self._gate_unit_present(req['unit'], constraint_spec)
        missing_units: List[str] = [] if present else [unit_label]
        if not missing_components and (not missing_units):
            return
        remediation_parts: List[str] = []
        if missing_components and req.get('component_remediation'):
            remediation_parts.append(req['component_remediation'])
        elif missing_components:
            remediation_parts.append(f'Use a property package that carries {', '.join(missing_components)}.')
        if missing_units and req.get('unit_remediation'):
            remediation_parts.append(req['unit_remediation'])
        elif missing_units:
            remediation_parts.append(f'Add {missing_units[0]} to the topology.')
        remediation = ' '.join(remediation_parts)
        deficits: List[str] = []
        if missing_components:
            deficits.append(f'missing component(s) {missing_components}')
        if missing_units:
            deficits.append(f'missing unit/actuator {missing_units}')
        message = f'Process-control target {canonical!r} ({req.get('label', canonical)}) is incompatible with this flowsheet: {'; '.join(deficits)}. {remediation}'
        raise TargetCompatibilityError(message, target=canonical, remediation=remediation, missing_components=missing_components, missing_units=missing_units, details={'target_label': req.get('label', canonical), 'constraint_type': constraint_type, 'constraint_name': constraint_spec.get('name'), 'available_components': sorted(available)})

    def _gate_available_components(self) -> Set[str]:
        """Return the union of every liquid property package's component names."""
        available: Set[str] = set()
        for pkg in self._property_packages.values():
            if pkg is None:
                continue
            for attr in ('component_list', 'solute_set'):
                members = getattr(pkg, attr, None)
                if members is None:
                    continue
                try:
                    available.update((str(c) for c in members))
                except Exception:
                    continue
        return available

    def _gate_unit_present(self, unit_kind: str, constraint_spec: Dict[str, Any]) -> Tuple[bool, str]:
        """Return ``(present, human_label)`` for a target's required unit/actuator.

        Read-only and mirrors the lookups the matching ``_add_*`` builder performs
        (same dose-unit / actuator helpers), so the gate's verdict can never
        diverge from what the builder would actually find. When the spec omits the
        key needed to LOCATE the unit (e.g. SRT without ``waste_unit``), the gate
        defers to the builder's own "requires <key>" spec-completeness error and
        reports the unit as present.
        """
        if unit_kind == 'chemical_p_dose':
            present = bool(self._chemical_p_dose_units(constraint_spec.get('dose_unit')))
            return (present, 'a ChemicalPhosphorusRemovalASM2D unit (metal_dose actuator)')
        if unit_kind == 'caustic_dose':
            dose_unit = constraint_spec.get('dose_unit') or constraint_spec.get('caustic_unit')
            present = bool(self._caustic_dose_units(dose_unit))
            return (present, 'an alkalinity dose unit (AlkalinityDoseASM2D caustic_dose actuator)')
        if unit_kind == 'carbon_dose':
            dose_unit = constraint_spec.get('dose_unit') or constraint_spec.get('carbon_unit')
            present = bool(self._carbon_dose_units(dose_unit))
            return (present, 'a SupplementalCarbonDoseASM2D unit (carbon_dose actuator)')
        if unit_kind == 'kla':
            return (self._gate_has_kla(constraint_spec), 'an aerobic KLa aeration actuator')
        if unit_kind == 'was_splitter':
            return (self._gate_has_was_splitter(constraint_spec), 'a WAS sludge-splitter actuator (split_fraction on the waste port)')
        if unit_kind == 'adm1_digester_heat':
            return (self._gate_has_adm1_digester(constraint_spec), 'an ADM1 digester (liquid_phase control volume with a heat duty)')
        if unit_kind == 'adm1_base_dose':
            dose_unit = constraint_spec.get('dose_unit') or constraint_spec.get('base_dose_unit')
            present = bool(self._adm1_base_dose_units(dose_unit))
            return (present, 'an ADM1BaseDose unit (caustic_dose + cations strong-ion balance)')
        if unit_kind == 'adm1_mg_dose':
            dose_unit = constraint_spec.get('dose_unit') or constraint_spec.get('mg_dose_unit')
            present = bool(self._adm1_mg_dose_units(dose_unit))
            return (present, 'an ADM1StruvitePRemoval unit (mg_dose actuator)')
        return (True, '')

    def _gate_has_kla(self, constraint_spec: Dict[str, Any]) -> bool:
        """True when the resolved aeration actuator unit exposes a KLa var.

        Mirrors the actuator resolution in :meth:`_add_do_target_constraint`.
        """
        actuator_unit_id = constraint_spec.get('actuator_unit') or constraint_spec.get('kla_unit') or constraint_spec.get('target_unit') or constraint_spec.get('unit')
        if not actuator_unit_id:
            return True
        unit = self._units.get(str(actuator_unit_id))
        if unit is None:
            return False
        return getattr(unit, str(constraint_spec.get('actuator_var', 'KLa')), None) is not None

    def _gate_has_was_splitter(self, constraint_spec: Dict[str, Any]) -> bool:
        """True when the named SRT waste unit exposes a WAS split actuator.

        Reuses :meth:`_srt_waste_actuator` so the gate matches the SRT builder.
        """
        waste_unit_id = constraint_spec.get('waste_unit')
        if not waste_unit_id:
            return True
        waste_unit = self._units.get(str(waste_unit_id))
        if waste_unit is None:
            return False
        waste_port = str(constraint_spec.get('waste_port', 'waste'))
        return self._srt_waste_actuator(waste_unit, waste_port) is not None

    def _gate_has_adm1_digester(self, constraint_spec: Dict[str, Any]) -> bool:
        """True when the named temperature-target unit is an ADM1 digester.

        An ADM1 digester exposes a ``liquid_phase`` control volume carrying a
        ``heat`` duty (the variable the temperature control frees).
        """
        unit_id = constraint_spec.get('target_unit') or constraint_spec.get('unit') or constraint_spec.get('digester')
        if not unit_id:
            return True
        unit = self._units.get(str(unit_id))
        if unit is None:
            return False
        liquid_phase = getattr(unit, 'liquid_phase', None)
        if liquid_phase is None:
            return False
        return getattr(liquid_phase, 'heat', None) is not None

    def _register_target_actuator(self, actuator_var: Any, *, constraint_type: str, constraint_name: str, build_seed: Any=None) -> None:
        """Record one manipulated variable owned by a target constraint."""
        key = str(getattr(actuator_var, 'name', id(actuator_var)))
        claims = getattr(self, '_target_control_actuator_claims', {})
        previous = claims.get(key)
        claim = f'{constraint_type}:{constraint_name}'
        if previous is not None and previous != claim:
            raise ModelBuildError(f'Target-control actuator conflict: {key} is freed by both {previous} and {claim}')
        claims[key] = claim
        self._target_control_actuator_claims = claims
        self._target_control_freed_actuators.append({'var': actuator_var, 'constraint_type': constraint_type, 'constraint_name': constraint_name, 'build_seed': build_seed})

    def _register_controlled_variable_unfix(self, controlled_var: Any, *, constraint_type: str, constraint_name: str) -> None:
        """Release a legacy fixed setpoint variable after explicit fixes run."""
        self._target_control_freed_actuators.append({'var': controlled_var, 'constraint_type': f'{constraint_type}_controlled', 'constraint_name': constraint_name, 'build_seed': None})

    def _free_target_control_actuators(self) -> None:
        """Unfix variables owned by target-driven process constraints."""
        for entry in getattr(self, '_target_control_freed_actuators', []):
            var = entry.get('var') if isinstance(entry, dict) else None
            build_seed = entry.get('build_seed') if isinstance(entry, dict) else None
            if var is None:
                continue
            try:
                if var.fixed:
                    var.unfix()
            except Exception:
                continue
            if build_seed is not None:
                try:
                    var.set_value(build_seed)
                except Exception:
                    pass

    def _add_srt_constraint(self, index: int, constraint_spec: Dict[str, Any]) -> None:
        """Add an algebraic sludge-retention-time constraint.

        The form is:
            sum(V_reactor * X_reactor) = target_srt_seconds * Q_was * X_was
        where X is the configured sum of biomass/solids components.
        """
        from pyomo.environ import Constraint, units as pyunits, value
        target_days = constraint_spec.get('target_days')
        if target_days is None:
            raise ModelBuildError('SRT process constraint requires target_days')
        reactor_units = constraint_spec.get('reactor_units') or []
        if not reactor_units:
            raise ModelBuildError('SRT process constraint requires reactor_units')
        components = constraint_spec.get('biomass_components') or []
        if not components:
            raise ModelBuildError('SRT process constraint requires biomass_components')
        waste_unit_id = constraint_spec.get('waste_unit')
        waste_port_name = constraint_spec.get('waste_port', 'waste')
        if not waste_unit_id:
            raise ModelBuildError('SRT process constraint requires waste_unit')
        inventory = 0
        nominal_inventory_scale_kg = 0.0
        for unit_id in reactor_units:
            unit_block = self._units.get(unit_id)
            if unit_block is None:
                raise ModelBuildError(f'SRT reactor unit not found: {unit_id}')
            volume = self._srt_reactor_volume(unit_block)
            if volume is None:
                raise ModelBuildError(f'SRT reactor {unit_id} has no volume[0] / volume_liquid[0]')
            concentration = sum((self._outlet_concentration(unit_block, component) for component in components))
            inventory += volume * concentration
            volume_value = value(volume, exception=False)
            if volume_value is not None:
                nominal_inventory_scale_kg += abs(float(volume_value))
        waste_unit = self._units.get(waste_unit_id)
        if waste_unit is None:
            raise ModelBuildError(f'SRT waste unit not found: {waste_unit_id}')
        waste_port = getattr(waste_unit, waste_port_name, None)
        if waste_port is None:
            raise ModelBuildError(f'SRT waste port {waste_port_name} not found on {waste_unit_id}')
        waste_flow = self._indexed_component(getattr(waste_port, 'flow_vol', None), 0, 0.0)
        if waste_flow is None:
            raise ModelBuildError(f'SRT waste port {waste_unit_id}.{waste_port_name} has no flow_vol[0]')
        waste_actuator = self._srt_waste_actuator(waste_unit, str(waste_port_name))
        if waste_actuator is None:
            raise ModelBuildError(f'SRT actuator not found: waste unit {waste_unit_id} port {waste_port_name} exposes no WAS split-fraction variable to free; the SRT target cannot be solved without a dedicated WAS actuator.')
        name = self._process_constraint_name(constraint_spec.get('name') or f'srt_{index + 1}')
        build_seed = constraint_spec.get('waste_split_seed')
        try:
            if build_seed is not None:
                build_seed = float(build_seed)
        except (TypeError, ValueError):
            build_seed = None
        self._apply_srt_waste_actuator_bounds(waste_actuator, constraint_spec)
        self._register_target_actuator(waste_actuator, constraint_type='srt', constraint_name=name, build_seed=build_seed)
        self._record_srt_target_spec(name, constraint_spec, build_seed=build_seed)
        self._apply_srt_retained_component_caps(constraint_spec)
        if waste_actuator.fixed:
            waste_actuator.unfix()
        waste_concentration = sum((self._port_concentration(waste_port, component) for component in components))
        target_seconds = float(target_days) * 24 * 3600 * pyunits.s
        inventory_scale_kg = max(float(constraint_spec.get('inventory_scale_kg') or nominal_inventory_scale_kg or 1.0), 1.0)
        inventory_scale = inventory_scale_kg * pyunits.kg
        setattr(self._flowsheet, name, Constraint(expr=inventory / inventory_scale == target_seconds * waste_flow * waste_concentration / inventory_scale))

    def _record_srt_target_spec(self, constraint_name: str, constraint_spec: Dict[str, Any], *, build_seed: Optional[float]) -> None:
        """Expose SRT target metadata to initialization and diagnostics."""
        try:
            metadata = self._session.config.runtime_metadata
        except Exception:
            return
        if not isinstance(metadata, dict):
            return
        target_days = constraint_spec.get('target_days')
        try:
            target_days_float = float(target_days)
        except (TypeError, ValueError):
            target_days_float = None
        entry = {'name': constraint_name, 'target_days': target_days_float, 'reactor_units': list(constraint_spec.get('reactor_units') or []), 'biomass_components': list(constraint_spec.get('biomass_components') or []), 'waste_unit': constraint_spec.get('waste_unit'), 'waste_port': constraint_spec.get('waste_port', 'waste'), 'waste_split_seed': build_seed, 'retained_streams': list(constraint_spec.get('retained_streams') or [])}
        specs = metadata.setdefault('srt_target_specs', [])
        if isinstance(specs, list):
            specs.append(entry)
        design_parameters = metadata.setdefault('design_parameters', {})
        if isinstance(design_parameters, dict):
            if target_days_float is not None:
                design_parameters['target_srt_days'] = target_days_float
            if build_seed is not None:
                design_parameters['srt_waste_split_seed'] = build_seed

    def _apply_srt_retained_component_caps(self, constraint_spec: Dict[str, Any]) -> None:
        """Apply explicit retained-stream concentration caps from an SRT spec.

        These are variable bounds, not extra equations, so the finite-SRT free+row
        swap remains DOF-neutral. The caps keep biomass-retaining recycle templates
        out of the unphysical retained-solids basin that the old fixed split could
        force (especially inert ``X_I``).
        """
        raw_caps = constraint_spec.get('retained_component_caps_kg_m3') or constraint_spec.get('component_caps_kg_m3') or {}
        if not isinstance(raw_caps, dict) or not raw_caps:
            return
        caps: Dict[str, float] = {}
        for component, cap in raw_caps.items():
            try:
                cap_value = float(cap)
            except (TypeError, ValueError):
                continue
            if cap_value > 0:
                caps[str(component)] = cap_value
        if not caps:
            return

        def _set_cap(var: Any, cap_value: float) -> None:
            try:
                existing = var.ub
                if existing is None or float(existing) > cap_value:
                    var.setub(cap_value)
            except Exception:
                pass
        for unit_id in constraint_spec.get('reactor_units') or []:
            unit = self._units.get(str(unit_id))
            if unit is None:
                continue
            for component, cap_value in caps.items():
                try:
                    _set_cap(self._outlet_concentration(unit, component), cap_value)
                except Exception:
                    continue
        retained_streams = constraint_spec.get('retained_streams') or []
        if not isinstance(retained_streams, list):
            return
        for stream in retained_streams:
            if not isinstance(stream, dict):
                continue
            unit = self._units.get(str(stream.get('unit')))
            if unit is None:
                continue
            port = getattr(unit, str(stream.get('port')), None)
            if port is None:
                continue
            for component, cap_value in caps.items():
                try:
                    _set_cap(self._port_concentration(port, component), cap_value)
                except Exception:
                    continue

    def _srt_waste_actuator(self, waste_unit: Any, waste_port_name: str) -> Optional[Any]:
        """Return the WAS split fraction when the waste port is splitter-owned."""
        split_fraction = getattr(waste_unit, 'split_fraction', None)
        if split_fraction is None:
            return None
        for index in ((0, waste_port_name), (0.0, waste_port_name), waste_port_name):
            try:
                return split_fraction[index]
            except Exception:
                continue
        return None

    def _apply_srt_waste_actuator_bounds(self, waste_actuator: Any, constraint_spec: Dict[str, Any]) -> None:
        """Keep SRT-controlled WAS splits in the physical interior of [0, 1]."""
        if not self._session_uses_adm1_package():
            return
        try:
            lower = float(constraint_spec.get('waste_split_lower_bound', 0.0001))
        except (TypeError, ValueError):
            lower = 0.0001
        try:
            upper = float(constraint_spec.get('waste_split_upper_bound', 0.95))
        except (TypeError, ValueError):
            upper = 0.95
        lower = max(0.0, min(lower, 0.999))
        upper = max(lower + 1e-09, min(upper, 0.999999))
        try:
            waste_actuator.setlb(lower)
            waste_actuator.setub(upper)
        except Exception:
            pass

    def _ensure_adm1_external_heat_balance(self, unit_block: Any, t: Any=0.0) -> Any:
        """Convert an ADM1 digester's adiabatic energy closure into an externally
        heated one and return the heat-duty Var ``heat[t]``.

        The WaterTAP AD builds two enthalpy rows on the liquid control volume:

          * the hardcoded ``unit_enthalpy_balance`` (``enthalpy_transfer + H_liq,in
            == H_liq,out + H_vap``), which combined with the IDAES liquid enthalpy
            balance forces ``heat == -H_vap`` and leaves the outlet temperature
            free — an ADIABATIC self-closure where ``heat[t]`` is a non-physical
            slack, not an external duty; and
          * the IDAES ``liquid_phase.enthalpy_balances`` (``H_liq,in - H_liq,out +
            heat + enthalpy_transfer == 0``), which already carries ``heat[t]``.

        To make the reactor a properly modeled isothermal HEATED stage we DEACTIVATE
        the WaterTAP ``unit_enthalpy_balance`` and add the vapor enthalpy-transfer
        relation ``enthalpy_transfer + H_vap == 0`` in its place, leaving the IDAES
        liquid enthalpy balance active. Substituting the new relation, that balance
        becomes ``H_liq,in + Q = H_liq,out + H_vap`` with ``Q = heat[t]`` the real
        external heating duty (solved, reported as a heating load). The swap is
        DOF-neutral (one row deactivated, one added); the temperature target then
        frees ``heat`` (+1) and pins the outlet T (-1). Idempotent per unit block.
        """
        from pyomo.environ import Constraint
        liquid_phase = getattr(unit_block, 'liquid_phase', None)
        if liquid_phase is None:
            raise ModelBuildError(f'temperature_target unit {unit_block.name} is not an ADM1 digester (no liquid_phase control volume / heat duty)')
        heat = self._indexed_component(getattr(liquid_phase, 'heat', None), t, 0.0)
        if heat is None:
            raise ModelBuildError(f'temperature_target unit {unit_block.name} exposes no liquid_phase.heat[t] duty variable')
        enthalpy_transfer = self._indexed_component(getattr(liquid_phase, 'enthalpy_transfer', None), t, 0.0)
        if enthalpy_transfer is None:
            raise ModelBuildError(f'temperature_target unit {unit_block.name} exposes no liquid_phase.enthalpy_transfer[t]')
        vapor_state = self._indexed_component(getattr(unit_block, 'vapor_phase', None), t, 0.0)
        if vapor_state is None or not hasattr(vapor_state, 'get_enthalpy_flow_terms'):
            raise ModelBuildError(f'temperature_target unit {unit_block.name} exposes no vapor phase enthalpy-flow terms for the heat-balance replacement')
        replacement_name = 'adm1_external_heat_vapor_transfer'
        if hasattr(unit_block, replacement_name):
            return heat
        unit_enthalpy_balance = getattr(unit_block, 'unit_enthalpy_balance', None)
        if unit_enthalpy_balance is None:
            raise ModelBuildError(f'temperature_target unit {unit_block.name} has no unit_enthalpy_balance adiabatic closure to replace')
        ueb_data = self._indexed_component(unit_enthalpy_balance, t, 0.0)
        if ueb_data is not None and ueb_data.active:
            ueb_data.deactivate()
        setattr(unit_block, replacement_name, Constraint(expr=enthalpy_transfer + vapor_state.get_enthalpy_flow_terms('Vap') == 0))
        return heat

    def _arc_partner_temperatures(self, controlled_temp: Any) -> List[Any]:
        """Return the arc-expanded temperature-equality partners of an outlet temp.

        The biological temperature demotion fixes the temperature of any non
        heat-balanced unit (e.g. a terminal effluent Product) to the feed datum.
        When a digester outlet temperature is pinned by a target, the arc that
        couples it to the immediately-downstream unit's temperature must DETERMINE
        that downstream temperature (not have the demotion independently fix it, which
        would over-constrain the setpoint). Return those partner temperature Vars so
        the caller can register them for unfix after demotion. (Downstream units that
        are themselves heat-balanced digesters keep their temperature protected, so
        this returns harmless never-fixed vars there.)
        """
        from pyomo.environ import Constraint
        from pyomo.core.expr.visitor import identify_variables
        partners: List[Any] = []
        for constraint in self._flowsheet.component_data_objects(Constraint, active=True, descend_into=True):
            name = getattr(constraint, 'name', '')
            if '_expanded' not in name or 'temperature_equality' not in name:
                continue
            try:
                variables = list(identify_variables(constraint.body, include_fixed=True))
            except Exception:
                continue
            if not any((var is controlled_temp for var in variables)):
                continue
            for var in variables:
                if var is controlled_temp:
                    continue
                try:
                    is_temperature = var.parent_component().local_name == 'temperature'
                except Exception:
                    is_temperature = False
                if is_temperature:
                    partners.append(var)
        return partners

    def _add_adm1_temperature_target_constraint(self, index: int, constraint_spec: Dict[str, Any]) -> None:
        """Hold an ADM1 digester's liquid-outlet temperature at a setpoint by SOLVING
        the external heating duty (the temperature->heat-duty control loop, A5-PR1).

        Mirrors the aerobic ``do_target`` / ``alkalinity_target`` "pin Y, free X"
        swaps, but the manipulated variable is the digester heat duty and the
        controlled metric is its liquid-outlet temperature. The build:

          1. converts the adiabatic energy closure into an externally-heated one
             (:meth:`_ensure_adm1_external_heat_balance`) so ``heat[t]`` is the real
             heating load;
          2. frees ``heat[t]`` and registers it as the target actuator;
          3. pins the liquid-outlet temperature to the setpoint with one equation;
          4. frees the immediately-downstream arc-coupled temperature so the
             demotion does not independently pin the setpoint (DOF-neutral: heat
             freed +1, outlet T pinned -1).

        HONESTY: the modified-ADM1 kinetics are constant-parameter, so the setpoint
        governs the ENERGY balance, the acid/base equilibria, and Henry-law gas
        transfer -- it is NOT an Arrhenius temperature correction of the biological
        rates.
        """
        from pyomo.environ import Constraint, Param
        target_unit_id = constraint_spec.get('target_unit') or constraint_spec.get('unit') or constraint_spec.get('digester')
        if not target_unit_id:
            raise ModelBuildError('temperature_target constraint requires target_unit (the digester id)')
        unit_block = self._units.get(str(target_unit_id))
        if unit_block is None:
            raise ModelBuildError(f'temperature_target unit not found: {target_unit_id}')
        target_K = None
        for key in ('target_K', 'target_temperature_K', 'temperature_K', 'target_value'):
            if constraint_spec.get(key) is not None:
                target_K = float(constraint_spec[key])
                break
        if target_K is None:
            raise ModelBuildError('temperature_target requires target_K (outlet temperature setpoint, K)')
        if target_K <= 0:
            raise ModelBuildError('temperature_target target_K must be positive')
        t = constraint_spec.get('time', 0.0)
        heat_var = self._ensure_adm1_external_heat_balance(unit_block, t=t)
        raw_name = constraint_spec.get('name') or str(index + 1)
        constraint_name = self._process_constraint_name(f'adm1_temperature_target_{raw_name}')
        if heat_var.fixed:
            heat_var.unfix()
        self._register_target_actuator(heat_var, constraint_type='adm1_temperature_target', constraint_name=constraint_name)
        liquid_out = self._indexed_component(unit_block.liquid_phase.properties_out, t, 0.0)
        if liquid_out is None or not hasattr(liquid_out, 'temperature'):
            raise ModelBuildError(f'temperature_target unit {target_unit_id} exposes no liquid_phase.properties_out[t].temperature')
        controlled_temp = liquid_out.temperature
        if controlled_temp.fixed:
            controlled_temp.unfix()
        self._register_controlled_variable_unfix(controlled_temp, constraint_type='adm1_temperature_target', constraint_name=constraint_name)
        param_name = self._process_component_name('process_param', f'adm1_temperature_target_{raw_name}')
        setattr(self._flowsheet, param_name, Param(initialize=target_K, mutable=True))
        target_param = getattr(self._flowsheet, param_name)
        setattr(self._flowsheet, constraint_name, Constraint(expr=controlled_temp == target_param))
        for partner in self._arc_partner_temperatures(controlled_temp):
            self._register_controlled_variable_unfix(partner, constraint_type='adm1_temperature_target', constraint_name=constraint_name)
        try:
            self._session.config.runtime_metadata.setdefault('adm1_temperature_target_specs', []).append({'target_unit': str(target_unit_id), 'target_K': target_K, 'heat_var': heat_var.name, 'constraint_name': constraint_name})
        except Exception:
            pass

    def _add_do_target_constraint(self, index: int, constraint_spec: Dict[str, Any]) -> None:
        """Pin outlet dissolved oxygen by freeing an aeration KLa variable."""
        from pyomo.environ import Constraint, Param, value
        target_mg_l = constraint_spec.get('target_mg_l') if constraint_spec.get('target_mg_l') is not None else constraint_spec.get('target_do_mg_l')
        if target_mg_l is None:
            raise ModelBuildError('do_target constraint requires target_mg_l')
        target_kg_m3 = float(target_mg_l) / 1000.0
        if target_kg_m3 < 0:
            raise ModelBuildError('do_target target_mg_l must be non-negative')
        target_unit_id = constraint_spec.get('target_unit') or constraint_spec.get('unit')
        if not target_unit_id:
            raise ModelBuildError('do_target constraint requires target_unit')
        target_port_name = str(constraint_spec.get('target_port', 'outlet'))
        component = str(constraint_spec.get('component', 'S_O2'))
        target_port = self._unit_port(str(target_unit_id), target_port_name)
        oxygen = self._port_concentration(target_port, component)
        actuator_unit_id = constraint_spec.get('actuator_unit') or constraint_spec.get('kla_unit') or target_unit_id
        actuator_unit = self._units.get(str(actuator_unit_id))
        if actuator_unit is None:
            raise ModelBuildError(f'do_target actuator unit not found: {actuator_unit_id}')
        kla = getattr(actuator_unit, str(constraint_spec.get('actuator_var', 'KLa')), None)
        if kla is None:
            raise ModelBuildError(f'do_target actuator unit {actuator_unit_id} has no KLa')
        kla_lower = constraint_spec.get('kla_lower')
        kla_upper = constraint_spec.get('kla_upper')
        if kla_lower is None:
            kla_lower = constraint_spec.get('actuator_lower')
        if kla_upper is None:
            kla_upper = constraint_spec.get('actuator_upper')
        if kla_lower is not None:
            kla.setlb(float(kla_lower))
        if kla_upper is not None:
            kla.setub(float(kla_upper))
        initial = value(kla, exception=False)
        if initial is None:
            seed = constraint_spec.get('kla_seed', constraint_spec.get('actuator_seed', 5.0))
            try:
                kla.set_value(float(seed))
            except Exception:
                pass
        if kla.fixed:
            kla.unfix()
        raw_name = constraint_spec.get('name') or str(index + 1)
        name = self._process_constraint_name(f'do_target_{raw_name}')
        self._register_target_actuator(kla, constraint_type='do_target', constraint_name=name)
        self._register_controlled_variable_unfix(oxygen, constraint_type='do_target', constraint_name=name)
        param_name = self._process_component_name('process_param', f'do_target_{raw_name}')
        setattr(self._flowsheet, param_name, Param(initialize=target_kg_m3, mutable=True))
        target_param = getattr(self._flowsheet, param_name)
        setattr(self._flowsheet, name, Constraint(expr=oxygen == target_param))
        try:
            self._session.config.runtime_metadata.setdefault('do_target_specs', []).append({'target_unit': str(target_unit_id), 'target_port': target_port_name, 'component': component, 'target_mg_l': float(target_mg_l), 'target_kg_m3': target_kg_m3, 'actuator_unit': str(actuator_unit_id), 'constraint_name': name})
        except Exception:
            pass

    def _apply_mlss_bounds(self, index: int, constraint_spec: Dict[str, Any]) -> None:
        """Apply template MLSS envelopes to reactor and MBR solids states."""
        component = str(constraint_spec.get('component') or 'X_TSS')
        lower = self._mg_l_bound_to_kg_m3(constraint_spec.get('lower_mg_l'))
        upper = self._mg_l_bound_to_kg_m3(constraint_spec.get('upper_mg_l'))
        target = self._mg_l_bound_to_kg_m3(constraint_spec.get('target_mg_l'))
        reactor_units = constraint_spec.get('reactor_units') or []
        if not reactor_units:
            raise ModelBuildError('MLSS bounds process constraint requires reactor_units')
        for unit_id in reactor_units:
            unit_block = self._units.get(unit_id)
            if unit_block is None:
                raise ModelBuildError(f'MLSS bounds reactor unit not found: {unit_id}')
            self._apply_port_concentration_bounds(getattr(unit_block, 'outlet', None), component, lower, upper, target, f'{unit_id}.outlet')
        cage_unit = constraint_spec.get('cage_unit')
        cage_port = constraint_spec.get('cage_port')
        if cage_unit and cage_port:
            unit_block = self._units.get(str(cage_unit))
            if unit_block is None:
                raise ModelBuildError(f'MLSS cage unit not found: {cage_unit}')
            self._apply_port_concentration_bounds(getattr(unit_block, str(cage_port), None), component, self._mg_l_bound_to_kg_m3(constraint_spec.get('cage_lower_mg_l')), self._mg_l_bound_to_kg_m3(constraint_spec.get('cage_upper_mg_l')), None, f'{cage_unit}.{cage_port}')

    def _mg_l_bound_to_kg_m3(self, value: Any) -> Optional[float]:
        if value is None:
            return None
        try:
            return float(value) / 1000.0
        except (TypeError, ValueError):
            return None

    def _apply_port_concentration_bounds(self, port: Any, component: str, lower: Optional[float], upper: Optional[float], target: Optional[float], label: str) -> None:
        if port is None or not hasattr(port, 'conc_mass_comp'):
            raise ModelBuildError(f'MLSS bounds port not found: {label}')
        concentrations = getattr(port, 'conc_mass_comp')
        var = self._indexed_component(concentrations, (0, component), (0.0, component), component)
        if var is None:
            raise ModelBuildError(f'MLSS component {component} not found on {label}')
        if target is not None:
            var.fix(target)
            return
        if lower is not None:
            var.setlb(lower)
        if upper is not None:
            var.setub(upper)

    def _add_recycle_ratio_constraint(self, index: int, constraint_spec: Dict[str, Any]) -> None:
        """Add a hydraulic recycle-ratio variable and closure equation."""
        from pyomo.environ import Constraint, Var
        ratio_name = constraint_spec.get('ratio_name') or constraint_spec.get('name')
        if not ratio_name:
            ratio_name = f'recycle_ratio_{index + 1}'
        numerator_unit = constraint_spec.get('numerator_unit')
        numerator_port_name = constraint_spec.get('numerator_port')
        denominator_unit = constraint_spec.get('denominator_unit')
        denominator_port_name = constraint_spec.get('denominator_port')
        if not numerator_unit or not numerator_port_name:
            raise ModelBuildError('Recycle-ratio constraint requires numerator_unit/port')
        if not denominator_unit or not denominator_port_name:
            raise ModelBuildError('Recycle-ratio constraint requires denominator_unit/port')
        numerator_port = self._unit_port(numerator_unit, numerator_port_name)
        denominator_port = self._unit_port(denominator_unit, denominator_port_name)
        numerator_flow = self._port_flow(numerator_port)
        denominator_flow = self._port_flow(denominator_port)
        lower = float(constraint_spec.get('lower_bound', 0.0))
        upper = float(constraint_spec.get('upper_bound', max(lower, 1.0)))
        target_ratio = constraint_spec.get('target_ratio')
        if target_ratio is not None:
            target_ratio = float(target_ratio)
            lower = min(lower, target_ratio)
            upper = max(upper, target_ratio)
        initial_value = constraint_spec.get('initial_value')
        if initial_value is None:
            initial_value = target_ratio if target_ratio is not None else (lower + upper) / 2.0
        initial_value = max(lower, min(upper, float(initial_value)))
        var_name = self._process_component_name('process_var', str(ratio_name))
        ratio_var = Var(initialize=initial_value, bounds=(lower, upper))
        setattr(self._flowsheet, var_name, ratio_var)
        ratio_var = getattr(self._flowsheet, var_name)
        if target_ratio is not None:
            ratio_var.fix(target_ratio)
        else:
            span = max(upper - lower, 1.0)
            self._free_recycle_ratio_objective_terms.append(((ratio_var - initial_value) / span) ** 2)
        constraint_name = self._process_constraint_name(str(ratio_name))
        setattr(self._flowsheet, constraint_name, Constraint(expr=numerator_flow == ratio_var * denominator_flow))

    def _free_chemical_p_target_doses(self) -> None:
        """Backward-compatible alias for older tests/callers."""
        self._free_target_control_actuators()

    def _chemical_p_dose_units(self, dose_unit_id: Optional[str]) -> List[Any]:
        """Return the ``metal_dose[0]`` Vars to free for chemical-P target control.

        If ``dose_unit_id`` is given only that unit is considered; otherwise every
        unit carrying a ``metal_dose`` Var (the ChemicalPhosphorusRemovalASM2D
        block) qualifies. Role-agnostic, mirroring the KLa search the aerobic
        DO<-KLa control uses.
        """
        if dose_unit_id:
            unit = self._units.get(str(dose_unit_id))
            candidates = {str(dose_unit_id): unit} if unit is not None else {}
        else:
            candidates = self._units
        dose_vars: List[Any] = []
        for unit in candidates.values():
            if unit is None:
                continue
            metal_dose = getattr(unit, 'metal_dose', None)
            if metal_dose is None:
                continue
            var = self._indexed_component(metal_dose, 0, 0.0)
            if var is not None:
                dose_vars.append(var)
        return dose_vars

    def _caustic_dose_units(self, dose_unit_id: Optional[str]) -> List[Any]:
        """Return ``caustic_dose[0]`` Vars available for alkalinity target control."""
        if dose_unit_id:
            unit = self._units.get(str(dose_unit_id))
            candidates = {str(dose_unit_id): unit} if unit is not None else {}
        else:
            candidates = self._units
        dose_vars: List[Any] = []
        for unit in candidates.values():
            if unit is None:
                continue
            caustic_dose = getattr(unit, 'caustic_dose', None)
            if caustic_dose is None:
                continue
            var = self._indexed_component(caustic_dose, 0, 0.0)
            if var is not None:
                dose_vars.append(var)
        return dose_vars

    def _carbon_dose_units(self, dose_unit_id: Optional[str]) -> List[Any]:
        """Return ``carbon_dose[0]`` Vars available for NO3 target control.

        If ``dose_unit_id`` is given only that unit is considered; otherwise every
        unit carrying a ``carbon_dose`` Var (the SupplementalCarbonDoseASM2D
        block) qualifies. Role-agnostic, mirroring the metal/caustic dose search
        the chemical-P / alkalinity controls use.
        """
        if dose_unit_id:
            unit = self._units.get(str(dose_unit_id))
            candidates = {str(dose_unit_id): unit} if unit is not None else {}
        else:
            candidates = self._units
        dose_vars: List[Any] = []
        for unit in candidates.values():
            if unit is None:
                continue
            carbon_dose = getattr(unit, 'carbon_dose', None)
            if carbon_dose is None:
                continue
            var = self._indexed_component(carbon_dose, 0, 0.0)
            if var is not None:
                dose_vars.append(var)
        return dose_vars

    def _adm1_base_dose_units(self, dose_unit_id: Optional[str]) -> List[Any]:
        """Return ``caustic_dose[0]`` Vars on ADM1BaseDose units (strong-base dose).

        The mADM1 base-dose analogue of ``_caustic_dose_units``. It filters to the
        ADM1BaseDose block (which carries both ``caustic_dose`` AND the
        ``cations_balance`` constraint) so the ADM1 pH controller never grabs an
        ASM-family ``AlkalinityDoseASM2D`` ``caustic_dose`` (which manipulates the
        native ``alkalinity``/``S_IC`` coordinate, not the mADM1 strong-ion
        ``cations``). Role-agnostic, mirroring the metal/caustic dose search the
        chemical-P / alkalinity controls use.
        """
        if dose_unit_id:
            unit = self._units.get(str(dose_unit_id))
            candidates = {str(dose_unit_id): unit} if unit is not None else {}
        else:
            candidates = self._units
        dose_vars: List[Any] = []
        for unit in candidates.values():
            if unit is None:
                continue
            caustic_dose = getattr(unit, 'caustic_dose', None)
            if caustic_dose is None or not hasattr(unit, 'cations_balance'):
                continue
            var = self._indexed_component(caustic_dose, 0, 0.0)
            if var is not None:
                dose_vars.append(var)
        return dose_vars

    def _adm1_mg_dose_units(self, dose_unit_id: Optional[str]) -> List[Any]:
        """Return ``mg_dose[0]`` Vars on ADM1StruvitePRemoval units (struvite dose).

        The mADM1 soluble-P analogue of ``_chemical_p_dose_units``. ``mg_dose`` is
        unique to the struvite block, so no class filtering is required.
        """
        if dose_unit_id:
            unit = self._units.get(str(dose_unit_id))
            candidates = {str(dose_unit_id): unit} if unit is not None else {}
        else:
            candidates = self._units
        dose_vars: List[Any] = []
        for unit in candidates.values():
            if unit is None:
                continue
            mg_dose = getattr(unit, 'mg_dose', None)
            if mg_dose is None:
                continue
            var = self._indexed_component(mg_dose, 0, 0.0)
            if var is not None:
                dose_vars.append(var)
        return dose_vars

    def _adm1_reaction_ph(self, unit_id: str, t: Any=0.0) -> Any:
        """Return the modified-ADM1 reaction-block ``pH`` Var for a digester.

        The reaction block lives on the AD liquid control volume
        (``liquid_phase.reactions[t]``); its ``pH`` Var is solved from the
        acid/base electroneutrality constraint ``S_H_cons`` against the liquid
        state's strong-ion difference (``cations`` / ``anions``). This is the new
        ADM1 pH target coordinate (electroneutrality-solved pH), NOT the ASM-family
        ``alkalinity`` / ``S_ALK`` coordinate.
        """
        unit_block = self._units.get(str(unit_id))
        if unit_block is None:
            raise ModelBuildError(f'adm1_ph_target unit not found: {unit_id}')
        liquid_phase = getattr(unit_block, 'liquid_phase', None)
        reactions = getattr(liquid_phase, 'reactions', None) if liquid_phase else None
        reaction_block = self._indexed_component(reactions, t, 0.0)
        if reaction_block is None or not hasattr(reaction_block, 'pH'):
            raise ModelBuildError(f'adm1_ph_target unit {unit_id} exposes no liquid_phase.reactions[t].pH (not an ADM1 digester reaction block)')
        return reaction_block.pH

    def _adm1_strong_ion_alkalinity(self, port: Any) -> Any:
        """Return the strong-ion alkalinity surrogate (cations - anions) on a port.

        A reaction-free alkalinity proxy for callers that target a port's strong-ion
        difference instead of the digester reaction pH. ``cations`` / ``anions`` are
        scalar state vars carried in the mADM1 ``define_state_vars`` set, so they are
        exposed on the connected port.
        """
        cations = getattr(port, 'cations', None)
        anions = getattr(port, 'anions', None)
        if cations is None or anions is None:
            raise ModelBuildError(f'Port {getattr(port, 'name', port)} has no cations/anions strong-ion coordinate for the adm1 strong-ion alkalinity surrogate')
        cations_var = self._indexed_component(cations, 0, 0.0, None)
        anions_var = self._indexed_component(anions, 0, 0.0, None)
        if cations_var is None or anions_var is None:
            raise ModelBuildError(f'Port {getattr(port, 'name', port)} cations/anions are not resolvable')
        return cations_var - anions_var

    def _port_alkalinity(self, port: Any, component: Optional[str]=None) -> Any:
        """Return the targetable alkalinity coordinate on a port."""
        alkalinity = getattr(port, 'alkalinity', None)
        if alkalinity is not None:
            var = self._indexed_component(alkalinity, 0, 0.0, None)
            if var is not None:
                return var
        concentrations = getattr(port, 'conc_mass_comp', None)
        if concentrations is None:
            raise ModelBuildError(f'Port {port.name} has no alkalinity coordinate')
        candidates = [component] if component else ['S_IC', 'S_ALK']
        for candidate in candidates:
            if not candidate:
                continue
            var = self._indexed_component(concentrations, (0, candidate), (0.0, candidate), candidate)
            if var is not None:
                return var
        raise ModelBuildError(f'Port {port.name} has no alkalinity/S_IC/S_ALK coordinate')

    @staticmethod
    def _alkalinity_target_value(constraint_spec: Dict[str, Any]) -> float:
        """Return target alkalinity in the engine's native mol/m^3 convention."""
        for key in ('target_mol_per_m3', 'target_alkalinity_mol_per_m3', 'target_alkalinity', 'target_value'):
            if constraint_spec.get(key) is not None:
                return float(constraint_spec[key])
        for key in ('target_mg_caco3_l', 'target_mg_l_as_caco3', 'target_alkalinity_mg_caco3_l'):
            if constraint_spec.get(key) is not None:
                return float(constraint_spec[key]) / 50.04
        if constraint_spec.get('target_mg_l') is not None:
            basis = str(constraint_spec.get('basis', 'caco3')).lower()
            value_mg_l = float(constraint_spec['target_mg_l'])
            if basis in ('caco3', 'as_caco3', 'mg_caco3_l'):
                return value_mg_l / 50.04
            return value_mg_l / 1000.0
        raise ModelBuildError('alkalinity_target requires target_mol_per_m3 or target_mg_caco3_l')

    def _add_alkalinity_target_constraint(self, index: int, constraint_spec: Dict[str, Any]) -> None:
        """Pin alkalinity by freeing a caustic/NaOH-equivalent dose."""
        from pyomo.environ import Constraint, Param, value
        constraint_kind = str(constraint_spec.get('constraint_kind', 'min')).lower()
        exact_kinds = {'eq', 'equality', 'equal', 'exact', 'target'}
        min_kinds = {'min', 'minimum', 'lower_bound', 'at_least', 'ge', '>='}
        if constraint_kind not in exact_kinds | min_kinds:
            raise ModelBuildError(f"alkalinity_target constraint_kind must be 'exact' or 'min'; got {constraint_kind!r}")
        is_minimum_target = constraint_kind in min_kinds
        target_value = self._alkalinity_target_value(constraint_spec)
        if target_value < 0:
            raise ModelBuildError('alkalinity_target must be non-negative')
        dose_unit_id = constraint_spec.get('dose_unit') or constraint_spec.get('caustic_unit')
        dose_vars = self._caustic_dose_units(dose_unit_id)
        if not dose_vars:
            raise ModelBuildError('alkalinity_target constraint found no unit carrying a caustic_dose var' + (f' named {dose_unit_id!r}' if dose_unit_id else ''))
        if dose_unit_id is None and len(dose_vars) != 1:
            raise ModelBuildError(f'alkalinity_target without dose_unit must find exactly one caustic_dose actuator; found {len(dose_vars)}')
        dose_lower = float(constraint_spec.get('dose_lower', 0.0))
        dose_upper = float(constraint_spec.get('dose_upper', 50.0))
        if dose_upper <= dose_lower:
            raise ModelBuildError('alkalinity_target dose_upper must exceed dose_lower')
        raw_name = constraint_spec.get('name') or str(index + 1)
        constraint_name = self._process_constraint_name(f'alkalinity_target_{raw_name}')
        explicit_seed = constraint_spec.get('dose_seed')
        build_seed = None
        if explicit_seed is not None:
            try:
                build_seed = min(max(float(explicit_seed), dose_lower), dose_upper)
            except (TypeError, ValueError):
                build_seed = None
        for dose_var in dose_vars:
            seed = value(dose_var, exception=False)
            dose_var.setlb(dose_lower)
            dose_var.setub(dose_upper)
            if build_seed is not None:
                dose_var.set_value(build_seed)
            elif seed is None or seed < dose_lower or seed > dose_upper:
                dose_var.set_value(min(max(0.0, dose_lower), dose_upper))
            if dose_var.fixed:
                dose_var.unfix()
            self._register_target_actuator(dose_var, constraint_type='alkalinity_target', constraint_name=constraint_name, build_seed=build_seed)
            if is_minimum_target:
                span = max(dose_upper - dose_lower, abs(target_value), 1.0)
                weight = float(constraint_spec.get('dose_objective_weight', 1000000.0))
                self._free_recycle_ratio_objective_terms.append(weight * dose_var / span)
                try:
                    self._session.config.runtime_metadata.setdefault('alkalinity_min_dose_actuators', []).append({'variable': dose_var.name, 'constraint_name': constraint_name, 'dose_unit': dose_unit_id, 'objective_scale': span, 'objective_weight': weight})
                except Exception:
                    pass
        target_unit = constraint_spec.get('target_unit')
        target_port_name = str(constraint_spec.get('target_port', 'outlet'))
        if not target_unit:
            raise ModelBuildError('alkalinity_target constraint requires target_unit')
        target_port = self._unit_port(str(target_unit), target_port_name)
        target_expr = self._port_alkalinity(target_port, constraint_spec.get('alkalinity_component'))
        param_name = self._process_component_name('process_param', f'alkalinity_target_{raw_name}')
        setattr(self._flowsheet, param_name, Param(initialize=target_value, mutable=True))
        target_param = getattr(self._flowsheet, param_name)
        setattr(self._flowsheet, constraint_name, Constraint(expr=target_expr >= target_param if is_minimum_target else target_expr == target_param))
        seed_spec: Dict[str, Any] = {'dose_unit': dose_unit_id, 'target_unit': str(target_unit), 'target_port': target_port_name, 'target_mol_per_m3': float(target_value), 'dose_lower': dose_lower, 'dose_upper': dose_upper, 'constraint_name': constraint_name, 'constraint_kind': 'min' if is_minimum_target else 'exact'}
        if explicit_seed is not None:
            try:
                seed_spec['dose_seed'] = float(explicit_seed)
            except (TypeError, ValueError):
                pass
        try:
            self._session.config.runtime_metadata.setdefault('alkalinity_target_specs', []).append(seed_spec)
        except Exception:
            pass

    def _add_adm1_ph_alkalinity_target_constraint(self, index: int, constraint_spec: Dict[str, Any]) -> None:
        """Pin an ADM1 digester pH (or strong-ion alkalinity) by freeing a base dose.

        The mADM1 base/pH analogue of the ASM ``alkalinity_target`` "pin Y, free X"
        swap. Unlike the ASM control (which manipulates the native ``alkalinity`` /
        ``S_IC`` coordinate), modified-ADM1 has no native alkalinity state: it solves
        pH from electroneutrality. The manipulated variable is therefore an
        ``ADM1BaseDose`` ``caustic_dose`` (a strong-cation / NaOH-equivalent addition
        to the mADM1 ``cations`` pool), and the controlled metric is — by default —
        the DOWNSTREAM digester reaction-block ``pH`` solved by ``S_H_cons``
        electroneutrality against the dosed strong-ion difference. Raising the cation
        pool raises alkalinity, hence pH; the swap is DOF-neutral (dose freed +1, pH
        target equation -1).

        The TARGET COORDINATE is the new part: ``target_metric='ph'`` (default) pins
        ``target_unit.liquid_phase.reactions[t].pH`` (the electroneutrality-solved
        digester pH), NOT an alkalinity/S_ALK coordinate. ``target_metric=
        'strong_ion_alkalinity'`` instead pins the strong-ion alkalinity surrogate
        (``cations - anions``) on a named port — a reaction-free proxy.

        EXACT-only (DOF-neutral): a pH/alkalinity FLOOR (min kind) would need a
        generic min-dose objective registered with the pipeline DOF gate; the ADM1
        family ships the exact swap (consistent with the temperature_target / SRT
        ADM1 controls). Credibility stays screening (the mADM1 kinetics are constant-
        parameter; this controls the energy-free acid/base electroneutrality).
        """
        from pyomo.environ import Constraint, Param, value
        constraint_kind = str(constraint_spec.get('constraint_kind', 'exact')).lower()
        exact_kinds = {'eq', 'equality', 'equal', 'exact', 'target'}
        if constraint_kind not in exact_kinds:
            raise ModelBuildError(f"adm1_ph_target supports constraint_kind 'exact' only (a pH/alkalinity floor would need a min-dose objective wired to the pipeline DOF gate); got {constraint_kind!r}")
        target_metric = str(constraint_spec.get('target_metric') or constraint_spec.get('metric') or 'ph').lower()
        dose_unit_id = constraint_spec.get('dose_unit') or constraint_spec.get('base_dose_unit')
        dose_vars = self._adm1_base_dose_units(dose_unit_id)
        if not dose_vars:
            raise ModelBuildError('adm1_ph_target constraint found no ADM1BaseDose unit carrying a caustic_dose var' + (f' named {dose_unit_id!r}' if dose_unit_id else ''))
        if dose_unit_id is None and len(dose_vars) != 1:
            raise ModelBuildError(f'adm1_ph_target without dose_unit must find exactly one ADM1BaseDose caustic_dose actuator; found {len(dose_vars)}')
        dose_lower = float(constraint_spec.get('dose_lower', -10.0))
        dose_upper = float(constraint_spec.get('dose_upper', 50.0))
        if dose_upper <= dose_lower:
            raise ModelBuildError('adm1_ph_target dose_upper must exceed dose_lower')
        raw_name = constraint_spec.get('name') or str(index + 1)
        constraint_name = self._process_constraint_name(f'adm1_ph_target_{raw_name}')
        explicit_seed = constraint_spec.get('dose_seed')
        build_seed = None
        if explicit_seed is not None:
            try:
                build_seed = min(max(float(explicit_seed), dose_lower), dose_upper)
            except (TypeError, ValueError):
                build_seed = None
        for dose_var in dose_vars:
            seed = value(dose_var, exception=False)
            dose_var.setlb(dose_lower)
            dose_var.setub(dose_upper)
            if build_seed is not None:
                dose_var.set_value(build_seed)
            elif seed is None or seed < dose_lower or seed > dose_upper:
                dose_var.set_value(min(max(0.0, dose_lower), dose_upper))
            if dose_var.fixed:
                dose_var.unfix()
            self._register_target_actuator(dose_var, constraint_type='adm1_ph_target', constraint_name=constraint_name, build_seed=build_seed)
        target_unit = constraint_spec.get('target_unit') or constraint_spec.get('unit')
        if not target_unit:
            raise ModelBuildError('adm1_ph_target constraint requires target_unit (the digester whose reaction pH is controlled, or the port unit for the strong-ion alkalinity surrogate)')
        if target_metric in ('ph', 'reaction_ph', 'digester_ph'):
            t = constraint_spec.get('time', 0.0)
            target_expr = self._adm1_reaction_ph(str(target_unit), t=t)
            target_value = None
            for key in ('target_ph', 'target_pH', 'target_value', 'target'):
                if constraint_spec.get(key) is not None:
                    target_value = float(constraint_spec[key])
                    break
            if target_value is None:
                raise ModelBuildError('adm1_ph_target (pH metric) requires target_ph (dimensionless pH)')
            if not 0.0 < target_value < 14.0:
                raise ModelBuildError(f'adm1_ph_target target_ph must be a physical pH (0-14); got {target_value}')
        elif target_metric in ('strong_ion_alkalinity', 'strong_ion_alk', 'alkalinity_surrogate'):
            target_port_name = str(constraint_spec.get('target_port', 'outlet'))
            target_port = self._unit_port(str(target_unit), target_port_name)
            target_expr = self._adm1_strong_ion_alkalinity(target_port)
            target_value = None
            for key in ('target_strong_ion_alk_kmol_m3', 'target_kmol_per_m3', 'target_value', 'target'):
                if constraint_spec.get(key) is not None:
                    target_value = float(constraint_spec[key])
                    break
            if target_value is None:
                raise ModelBuildError('adm1_ph_target (strong_ion_alkalinity metric) requires target_strong_ion_alk_kmol_m3 (cations - anions, kmol/m^3)')
        else:
            raise ModelBuildError(f"adm1_ph_target target_metric must be 'ph' or 'strong_ion_alkalinity'; got {target_metric!r}")
        param_name = self._process_component_name('process_param', f'adm1_ph_target_{raw_name}')
        setattr(self._flowsheet, param_name, Param(initialize=target_value, mutable=True))
        target_param = getattr(self._flowsheet, param_name)
        setattr(self._flowsheet, constraint_name, Constraint(expr=target_expr == target_param))
        try:
            self._session.config.runtime_metadata.setdefault('adm1_ph_target_specs', []).append({'dose_unit': dose_unit_id, 'target_unit': str(target_unit), 'target_metric': target_metric, 'target_value': float(target_value), 'dose_lower': dose_lower, 'dose_upper': dose_upper, 'constraint_name': constraint_name, 'credibility': 'screening'})
        except Exception:
            pass

    def _add_adm1_metal_target_constraint(self, index: int, constraint_spec: Dict[str, Any]) -> None:
        """Pin an ADM1 soluble inorganic-P (S_IP) by freeing a struvite (Mg) dose.

        The mADM1 soluble-P analogue of the ASM ``chemical_p_target`` swap: free the
        ``ADM1StruvitePRemoval`` ``mg_dose`` and pin the soluble inorganic phosphorus
        ``S_IP`` at a named port to a target. DOF-neutral (dose freed +1, S_IP target
        equation -1).

        HONESTY about the modified-ADM1 package limits:

          * **Soluble-P / struvite proxy (supported, this path).** The struvite unit
            removes soluble ``S_IP`` at the Mg:P ratio; the precipitated struvite is
            NOT carried as an mADM1 solid (no metal-phosphate / struvite particulate
            species exists), so the removed P leaves the modeled liquid and total P
            does not close inside mADM1's component set. The controlled metric is the
            liquid soluble ``S_IP``.
          * **Biogas H2S (NOT in the package) is REJECTED, not faked.** modified-ADM1
            has no sulfur/iron state and no vapor H2S species (the vapor package is
            H2O / S_h2 / S_ch4 / S_co2; ``Z_h2s`` is a constant parameter defaulted to
            0), so there is no H2S balance to manipulate. An H2S target raises a
            structured ModelBuildError rather than dressing a P-only unit as an H2S
            controller.

        Credibility stays screening (the struvite proxy is a deterministic removal
        surrogate, not a thermodynamic precipitation equilibrium).
        """
        from pyomo.environ import Constraint, Param, value
        target_metric = str(constraint_spec.get('target_metric') or constraint_spec.get('metric') or 'soluble_p').lower()
        h2s_requested = target_metric in ('biogas_h2s', 'h2s', 'biogas_sulfide', 'sulfide') or constraint_spec.get('target_h2s_ppm') is not None or constraint_spec.get('target_h2s_mg_m3') is not None
        if h2s_requested:
            raise ModelBuildError("adm1_metal_target: biogas H2S control requires a sulfur/iron state extension not in modified-ADM1 (the package has no sulfur/iron species and no vapor H2S — Z_h2s is a constant parameter defaulted to 0). The struvite/metal dose unit models soluble-P removal only; an H2S/FeS balance is not faked. Use target_metric='soluble_p'.")
        if target_metric not in ('soluble_p', 's_ip', 'soluble_inorganic_p', 'struvite_p'):
            raise ModelBuildError(f"adm1_metal_target target_metric must be 'soluble_p' (S_IP); got {target_metric!r}")
        constraint_kind = str(constraint_spec.get('constraint_kind', 'exact')).lower()
        exact_kinds = {'eq', 'equality', 'equal', 'exact', 'target'}
        if constraint_kind not in exact_kinds:
            raise ModelBuildError(f"adm1_metal_target supports constraint_kind 'exact' only; got {constraint_kind!r}")
        dose_unit_id = constraint_spec.get('dose_unit') or constraint_spec.get('mg_dose_unit')
        dose_vars = self._adm1_mg_dose_units(dose_unit_id)
        if not dose_vars:
            raise ModelBuildError('adm1_metal_target constraint found no ADM1StruvitePRemoval unit carrying an mg_dose var' + (f' named {dose_unit_id!r}' if dose_unit_id else ''))
        if dose_unit_id is None and len(dose_vars) != 1:
            raise ModelBuildError(f'adm1_metal_target without dose_unit must find exactly one ADM1StruvitePRemoval mg_dose actuator; found {len(dose_vars)}')
        dose_lower = float(constraint_spec.get('dose_lower', 0.0))
        dose_upper = float(constraint_spec.get('dose_upper', 5.0))
        if dose_upper <= dose_lower:
            raise ModelBuildError('adm1_metal_target dose_upper must exceed dose_lower')
        raw_name = constraint_spec.get('name') or str(index + 1)
        constraint_name = self._process_constraint_name(f'adm1_metal_target_{raw_name}')
        explicit_seed = constraint_spec.get('dose_seed')
        build_seed = None
        if explicit_seed is not None:
            try:
                build_seed = min(max(float(explicit_seed), dose_lower), dose_upper)
            except (TypeError, ValueError):
                build_seed = None
        for dose_var in dose_vars:
            seed = value(dose_var, exception=False)
            dose_var.setlb(dose_lower)
            dose_var.setub(dose_upper)
            if build_seed is not None:
                dose_var.set_value(build_seed)
            elif seed is None or seed < dose_lower or seed > dose_upper:
                dose_var.set_value(min(max(0.0, dose_lower), dose_upper))
            if dose_var.fixed:
                dose_var.unfix()
            self._register_target_actuator(dose_var, constraint_type='adm1_metal_target', constraint_name=constraint_name, build_seed=build_seed)
        target_unit = constraint_spec.get('target_unit') or constraint_spec.get('unit')
        target_port_name = str(constraint_spec.get('target_port', 'outlet'))
        if not target_unit:
            raise ModelBuildError('adm1_metal_target constraint requires target_unit (the unit whose port carries the soluble-P compliance metric)')
        target_port = self._unit_port(str(target_unit), target_port_name)
        target_expr = self._port_concentration(target_port, 'S_IP')
        target_mg_l = None
        for key in ('target_mg_l', 'target_s_ip_mg_l', 'target_soluble_p_mg_l'):
            if constraint_spec.get(key) is not None:
                target_mg_l = float(constraint_spec[key])
                break
        if target_mg_l is not None:
            target_kg_m3 = target_mg_l / 1000.0
        else:
            for key in ('target_kg_m3', 'target_value', 'target'):
                if constraint_spec.get(key) is not None:
                    target_kg_m3 = float(constraint_spec[key])
                    break
            else:
                raise ModelBuildError('adm1_metal_target requires target_mg_l (soluble inorganic P, mg/L as P) or target_kg_m3')
        if target_kg_m3 < 0:
            raise ModelBuildError('adm1_metal_target target must be non-negative')
        param_name = self._process_component_name('process_param', f'adm1_metal_target_{raw_name}')
        setattr(self._flowsheet, param_name, Param(initialize=target_kg_m3, mutable=True))
        target_param = getattr(self._flowsheet, param_name)
        setattr(self._flowsheet, constraint_name, Constraint(expr=target_expr == target_param))
        try:
            self._session.config.runtime_metadata.setdefault('adm1_metal_target_specs', []).append({'dose_unit': dose_unit_id, 'target_unit': str(target_unit), 'target_port': target_port_name, 'target_metric': 'soluble_p', 'target_kg_m3': float(target_kg_m3), 'dose_lower': dose_lower, 'dose_upper': dose_upper, 'constraint_name': constraint_name, 'credibility': 'screening', 'honesty': 'soluble-P struvite proxy; struvite solid not tracked in mADM1; no biogas H2S/FeS state in the package'})
        except Exception:
            pass

    def _resolve_single_var(self, unit_id: str, var_path: str) -> Optional[Any]:
        """Resolve ``unit_id.var_path`` to a single scalar Pyomo VarData.

        Promotes :meth:`_resolve_variable_path` into a "one variable" accessor for
        the generic ``raw_var_target`` swap: returns the resolved scalar variable
        (or ``None`` when the path does not resolve or resolves to a wildcard set,
        which is ambiguous for a single actuator/target).
        """
        unit = self._units.get(unit_id)
        if unit is None:
            return None
        resolved, resolved_index = self._resolve_variable_path(unit, var_path)
        if resolved is None or isinstance(resolved, list):
            return None
        if resolved_index is not None:
            try:
                return resolved[resolved_index]
            except Exception:
                return None
        return resolved

    def _add_raw_var_target_constraint(self, index: int, constraint_spec: Dict[str, Any]) -> None:
        """General "pin Y, free X" spec swap over arbitrary model variables.

        The fully general analogue of the SRT/DO/alkalinity/chemical-P controls:
        free one explicitly named actuator variable (``actuator_unit`` +
        ``actuator_var_path``) and pin one explicitly named target variable
        (``target_unit`` + ``target_var_path``) to a value. This adds one equation
        and frees one variable — DOF-neutral.

        PR1 supports ``constraint_kind='exact'`` only: a min/max inequality on an
        arbitrary var needs a generic objective-actuator registered with the
        solver/pipeline DOF gate (which today only admits the recycle-ratio and
        alkalinity-min actuators it recognizes), so an arbitrary raw-var floor/cap
        would be ill-posed and is rejected here.
        """
        from pyomo.environ import Constraint, Param, value
        kind = str(constraint_spec.get('constraint_kind', 'exact')).lower()
        exact_kinds = {'exact', 'eq', 'equality', 'equal', 'target'}
        if kind not in exact_kinds:
            raise ModelBuildError(f"raw_var_target supports constraint_kind 'exact' only in PR1 (min/max floors/caps are not wired for raw_var); got {kind!r}")
        target_value = constraint_spec.get('target_value')
        if target_value is None:
            raise ModelBuildError('raw_var_target requires target_value')
        target_value = float(target_value)
        actuator_unit = constraint_spec.get('actuator_unit')
        actuator_path = constraint_spec.get('actuator_var_path')
        if not actuator_unit or not actuator_path:
            raise ModelBuildError('raw_var_target requires actuator_unit + actuator_var_path')
        actuator_var = self._resolve_single_var(str(actuator_unit), str(actuator_path))
        if actuator_var is None:
            raise ModelBuildError(f'raw_var_target actuator not found: {actuator_unit}.{actuator_path}')
        a_lower = constraint_spec.get('actuator_lower')
        a_upper = constraint_spec.get('actuator_upper')
        if a_lower is not None:
            actuator_var.setlb(float(a_lower))
        if a_upper is not None:
            actuator_var.setub(float(a_upper))
        seed = constraint_spec.get('actuator_seed')
        if seed is not None:
            try:
                actuator_var.set_value(float(seed))
            except Exception:
                pass
        if getattr(actuator_var, 'fixed', False):
            actuator_var.unfix()
        raw_name = constraint_spec.get('name') or str(index + 1)
        constraint_name = self._process_constraint_name(f'raw_var_target_{raw_name}')
        self._register_target_actuator(actuator_var, constraint_type='raw_var_target', constraint_name=constraint_name, build_seed=None if seed is None else float(seed))
        target_unit = constraint_spec.get('target_unit')
        target_path = constraint_spec.get('target_var_path')
        if not target_unit or not target_path:
            raise ModelBuildError('raw_var_target requires target_unit + target_var_path')
        target_var = self._resolve_single_var(str(target_unit), str(target_path))
        if target_var is None:
            raise ModelBuildError(f'raw_var_target target not found: {target_unit}.{target_path}')
        param_name = self._process_component_name('process_param', f'raw_var_target_{raw_name}')
        setattr(self._flowsheet, param_name, Param(initialize=target_value, mutable=True))
        target_param = getattr(self._flowsheet, param_name)
        setattr(self._flowsheet, constraint_name, Constraint(expr=target_var == target_param))

    def _add_chemical_p_target_constraint(self, index: int, constraint_spec: Dict[str, Any]) -> None:
        """Pin effluent total P (or ortho-P) to a target by freeing the metal dose.

        The chemical-P analogue of SRT<-WAS and DO<-KLa: instead of a hand-fixed
        ferric/alum dose, the dose becomes the *manipulated* variable that the
        solver tunes so the compliance metric — effluent total phosphorus at the
        named permeate/product port — meets the target. The swap is DOF-neutral:
        this adds one equation (the target) and frees one variable (metal_dose),
        so a square model stays square. When no chemical_p_target constraint is
        present the dose stays fixed and behaviour is unchanged.
        """
        from pyomo.environ import Constraint, value
        target_mg_l = constraint_spec.get('target_mg_l') if constraint_spec.get('target_mg_l') is not None else constraint_spec.get('target_tp_mg_l') if constraint_spec.get('target_tp_mg_l') is not None else constraint_spec.get('target_effluent_tp_mg_l')
        if target_mg_l is None:
            raise ModelBuildError('chemical_p_target constraint requires target_mg_l (effluent total P, mg/L)')
        target_kg_m3 = float(target_mg_l) / 1000.0
        if target_kg_m3 <= 0:
            raise ModelBuildError('chemical_p_target target_mg_l must be positive')
        dose_unit_id = constraint_spec.get('dose_unit')
        dose_vars = self._chemical_p_dose_units(dose_unit_id)
        if not dose_vars:
            raise ModelBuildError('chemical_p_target constraint found no unit carrying a metal_dose var' + (f' named {dose_unit_id!r}' if dose_unit_id else ''))
        if dose_unit_id is None and len(dose_vars) != 1:
            raise ModelBuildError(f'chemical_p_target without dose_unit must find exactly one metal_dose actuator; found {len(dose_vars)}')
        dose_lower = float(constraint_spec.get('dose_lower', 0.0))
        dose_upper = float(constraint_spec.get('dose_upper', 0.5))
        if dose_upper <= dose_lower:
            raise ModelBuildError('chemical_p_target dose_upper must exceed dose_lower')
        raw_name = constraint_spec.get('name') or str(index + 1)
        constraint_name = self._process_constraint_name(f'chemical_p_target_{raw_name}')
        explicit_seed = constraint_spec.get('dose_seed')
        seed_post_bio = constraint_spec.get('seed_post_bio_soluble_p_mg_l')
        build_seed = self._chemical_p_build_seed(explicit_seed, seed_post_bio, target_mg_l, dose_lower, dose_upper)
        for dose_var in dose_vars:
            seed = value(dose_var, exception=False)
            dose_var.setlb(dose_lower)
            dose_var.setub(dose_upper)
            if build_seed is not None:
                dose_var.set_value(build_seed)
            elif seed is None or seed < dose_lower or seed > dose_upper:
                dose_var.set_value(min(max(0.0075, dose_lower), dose_upper))
            if dose_var.fixed:
                dose_var.unfix()
            self._chemical_p_target_freed_doses.append((dose_var, build_seed))
            self._register_target_actuator(dose_var, constraint_type='chemical_p_target', constraint_name=constraint_name, build_seed=build_seed)
        seed_spec: Dict[str, Any] = {'dose_unit': dose_unit_id, 'target_mg_l': float(target_mg_l), 'p_basis': str(constraint_spec.get('p_basis', 'total_p')).lower(), 'dose_lower': dose_lower, 'dose_upper': dose_upper}
        if explicit_seed is not None:
            try:
                seed_spec['dose_seed'] = float(explicit_seed)
            except (TypeError, ValueError):
                pass
        if seed_post_bio is not None:
            try:
                seed_spec['seed_post_bio_soluble_p_mg_l'] = float(seed_post_bio)
            except (TypeError, ValueError):
                pass
        try:
            specs = self._session.config.runtime_metadata.setdefault('chemical_p_target_specs', [])
            specs.append(seed_spec)
        except Exception:
            pass
        target_unit = constraint_spec.get('target_unit')
        target_port_name = str(constraint_spec.get('target_port', 'inlet'))
        if not target_unit:
            raise ModelBuildError('chemical_p_target constraint requires target_unit (the effluent/permeate unit whose port carries the compliance metric)')
        target_port = self._unit_port(str(target_unit), target_port_name)

        def _conc(component: str) -> Any:
            try:
                return self._port_concentration(target_port, component)
            except ModelBuildError:
                return 0.0
        p_basis = str(constraint_spec.get('p_basis', 'total_p')).lower()
        if p_basis in ('ortho_p', 'ortho-p', 's_po4', 'soluble_p'):
            target_expr = _conc('S_PO4')
        else:
            from watertap_engine_mcp.units.chemical_p_asm2d import asm2d_total_p_expr
            target_expr = asm2d_total_p_expr(_conc)
        from pyomo.environ import Param
        param_name = self._process_component_name('process_param', f'chemical_p_target_{raw_name}')
        setattr(self._flowsheet, param_name, Param(initialize=target_kg_m3, mutable=True))
        target_param = getattr(self._flowsheet, param_name)
        setattr(self._flowsheet, constraint_name, Constraint(expr=target_expr == target_param))

    @staticmethod
    def _chemical_p_build_seed(explicit_seed: Any, seed_post_bio_soluble_p_mg_l: Any, target_mg_l: float, dose_lower: float, dose_upper: float) -> Optional[float]:
        """Cold-start metal_dose seed from an explicit value or a post-bio hint.

        Returns the engine-basis seed (kg Fe(OH)3-eq/m^3) when the spec carries
        an explicit ``dose_seed`` (opt-out, clipped to bounds) or a
        ``seed_post_bio_soluble_p_mg_l`` hint (seeded at the water-chemistry
        equivalence dose via engineering_utils.dosing). Returns None when neither
        is given, leaving the caller's existing-value/floor logic in charge — the
        initializer reseeds from the settled inlet regardless.
        """
        if explicit_seed is not None:
            try:
                return min(max(float(explicit_seed), dose_lower), dose_upper)
            except (TypeError, ValueError):
                return None
        if seed_post_bio_soluble_p_mg_l is None:
            return None
        try:
            from puran_mcp_support.engineering.dosing import engine_metal_dose_seed
        except Exception:
            return None
        try:
            return engine_metal_dose_seed(float(seed_post_bio_soluble_p_mg_l), float(target_mg_l), dose_lower=dose_lower, dose_upper=dose_upper)
        except Exception:
            return None

    def _add_no3_target_constraint(self, index: int, constraint_spec: Dict[str, Any]) -> None:
        """Pin effluent (or anoxic-zone) nitrate by freeing a supplemental carbon dose.

        The denitrification analogue of chemical_p_target (effluent TP <- metal
        dose) and do_target (outlet DO <- KLa): instead of a hand-fixed external
        carbon dose, the dose becomes the *manipulated* variable the solver tunes
        so the compliance metric — effluent S_NO3 at the named product/anoxic port
        — meets the target. The added carbon enters as S_A (rbCOD) and the ASM2D
        heterotroph denitrification kinetics consume it to reduce S_NO3. The swap
        is DOF-neutral: this adds one equation (the target) and frees one variable
        (carbon_dose), so a square model stays square. When no no3_target
        constraint is present the dose stays fixed and behaviour is unchanged.
        """
        from pyomo.environ import Constraint, Param, value
        target_mg_l = constraint_spec.get('target_mg_l') if constraint_spec.get('target_mg_l') is not None else constraint_spec.get('target_no3_mg_l') if constraint_spec.get('target_no3_mg_l') is not None else constraint_spec.get('target_effluent_no3_mg_l')
        if target_mg_l is None:
            raise ModelBuildError('no3_target constraint requires target_mg_l (effluent NO3-N, mg/L)')
        target_kg_m3 = float(target_mg_l) / 1000.0
        if target_kg_m3 < 0:
            raise ModelBuildError('no3_target target_mg_l must be non-negative')
        dose_unit_id = constraint_spec.get('dose_unit') or constraint_spec.get('carbon_unit')
        dose_vars = self._carbon_dose_units(dose_unit_id)
        if not dose_vars:
            raise ModelBuildError('no3_target constraint found no unit carrying a carbon_dose var' + (f' named {dose_unit_id!r}' if dose_unit_id else ''))
        if dose_unit_id is None and len(dose_vars) != 1:
            raise ModelBuildError(f'no3_target without dose_unit must find exactly one carbon_dose actuator; found {len(dose_vars)}')
        dose_lower = float(constraint_spec.get('dose_lower', 0.0))
        dose_upper = float(constraint_spec.get('dose_upper', 2.0))
        if dose_upper <= dose_lower:
            raise ModelBuildError('no3_target dose_upper must exceed dose_lower')
        raw_name = constraint_spec.get('name') or str(index + 1)
        constraint_name = self._process_constraint_name(f'no3_target_{raw_name}')
        explicit_seed = constraint_spec.get('dose_seed')
        seed_inlet_no3 = constraint_spec.get('seed_inlet_no3_mg_l')
        build_seed = self._no3_target_build_seed(explicit_seed, seed_inlet_no3, target_mg_l, dose_lower, dose_upper)
        for dose_var in dose_vars:
            seed = value(dose_var, exception=False)
            dose_var.setlb(dose_lower)
            dose_var.setub(dose_upper)
            if build_seed is not None:
                dose_var.set_value(build_seed)
            elif seed is None or seed < dose_lower or seed > dose_upper:
                dose_var.set_value(min(max(0.0, dose_lower), dose_upper))
            if dose_var.fixed:
                dose_var.unfix()
            self._register_target_actuator(dose_var, constraint_type='no3_target', constraint_name=constraint_name, build_seed=build_seed)
        seed_spec: Dict[str, Any] = {'dose_unit': dose_unit_id, 'target_mg_l': float(target_mg_l), 'dose_lower': dose_lower, 'dose_upper': dose_upper, 'constraint_name': constraint_name}
        if explicit_seed is not None:
            try:
                seed_spec['dose_seed'] = float(explicit_seed)
            except (TypeError, ValueError):
                pass
        if seed_inlet_no3 is not None:
            try:
                seed_spec['seed_inlet_no3_mg_l'] = float(seed_inlet_no3)
            except (TypeError, ValueError):
                pass
        try:
            self._session.config.runtime_metadata.setdefault('no3_target_specs', []).append(seed_spec)
        except Exception:
            pass
        target_unit = constraint_spec.get('target_unit')
        target_port_name = str(constraint_spec.get('target_port', 'inlet'))
        if not target_unit:
            raise ModelBuildError('no3_target constraint requires target_unit (the effluent/anoxic unit whose port carries the S_NO3 compliance metric)')
        target_port = self._unit_port(str(target_unit), target_port_name)
        component = str(constraint_spec.get('component', 'S_NO3'))
        target_expr = self._port_concentration(target_port, component)
        param_name = self._process_component_name('process_param', f'no3_target_{raw_name}')
        setattr(self._flowsheet, param_name, Param(initialize=target_kg_m3, mutable=True))
        target_param = getattr(self._flowsheet, param_name)
        setattr(self._flowsheet, constraint_name, Constraint(expr=target_expr == target_param))

    @staticmethod
    def _no3_target_build_seed(explicit_seed: Any, seed_inlet_no3_mg_l: Any, target_mg_l: float, dose_lower: float, dose_upper: float) -> Optional[float]:
        """Cold-start carbon_dose seed from an explicit value or an inlet-NO3 hint.

        Returns the engine-basis seed (kg COD/m^3 added as S_A) when the spec
        carries an explicit ``dose_seed`` (opt-out, clipped to bounds) or a
        ``seed_inlet_no3_mg_l`` hint (seeded at the stoichiometric denitrification
        COD demand, ~2.86 g COD / g NO3-N reduced toward the target). Returns None
        when neither is given, leaving the caller's existing-value/floor logic in
        charge — the ASM2D kinetics set the final dose regardless.
        """
        from watertap_engine_mcp.units.supplemental_carbon_asm2d import DENITRIFICATION_COD_PER_NO3_N
        if explicit_seed is not None:
            try:
                return min(max(float(explicit_seed), dose_lower), dose_upper)
            except (TypeError, ValueError):
                return None
        if seed_inlet_no3_mg_l is None:
            return None
        try:
            delta_no3 = max(float(seed_inlet_no3_mg_l) - float(target_mg_l), 0.0)
        except (TypeError, ValueError):
            return None
        seed = DENITRIFICATION_COD_PER_NO3_N * delta_no3 / 1000.0
        return min(max(seed, dose_lower), dose_upper)

    def _unit_port(self, unit_id: str, port_name: str) -> Any:
        unit = self._units.get(unit_id)
        if unit is None:
            raise ModelBuildError(f'Recycle-ratio unit not found: {unit_id}')
        port = getattr(unit, port_name, None)
        if port is None:
            raise ModelBuildError(f'Recycle-ratio port {port_name} not found on {unit_id}')
        return port

    def _port_flow(self, port: Any) -> Any:
        flow_vol = getattr(port, 'flow_vol', None)
        if flow_vol is None:
            raise ModelBuildError(f'Port {port.name} has no flow_vol')
        flow = self._indexed_component(flow_vol, 0, 0.0, None)
        if flow is None:
            raise ModelBuildError(f'Port {port.name} has no flow_vol[0]')
        return flow

    def _process_component_name(self, prefix: str, raw_name: str) -> str:
        safe = ''.join((ch if ch.isalnum() else '_' for ch in str(raw_name))).strip('_')
        return f'{prefix}_{safe or 'component'}'

    def _add_recycle_ratio_regularization_objective(self) -> None:
        """Add one objective for bounded recycle ratios and min-dose controls."""
        if not self._free_recycle_ratio_objective_terms:
            return
        from pyomo.environ import Objective, minimize
        active_objectives = list(self._model.component_data_objects(Objective, active=True, descend_into=True))
        if active_objectives:
            if len(active_objectives) == 1:
                active_objectives[0].set_value(active_objectives[0].expr + sum(self._free_recycle_ratio_objective_terms))
            return
        setattr(self._flowsheet, 'asm2d_recycle_ratio_regularization_objective', Objective(expr=sum(self._free_recycle_ratio_objective_terms), sense=minimize))

    def _process_constraint_name(self, raw_name: str) -> str:
        """Return a stable Pyomo component name for a process constraint."""
        safe = ''.join((ch if ch.isalnum() else '_' for ch in raw_name)).strip('_')
        return f'process_constraint_{safe or 'constraint'}'

    def _indexed_component(self, component: Any, *candidate_indexes: Any) -> Any:
        """Return the first valid indexed component entry from candidate indexes."""
        if component is None:
            return None
        if not hasattr(component, 'is_indexed') or not component.is_indexed():
            return component
        for candidate in candidate_indexes:
            try:
                return component[candidate]
            except Exception:
                continue
        return None

    def _outlet_concentration(self, unit_block: Any, component: str) -> Any:
        """Return reactor outlet concentration for one component.

        Handles the aerobic ``control_volume.properties_out`` reactors, the ADM1
        anaerobic digester (``liquid_phase.properties_out`` + a ``liquid_outlet``
        port rather than ``control_volume`` / ``outlet``), and any remaining unit
        exposing a plain ``outlet`` port.
        """
        try:
            state = self._indexed_component(unit_block.control_volume.properties_out, 0, 0.0)
            if state is not None and hasattr(state, 'conc_mass_comp'):
                return state.conc_mass_comp[component]
        except Exception:
            pass
        try:
            liquid_phase = getattr(unit_block, 'liquid_phase', None)
            if liquid_phase is not None:
                state = self._indexed_component(liquid_phase.properties_out, 0, 0.0)
                if state is not None and hasattr(state, 'conc_mass_comp'):
                    return state.conc_mass_comp[component]
        except Exception:
            pass
        outlet = getattr(unit_block, 'outlet', None) or getattr(unit_block, 'liquid_outlet', None)
        if outlet is None:
            raise ModelBuildError(f'Unit {unit_block.name} has no outlet port')
        return self._port_concentration(outlet, component)

    def _srt_reactor_volume(self, unit_block: Any) -> Any:
        """Return the reactor liquid-holdup volume Var for the SRT inventory.

        Aerobic CSTR-style reactors expose ``volume[0]``; the ADM1 anaerobic
        digester exposes ``volume_liquid[0]`` (a Reference to the liquid control
        volume's holdup, the SRT-relevant biomass-holding volume) and has no plain
        ``volume``. Return the first that resolves.
        """
        for attr in ('volume', 'volume_liquid'):
            candidate = self._indexed_component(getattr(unit_block, attr, None), 0, 0.0)
            if candidate is not None:
                return candidate
        return None

    def _port_concentration(self, port: Any, component: str) -> Any:
        """Return port concentration for one component."""
        conc = getattr(port, 'conc_mass_comp', None)
        if conc is None:
            raise ModelBuildError(f'Port {port.name} has no conc_mass_comp')
        value = self._indexed_component(conc, (0, component), (0.0, component), component)
        if value is None:
            raise ModelBuildError(f'Port {port.name} has no conc_mass_comp entry for {component}')
        return value

    def _apply_feed_state(self):
        """Apply per-unit feed state to Feed/FeedZO units."""
        if not self._session.feed_states:
            return
        for unit_id, feed_state in self._session.feed_states.items():
            state_args = (feed_state or {}).get('state_args')
            if not state_args:
                continue
            self._apply_feed_state_to_unit(unit_id, state_args)

    def _apply_feed_state_to_unit(self, unit_id: str, state_args: Dict[str, Any]) -> None:
        """Apply one state-args payload to a feed-style unit."""
        unit_block = self._units.get(unit_id)
        if unit_block is None:
            return
        try:
            state_block = getattr(unit_block, 'properties', None)
            if state_block is not None:
                state_block = state_block[0]
            port_block = getattr(unit_block, 'outlet', None) or getattr(unit_block, 'inlet', None)
            for key, value in state_args.items():
                candidates = []
                if hasattr(unit_block, key):
                    candidates.append(getattr(unit_block, key))
                if state_block is not None and hasattr(state_block, key):
                    candidates.append(getattr(state_block, key))
                if port_block is not None and hasattr(port_block, key):
                    candidates.append(getattr(port_block, key))
                for candidate in candidates:
                    if self._apply_state_value(candidate, value):
                        break
        except Exception as e:
            import sys
            print(f'Warning: Cannot apply feed state to {unit_id}: {e}', file=sys.stderr)

    def _apply_state_value(self, target: Any, value: Any) -> bool:
        """Fix a state variable or indexed state variable from serialized args."""
        if isinstance(value, dict):
            applied = False
            for idx, entry in value.items():
                for candidate_idx in (idx, (0, idx), (0.0, idx)):
                    try:
                        if candidate_idx in target and hasattr(target[candidate_idx], 'fix'):
                            target[candidate_idx].fix(entry)
                            applied = True
                            break
                    except Exception:
                        continue
            return applied
        if hasattr(target, 'fix'):
            target.fix(value)
            return True
        if hasattr(target, 'is_indexed') and target.is_indexed():
            applied = False
            for idx in target:
                try:
                    if hasattr(target[idx], 'fix'):
                        target[idx].fix(value)
                        applied = True
                except Exception:
                    continue
            return applied
        return False

    def _apply_zero_order_parameters(self) -> None:
        """Reload persisted zero-order database parameters on rebuilt models."""
        for unit_id, unit_inst in self._session.units.items():
            if not unit_inst.unit_type.endswith('ZO'):
                continue
            unit_block = self._units.get(unit_id)
            if unit_block is None or not hasattr(unit_block, 'load_parameters_from_database'):
                continue
            zo_config = unit_inst.zo_config or {}
            database = zo_config.get('database')
            process_subtype = zo_config.get('process_subtype')
            try:
                if database and hasattr(unit_block.config, 'database'):
                    try:
                        from watertap.core.wt_database import Database
                        unit_block.config.database = Database() if database == 'default' else Database(database)
                    except Exception:
                        pass
                if process_subtype and hasattr(unit_block.config, 'process_subtype'):
                    unit_block.config.process_subtype = process_subtype
                if database or process_subtype or zo_config.get('parameters_loaded'):
                    unit_block.load_parameters_from_database(use_default_removal=True)
            except Exception:
                continue

    def _apply_fixed_variables(self):
        """Apply fixed variables from session to model."""
        for unit_id, unit_inst in self._session.units.items():
            unit_block = self._units.get(unit_id)
            if unit_block is None:
                continue
            spec = UNITS.get(unit_inst.unit_type)
            profile = self._unit_profiles.get(unit_id)
            for var_path, var_value in unit_inst.fixed_vars.items():
                declared_units = self._declared_fix_units(spec, profile, var_path)
                self._fix_variable(unit_block, var_path, var_value, declared_units=declared_units)

    def _session_uses_adm1_package(self) -> bool:
        """Return True for ADM1/mADM1 model sessions."""
        config = getattr(self._session, 'config', None)
        package = getattr(config, 'default_property_package', None)
        if isinstance(package, PropertyPackageType):
            package_type = package
        else:
            package_type = None
            try:
                package_type = PropertyPackageType(package)
            except Exception:
                try:
                    package_type = PropertyPackageType[str(package).upper()]
                except Exception:
                    package_text = str(package or '').lower()
                    return package_text in {'adm1', 'modified_adm1', 'madm1', 'adm1parameterblock', 'modifiedadm1parameterblock'}
        return package_type in {PropertyPackageType.ADM1, PropertyPackageType.MODIFIED_ADM1, PropertyPackageType.MODIFIED_ADM1_SFE, PropertyPackageType.MODIFIED_ADM1_SFE_ORGANIC_S, PropertyPackageType.MODIFIED_ADM1_SFE_MICROAERATION}

    def _apply_split_fraction_bounds(self) -> None:
        """Apply physical 0..1 bounds to separator split-fraction variables."""
        if not self._session_uses_adm1_package():
            return
        for unit_block in self._units.values():
            split_fraction = getattr(unit_block, 'split_fraction', None)
            if split_fraction is None:
                continue
            try:
                values = split_fraction.values() if getattr(split_fraction, 'is_indexed', lambda: False)() else [split_fraction]
            except Exception:
                continue
            for var in values:
                try:
                    lower = var.lb
                    upper = var.ub
                    if lower is None or float(lower) < 0.0:
                        var.setlb(0.0)
                    if upper is None or float(upper) > 1.0:
                        var.setub(1.0)
                except Exception:
                    continue

    def _seed_adm1_vapor_pressure_branch_guard(self) -> None:
        """Seed ADM1 vapor pressure before the solver-stage floor is applied.

        WaterTAP's AD gas take-off equations permit a spurious near-zero total
        vapor-pressure root in stiff target-control/recycle solves. The hard
        atmospheric lower bound is applied after unit initialization by the hygiene
        pipeline; doing it at build time disrupts the digester initializer's basin
        search. This build-time pass only provides a physical atmospheric seed
        while preserving pressure BY NAME and unfixed.
        """
        if not self._session_uses_adm1_package():
            return
        floor_pa = 101325.0
        metadata = self._session.config.runtime_metadata
        apply_immediately = bool(metadata.get('adm1_temperature_target_specs'))
        seeded: List[str] = []
        bounded: List[str] = []
        for unit_block in self._units.values():
            if not (hasattr(unit_block, 'vapor_phase') and hasattr(unit_block, 'flow_vol_vap') and hasattr(unit_block, 'outlet_P')):
                continue
            try:
                time_set = list(unit_block.vapor_phase.keys())
            except Exception:
                time_set = [0]
            for t in time_set:
                try:
                    pressure = unit_block.vapor_phase[t].pressure
                except Exception:
                    continue
                try:
                    current = pressure.value
                    if current is None or float(current) < floor_pa:
                        pressure.set_value(floor_pa, skip_validation=True)
                    if apply_immediately:
                        lower = pressure.lb
                        if lower is None or float(lower) < floor_pa:
                            pressure.setlb(floor_pa)
                        bounded.append(pressure.name)
                    seeded.append(pressure.name)
                except Exception:
                    continue
        if seeded:
            metadata['adm1_vapor_pressure_floor'] = {'floor_Pa': floor_pa, 'seeded_pressure_variable_names': sorted(set(seeded)), 'bounded_pressure_variable_names': sorted(set(bounded)), 'mode': 'temperature_target_build_lower_bound' if apply_immediately else 'seed_then_pre_solve_lower_bound'}

    def _apply_asm2d_recycle_pressure_reference(self) -> None:
        """Demote non-process auxiliary states in biological recycle trains.

        Activated-sludge recycle templates are liquid-only and isothermal, so
        pressure, temperature/enthalpy, and power-reporting rows carry no process
        information and are fixed to feed reference data.  Anaerobic ADM1 units
        can carry real gas-phase pressure and active heating balances, so this
        classifier protects any pressure/temperature coupled to vapor, Henry/VLE,
        gas outflow, or an unfixed heat duty and demotes only the remaining
        liquid-side hydraulic/reporting null-space.
        """
        try:
            from watertap_engine_mcp.core.roles import RoleResolver, UnitRole
            from pyomo.environ import Constraint, Var
            from pyomo.core.expr.visitor import identify_variables
        except Exception:
            return

        def normalize_package(value: Any) -> Optional[PropertyPackageType]:
            if isinstance(value, PropertyPackageType):
                return value
            if isinstance(value, str):
                for candidate in PropertyPackageType:
                    if value == candidate.value or value == candidate.name:
                        return candidate
                    if value.upper() == candidate.name:
                        return candidate
            return None
        default_package = normalize_package(self._session.config.default_property_package)
        roles = RoleResolver.from_session(self._session)
        is_asm2d_recycle = is_asm2d_property_package_type(default_package) and roles.is_asm2d_recycle_family()
        adm1_packages = {PropertyPackageType.ADM1, PropertyPackageType.MODIFIED_ADM1, PropertyPackageType.MODIFIED_ADM1_SFE, PropertyPackageType.MODIFIED_ADM1_SFE_ORGANIC_S, PropertyPackageType.MODIFIED_ADM1_SFE_MICROAERATION, PropertyPackageType.ADM1_VAPOR, PropertyPackageType.ADM1_SFE_VAPOR, PropertyPackageType.ADM1_SFE_MICROAERATION_VAPOR}

        def unit_package_type(unit_id: str) -> Optional[PropertyPackageType]:
            unit_inst = self._session.units[unit_id]
            config = unit_inst.config if isinstance(unit_inst.config, dict) else {}
            for key in ('property_package', 'liquid_property_package', 'property_package_liquid', 'property_package_feed', 'property_package_vapor'):
                package = normalize_package(config.get(key))
                if package is not None:
                    return package
            return default_package
        unit_blocks = {unit_id: self._units[unit_id] for unit_id in self._session.units if unit_id in self._units}
        if not unit_blocks:
            return
        unit_packages = {unit_id: unit_package_type(unit_id) for unit_id in unit_blocks}
        has_adm1_surface = any((package in adm1_packages for package in unit_packages.values()))
        if not is_asm2d_recycle and (not has_adm1_surface):
            return
        reference_pressure = self._asm2d_pressure_reference_pa()
        reference_temperature = self._asm2d_temperature_reference_k()
        pressure_var_names = {'pressure'}
        pressure_aux_names = {'deltaP', 'ratioP'}
        pressure_changer_aux_names = {'deltaP', 'ratioP', 'work'}
        pressure_equation_names = pressure_var_names | pressure_aux_names
        temperature_var_names = {'temperature'}
        reporting_power_names = {'electricity', 'electricity_consumption', 'power', 'blower_power', 'work', 'work_fluid', 'work_isentropic', 'work_mechanical'}
        heat_duty_names = {'heat', 'heat_duty', 'Q', 'duty', 'enthalpy_transfer'}
        auxiliary_equation_names = pressure_equation_names | temperature_var_names | reporting_power_names
        unit_prefixes = tuple((f'{block.name}.' for block in unit_blocks.values()))
        unit_prefix_by_id = {unit_id: f'{block.name}.' for unit_id, block in unit_blocks.items()}
        pressure_changer_prefixes = tuple((f'{block.name}.' for unit_id, block in unit_blocks.items() if self._session.units[unit_id].unit_type == 'PressureChanger'))
        core_mixer_prefixes = tuple((f'{block.name}.' for unit_id, block in unit_blocks.items() if getattr(self._session.units[unit_id], 'role', None) == UnitRole.MIXER.value))
        gas_unit_ids = {unit_id for unit_id, package in unit_packages.items() if package in (PropertyPackageType.ADM1_VAPOR, PropertyPackageType.ADM1_SFE_VAPOR, PropertyPackageType.ADM1_SFE_MICROAERATION_VAPOR)}

        def local_name(var: Any) -> str:
            try:
                return var.parent_component().local_name
            except Exception:
                return ''

        def unit_id_for_var(var: Any) -> Optional[str]:
            name = getattr(var, 'name', '')
            for unit_id, prefix in unit_prefix_by_id.items():
                if name.startswith(prefix):
                    return unit_id
            return None

        def belongs_to_biological_unit(var: Any) -> bool:
            return unit_id_for_var(var) is not None

        def belongs_to_pressure_changer(var: Any) -> bool:
            name = getattr(var, 'name', '')
            return any((name.startswith(prefix) for prefix in pressure_changer_prefixes))

        def belongs_to_core_mixer(var: Any) -> bool:
            name = getattr(var, 'name', '')
            return any((name.startswith(prefix) for prefix in core_mixer_prefixes))

        def has_gas_phase_structure(unit_id: str, block: Any) -> bool:
            spec = UNITS.get(self._session.units[unit_id].unit_type)
            if spec and any((role == 'gas' for role in (spec.port_roles or {}).values())):
                return True
            if unit_id in gas_unit_ids:
                return True
            return any((hasattr(block, attr) for attr in ('vapor_phase', 'gas_phase', 'vapor_outlet', 'gas_outlet', 'biogas')))
        gas_structured_unit_ids = {unit_id for unit_id, block in unit_blocks.items() if has_gas_phase_structure(unit_id, block)}

        def is_gas_coupled_var(var: Any) -> bool:
            unit_id = unit_id_for_var(var)
            name = getattr(var, 'name', '').lower()
            lname = local_name(var)
            if unit_id in gas_unit_ids:
                return True
            if lname == 'pressure_sat':
                return True
            gas_tokens = ('.vapor_phase', '.gas_phase', '.vapor_outlet', '.gas_outlet', '.biogas', 'adm1_vapor')
            if any((token in name for token in gas_tokens)):
                return True
            return unit_id in gas_structured_unit_ids and any((token in name for token in ('vapor', 'gas', '_vap')))

        def constraint_has_gas_coupling(constraint: Any, variables: List[Any]) -> bool:
            cname = getattr(constraint, 'name', '').lower()
            gas_constraint_tokens = ('vapor', 'gas', 'henry', 'pressure_sat', 'partial_pressure', 'partialpressure', 'vle')
            return any((token in cname for token in gas_constraint_tokens)) or any((is_gas_coupled_var(var) for var in variables))
        constraint_variables: List[Tuple[Any, List[Any]]] = []
        for constraint in self._flowsheet.component_data_objects(Constraint, active=True, descend_into=True):
            try:
                variables = list(identify_variables(constraint.body, include_fixed=True))
            except Exception:
                continue
            if variables and all((belongs_to_biological_unit(var) for var in variables)):
                constraint_variables.append((constraint, variables))
        protected_pressure_variables: Set[str] = set()
        protected_temperature_variables: Set[str] = set()
        protected_heat_work_variables: Set[str] = set()
        active_heat_balance_units: Set[str] = set()
        protected_pressure_reasons: Dict[str, str] = {}
        demoted_pressure_variables: Set[str] = set()
        demoted_pressure_reasons: Dict[str, str] = {}
        for constraint, variables in constraint_variables:
            if constraint_has_gas_coupling(constraint, variables):
                for var in variables:
                    if local_name(var) == 'pressure':
                        protected_pressure_variables.add(var.name)
                        protected_pressure_reasons.setdefault(var.name, 'gas_coupled')
                    elif local_name(var) == 'temperature':
                        protected_temperature_variables.add(var.name)
            cname = getattr(constraint, 'name', '').lower()
            if not any((token in cname for token in ('enthalpy', 'heat'))):
                continue
            heat_like_vars = [var for var in variables if local_name(var) in heat_duty_names | reporting_power_names and (not var.fixed) and (not belongs_to_pressure_changer(var))]
            if not heat_like_vars:
                continue
            unit_ids = {unit_id_for_var(var) for var in variables}
            active_heat_balance_units.update((unit_id for unit_id in unit_ids if unit_id is not None))
            for var in variables:
                if local_name(var) == 'temperature':
                    protected_temperature_variables.add(var.name)
                if local_name(var) in heat_duty_names | reporting_power_names:
                    protected_heat_work_variables.add(var.name)
        if is_asm2d_recycle:
            self._session.config.runtime_metadata['asm2d_pressure_reference'] = {'mode': 'active_reference_constraints', 'scope': 'asm2d_recycle', 'reference_pressure_Pa': reference_pressure, 'reference_temperature_K': reference_temperature, 'deactivated_constraints': 0, 'deactivated_pressure_constraints': 0, 'deactivated_temperature_constraints': 0, 'deactivated_energy_constraints': 0, 'deactivated_reporting_power_constraints': 0, 'deactivated_solvent_hydraulic_constraints': 0, 'protected_pressure_variables': len(protected_pressure_variables), 'protected_temperature_variables': len(protected_temperature_variables), 'protected_heat_work_variables': len(protected_heat_work_variables), 'protected_pressure_variable_names': sorted(protected_pressure_variables), 'demoted_pressure_variable_names': [], 'pressure_protection_reasons': dict(protected_pressure_reasons), 'pressure_demotion_reasons': {}, 'active_heat_balance_units': sorted(active_heat_balance_units), 'gas_phase_units': sorted(gas_structured_unit_ids | gas_unit_ids)}
            return

        def pressure_demotable(var: Any) -> bool:
            lname = local_name(var)
            if lname == 'pressure':
                return var.name not in protected_pressure_variables
            return lname in pressure_aux_names

        def temperature_demotable(var: Any) -> bool:
            return local_name(var) == 'temperature' and var.name not in protected_temperature_variables and (unit_id_for_var(var) not in active_heat_balance_units)
        for unit_id, unit_block in unit_blocks.items():
            is_pressure_changer = self._session.units[unit_id].unit_type == 'PressureChanger'
            for var in unit_block.component_data_objects(Var, descend_into=True):
                name = local_name(var)
                if name == 'pressure' and pressure_demotable(var):
                    try:
                        var.fix(reference_pressure)
                        demoted_pressure_variables.add(var.name)
                        demoted_pressure_reasons.setdefault(var.name, 'liquid_hydraulic_nullspace')
                    except Exception:
                        continue
                elif name == 'temperature' and temperature_demotable(var):
                    try:
                        var.fix(reference_temperature)
                    except Exception:
                        continue
                elif is_pressure_changer and name in pressure_changer_aux_names:
                    try:
                        if name == 'ratioP':
                            var.fix(1.0)
                        else:
                            var.fix(0.0)
                    except Exception:
                        continue
                elif name in reporting_power_names and var.name not in protected_heat_work_variables:
                    try:
                        var.fix(0.0)
                    except Exception:
                        continue
        deactivated_total = 0
        deactivated_pressure = 0
        deactivated_temperature = 0
        deactivated_energy = 0
        deactivated_reporting_power = 0
        deactivated_solvent_hydraulic = 0

        def constraint_index_tokens(constraint: Any) -> Set[str]:
            try:
                index = constraint.index()
            except Exception:
                return set()
            if not isinstance(index, tuple):
                index = (index,)
            return {str(part) for part in index}
        for constraint in self._flowsheet.component_data_objects(Constraint, active=True, descend_into=True):
            try:
                variables = list(identify_variables(constraint.body, include_fixed=True))
            except Exception:
                continue
            if not variables or not all((belongs_to_biological_unit(var) for var in variables)):
                continue
            if constraint_has_gas_coupling(constraint, variables):
                continue
            local_names = {local_name(var) for var in variables}
            pressure_variables_demotable = all((pressure_demotable(var) for var in variables if local_name(var) in pressure_equation_names))
            temperature_variables_demotable = all((temperature_demotable(var) for var in variables if local_name(var) == 'temperature'))
            is_pressure_only = local_names <= pressure_equation_names and pressure_variables_demotable
            is_temperature_only = local_names <= temperature_var_names and temperature_variables_demotable
            is_pressure_changer_enthalpy = 'enthalpy_balances' in constraint.name and any((local_name(var) == 'work' for var in variables)) and all((belongs_to_pressure_changer(var) or local_name(var) in {'flow_vol', 'temperature'} for var in variables)) and temperature_variables_demotable
            is_core_mixer_enthalpy = 'enthalpy_mixing_equations' in constraint.name and variables and all((belongs_to_core_mixer(var) for var in variables)) and ({local_name(var) for var in variables} <= {'flow_vol', 'temperature'}) and temperature_variables_demotable
            is_isothermal_energy_balance = ('enthalpy_balances' in constraint.name or 'enthalpy_mixing_equations' in constraint.name) and variables and (local_names <= {'flow_vol', 'temperature', 'work'}) and temperature_variables_demotable
            is_reporting_power_balance = any((token in constraint.name for token in ('electricity', 'power'))) and local_names <= reporting_power_names | {'flow_vol', 'KLa'} and any((name in reporting_power_names for name in local_names)) and (not any((var.name in protected_heat_work_variables for var in variables)))
            deactivate_reason: Optional[str] = None
            if is_pressure_only:
                deactivate_reason = 'pressure'
            elif is_temperature_only:
                deactivate_reason = 'temperature'
            elif is_pressure_changer_enthalpy or is_core_mixer_enthalpy or is_isothermal_energy_balance:
                deactivate_reason = 'energy'
            elif is_reporting_power_balance:
                deactivate_reason = 'reporting_power'
            if deactivate_reason is not None and local_names <= auxiliary_equation_names | {'flow_vol', 'KLa'}:
                constraint.deactivate()
                deactivated_total += 1
                if deactivate_reason == 'pressure':
                    deactivated_pressure += 1
                elif deactivate_reason == 'temperature':
                    deactivated_temperature += 1
                elif deactivate_reason == 'energy':
                    deactivated_energy += 1
                elif deactivate_reason == 'reporting_power':
                    deactivated_reporting_power += 1
                elif deactivate_reason == 'solvent_hydraulic':
                    deactivated_solvent_hydraulic += 1
        self._session.config.runtime_metadata['asm2d_pressure_reference'] = {'mode': 'fixed_reference', 'scope': 'asm2d_recycle' if is_asm2d_recycle else 'biological_phase_balance_aware', 'reference_pressure_Pa': reference_pressure, 'reference_temperature_K': reference_temperature, 'deactivated_constraints': deactivated_total, 'deactivated_pressure_constraints': deactivated_pressure, 'deactivated_temperature_constraints': deactivated_temperature, 'deactivated_energy_constraints': deactivated_energy, 'deactivated_reporting_power_constraints': deactivated_reporting_power, 'deactivated_solvent_hydraulic_constraints': deactivated_solvent_hydraulic, 'protected_pressure_variables': len(protected_pressure_variables), 'protected_temperature_variables': len(protected_temperature_variables), 'protected_heat_work_variables': len(protected_heat_work_variables), 'protected_pressure_variable_names': sorted(protected_pressure_variables), 'demoted_pressure_variable_names': sorted(demoted_pressure_variables), 'pressure_protection_reasons': dict(protected_pressure_reasons), 'pressure_demotion_reasons': dict(demoted_pressure_reasons), 'active_heat_balance_units': sorted(active_heat_balance_units), 'gas_phase_units': sorted(gas_structured_unit_ids | gas_unit_ids)}

    def _asm2d_pressure_reference_pa(self) -> float:
        """Return the feed pressure datum used for ASM2D recycle demotion."""
        for state in (self._session.feed_states or {}).values():
            state_args = state.get('state_args') if isinstance(state, dict) else None
            if not isinstance(state_args, dict):
                continue
            pressure = state_args.get('pressure')
            if isinstance(pressure, dict):
                for key in (0, '0', 0.0, '0.0'):
                    if key in pressure:
                        try:
                            return float(pressure[key])
                        except (TypeError, ValueError):
                            continue
                for value in pressure.values():
                    try:
                        return float(value)
                    except (TypeError, ValueError):
                        continue
            elif pressure is not None:
                try:
                    return float(pressure)
                except (TypeError, ValueError):
                    continue
        return 101325.0

    def _asm2d_temperature_reference_k(self) -> float:
        """Return the feed temperature datum used for ASM2D recycle demotion."""
        for state in (self._session.feed_states or {}).values():
            state_args = state.get('state_args') if isinstance(state, dict) else None
            if not isinstance(state_args, dict):
                continue
            temperature = state_args.get('temperature')
            if isinstance(temperature, dict):
                for key in (0, '0', 0.0, '0.0'):
                    if key in temperature:
                        try:
                            return float(temperature[key])
                        except (TypeError, ValueError):
                            continue
                for value in temperature.values():
                    try:
                        return float(value)
                    except (TypeError, ValueError):
                        continue
            elif temperature is not None:
                try:
                    return float(temperature)
                except (TypeError, ValueError):
                    continue
        return 298.15

    def _declared_fix_units(self, spec: Optional[UnitSpec], profile: Optional[UnitProfile], var_path: str) -> Optional[str]:
        """Return the registry-declared engineering units for a fixed variable."""
        fix_specs = []
        if profile is not None and profile.required_fixes:
            fix_specs.extend(profile.required_fixes)
        if spec is not None and spec.required_fixes:
            fix_specs.extend(spec.required_fixes)
        for fix_spec in fix_specs:
            if fix_spec.name == var_path:
                return fix_spec.units
        return None

    def _resolve_variable_path(self, unit: Any, var_path: str) -> Tuple[Optional[Any], Optional[Any]]:
        """Resolve a variable path to the actual Pyomo variable.

        Handles:
        - Simple: "area"
        - Indexed: "A_comp[0,H2O]"
        - Dotted: "control_volume.properties_out[0].pressure"
        - Port: "permeate.pressure[0]"
        - Wildcards: "feed_side.cp_modulus[0,*,*]" (returns list of matches)

        Args:
            unit: Unit block to resolve path on
            var_path: Variable path string

        Returns:
            Tuple of (variable_or_list, index_or_None)
            - For wildcards: (list of (var, index) tuples, None)
            - For indexed: (indexed_var, index)
            - For scalar: (var, None)
            - On failure: (None, None)
        """
        import sys
        if '*' in var_path:
            return self._resolve_wildcard_path(unit, var_path)
        try:
            if hasattr(unit, 'find_component'):
                component = unit.find_component(var_path)
                if component is not None:
                    return (component, None)
            return self._resolve_path_manually(unit, var_path)
        except Exception as e:
            print(f'Warning: Cannot resolve path {var_path}: {e}', file=sys.stderr)
            return (None, None)

    def _resolve_path_manually(self, unit: Any, var_path: str) -> Tuple[Optional[Any], Optional[Any]]:
        """Manually resolve a dotted variable path.

        Parses paths like:
        - "control_volume.properties_out[0].pressure"
        - "permeate.pressure[0]"
        - "A_comp[0,H2O]"

        Args:
            unit: Starting unit block
            var_path: Variable path to resolve

        Returns:
            (variable, index) or (None, None) on failure
        """
        current = unit
        remaining = var_path
        final_index = None
        while remaining:
            dot_pos = self._find_dot_outside_brackets(remaining)
            if dot_pos == -1:
                segment = remaining
                remaining = ''
            else:
                segment = remaining[:dot_pos]
                remaining = remaining[dot_pos + 1:]
            if '[' in segment:
                attr_name, index_str = segment.split('[', 1)
                index_str = index_str.rstrip(']')
                indices = self._parse_index(index_str)
                current = getattr(current, attr_name, None)
                if current is None:
                    return (None, None)
                if remaining:
                    try:
                        if len(indices) == 1:
                            current = current[indices[0]]
                        else:
                            current = current[tuple(indices)]
                    except (KeyError, IndexError):
                        return (None, None)
                else:
                    final_index = indices[0] if len(indices) == 1 else tuple(indices)
            else:
                current = getattr(current, segment, None)
                if current is None:
                    return (None, None)
        return (current, final_index)

    def _resolve_wildcard_path(self, unit: Any, var_path: str) -> Tuple[Optional[List], None]:
        """Resolve paths with wildcards like feed_side.cp_modulus[0,*,*].

        Wildcards (*) match all indices at that position.

        Args:
            unit: Unit block
            var_path: Path with wildcards

        Returns:
            (list of (var_at_index, index) tuples, None) or (None, None)
        """
        import sys
        if '[' not in var_path:
            return (None, None)
        bracket_pos = var_path.rfind('[')
        base_path = var_path[:bracket_pos]
        index_pattern = var_path[bracket_pos + 1:].rstrip(']')
        base_var, _ = self._resolve_path_manually(unit, base_path)
        if base_var is None:
            if hasattr(unit, 'find_component'):
                base_var = unit.find_component(base_path)
            if base_var is None:
                return (None, None)
        pattern_parts = [p.strip() for p in index_pattern.split(',')]
        matches = []
        try:
            for idx in base_var.index_set():
                if not isinstance(idx, tuple):
                    idx_tuple = (idx,)
                else:
                    idx_tuple = idx
                if len(idx_tuple) != len(pattern_parts):
                    continue
                match = True
                for i, (actual, pattern) in enumerate(zip(idx_tuple, pattern_parts)):
                    if pattern != '*':
                        parsed_pattern = self._parse_single_index(pattern)
                        if actual != parsed_pattern:
                            match = False
                            break
                if match:
                    matches.append((base_var, idx))
        except Exception as e:
            print(f'Warning: Cannot iterate wildcard indices for {var_path}: {e}', file=sys.stderr)
            return (None, None)
        if not matches:
            return (None, None)
        return (matches, None)

    def _find_dot_outside_brackets(self, s: str) -> int:
        """Find position of first dot not inside brackets.

        Args:
            s: String to search

        Returns:
            Position of dot, or -1 if not found
        """
        depth = 0
        for i, c in enumerate(s):
            if c == '[':
                depth += 1
            elif c == ']':
                depth -= 1
            elif c == '.' and depth == 0:
                return i
        return -1

    def _parse_index(self, index_str: str) -> List:
        """Parse index string into list of values.

        Args:
            index_str: Comma-separated index string like "0, H2O"

        Returns:
            List of parsed index values
        """
        indices = [s.strip() for s in index_str.split(',')]
        return [self._parse_single_index(idx) for idx in indices]

    def _parse_single_index(self, idx: str) -> Any:
        """Parse a single index value.

        Args:
            idx: Index string

        Returns:
            Parsed value (int, float, or string)
        """
        try:
            return int(idx)
        except ValueError:
            pass
        try:
            return float(idx)
        except ValueError:
            pass
        return idx

    def _fix_variable(self, unit: Any, var_path: str, value: float, *, declared_units: Optional[str]=None):
        """Fix a variable on a unit block.

        Supports:
        - Simple variables: "area"
        - Indexed variables: "A_comp[0,H2O]"
        - Dotted paths: "control_volume.properties_out[0].pressure"
        - Port properties: "permeate.pressure[0]"
        - Wildcards: "feed_side.cp_modulus[0,*,*]" (fixes all matching indices)

        Args:
            unit: Unit block
            var_path: Variable path (e.g., "control_volume.properties_out[0].pressure")
            value: Value to fix
        """
        import sys
        try:
            var, index = self._resolve_variable_path(unit, var_path)
            if var is None:
                print(f'Warning: Variable path not found: {var_path}', file=sys.stderr)
                return
            if isinstance(var, list):
                for var_item, idx in var:
                    try:
                        target = var_item[idx] if idx is not None else var_item
                        fixed_value = self._convert_fixed_value_to_target_units(value, declared_units, target)
                        if idx is not None:
                            var_item[idx].fix(fixed_value)
                        elif hasattr(var_item, 'fix'):
                            var_item.fix(fixed_value)
                    except Exception as e:
                        print(f'Warning: Cannot fix {var_path} at index {idx}: {e}', file=sys.stderr)
                return
            if index is not None:
                fixed_value = self._convert_fixed_value_to_target_units(value, declared_units, var[index])
                var[index].fix(fixed_value)
            elif hasattr(var, 'fix'):
                fixed_value = self._convert_fixed_value_to_target_units(value, declared_units, var)
                var.fix(fixed_value)
            elif hasattr(var, '__iter__') and hasattr(var, 'values'):
                for v in var.values():
                    fixed_value = self._convert_fixed_value_to_target_units(value, declared_units, v)
                    v.fix(fixed_value)
        except Exception as e:
            print(f'Warning: Cannot fix variable {var_path}: {e}', file=sys.stderr)

    def _convert_fixed_value_to_target_units(self, raw_value: Any, declared_units: Optional[str], target: Any) -> Any:
        """Convert registry-declared engineering units to the target variable units."""
        if declared_units in (None, '', '-'):
            return raw_value
        try:
            numeric_value = float(raw_value)
        except (TypeError, ValueError):
            return raw_value
        try:
            from pyomo.environ import units as pyunits
            source_units = self._lookup_engineering_units(declared_units)
            if source_units is None:
                return raw_value
            target_units = pyunits.get_units(target)
            if target_units is None:
                return raw_value
            return pyunits.convert_value(numeric_value, from_units=source_units, to_units=target_units)
        except Exception:
            return raw_value

    def _lookup_engineering_units(self, unit_name: str) -> Any:
        """Resolve the public engineering-unit literal used in registry metadata."""
        from pyomo.environ import units as pyunits
        unit_lookup = {'K': pyunits.K, 'Pa': pyunits.Pa, 'V': pyunits.V, 'W/m/K': pyunits.W / pyunits.m / pyunits.K, 'W/m^2/K': pyunits.W / pyunits.m ** 2 / pyunits.K, 'cm^2/J': pyunits.cm ** 2 / pyunits.J, 'eq/L': pyunits.eq / pyunits.L, 'gpm/ft^2': pyunits.gallon / pyunits.minute / pyunits.ft ** 2, 'kWh/m^3': pyunits.kWh / pyunits.m ** 3, 'kg/m^3': pyunits.kg / pyunits.m ** 3, 'kg/s': pyunits.kg / pyunits.s, 'm': pyunits.m, 'm/s': pyunits.m / pyunits.s, 'm/s/Pa': pyunits.m / pyunits.s / pyunits.Pa, 'mW/cm^2': pyunits.mW / pyunits.cm ** 2, 'm^2': pyunits.m ** 2, 'm^2/s': pyunits.m ** 2 / pyunits.s, 'm^3': pyunits.m ** 3, 'mol/m^3': pyunits.mol / pyunits.m ** 3, 'mol/s': pyunits.mol / pyunits.s, 'ohm*m^2': pyunits.ohm * pyunits.m ** 2, 's': pyunits.s}
        return unit_lookup.get(unit_name)

    def _apply_scaling_factors(self):
        """Apply scaling factors from session to model."""
        try:
            import idaes.core.util.scaling as iscale
        except ImportError:
            return
        for unit_id, unit_inst in self._session.units.items():
            unit_block = self._units.get(unit_id)
            if unit_block is None:
                continue
            for var_path, factor in unit_inst.scaling_factors.items():
                self._set_scaling(unit_block, var_path, factor, iscale)

    def _apply_activated_sludge_scaling_defaults(self) -> None:
        """Apply upstream WaterTAP ASM2D scaling to constructed state blocks."""
        active_asm2d = is_asm2d_property_package_type(self._session.config.default_property_package) and any((unit_inst.unit_type in {'CSTR', 'CSTR_Injection', 'SecondaryClarifierASM', 'FlatBottomCircularClarifier', 'MBRSeparator'} for unit_inst in self._session.units.values()))
        if not active_asm2d:
            return
        try:
            import idaes.core.util.scaling as iscale
            from pyomo.environ import Constraint, Var
        except ImportError:
            return
        try:
            from watertap_engine_mcp.solver.scaler import DEFAULT_ASM2D_SCALING_FACTORS
            component_scaling = DEFAULT_ASM2D_SCALING_FACTORS
        except Exception:
            component_scaling = {'S_O2': 1000.0, 'S_F': 1000.0, 'S_A': 1000.0, 'S_NH4': 100.0, 'S_NO3': 100.0, 'S_PO4': 1000.0, 'S_IC': 1000.0, 'S_K': 100.0, 'S_Mg': 100.0, 'S_I': 100.0, 'S_N2': 100.0, 'X_I': 10.0, 'X_S': 100.0, 'X_H': 10.0, 'X_PAO': 10.0, 'X_PP': 100.0, 'X_PHA': 100.0, 'X_AUT': 10.0, 'X_MeOH': 10000000.0, 'X_MeP': 10000000.0, 'X_TSS': 1.0}

        def _index_component(index: Any) -> Optional[str]:
            if isinstance(index, tuple):
                for item in reversed(index):
                    if isinstance(item, str):
                        return item
                return None
            if isinstance(index, str):
                return index
            return None
        for var in self._model.fs.component_data_objects(Var, descend_into=True):
            var_name = str(var.name)
            parent_name = getattr(var.parent_component(), 'local_name', '')
            factor = None
            if parent_name == 'flow_vol' or 'flow_vol' in var_name:
                factor = 100.0
            elif parent_name == 'temperature' or 'temperature' in var_name:
                factor = 0.1
            elif 'pressure' in parent_name or 'pressure' in var_name:
                factor = 0.0001
            elif parent_name == 'enth_mol' or 'enth_mol' in var_name:
                factor = 0.001
            elif parent_name == 'alkalinity' or 'alkalinity' in var_name:
                factor = 1000.0
            elif parent_name == 'caustic_dose' or 'caustic_dose' in var_name:
                factor = 1.0
            elif parent_name == 'volume' or var_name.endswith('.control_volume.volume'):
                factor = 0.001
            elif parent_name == 'work' or var_name.endswith('.work[0.0]'):
                factor = 0.001
            elif parent_name == 'conc_mass_comp':
                component = _index_component(var.index())
                factor = component_scaling.get(component)
            elif 'rate_reaction_extent' in var_name or 'reaction_rate' in var_name:
                factor = 1000.0
            elif 'electricity_consumption' in var_name:
                factor = 0.001
            if factor is not None:
                try:
                    iscale.set_scaling_factor(var, factor)
                except Exception:
                    continue
        for unit_id, unit_inst in self._session.units.items():
            if unit_inst.unit_type not in {'CSTR', 'CSTR_Injection'}:
                continue
            block = self._units.get(unit_id)
            if block is None:
                continue
            for attr_name in ('cstr_performance_eqn', 'control_volume.rate_reaction_stoichiometry_constraint', 'control_volume.material_balances', 'control_volume.rate_reaction_extent_constraint'):
                try:
                    component = block
                    for part in attr_name.split('.'):
                        component = getattr(component, part)
                    iscale.set_scaling_factor(component, 1000.0)
                except Exception:
                    continue
            try:
                iscale.set_scaling_factor(block.control_volume.reactions[0.0].rate_expression, 1000.0)
            except Exception:
                pass
        for constraint in self._model.fs.component_data_objects(Constraint, active=True, descend_into=True):
            name = str(constraint.name)
            if 'process_constraint_' in name and 'srt' in name:
                try:
                    iscale.constraint_scaling_transform(constraint, 1.0, overwrite=False)
                except Exception:
                    pass
                continue
            if 'process_constraint_' in name and 'do_target' in name:
                try:
                    iscale.constraint_scaling_transform(constraint, 1000.0, overwrite=False)
                except Exception:
                    pass
                continue
            if 'process_constraint_' in name and 'alkalinity_target' in name:
                try:
                    iscale.constraint_scaling_transform(constraint, 1.0, overwrite=False)
                except Exception:
                    pass
                continue
            if 'process_constraint_' in name and 'chemical_p_target' in name:
                try:
                    iscale.constraint_scaling_transform(constraint, 1000.0, overwrite=False)
                except Exception:
                    pass
                continue
            if 'process_constraint_asm2d_' in name and 'recycle_ratio' in name:
                try:
                    iscale.constraint_scaling_transform(constraint, 100.0, overwrite=False)
                except Exception:
                    pass
                continue
            if 'flow_vol_equality' in name:
                try:
                    iscale.constraint_scaling_transform(constraint, 100.0, overwrite=False)
                except Exception:
                    pass
                continue
            if 'CSTR_retention_time' in name:
                try:
                    iscale.constraint_scaling_transform(constraint, 0.001, overwrite=False)
                except Exception:
                    pass
                continue
            if 'sum_split_frac' in name:
                try:
                    iscale.constraint_scaling_transform(constraint, 1.0, overwrite=False)
                except Exception:
                    pass
                continue
            if not any((token in name for token in ('rate_reaction', 'material_balances', 'cstr_performance', 'mass_transfer'))):
                continue
            try:
                iscale.constraint_scaling_transform(constraint, 1000.0, overwrite=False)
            except Exception:
                continue
        try:
            iscale.calculate_scaling_factors(self._model)
        except Exception:
            pass

    def _set_scaling(self, unit: Any, var_path: str, factor: float, iscale):
        """Set scaling factor for a variable.

        Supports:
        - Simple variables: "area"
        - Indexed variables: "A_comp[0,H2O]"
        - Dotted paths: "control_volume.properties_out[0].pressure"
        - Port properties: "permeate.pressure[0]"
        - Wildcards: "feed_side.cp_modulus[0,*,*]" (scales all matching indices)

        Args:
            unit: Unit block
            var_path: Variable path
            factor: Scaling factor
            iscale: IDAES scaling module
        """
        import sys
        try:
            var, index = self._resolve_variable_path(unit, var_path)
            if var is None:
                print(f'Warning: Variable path not found for scaling: {var_path}', file=sys.stderr)
                return
            if isinstance(var, list):
                for var_item, idx in var:
                    try:
                        if idx is not None:
                            iscale.set_scaling_factor(var_item[idx], factor)
                        else:
                            iscale.set_scaling_factor(var_item, factor)
                    except Exception as e:
                        print(f'Warning: Cannot scale {var_path} at index {idx}: {e}', file=sys.stderr)
                return
            if index is not None:
                iscale.set_scaling_factor(var[index], factor)
            else:
                iscale.set_scaling_factor(var, factor)
        except Exception as e:
            print(f'Warning: Cannot set scaling for {var_path}: {e}', file=sys.stderr)

    def _create_costing(self):
        """Create costing blocks if costing is enabled in session.

        Creates:
        - System-level costing block (WaterTAPCosting or ZeroOrderCosting)
        - UnitModelCostingBlock for each unit with costing_enabled=True
        - Calls cost_process() to aggregate costs
        """
        costing_config = self._session.costing_config
        requested_units = [{'unit_id': unit_id, 'unit_type': unit_inst.unit_type} for unit_id, unit_inst in self._session.units.items() if unit_inst.costing_enabled]
        self._costing_report = {'enabled': bool(costing_config and costing_config.get('enabled')), 'package': (costing_config or {}).get('package'), 'requested_units': requested_units, 'supported_units': [], 'skipped_units': [], 'warnings': [], 'product_stream': None}
        if not costing_config or not costing_config.get('enabled'):
            return
        costing_package = costing_config.get('package', 'watertap')
        try:
            from idaes.core import UnitModelCostingBlock
            if costing_package == 'watertap':
                from watertap.costing import WaterTAPCosting
                self._flowsheet.costing = WaterTAPCosting()
            elif costing_package == 'zero_order':
                from watertap.costing import ZeroOrderCosting
                self._flowsheet.costing = ZeroOrderCosting()
            else:
                self._costing_report['warnings'].append(f"Unknown costing package '{costing_package}'")
                return
            costing_block = self._flowsheet.costing
            self._apply_flowsheet_costing_parameters(costing_block, costing_config)
            for unit_id, unit_inst in self._session.units.items():
                if not unit_inst.costing_enabled:
                    continue
                unit_block = self._units.get(unit_id)
                if unit_block is None:
                    self._costing_report['skipped_units'].append({'unit_id': unit_id, 'unit_type': unit_inst.unit_type, 'reason': 'Unit block not found in built model'})
                    continue
                costing_method, method_source, reason = self._resolve_costing_method(unit_inst, unit_block, costing_block, costing_package)
                if reason is not None:
                    self._costing_report['skipped_units'].append({'unit_id': unit_id, 'unit_type': unit_inst.unit_type, 'reason': reason})
                    continue
                try:
                    kwargs = {'flowsheet_costing_block': costing_block}
                    if costing_method is not None:
                        kwargs['costing_method'] = costing_method
                    unit_block.costing = UnitModelCostingBlock(**kwargs)
                    self._costing_report['supported_units'].append({'unit_id': unit_id, 'unit_type': unit_inst.unit_type, 'method_source': method_source})
                except Exception as e:
                    self._costing_report['skipped_units'].append({'unit_id': unit_id, 'unit_type': unit_inst.unit_type, 'reason': str(e)})
            self._apply_unit_costing_parameters(costing_block, costing_config)
            try:
                costing_block.cost_process()
            except Exception as e:
                self._costing_report['warnings'].append(f'cost_process() failed: {e}')
            self.ensure_lcow()
        except ImportError as e:
            self._costing_report['warnings'].append(f'Cannot create costing - missing imports: {e}')
        except Exception as e:
            self._costing_report['warnings'].append(f'Costing creation failed: {e}')

    def _apply_flowsheet_costing_parameters(self, costing_block: Any, costing_config: Dict[str, Any]) -> None:
        """Apply supported flowsheet-level costing parameters."""
        parameter_map = {'electricity_cost': 'electricity_cost', 'utilization_factor': 'utilization_factor', 'plant_lifetime': 'plant_lifetime', 'factor_total_investment': 'total_investment_factor'}
        for config_key, attr_name in parameter_map.items():
            if config_key not in costing_config:
                continue
            target = getattr(costing_block, attr_name, None)
            if target is None or not hasattr(target, 'fix'):
                self._costing_report['warnings'].append(f"Cannot apply costing parameter '{config_key}' to flowsheet costing block")
                continue
            try:
                target.fix(costing_config[config_key])
            except Exception as exc:
                self._costing_report['warnings'].append(f'Cannot set {config_key}: {exc}')

    def _apply_unit_costing_parameters(self, costing_block: Any, costing_config: Dict[str, Any]) -> None:
        """Apply unit-specific costing parameter overrides after costing blocks exist."""
        membrane_cost = costing_config.get('membrane_cost')
        if membrane_cost is None:
            return
        applied_blocks: List[str] = []
        for block_name in ('reverse_osmosis', 'nanofiltration'):
            param_block = getattr(costing_block, block_name, None)
            if param_block is None:
                continue
            target = getattr(param_block, 'membrane_cost', None)
            if target is None or not hasattr(target, 'fix'):
                continue
            try:
                target.fix(membrane_cost)
                applied_blocks.append(block_name)
            except Exception as exc:
                self._costing_report['warnings'].append(f'Cannot set membrane_cost on {block_name}: {exc}')
        if membrane_cost is not None and (not applied_blocks):
            self._costing_report['warnings'].append('membrane_cost was provided but no membrane costing parameter blocks were active')

    def _resolve_costing_method(self, unit_inst: UnitInstance, unit_block: Any, costing_block: Any, costing_package: str) -> Tuple[Optional[Any], str, Optional[str]]:
        """Resolve the costing method for a unit or explain why costing is unsupported."""
        spec = UNITS[unit_inst.unit_type]
        costing = spec.costing
        if not unit_supports_costing_package(spec, costing_package):
            return (None, costing.source.value, f"Unit type '{unit_inst.unit_type}' is not cost-supported on the pinned upstream WaterTAP stack for costing package '{costing_package}'")
        if costing.method_module and costing.method_name:
            module = importlib.import_module(costing.method_module)
            return (getattr(module, costing.method_name), costing.source.value, None)
        runtime_method = None
        try:
            runtime_method = costing_block._get_costing_method_for(unit_block)
        except Exception:
            runtime_method = None
        if runtime_method is not None:
            return (runtime_method, costing.source.value, None)
        return (None, costing.source.value, f"Unit type '{unit_inst.unit_type}' is marked as costing-supported, but no runtime costing method was available on the built unit")

    def _costing_report_status(self) -> str:
        """Return the structured costing status for the current build."""
        if not self._costing_report.get('enabled'):
            return 'disabled'
        supported_units = self._costing_report.get('supported_units', [])
        skipped_units = self._costing_report.get('skipped_units', [])
        if not supported_units:
            return 'error'
        if skipped_units:
            return 'partial'
        return 'complete'

    def _candidate_product_states(self, unit_id: str, requested_port_name: str) -> List[Tuple[str, str]]:
        """Return state-block attribute candidates for a requested product port."""
        unit_inst = self._session.units.get(unit_id)
        normalized_port = requested_port_name.lower()
        if unit_inst is not None and unit_inst.unit_type == 'Product':
            return [('properties', 'inlet')]
        port_map = {'inlet': [('process_flow.properties_in', 'inlet'), ('properties_in', 'inlet'), ('properties', 'inlet')], 'outlet': [('separator.treated_state', 'outlet'), ('control_volume.properties_out', 'outlet'), ('process_flow.properties_out', 'outlet'), ('dissolution_reactor.properties_out', 'outlet'), ('precipitation_reactor.properties_out', 'outlet'), ('properties_out_liq', 'outlet'), ('properties_out', 'outlet'), ('properties', 'outlet')], 'permeate': [('mixed_permeate', 'permeate'), ('properties_permeate', 'permeate')], 'retentate': [('properties_retentate', 'retentate'), ('feed_side.properties_out', 'retentate')], 'treated': [('properties_treated', 'treated'), ('properties', 'treated')], 'product': [('properties_product', 'product'), ('properties', 'product')], 'brine': [('properties_retentate', 'brine'), ('properties', 'brine')], 'regen': [('regeneration_stream', 'regen')], 'diluate': [('diluate.properties_out', 'outlet_diluate')], 'outlet_diluate': [('diluate.properties_out', 'outlet_diluate')], 'concentrate': [('concentrate.properties_out', 'outlet_concentrate')], 'outlet_concentrate': [('concentrate.properties_out', 'outlet_concentrate')]}
        return port_map.get(normalized_port, [(f'properties_{normalized_port}', normalized_port), ('properties', normalized_port)])

    def _get_nested_attr(self, obj: Any, dotted_path: str) -> Any:
        """Resolve a dotted attribute path on a live Pyomo block."""
        current = obj
        for segment in dotted_path.split('.'):
            current = getattr(current, segment, None)
            if current is None:
                return None
        return current

    def _coerce_state_block(self, state_candidate: Any) -> Any:
        """Normalize an indexed state block candidate to the t=0 state block."""
        if state_candidate is None:
            return None
        if hasattr(state_candidate, 'flow_vol') or hasattr(state_candidate, 'flow_vol_phase'):
            return state_candidate
        for index in (0, 0.0):
            try:
                return state_candidate[index]
            except Exception:
                continue
        return state_candidate

    def _state_flow_reference(self, state_block: Any) -> Optional[Any]:
        """Return a volumetric product-flow reference for LCOW calculations."""
        state_block = self._coerce_state_block(state_block)
        if state_block is None:
            return None
        flow_vol = getattr(state_block, 'flow_vol', None)
        if flow_vol is not None:
            if hasattr(flow_vol, 'is_indexed') and flow_vol.is_indexed():
                for index in flow_vol:
                    return flow_vol[index]
            return flow_vol
        flow_vol_phase = getattr(state_block, 'flow_vol_phase', None)
        if flow_vol_phase is not None:
            for preferred_index in ('Liq', 0, 0.0):
                try:
                    return flow_vol_phase[preferred_index]
                except Exception:
                    continue
            for index in flow_vol_phase:
                return flow_vol_phase[index]
        return None

    def _configured_product_stream(self) -> Tuple[Optional[str], str]:
        """Return the configured product stream from session costing config, if any."""
        costing_config = self._session.costing_config or {}
        return (costing_config.get('product_stream_unit'), costing_config.get('product_stream_port', 'permeate'))

    def resolve_product_stream(self, product_stream_unit: Optional[str]=None, product_stream_port: str='permeate') -> Tuple[Optional[Any], Dict[str, Any]]:
        """Resolve the product stream used for LCOW calculations."""
        if product_stream_unit is not None:
            requested = [(product_stream_unit, product_stream_port, 'explicit')]
        else:
            requested = []
            for unit_id, unit_inst in self._session.units.items():
                if unit_inst.unit_type == 'Product':
                    requested.append((unit_id, 'inlet', 'auto'))
            connected_source_ports = {(connection.source_unit, connection.source_port) for connection in self._session.connections}
            for unit_id, unit_inst in self._session.units.items():
                if unit_inst.unit_type in {'Product', 'Feed', 'FeedZO'}:
                    continue
                for port_name in ('permeate', 'treated', 'outlet', 'outlet_diluate'):
                    if (unit_id, port_name) in connected_source_ports:
                        continue
                    requested.append((unit_id, port_name, 'auto_terminal'))
            for unit_id, unit_inst in self._session.units.items():
                if unit_inst.unit_type in {'Product', 'Feed', 'FeedZO'}:
                    continue
                for port_name in ('permeate', 'treated', 'outlet', 'outlet_diluate'):
                    requested.append((unit_id, port_name, 'auto'))
        for unit_id, requested_port_name, resolution in requested:
            unit_block = self._units.get(unit_id)
            if unit_block is None:
                continue
            if resolution != 'explicit' and (not hasattr(unit_block, requested_port_name)):
                continue
            for attr_path, actual_port_name in self._candidate_product_states(unit_id, requested_port_name):
                state_candidate = self._get_nested_attr(unit_block, attr_path)
                flow_ref = self._state_flow_reference(state_candidate)
                if flow_ref is None:
                    continue
                metadata = {'unit_id': unit_id, 'port_name': actual_port_name, 'requested_port_name': requested_port_name, 'canonical_stream': f'{unit_id}.{actual_port_name}', 'resolution': resolution}
                return (flow_ref, metadata)
        return (None, {'unit_id': product_stream_unit, 'port_name': product_stream_port, 'requested_port_name': product_stream_port, 'canonical_stream': None, 'resolution': 'unresolved'})

    def ensure_lcow(self, product_stream_unit: Optional[str]=None, product_stream_port: str='permeate') -> Dict[str, Any]:
        """Ensure LCOW is registered on the flowsheet costing block."""
        costing_block = getattr(self._flowsheet, 'costing', None)
        if costing_block is None:
            return {'resolved': False, 'reason': 'Flowsheet costing block was not created'}
        configured_unit, configured_port = self._configured_product_stream()
        if product_stream_unit is None and configured_unit is not None:
            product_stream_unit = configured_unit
            product_stream_port = configured_port
        if hasattr(costing_block, 'LCOW') and self._costing_report.get('product_stream'):
            return self._costing_report['product_stream']
        product_flow, metadata = self.resolve_product_stream(product_stream_unit=product_stream_unit, product_stream_port=product_stream_port)
        if product_flow is None:
            metadata['resolved'] = False
            metadata['reason'] = 'No product stream could be resolved for LCOW calculation'
            self._costing_report['product_stream'] = metadata
            if metadata['reason'] not in self._costing_report['warnings']:
                self._costing_report['warnings'].append(metadata['reason'])
            return metadata
        metadata['resolved'] = True
        self._costing_report['product_stream'] = metadata
        if hasattr(costing_block, 'LCOW'):
            return metadata
        try:
            costing_block.add_LCOW(product_flow)
        except Exception as exc:
            metadata['resolved'] = False
            metadata['reason'] = f'add_LCOW() failed: {exc}'
            if metadata['reason'] not in self._costing_report['warnings']:
                self._costing_report['warnings'].append(metadata['reason'])
        return metadata

    def extract_costing_results(self, product_stream_unit: Optional[str]=None, product_stream_port: str='permeate', source: str='rebuilt_model') -> Dict[str, Any]:
        """Extract a structured costing payload from the current model."""
        from pyomo.environ import value
        costing_block = getattr(self._flowsheet, 'costing', None)
        report = self.get_costing_report()
        payload: Dict[str, Any] = {'session_id': self._session.config.session_id, 'source': source, 'status': report['status'], 'supported_units': report['supported_units'], 'skipped_units': report['skipped_units'], 'warnings': list(report['warnings']), 'product_stream': report.get('product_stream')}
        if costing_block is None:
            payload['status'] = 'error'
            payload['error'] = 'Costing block not created. Enable flowsheet costing first.'
            return payload
        if report['status'] == 'error':
            payload['error'] = 'No requested units could be costed with the active costing package.'
            return payload
        product_metadata = self.ensure_lcow(product_stream_unit=product_stream_unit, product_stream_port=product_stream_port)
        payload['product_stream'] = product_metadata
        payload['warnings'] = list(self._costing_report['warnings'])
        scalar_fields = {'LCOW': 'LCOW', 'total_capital_cost': 'total_capital_cost', 'total_operating_cost': 'total_operating_cost', 'total_annualized_cost': 'total_annualized_cost', 'capital_recovery_factor': 'capital_recovery_factor', 'utilization_factor': 'utilization_factor', 'aggregate_capital_cost': 'aggregate_capital_cost', 'aggregate_fixed_operating_cost': 'aggregate_fixed_operating_cost', 'aggregate_variable_operating_cost': 'aggregate_variable_operating_cost', 'maintenance_labor_chemical_operating_cost': 'maintenance_labor_chemical_operating_cost'}
        for output_key, attr_name in scalar_fields.items():
            component = getattr(costing_block, attr_name, None)
            if component is None:
                continue
            try:
                payload[output_key] = value(component, exception=False)
            except Exception:
                payload[output_key] = None
        electricity_component = getattr(costing_block, 'aggregate_flow_electricity', None)
        if electricity_component is not None:
            payload['aggregate_electricity'] = value(electricity_component, exception=False)
        else:
            try:
                payload['aggregate_electricity'] = value(costing_block.aggregate_flow_costs['electricity'], exception=False)
            except Exception:
                payload['aggregate_electricity'] = None
        unit_costs: Dict[str, Dict[str, Any]] = {}
        for unit_meta in report['supported_units']:
            unit_id = unit_meta['unit_id']
            unit_block = self._units.get(unit_id)
            if unit_block is None or not hasattr(unit_block, 'costing'):
                continue
            unit_payload: Dict[str, Any] = {}
            for attr_name in ('capital_cost', 'fixed_operating_cost', 'variable_operating_cost'):
                component = getattr(unit_block.costing, attr_name, None)
                if component is None:
                    continue
                try:
                    unit_payload[attr_name] = value(component, exception=False)
                except Exception:
                    unit_payload[attr_name] = None
            if unit_payload:
                unit_costs[unit_id] = unit_payload
        if unit_costs:
            payload['unit_costs'] = unit_costs
        unresolved = [key for key in ('total_capital_cost', 'total_operating_cost') if payload.get(key) is None]
        if unresolved:
            payload['status'] = 'partial'
            warning = 'Aggregate costing metrics are unavailable until the flowsheet has a solved costing state'
            if warning not in payload['warnings']:
                payload['warnings'].append(warning)
        if not product_metadata.get('resolved'):
            payload['status'] = 'partial'
        return payload

    def get_costing_report(self) -> Dict[str, Any]:
        """Return the structured costing report for the current build."""
        report = dict(self._costing_report)
        report['status'] = self._costing_report_status()
        return report

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

    def get_reaction_packages(self) -> Dict[str, Any]:
        """Get the created biological reaction packages."""
        return self._reaction_packages

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
    return (model, builder.get_units())
