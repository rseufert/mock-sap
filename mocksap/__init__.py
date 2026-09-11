"""mock-sap - an open source black-box mock of SAP integration endpoints.

The mock speaks the shapes SAP speaks (OData V2 with SAP Gateway
conventions, BAPI/RFC over JSON and SOAP, IDocs) backed by SQLite.  It does
not implement any SAP business logic.
"""
__version__ = "0.1.0"

from .server import Config, make_server  # noqa: F401
