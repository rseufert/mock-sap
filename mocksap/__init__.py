"""mock-sap - an open source black-box mock of SAP integration endpoints.

The mock speaks the shapes SAP speaks (OData V2 with SAP Gateway
conventions, BAPI/RFC over JSON and SOAP, IDocs) backed by SQLite.  It does
not implement any SAP business logic.
"""
import os
import re

__all__ = ["Config", "make_server", "__version__"]


def _discover_version():
    """The version comes from the installed package metadata.

    pyproject.toml is the single source of truth.  When the package is
    installed, importlib.metadata reads the version recorded at build time;
    when it is run straight from a checkout that was never installed, fall
    back to reading pyproject.toml next to the package.
    """
    try:
        from importlib.metadata import PackageNotFoundError, version
    except ImportError:  # pragma: no cover - Python < 3.8
        return "0+unknown"
    try:
        return version("mock-sap")
    except PackageNotFoundError:
        pyproject = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "pyproject.toml")
        try:
            with open(pyproject, encoding="utf-8") as handle:
                match = re.search(r'^version\s*=\s*"([^"]+)"', handle.read(), re.M)
        except OSError:
            return "0+unknown"
        return match.group(1) + "+source" if match else "0+unknown"


__version__ = _discover_version()

from .server import Config, make_server  # noqa: E402,F401
