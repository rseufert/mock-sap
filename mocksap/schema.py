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
    concurrency: bool = False  # EDM ConcurrencyMode="Fixed": feeds the ETag
    complex_type: Optional[str] = None  # names a ComplexType instead of an Edm type

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
class ComplexType:
    """A structured property type, e.g. GWSAMPLE_BASIC's CT_Address.

    Its sub-properties are stored flattened - `Address` + `City` becomes the
    column `Address_City` - and nested again on the wire.
    """

    name: str
    props: List[Prop]
    label: str = ""

    def prop(self, name: str) -> Optional[Prop]:
        for p in self.props:
            if p.name == name:
                return p
        return None


COMPLEX_TYPES: Dict[str, "ComplexType"] = {}


def register_complex(ct: ComplexType) -> ComplexType:
    COMPLEX_TYPES[ct.name] = ct
    return ct


@dataclass
class Nav:
    """A navigation property (association) between two entity types."""

    name: str
    target: str  # entity type name
    multiplicity: str  # "1" or "*"
    join: List[Tuple[str, str]]  # [(local_column, target_column), ...]


@dataclass
class UI:
    """What a Fiori elements app needs to know to draw this entity type.

    A small, deliberate subset of SAP's UI vocabulary: enough for a list
    report and an object page to render without hand-editing a manifest.
    """

    type_name: str = ""
    type_name_plural: str = ""
    title: str = ""               # property shown as the heading
    description: str = ""         # property shown beneath it
    line_items: List[str] = field(default_factory=list)      # list report columns
    selection_fields: List[str] = field(default_factory=list)  # filter bar
    identification: List[str] = field(default_factory=list)  # object page fields
    insertable: bool = True
    updatable: bool = True
    deletable: bool = True


@dataclass
class EntityType:
    name: str
    props: List[Prop]
    navs: List[Nav] = field(default_factory=list)
    label: str = ""
    # The S/4 A2X APIs name the EDM type A_SalesOrderType; the classic
    # Gateway services name it plainly, BusinessPartner.
    edm_suffix: str = "Type"
    ui: Optional["UI"] = None

    def columns(self) -> List[Tuple[str, Prop]]:
        """Every stored column: (column name, the property behind it).

        A complex property contributes one column per sub-property; a plain
        one contributes itself.
        """
        out: List[Tuple[str, Prop]] = []
        for p in self.props:
            if p.complex_type:
                for sub in COMPLEX_TYPES[p.complex_type].props:
                    out.append(("%s_%s" % (p.name, sub.name), sub))
            else:
                out.append((p.name, p))
        return out

    def resolve(self, path: str) -> Optional[Tuple[str, Prop]]:
        """Resolve `Address/City` (or a plain name) to its column."""
        parts = path.split("/")
        prop = self.prop(parts[0])
        if prop is None:
            return None
        if len(parts) == 1:
            return None if prop.complex_type else (prop.name, prop)
        if len(parts) != 2 or not prop.complex_type:
            return None
        sub = COMPLEX_TYPES[prop.complex_type].prop(parts[1])
        if sub is None:
            return None
        return ("%s_%s" % (prop.name, sub.name), sub)

    @property
    def concurrency_props(self) -> List[Prop]:
        """Properties that make up this type's ETag, if it has one."""
        return [p for p in self.props if p.concurrency]

    @property
    def edm_name(self) -> str:
        """The type name as it appears in $metadata and __metadata.type."""
        return self.name + self.edm_suffix

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
    prefix: str = "sap"   # the segment before the service name in the URL
    version: int = 2      # the OData version this service speaks

    @property
    def path(self) -> str:
        if self.version >= 4:
            # S/4HANA serves its V4 services from a longer, versioned path
            return "/sap/opu/odata4/%s/%s/srvd_a2x/sap/%s/0001" % (
                self.prefix, self.name, self.name)
        return "/sap/opu/odata/%s/%s" % (self.prefix, self.name)


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
            DT("LastChangeDate", label="Changed On", creatable=False, updatable=False, concurrency=True),
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
            DT("LastChangeDate", creatable=False, updatable=False, concurrency=True),
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
            DT("LastChangeDate", creatable=False, updatable=False, concurrency=True),
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
# GWSAMPLE_BASIC - the classic SAP Gateway demo service
#
# Every SAP OData tutorial uses this one, and it is where SAP's structured
# CT_Address type comes from.  Its entity sets are named differently from its
# entity types (BusinessPartnerSet holds BusinessPartner), which the classic
# services do and the A2X APIs do not.
# --------------------------------------------------------------------------

