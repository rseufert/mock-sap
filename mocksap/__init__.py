"""mock-sap - an open source black-box mock of SAP integration endpoints.

The mock speaks the shapes SAP speaks (OData V2 with SAP Gateway
conventions, BAPI/RFC over JSON and SOAP, IDocs) backed by SQLite.  It does
not implement any SAP business logic.
"""
import os
import re

__all__ = ["Config", "make_server", "__version__"]


def _discover_version():
    """pyproject.toml is the single source of truth for the version.

    Running from a checkout, the pyproject.toml sitting next to the package is
    authoritative and the version is marked `+source`; stale build metadata in
    the working tree (a leftover *.egg-info directory, say) would otherwise
    shadow it and report a version that has already moved on. Installed - in
    site-packages, a wheel, a container - there is no pyproject.toml alongside,
    and the version recorded at build time is read instead.
    """
    pyproject = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "pyproject.toml")
    try:
        with open(pyproject, encoding="utf-8") as handle:
            match = re.search(r'^version\s*=\s*"([^"]+)"', handle.read(), re.M)
        if match:
            return match.group(1) + "+source"
    except OSError:
        pass
    try:
        from importlib.metadata import PackageNotFoundError, version
    except ImportError:  # pragma: no cover - Python < 3.8
        return "0+unknown"
    try:
        return version("mock-sap")
    except PackageNotFoundError:
        return "0+unknown"


__version__ = _discover_version()

from .server import Config, make_server  # noqa: E402,F401
