"""Declarative definitions of the SAP OData shapes this mock serves.

Everything the mock knows about SAP entity shapes lives here.  The database
tables, the ``$metadata`` (EDMX) document, the JSON payload shapes and the
seed data are all derived from these definitions, so adding an entity set is
a matter of appending to this file - no other module needs to change.

The field names follow the public S/4HANA Cloud OData V2 APIs
(API_BUSINESS_PARTNER_SRV, API_SALES_ORDER_SRV, ...) so payloads look like
the real thing to any client that was written against SAP.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

# --------------------------------------------------------------------------
# Building blocks
# --------------------------------------------------------------------------


@dataclass
class Prop:
    """A single EDM property of an entity type."""

    name: str
    type: str = "Edm.String"  # Edm.String/Int32/Decimal/DateTime/Boolean/Time
    key: bool = False
    nullable: bool = True
    max_length: Optional[int] = None
    precision: Optional[int] = None
    scale: Optional[int] = None
    label: str = ""
    creatable: bool = True
    updatable: bool = True

    @property
    def sql_type(self) -> str:
        return {
            "Edm.Int32": "INTEGER",
            "Edm.Int16": "INTEGER",
            "Edm.Decimal": "REAL",
            "Edm.Double": "REAL",
            "Edm.Boolean": "INTEGER",
        }.get(self.type, "TEXT")


@dataclass
class Nav:
    """A navigation property (association) between two entity types."""

    name: str
    target: str  # entity type name
    multiplicity: str  # "1" or "*"
    join: List[Tuple[str, str]]  # [(local_column, target_column), ...]


@dataclass
class EntityType:
    name: str
    props: List[Prop]
    navs: List[Nav] = field(default_factory=list)
    label: str = ""

    @property
    def edm_name(self) -> str:
        """SAP names the EDM entity type with a `Type` suffix."""
        return self.name + "Type"

    @property
    def keys(self) -> List[Prop]:
        return [p for p in self.props if p.key]

    def prop(self, name: str) -> Optional[Prop]:
        for p in self.props:
            if p.name == name:
                return p
        return None

    def nav(self, name: str) -> Optional[Nav]:
        for n in self.navs:
            if n.name == name:
                return n
        return None


@dataclass
class Service:
    """One OData service, e.g. /sap/opu/odata/sap/API_SALES_ORDER_SRV."""

    name: str
    namespace: str
    title: str
    sets: Dict[str, str]  # EntitySet name -> EntityType name

    @property
    def path(self) -> str:
        return "/sap/opu/odata/sap/" + self.name


def S(name, **kw):
    """Short-hand for a string property."""
    return Prop(name, "Edm.String", **kw)


def DT(name, **kw):
    return Prop(name, "Edm.DateTime", **kw)


def DEC(name, precision=13, scale=3, **kw):
    return Prop(name, "Edm.Decimal", precision=precision, scale=scale, **kw)


def BOOL(name, **kw):
    return Prop(name, "Edm.Boolean", **kw)


# --------------------------------------------------------------------------
# Entity types - API_BUSINESS_PARTNER_SRV
# --------------------------------------------------------------------------

ENTITY_TYPES: Dict[str, EntityType] = {}


def _register(et: EntityType) -> EntityType:
    ENTITY_TYPES[et.name] = et
    return et


_register(
    EntityType(
        "A_BusinessPartner",
        label="Business Partner",
        props=[
            S("BusinessPartner", key=True, nullable=False, max_length=10, label="Business Partner", updatable=False),
            S("Customer", max_length=10, label="Customer"),
            S("Supplier", max_length=10, label="Supplier"),
            S("BusinessPartnerCategory", max_length=1, label="BP Category"),
            S("BusinessPartnerFullName", max_length=81, label="Full Name"),
            S("BusinessPartnerName", max_length=81, label="Name"),
            S("BusinessPartnerGrouping", max_length=4, label="Grouping"),
            S("OrganizationBPName1", max_length=40, label="Name 1"),
            S("FirstName", max_length=40, label="First Name"),
            S("LastName", max_length=40, label="Last Name"),
            S("SearchTerm1", max_length=20, label="Search Term"),
            S("Industry", max_length=10, label="Industry"),
            S("CreatedByUser", max_length=12, label="Created By", creatable=False, updatable=False),
            DT("CreationDate", label="Created On", creatable=False, updatable=False),
            S("LastChangedByUser", max_length=12, label="Changed By", creatable=False, updatable=False),
            DT("LastChangeDate", label="Changed On", creatable=False, updatable=False),
            BOOL("BusinessPartnerIsBlocked", label="Central Block"),
        ],
        navs=[
            Nav("to_BusinessPartnerAddress", "A_BusinessPartnerAddress", "*", [("BusinessPartner", "BusinessPartner")]),
            Nav("to_BusinessPartnerRole", "A_BusinessPartnerRole", "*", [("BusinessPartner", "BusinessPartner")]),
        ],
    )
)

_register(
    EntityType(
        "A_BusinessPartnerAddress",
        label="BP Address",
        props=[
            S("BusinessPartner", key=True, nullable=False, max_length=10),
            S("AddressID", key=True, nullable=False, max_length=10),
            S("CityName", max_length=40, label="City"),
            S("PostalCode", max_length=10, label="Postal Code"),
            S("StreetName", max_length=60, label="Street"),
            S("HouseNumber", max_length=10, label="House Number"),
            S("Country", max_length=3, label="Country"),
            S("Region", max_length=3, label="Region"),
            S("Language", max_length=2, label="Language"),
            S("PhoneNumber", max_length=30, label="Telephone"),
            S("EmailAddress", max_length=241, label="E-Mail"),
        ],
    )
)

_register(
    EntityType(
        "A_BusinessPartnerRole",
        label="BP Role",
        props=[
            S("BusinessPartner", key=True, nullable=False, max_length=10),
            S("BusinessPartnerRole", key=True, nullable=False, max_length=6),
            DT("ValidFrom"),
            DT("ValidTo"),
        ],
    )
)

# --------------------------------------------------------------------------
# Entity types - API_PRODUCT_SRV
# --------------------------------------------------------------------------

_register(
    EntityType(
        "A_Product",
        label="Product",
        props=[
            S("Product", key=True, nullable=False, max_length=40, label="Material", updatable=False),
            S("ProductType", max_length=4, label="Material Type"),
            S("ProductGroup", max_length=9, label="Material Group"),
            S("BaseUnit", max_length=3, label="Base Unit"),
            S("Division", max_length=2, label="Division"),
            S("ProductOldID", max_length=18, label="Old Material Number"),
            DEC("GrossWeight", label="Gross Weight"),
            DEC("NetWeight", label="Net Weight"),
            S("WeightUnit", max_length=3, label="Weight Unit"),
            S("ItemCategoryGroup", max_length=4, label="Item Category Group"),
            BOOL("IsMarkedForDeletion", label="Deletion Flag"),
            S("CreatedByUser", max_length=12, creatable=False, updatable=False),
            DT("CreationDate", creatable=False, updatable=False),
            DT("LastChangeDate", creatable=False, updatable=False),
        ],
        navs=[
            Nav("to_Description", "A_ProductDescription", "*", [("Product", "Product")]),
            Nav("to_Plant", "A_ProductPlant", "*", [("Product", "Product")]),
        ],
    )
)

_register(
    EntityType(
        "A_ProductDescription",
        label="Product Description",
        props=[
            S("Product", key=True, nullable=False, max_length=40),
            S("Language", key=True, nullable=False, max_length=2),
            S("ProductDescription", max_length=40, label="Description"),
        ],
    )
)

_register(
    EntityType(
        "A_ProductPlant",
        label="Product Plant",
        props=[
            S("Product", key=True, nullable=False, max_length=40),
            S("Plant", key=True, nullable=False, max_length=4),
            S("PurchasingGroup", max_length=3),
            S("ProfitCenter", max_length=10),
            S("AvailabilityCheckType", max_length=2),
            S("MRPType", max_length=2),
            BOOL("IsMarkedForDeletion"),
        ],
    )
)

# --------------------------------------------------------------------------
# Entity types - API_SALES_ORDER_SRV
# --------------------------------------------------------------------------

_register(
    EntityType(
        "A_SalesOrder",
        label="Sales Order",
        props=[
            S("SalesOrder", key=True, nullable=False, max_length=10, label="Sales Order", creatable=False, updatable=False),
            S("SalesOrderType", max_length=4, label="Order Type"),
            S("SalesOrganization", max_length=4, label="Sales Organization"),
            S("DistributionChannel", max_length=2, label="Distribution Channel"),
            S("OrganizationDivision", max_length=2, label="Division"),
            S("SoldToParty", max_length=10, label="Sold-To Party"),
            S("PurchaseOrderByCustomer", max_length=35, label="Customer Reference"),
            S("TransactionCurrency", max_length=5, label="Currency"),
            DEC("TotalNetAmount", precision=16, scale=3, label="Net Value", creatable=False, updatable=False),
            DT("SalesOrderDate", label="Document Date"),
            DT("RequestedDeliveryDate", label="Requested Delivery Date"),
            S("ShippingCondition", max_length=2, label="Shipping Condition"),
            S("IncotermsClassification", max_length=3, label="Incoterms"),
            S("CustomerPaymentTerms", max_length=4, label="Payment Terms"),
            S("OverallSDProcessStatus", max_length=1, label="Overall Status", creatable=False, updatable=False),
            S("OverallDeliveryStatus", max_length=1, label="Delivery Status", creatable=False, updatable=False),
            S("CreatedByUser", max_length=12, creatable=False, updatable=False),
            DT("CreationDate", creatable=False, updatable=False),
            DT("LastChangeDate", creatable=False, updatable=False),
        ],
        navs=[
            Nav("to_Item", "A_SalesOrderItem", "*", [("SalesOrder", "SalesOrder")]),
            Nav("to_Partner", "A_SalesOrderHeaderPartner", "*", [("SalesOrder", "SalesOrder")]),
        ],
    )
)

_register(
    EntityType(
        "A_SalesOrderItem",
        label="Sales Order Item",
        props=[
            S("SalesOrder", key=True, nullable=False, max_length=10),
            S("SalesOrderItem", key=True, nullable=False, max_length=6),
            S("HigherLevelItem", max_length=6),
            S("Material", max_length=40, label="Material"),
            S("MaterialByCustomer", max_length=35),
            S("SalesOrderItemText", max_length=40, label="Item Description"),
            S("SalesOrderItemCategory", max_length=4),
            DEC("RequestedQuantity", label="Order Quantity"),
            S("RequestedQuantityUnit", max_length=3),
            DEC("NetAmount", precision=16, scale=3, label="Net Value"),
            S("TransactionCurrency", max_length=5),
            S("MaterialGroup", max_length=9),
            S("Plant", max_length=4),
            S("ShippingPoint", max_length=4),
            DT("RequestedDeliveryDate"),
        ],
        navs=[
            Nav("to_SalesOrder", "A_SalesOrder", "1", [("SalesOrder", "SalesOrder")]),
        ],
    )
)

_register(
    EntityType(
        "A_SalesOrderHeaderPartner",
        label="Sales Order Partner",
        props=[
            S("SalesOrder", key=True, nullable=False, max_length=10),
            S("PartnerFunction", key=True, nullable=False, max_length=2),
            S("Customer", max_length=10),
            S("Supplier", max_length=10),
            S("Personnel", max_length=8),
            S("ContactPerson", max_length=10),
            S("AddressID", max_length=10),
        ],
    )
)

# --------------------------------------------------------------------------
# Entity types - API_PURCHASEORDER_PROCESS_SRV
# --------------------------------------------------------------------------

_register(
    EntityType(
        "A_PurchaseOrder",
        label="Purchase Order",
        props=[
            S("PurchaseOrder", key=True, nullable=False, max_length=10, creatable=False, updatable=False),
            S("PurchaseOrderType", max_length=4, label="Document Type"),
            S("CompanyCode", max_length=4),
            S("PurchasingOrganization", max_length=4),
            S("PurchasingGroup", max_length=3),
            S("Supplier", max_length=10, label="Supplier"),
            S("DocumentCurrency", max_length=5),
            S("Language", max_length=2),
            S("PaymentTerms", max_length=4),
            Prop("NetPaymentDays", "Edm.Int32"),
            DT("PurchaseOrderDate"),
            S("CreatedByUser", max_length=12, creatable=False, updatable=False),
            DT("CreationDate", creatable=False, updatable=False),
            S("PurchasingProcessingStatus", max_length=2, creatable=False, updatable=False),
        ],
        navs=[
            Nav("to_PurchaseOrderItem", "A_PurchaseOrderItem", "*", [("PurchaseOrder", "PurchaseOrder")]),
        ],
    )
)

_register(
    EntityType(
        "A_PurchaseOrderItem",
        label="Purchase Order Item",
        props=[
            S("PurchaseOrder", key=True, nullable=False, max_length=10),
            S("PurchaseOrderItem", key=True, nullable=False, max_length=5),
            S("PurchaseOrderItemText", max_length=40),
            S("Material", max_length=40),
            S("MaterialGroup", max_length=9),
            S("Plant", max_length=4),
            S("StorageLocation", max_length=4),
            DEC("OrderQuantity"),
            S("PurchaseOrderQuantityUnit", max_length=3),
            DEC("NetPriceAmount", precision=16, scale=2),
            DEC("NetPriceQuantity", precision=13, scale=3),
            S("DocumentCurrency", max_length=5),
            S("TaxCode", max_length=2),
            BOOL("IsCompletelyDelivered"),
        ],
    )
)

# --------------------------------------------------------------------------
# Services
# --------------------------------------------------------------------------

SERVICES: Dict[str, Service] = {}

for _svc in [
    Service(
        "API_BUSINESS_PARTNER_SRV",
        "API_BUSINESS_PARTNER",
        "Business Partner (A2X)",
        {
            "A_BusinessPartner": "A_BusinessPartner",
            "A_BusinessPartnerAddress": "A_BusinessPartnerAddress",
            "A_BusinessPartnerRole": "A_BusinessPartnerRole",
        },
    ),
    Service(
        "API_PRODUCT_SRV",
        "API_PRODUCT_SRV",
        "Product (A2X)",
        {
            "A_Product": "A_Product",
            "A_ProductDescription": "A_ProductDescription",
            "A_ProductPlant": "A_ProductPlant",
        },
    ),
    Service(
        "API_SALES_ORDER_SRV",
        "API_SALES_ORDER_SRV",
        "Sales Order (A2X)",
        {
            "A_SalesOrder": "A_SalesOrder",
            "A_SalesOrderItem": "A_SalesOrderItem",
            "A_SalesOrderHeaderPartner": "A_SalesOrderHeaderPartner",
        },
    ),
    Service(
        "API_PURCHASEORDER_PROCESS_SRV",
        "API_PURCHASEORDER_PROCESS_SRV",
        "Purchase Order (A2X)",
        {
            "A_PurchaseOrder": "A_PurchaseOrder",
            "A_PurchaseOrderItem": "A_PurchaseOrderItem",
        },
    ),
]:
    SERVICES[_svc.name] = _svc


def service_for_path(path: str):
    """Return (service, remainder) for a request path, or (None, None)."""
    for svc in SERVICES.values():
        if path == svc.path:
            return svc, ""
        if path.startswith(svc.path + "/"):
            return svc, path[len(svc.path) + 1:]
    return None, None


def set_for_type(svc: Service, type_name: str) -> str:
    """Entity set name inside `svc` that exposes `type_name`."""
    for set_name, tname in svc.sets.items():
        if tname == type_name:
            return set_name
    return type_name