register_complex(
    ComplexType(
        "CT_Address",
        label="Address",
        props=[
            S("City", max_length=40, label="City"),
            S("PostalCode", max_length=10, label="Postal Code"),
            S("Street", max_length=60, label="Street"),
            S("Building", max_length=10, label="Building"),
            S("Country", max_length=3, label="Country"),
            S("AddressType", max_length=2, label="Address Type"),
        ],
    )
)


def CX(name, complex_type, **kw):
    """Short-hand for a structured property."""
    return Prop(name, "", complex_type=complex_type, **kw)


_register(
    EntityType(
        "BusinessPartner",
        edm_suffix="", label="Business Partner",
        props=[
            S("BusinessPartnerID", key=True, nullable=False, max_length=10,
              label="Bus. Part. ID", updatable=False),
            S("CompanyName", max_length=80, label="Company Name"),
            S("WebAddress", label="Web Address"),
            S("EmailAddress", max_length=255, label="E-Mail Address"),
            S("PhoneNumber", max_length=30, label="Phone No."),
            S("FaxNumber", max_length=30, label="Fax No."),
            S("LegalForm", max_length=10, label="Legal Form"),
            S("CurrencyCode", max_length=5, label="Currency"),
            S("BusinessPartnerRole", max_length=3, label="Bus. Part. Role"),
            CX("Address", "CT_Address", label="Address"),
            DT("CreatedAt", label="Time Stamp", creatable=False, updatable=False),
            DT("ChangedAt", label="Time Stamp", creatable=False, updatable=False,
               concurrency=True),
        ],
        navs=[
            Nav("ToSalesOrders", "SalesOrder", "*", [("BusinessPartnerID", "CustomerID")]),
            Nav("ToContacts", "Contact", "*", [("BusinessPartnerID", "BusinessPartnerID")]),
        ],
    )
)

_register(
    EntityType(
        "Contact",
        edm_suffix="", label="Contact",
        props=[
            S("ContactGuid", key=True, nullable=False, max_length=36, label="Contact GUID"),
            S("BusinessPartnerID", max_length=10, label="Bus. Part. ID"),
            S("Title", max_length=10, label="Title"),
            S("FirstName", max_length=40, label="First Name"),
            S("MiddleName", max_length=40, label="Middle Name"),
            S("LastName", max_length=40, label="Last Name"),
            S("Sex", max_length=1, label="Sex"),
            S("PhoneNumber", max_length=30, label="Phone No."),
            S("EmailAddress", max_length=255, label="E-Mail Address"),
            CX("Address", "CT_Address", label="Address"),
            DT("DateOfBirth", label="Date of Birth"),
        ],
        navs=[
            Nav("ToBusinessPartner", "BusinessPartner", "1",
                [("BusinessPartnerID", "BusinessPartnerID")]),
        ],
    )
)

_register(
    EntityType(
        "Product",
        edm_suffix="", label="Product",
        props=[
            S("ProductID", key=True, nullable=False, max_length=10, label="Product ID",
              updatable=False),
            S("TypeCode", max_length=2, label="Type Code"),
            S("Category", max_length=40, label="Category"),
            S("Name", max_length=255, label="Name"),
            S("NameLanguage", max_length=2, label="Language"),
            S("Description", max_length=255, label="Description"),
            S("DescriptionLanguage", max_length=2, label="Language"),
            S("SupplierID", max_length=10, label="Bus. Part. ID"),
            S("SupplierName", max_length=80, label="Company Name"),
            Prop("TaxTarifCode", "Edm.Int16", label="Tax Tarif Code"),
            S("MeasureUnit", max_length=3, label="Qty. Unit"),
            DEC("WeightMeasure", precision=13, scale=3, label="Weight"),
            S("WeightUnit", max_length=3, label="Qty. Unit"),
            S("CurrencyCode", max_length=5, label="Currency"),
            DEC("Price", precision=16, scale=3, label="Unit Price"),
            DEC("Width", label="Dimensions"),
            DEC("Depth", label="Dimensions"),
            DEC("Height", label="Dimensions"),
            S("DimUnit", max_length=3, label="Dim. Unit"),
            S("ProductPicUrl", label="Picture URL"),
        ],
        navs=[
            Nav("ToSupplier", "BusinessPartner", "1", [("SupplierID", "BusinessPartnerID")]),
            Nav("ToSalesOrderLineItems", "SalesOrderLineItem", "*",
                [("ProductID", "ProductID")]),
        ],
    )
)

