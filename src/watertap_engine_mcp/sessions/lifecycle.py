"""Session lifecycle operations exposed by MCP actions."""

from __future__ import annotations

from typing import Any

from watertap_engine_mcp.core import PROPERTY_PACKAGES, FlowsheetSession, PropertyPackageType, SessionConfig
from watertap_engine_mcp.core import get_session_preset as get_session_preset_from_registry


class SessionLifecycle:
    """Create, read, list, and delete flowsheet sessions."""

    def __init__(self, store: Any):
        self.store = store

    def create(
        self,
        *,
        name: str = "",
        description: str = "",
        session_preset: str | None = None,
        property_package: str = "SEAWATER",
        property_package_config: dict[str, Any] | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """Create and persist a new flowsheet session."""

        try:
            if session_preset:
                preset = get_session_preset_from_registry(session_preset)
                pkg_type = preset.property_package
                merged_package_config = preset.merged_package_config(property_package_config)
                missing_overrides = [
                    key
                    for key in preset.required_override_keys
                    if merged_package_config.get(key) is None
                ]
                if missing_overrides:
                    return {
                        "error": (
                            f"Session preset {preset.name} requires property_package_config "
                            f"overrides for: {missing_overrides}"
                        ),
                        "session_preset": preset.name,
                        "property_package": pkg_type.name,
                        "required_override_keys": preset.required_override_keys,
                        "property_package_config_defaults": (
                            preset.property_package_config_defaults
                        ),
                    }
                property_package = pkg_type.name
                property_package_config = merged_package_config
            else:
                pkg_type = PropertyPackageType[property_package.upper()]
        except KeyError:
            valid = [p.name for p in PropertyPackageType]
            return {"error": f"Invalid property_package. Valid: {valid}"}

        pkg_spec = PROPERTY_PACKAGES.get(pkg_type)
        if pkg_spec and pkg_spec.requires_config and not property_package_config:
            return {
                "error": f"Property package {property_package} requires configuration",
                "required_fields": pkg_spec.config_fields,
                "config_help": pkg_spec.config_kwargs,
            }

        if session_id:
            try:
                self.store.load(session_id)
            except FileNotFoundError:
                pass
            else:
                return {"error": f"Session '{session_id}' already exists"}

        config_kwargs: dict[str, Any] = {}
        if session_id:
            config_kwargs["session_id"] = session_id

        config = SessionConfig(
            name=name,
            description=description,
            session_preset=session_preset.upper() if session_preset else None,
            id_strategy="user" if session_id else "generated",
            default_property_package=pkg_type,
            property_package_config=property_package_config or {},
            **config_kwargs,
        )
        session = FlowsheetSession(config=config)
        self.store.save(session)

        return {
            "session_id": session.config.session_id,
            "name": name,
            "session_preset": session.config.session_preset,
            "property_package": property_package,
            "property_package_config": property_package_config,
            "status": session.status.value,
            "created_at": session.config.created_at,
        }

    def get(self, session_id: str) -> dict[str, Any]:
        """Return a serialized session."""

        try:
            return self.store.load(session_id).to_dict()
        except FileNotFoundError:
            return {"error": f"Session '{session_id}' not found"}

    def list(self) -> list[dict[str, Any]]:
        """Return session summaries."""

        return self.store.list_sessions()

    def delete(self, session_id: str) -> dict[str, Any]:
        """Delete one session."""

        try:
            self.store.delete(session_id)
            return {"deleted": session_id}
        except FileNotFoundError:
            return {"error": f"Session '{session_id}' not found"}

