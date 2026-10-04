"""Configuration: the parameter registry (model quantities) and settings (operational).

Keep the two apart. A quantity that can change a published number belongs in
``config/parameters.toml`` and counts against the 30-parameter cap; everything else is a setting.
"""
from .parameters import Parameter, ParameterRegistry, RegistryError, load_registry
from .settings import Settings, load_settings

__all__ = ["Parameter", "ParameterRegistry", "RegistryError", "load_registry", "Settings", "load_settings"]