_register(
    EntityType(
        "SalesOrder",
        edm_suffix="", label="Sales Order",
        props=[
            S("SalesOrderID", key=True, nullable=False, max_length=10, label="Sa. Ord. ID",
              creatable=False, updatable=False),
            S("Note", max_length=255, label="Description"),
            S("NoteLanguage", max_length=2, label="Language"),
            S("CustomerID", max_length=10, label="Bus. Part. ID"),
            S("CustomerName", max_length=80, label="Company Name"),
            S("CurrencyCode", max_length=5, label="Currency"),
            DEC("GrossAmount", precision=16, scale=3, label="Gross Amount"),
            DEC("NetAmount", precision=16, scale=3, label="Net Amount"),
            DEC("TaxAmount", precision=16, scale=3, label="Tax Amount"),
            S("LifecycleStatus", max_length=1, label="Lifecycle Status"),
            S("LifecycleStatusDescription", max_length=60, label="Lifecycle Status"),
            S("BillingStatus", max_length=1, label="Billing Status"),
            S("BillingStatusDescription", max_length=60, label="Billing Status"),
            S("DeliveryStatus", max_length=1, label="Delivery Status"),
            S("DeliveryStatusDescription", max_length=60, label="Delivery Status"),
            DT("CreatedAt", label="Time Stamp", creatable=False, updatable=False),
            DT("ChangedAt", label="Time Stamp", creatable=False, updatable=False,
               concurrency=True),
        ],
        navs=[
            Nav("ToLineItems", "SalesOrderLineItem", "*", [("SalesOrderID", "SalesOrderID")]),
            Nav("ToBusinessPartner", "BusinessPartner", "1",
                [("CustomerID", "BusinessPartnerID")]),
        ],
    )
)

_register(
    EntityType(
        "SalesOrderLineItem",
        edm_suffix="", label="Sales Order Line Item",
        props=[
            S("SalesOrderID", key=True, nullable=False, max_length=10, label="Sa. Ord. ID"),
            S("ItemPosition", key=True, nullable=False, max_length=10, label="Position"),
            S("ProductID", max_length=10, label="Product ID"),
            S("Note", max_length=255, label="Description"),
            S("NoteLanguage", max_length=2, label="Language"),
            S("CurrencyCode", max_length=5, label="Currency"),
            DEC("GrossAmount", precision=16, scale=3, label="Gross Amount"),
            DEC("NetAmount", precision=16, scale=3, label="Net Amount"),
            DEC("TaxAmount", precision=16, scale=3, label="Tax Amount"),
            DT("DeliveryDate", label="Delivery Date"),
            DEC("Quantity", label="Quantity"),
            S("QuantityUnit", max_length=3, label="Qty. Unit"),
        ],
        navs=[
            Nav("ToHeader", "SalesOrder", "1", [("SalesOrderID", "SalesOrderID")]),
            Nav("ToProduct", "Product", "1", [("ProductID", "ProductID")]),
        ],
    )
)

# --------------------------------------------------------------------------
# UI annotations
#
# Structure alone does not make an app.  These say which columns a list report
# shows, what the filter bar offers and what the object page holds - the
# handful of terms Fiori elements actually reads.
# --------------------------------------------------------------------------

ENTITY_TYPES["A_SalesOrder"].ui = UI(
    type_name="Sales Order",
    type_name_plural="Sales Orders",
    title="SalesOrder",
    description="PurchaseOrderByCustomer",
    line_items=["SalesOrder", "SoldToParty", "SalesOrderType", "TotalNetAmount",
                "TransactionCurrency", "OverallSDProcessStatus"],
    selection_fields=["SalesOrganization", "SoldToParty", "OverallSDProcessStatus",
                      "SalesOrderDate"],
    identification=["SalesOrder", "SalesOrderType", "SalesOrganization",
                    "DistributionChannel", "SoldToParty", "PurchaseOrderByCustomer",
                    "TotalNetAmount", "RequestedDeliveryDate"],
    deletable=True,
)

