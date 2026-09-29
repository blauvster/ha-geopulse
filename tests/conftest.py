"""Pytest configuration for the GeoPulse test suite.

pytest-homeassistant-custom-component (needed for config-flow/coordinator/
entity tests from Phase 2 onward) doesn't yet publish wheels for Python
3.14, which is what's on this machine. Load it only if present so the
Phase 1 api.py tests - which have no Home Assistant dependency - can run
standalone; revisit once a supported interpreter is available.
"""

import importlib.util

if importlib.util.find_spec("pytest_homeassistant_custom_component") is not None:
    pytest_plugins = "pytest_homeassistant_custom_component"
