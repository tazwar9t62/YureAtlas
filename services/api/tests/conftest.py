import os

import pytest


def pytest_configure(config):
    if os.getenv("YUREATLAS_INTEGRATION") == "1":
        from scripts.test_backend import validate_isolation

        try:
            validate_isolation(os.environ)
        except Exception as error:  # noqa: BLE001 - fail closed without exposing connection credentials
            raise pytest.UsageError(
                f"Unsafe integration environment; use scripts/test_backend.py ({type(error).__name__})"
            ) from None


def pytest_collection_modifyitems(items):
    if os.getenv("YUREATLAS_INTEGRATION") != "1":
        for item in items:
            if item.get_closest_marker("integration"):
                item.add_marker(pytest.mark.skip(reason="Use scripts/test_backend.py for owned PostGIS"))