ENTITY_TYPES["A_SalesOrderItem"].ui = UI(
    type_name="Sales Order Item",
    type_name_plural="Sales Order Items",
    title="SalesOrderItem",
    description="SalesOrderItemText",
    line_items=["SalesOrderItem", "Material", "SalesOrderItemText",
                "RequestedQuantity", "RequestedQuantityUnit", "NetAmount"],
    selection_fields=["Material", "Plant"],
    identification=["SalesOrderItem", "Material", "SalesOrderItemText",
                    "RequestedQuantity", "NetAmount", "Plant"],
)

ENTITY_TYPES["A_BusinessPartner"].ui = UI(
    type_name="Business Partner",
    type_name_plural="Business Partners",
    title="BusinessPartnerFullName",
    description="BusinessPartner",
    line_items=["BusinessPartner", "BusinessPartnerFullName", "BusinessPartnerCategory",
                "Customer", "Supplier"],
    selection_fields=["BusinessPartnerCategory", "BusinessPartnerGrouping", "Industry"],
    identification=["BusinessPartner", "BusinessPartnerFullName", "FirstName",
                    "LastName", "SearchTerm1", "Industry"],
    deletable=False,
)

ENTITY_TYPES["A_Product"].ui = UI(
    type_name="Product",
    type_name_plural="Products",
    title="Product",
    description="ProductGroup",
    line_items=["Product", "ProductType", "ProductGroup", "BaseUnit", "GrossWeight",
                "WeightUnit"],
    selection_fields=["ProductType", "ProductGroup", "Division"],
    identification=["Product", "ProductType", "ProductGroup", "BaseUnit",
                    "NetWeight", "GrossWeight", "WeightUnit"],
    deletable=False,
)

ENTITY_TYPES["A_PurchaseOrder"].ui = UI(
    type_name="Purchase Order",
    type_name_plural="Purchase Orders",
    title="PurchaseOrder",
    description="Supplier",
    line_items=["PurchaseOrder", "PurchaseOrderType", "Supplier", "CompanyCode",
                "DocumentCurrency", "PurchasingProcessingStatus"],
    selection_fields=["CompanyCode", "PurchasingOrganization", "Supplier"],
    identification=["PurchaseOrder", "PurchaseOrderType", "CompanyCode",
                    "PurchasingOrganization", "Supplier", "PaymentTerms"],
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
        "GWSAMPLE_BASIC",
        "GWSAMPLE_BASIC",
        "SAP Gateway sample service (basic)",
        {
            "BusinessPartnerSet": "BusinessPartner",
            "ContactSet": "Contact",
            "ProductSet": "Product",
            "SalesOrderSet": "SalesOrder",
            "SalesOrderLineItemSet": "SalesOrderLineItem",
        },
        prefix="IWBEP",
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
    # The same sales order data, served again in OData V4. S/4HANA exposes
    # its V4 APIs beside the V2 ones like this, and a client of either sees
    # the same documents in the shapes of its own dialect.
    Service(
        "api_salesorder",
        "com.sap.gateway.srvd_a2x.api_salesorder.v0001",
        "Sales Order (A2X, OData V4)",
        {
            "SalesOrder": "A_SalesOrder",
            "SalesOrderItem": "A_SalesOrderItem",
            "SalesOrderHeaderPartner": "A_SalesOrderHeaderPartner",
        },
        version=4,
    ),
    Service(
        "api_product",
        "com.sap.gateway.srvd_a2x.api_product.v0001",
        "Product (A2X, OData V4)",
        {
            "Product": "A_Product",
            "ProductDescription": "A_ProductDescription",
            "ProductPlant": "A_ProductPlant",
        },
        version=4,
    ),
    Service(
        "api_purchaseorder",
        "com.sap.gateway.srvd_a2x.api_purchaseorder.v0001",
        "Purchase Order (A2X, OData V4)",
        {
            "PurchaseOrder": "A_PurchaseOrder",
            "PurchaseOrderItem": "A_PurchaseOrderItem",
        },
        version=4,
    ),
    Service(
        "api_businesspartner",
        "com.sap.gateway.srvd_a2x.api_businesspartner.v0001",
        "Business Partner (A2X, OData V4)",
        {
            "BusinessPartner": "A_BusinessPartner",
            "BusinessPartnerAddress": "A_BusinessPartnerAddress",
            "BusinessPartnerRole": "A_BusinessPartnerRole",
        },
        version=4,
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
