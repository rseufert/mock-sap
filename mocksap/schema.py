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
class FieldGroup:
    """A named set of fields, referenced from a facet by its qualifier."""

    qualifier: str
    label: str
    fields: List[str]


@dataclass
class Facet:
    """One section of an object page.

    `target` is either a field group on this type - `@UI.FieldGroup#General` -
    or a navigation property's own line items, `to_Item/@UI.LineItem`, which is
    how an object page grows a table of its items.
    """

    label: str
    target: str


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
    field_groups: List["FieldGroup"] = field(default_factory=list)
    facets: List["Facet"] = field(default_factory=list)
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
    # V2 services publish UI intent in a document of their own, and the A2X
    # integration APIs do not publish any: only a UI service opts in.
    annotations: bool = False

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
            S("CustomerPurchaseOrderType", max_length=4, label="Customer Reference Type"),
            DT("CustomerPurchaseOrderDate", label="Customer Reference Date"),
            S("TransactionCurrency", max_length=5, label="Currency"),
            S("SDDocumentReason", max_length=3, label="Order Reason"),
            DEC("TotalNetAmount", precision=16, scale=3, label="Net Value", creatable=False, updatable=False),
            DT("SalesOrderDate", label="Document Date"),
            DT("RequestedDeliveryDate", label="Requested Delivery Date"),
            S("ShippingCondition", max_length=2, label="Shipping Condition"),
            S("IncotermsClassification", max_length=3, label="Incoterms"),
            S("CustomerPaymentTerms", max_length=4, label="Payment Terms"),
            DT("PricingDate", label="Pricing Date"),
            S("ReferenceSDDocument", max_length=10, label="Reference Document"),
            S("ReferenceSDDocumentCategory", max_length=1, label="Reference Document Category"),
            S("OverallSDProcessStatus", max_length=1, label="Overall Status", creatable=False, updatable=False),
            S("OverallDeliveryStatus", max_length=1, label="Delivery Status", creatable=False, updatable=False),
            S("CreatedByUser", max_length=12, creatable=False, updatable=False),
            DT("CreationDate", creatable=False, updatable=False),
            DT("LastChangeDate", creatable=False, updatable=False, concurrency=True),
        ],
        navs=[
            Nav("to_Item", "A_SalesOrderItem", "*", [("SalesOrder", "SalesOrder")]),
            Nav("to_Partner", "A_SalesOrderHeaderPartner", "*", [("SalesOrder", "SalesOrder")]),
            Nav("to_PricingElement", "A_SalesOrderHeaderPrElement", "*", [("SalesOrder", "SalesOrder")]),
            Nav("to_Text", "A_SalesOrderText", "*", [("SalesOrder", "SalesOrder")]),
        ],
    )
)

_register(
    EntityType(
        "A_SalesOrderItem",
        label="Sales Order Item",
        props=[
            S("SalesOrder", key=True, nullable=False, max_length=10),
            S("SalesOrderItem", key=True, nullable=False, max_length=6, creatable=False),
            S("HigherLevelItem", max_length=6),
            S("Material", max_length=40, label="Material"),
            S("MaterialByCustomer", max_length=35),
            S("SalesOrderItemText", max_length=40, label="Item Description"),
            S("SalesOrderItemCategory", max_length=4),
            S("PurchaseOrderByCustomer", max_length=35, label="Customer Reference"),
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
            Nav("to_PricingElement", "A_SalesOrderItemPrElement", "*",
                [("SalesOrder", "SalesOrder"), ("SalesOrderItem", "SalesOrderItem")]),
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
            S("PartnerFunctionInternalCode", max_length=2, creatable=False, updatable=False),
            S("Customer", max_length=10),
            S("Supplier", max_length=10),
            S("Personnel", max_length=8),
            S("ContactPerson", max_length=10),
            S("ReferenceBusinessPartner", max_length=10),
            S("AddressID", max_length=10),
            S("VATRegistration", max_length=20),
        ],
        navs=[
            Nav("to_Address", "A_SalesOrderPartnerAddress", "*",
                [("SalesOrder", "SalesOrder"), ("PartnerFunction", "PartnerFunction")]),
            Nav("to_SalesOrder", "A_SalesOrder", "1", [("SalesOrder", "SalesOrder")]),
        ],
    )
)

_register(
    EntityType(
        "A_SalesOrderPartnerAddress",
        label="Sales Order Partner Address",
        props=[
            S("SalesOrder", key=True, nullable=False, max_length=10),
            S("PartnerFunction", key=True, nullable=False, max_length=2),
            S("AddressRepresentationCode", max_length=1),
            S("CorrespondenceLanguage", max_length=2),
            S("AddresseeFullName", max_length=80, label="Full Name of Person"),
            S("OrganizationName1", max_length=40, label="Name 1"),
            S("OrganizationName2", max_length=40),
            S("OrganizationName3", max_length=40),
            S("OrganizationName4", max_length=40),
            S("CityName", max_length=40, label="City"),
            S("DistrictName", max_length=40),
            S("PostalCode", max_length=10, label="Postal Code"),
            S("StreetPrefixName1", max_length=40),
            S("StreetPrefixName2", max_length=40),
            S("StreetName", max_length=60, label="Street"),
            S("StreetSuffixName1", max_length=40),
            S("StreetSuffixName2", max_length=40),
            S("HouseNumber", max_length=10),
            S("Country", max_length=3, label="Country"),
            S("Region", max_length=3, label="Region"),
            S("FormOfAddress", max_length=4),
            S("TaxJurisdiction", max_length=15),
            S("TransportZone", max_length=10),
            S("POBox", max_length=10),
            S("POBoxPostalCode", max_length=10),
            S("EmailAddress", max_length=241),
            S("MobilePhoneCountry", max_length=3),
            S("MobileNumber", max_length=30),
            S("PhoneNumberCountry", max_length=3),
            S("PhoneNumber", max_length=30),
            S("PhoneExtensionNumber", max_length=10),
            S("FaxNumberCountry", max_length=3),
            S("FaxAreaCodeSubscriberNumber", max_length=30),
            S("FaxExtensionNumber", max_length=10),
        ],
        navs=[
            Nav("to_Partner", "A_SalesOrderHeaderPartner", "1",
                [("SalesOrder", "SalesOrder"), ("PartnerFunction", "PartnerFunction")]),
            Nav("to_SalesOrder", "A_SalesOrder", "1", [("SalesOrder", "SalesOrder")]),
        ],
    )
)

_register(
    EntityType(
        "A_SalesOrderText",
        label="Sales Order Text",
        props=[
            S("SalesOrder", key=True, nullable=False, max_length=10),
            S("Language", key=True, nullable=False, max_length=2),
            S("LongTextID", key=True, nullable=False, max_length=4),
            S("LongText", max_length=0, label="Text"),
        ],
        navs=[
            Nav("to_SalesOrder", "A_SalesOrder", "1", [("SalesOrder", "SalesOrder")]),
        ],
    )
)

_register(
    EntityType(
        "A_SalesOrderHeaderPrElement",
        label="Sales Order Header Pricing Element",
        props=[
            S("SalesOrder", key=True, nullable=False, max_length=10),
            S("PricingProcedureStep", key=True, nullable=False, max_length=3, creatable=False),
            S("PricingProcedureCounter", key=True, nullable=False, max_length=3, creatable=False),
            S("ConditionType", max_length=4, label="Condition Type"),
            DEC("ConditionRateValue", precision=16, scale=3, label="Amount"),
            S("ConditionCurrency", max_length=5),
            DEC("ConditionQuantity", label="Condition Quantity"),
            S("ConditionQuantityUnit", max_length=3),
            DEC("ConditionAmount", precision=16, scale=3, label="Condition Value"),
            S("ConditionCategory", max_length=1),
            S("ConditionIsManuallyChanged", max_length=1),
            S("TransactionCurrency", max_length=5),
        ],
        navs=[
            Nav("to_SalesOrder", "A_SalesOrder", "1", [("SalesOrder", "SalesOrder")]),
        ],
    )
)

_register(
    EntityType(
        "A_SalesOrderItemPrElement",
        label="Sales Order Item Pricing Element",
        props=[
            S("SalesOrder", key=True, nullable=False, max_length=10),
            S("SalesOrderItem", key=True, nullable=False, max_length=6),
            S("PricingProcedureStep", key=True, nullable=False, max_length=3, creatable=False),
            S("PricingProcedureCounter", key=True, nullable=False, max_length=3, creatable=False),
            S("ConditionType", max_length=4, label="Condition Type"),
            DEC("ConditionRateValue", precision=16, scale=3, label="Amount"),
            S("ConditionCurrency", max_length=5),
            DEC("ConditionQuantity", label="Condition Quantity"),
            S("ConditionQuantityUnit", max_length=3),
            DEC("ConditionBaseValue", precision=16, scale=3),
            DEC("ConditionAmount", precision=16, scale=3, label="Condition Value"),
            S("ConditionCategory", max_length=1),
            S("ConditionIsManuallyChanged", max_length=1),
            S("TransactionCurrency", max_length=5),
        ],
        navs=[
            Nav("to_SalesOrder", "A_SalesOrder", "1", [("SalesOrder", "SalesOrder")]),
            Nav("to_SalesOrderItem", "A_SalesOrderItem", "1",
                [("SalesOrder", "SalesOrder"), ("SalesOrderItem", "SalesOrderItem")]),
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
            S("PurchaseOrderItem", key=True, nullable=False, max_length=5, creatable=False),
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
# Entity types - the documents a sales order turns into
#
# API_OUTBOUND_DELIVERY_SRV, API_BILLING_DOCUMENT_SRV and API_JOURNALENTRY_SRV.
# Each carries the reference back to the document it came from, so the chain
# order -> delivery -> invoice -> journal entry can be walked.
# --------------------------------------------------------------------------

_register(
    EntityType(
        "A_OutbDeliveryHeader",
        label="Outbound Delivery",
        props=[
            S("DeliveryDocument", key=True, nullable=False, max_length=10,
              label="Delivery", creatable=False, updatable=False),
            S("DeliveryDocumentType", max_length=4, label="Delivery Type"),
            S("ShippingPoint", max_length=4, label="Shipping Point"),
            S("SalesOrganization", max_length=4),
            S("SoldToParty", max_length=10, label="Sold-To Party"),
            S("ShipToParty", max_length=10, label="Ship-To Party"),
            DT("DeliveryDate", label="Delivery Date"),
            DT("ActualGoodsMovementDate", label="Actual Goods Movement Date"),
            S("OverallSDProcessStatus", max_length=1, label="Overall Status",
              creatable=False, updatable=False),
            S("OverallGoodsMovementStatus", max_length=1, creatable=False, updatable=False),
            DEC("TotalWeight", label="Total Weight"),
            S("WeightUnit", max_length=3),
            S("CreatedByUser", max_length=12, creatable=False, updatable=False),
            DT("CreationDate", creatable=False, updatable=False),
            DT("LastChangeDate", creatable=False, updatable=False, concurrency=True),
        ],
        navs=[
            Nav("to_DeliveryDocumentItem", "A_OutbDeliveryItem", "*",
                [("DeliveryDocument", "DeliveryDocument")]),
        ],
    )
)

_register(
    EntityType(
        "A_OutbDeliveryItem",
        label="Delivery Item",
        props=[
            S("DeliveryDocument", key=True, nullable=False, max_length=10),
            S("DeliveryDocumentItem", key=True, nullable=False, max_length=6, creatable=False),
            S("Material", max_length=40),
            S("DeliveryDocumentItemText", max_length=40),
            DEC("ActualDeliveredQtyInOrderQtyUnit", label="Delivered Quantity"),
            S("OrderQuantityUnit", max_length=3),
            S("Plant", max_length=4),
            S("StorageLocation", max_length=4),
            S("ReferenceSDDocument", max_length=10, label="Reference Document"),
            S("ReferenceSDDocumentItem", max_length=6, label="Reference Item"),
            DEC("ItemGrossWeight"),
            S("ItemWeightUnit", max_length=3),
        ],
        navs=[
            Nav("to_DeliveryDocument", "A_OutbDeliveryHeader", "1",
                [("DeliveryDocument", "DeliveryDocument")]),
        ],
    )
)

_register(
    EntityType(
        "A_BillingDocument",
        label="Billing Document",
        props=[
            S("BillingDocument", key=True, nullable=False, max_length=10,
              label="Billing Document", creatable=False, updatable=False),
            S("BillingDocumentType", max_length=4, label="Billing Type"),
            S("SDDocumentCategory", max_length=4),
            S("SalesOrganization", max_length=4),
            S("SoldToParty", max_length=10),
            S("PayerParty", max_length=10),
            DT("BillingDocumentDate", label="Billing Date"),
            S("TransactionCurrency", max_length=5),
            DEC("TotalNetAmount", precision=16, scale=3, label="Net Value"),
            DEC("TotalTaxAmount", precision=16, scale=3, label="Tax Amount"),
            DEC("TotalGrossAmount", precision=16, scale=3, label="Gross Amount"),
            S("AccountingPostingStatus", max_length=1, label="Posting Status"),
            S("AccountingDocument", max_length=10, label="Accounting Document"),
            S("CreatedByUser", max_length=12, creatable=False, updatable=False),
            DT("CreationDate", creatable=False, updatable=False),
            DT("LastChangeDate", creatable=False, updatable=False, concurrency=True),
        ],
        navs=[
            Nav("to_Item", "A_BillingDocumentItem", "*",
                [("BillingDocument", "BillingDocument")]),
        ],
    )
)

_register(
    EntityType(
        "A_BillingDocumentItem",
        label="Billing Document Item",
        props=[
            S("BillingDocument", key=True, nullable=False, max_length=10),
            S("BillingDocumentItem", key=True, nullable=False, max_length=6, creatable=False),
            S("Material", max_length=40),
            S("BillingDocumentItemText", max_length=40),
            DEC("BillingQuantity", label="Billed Quantity"),
            S("BillingQuantityUnit", max_length=3),
            DEC("NetAmount", precision=16, scale=3),
            DEC("TaxAmount", precision=16, scale=3),
            S("TransactionCurrency", max_length=5),
            S("SalesDocument", max_length=10, label="Sales Document"),
            S("SalesDocumentItem", max_length=6),
        ],
    )
)

_register(
    EntityType(
        "A_JournalEntry",
        label="Journal Entry",
        props=[
            S("AccountingDocument", key=True, nullable=False, max_length=10,
              label="Journal Entry", creatable=False, updatable=False),
            S("CompanyCode", key=True, nullable=False, max_length=4),
            S("FiscalYear", key=True, nullable=False, max_length=4),
            S("AccountingDocumentType", max_length=2, label="Document Type"),
            DT("DocumentDate", label="Document Date"),
            DT("PostingDate", label="Posting Date"),
            S("FiscalPeriod", max_length=3),
            S("TransactionCurrency", max_length=5),
            S("AccountingDocumentHeaderText", max_length=25, label="Header Text"),
            S("ReferenceDocument", max_length=16, label="Reference"),
            S("CreatedByUser", max_length=12, creatable=False, updatable=False),
            DT("CreationDate", creatable=False, updatable=False),
            DT("LastChangeDate", creatable=False, updatable=False, concurrency=True),
        ],
        navs=[
            Nav("to_JournalEntryItem", "A_JournalEntryItem", "*",
                [("AccountingDocument", "AccountingDocument"),
                 ("CompanyCode", "CompanyCode"), ("FiscalYear", "FiscalYear")]),
        ],
    )
)

_register(
    EntityType(
        "A_JournalEntryItem",
        label="Journal Entry Item",
        props=[
            S("AccountingDocument", key=True, nullable=False, max_length=10),
            S("CompanyCode", key=True, nullable=False, max_length=4),
            S("FiscalYear", key=True, nullable=False, max_length=4),
            S("AccountingDocumentItem", key=True, nullable=False, max_length=6,
              creatable=False),
            S("GLAccount", max_length=10, label="G/L Account"),
            S("DebitCreditCode", max_length=1, label="Debit/Credit"),
            DEC("AmountInTransactionCurrency", precision=16, scale=3, label="Amount"),
            S("TransactionCurrency", max_length=5),
            S("DocumentItemText", max_length=50),
            S("CostCenter", max_length=10),
            S("ProfitCenter", max_length=10),
            S("Customer", max_length=10),
            S("Supplier", max_length=10),
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

ENTITY_TYPES["A_SalesOrder"].ui.field_groups = [
    FieldGroup("General", "General Information",
               ["SalesOrderType", "SalesOrganization", "DistributionChannel",
                "OrganizationDivision", "SoldToParty", "PurchaseOrderByCustomer"]),
    FieldGroup("Amounts", "Amounts",
               ["TotalNetAmount", "TransactionCurrency", "CustomerPaymentTerms",
                "IncotermsClassification"]),
    FieldGroup("Dates", "Dates and Status",
               ["SalesOrderDate", "RequestedDeliveryDate", "OverallSDProcessStatus",
                "OverallDeliveryStatus"]),
    FieldGroup("Admin", "Administrative Data",
               ["CreatedByUser", "CreationDate", "LastChangeDate"]),
]
ENTITY_TYPES["A_SalesOrder"].ui.facets = [
    Facet("General Information", "@UI.FieldGroup#General"),
    Facet("Amounts", "@UI.FieldGroup#Amounts"),
    Facet("Dates and Status", "@UI.FieldGroup#Dates"),
    Facet("Items", "to_Item/@UI.LineItem"),
    Facet("Partners", "to_Partner/@UI.LineItem"),
    Facet("Administrative Data", "@UI.FieldGroup#Admin"),
]

ENTITY_TYPES["A_SalesOrderHeaderPartner"].ui = UI(
    type_name="Partner",
    type_name_plural="Partners",
    title="PartnerFunction",
    line_items=["PartnerFunction", "Customer", "Supplier", "ContactPerson"],
    identification=["PartnerFunction", "Customer", "Supplier"],
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

ENTITY_TYPES["A_SalesOrderItem"].ui.field_groups = [
    FieldGroup("General", "General Information",
               ["Material", "SalesOrderItemText", "SalesOrderItemCategory",
                "MaterialGroup", "Plant", "ShippingPoint"]),
    FieldGroup("Quantities", "Quantities and Amounts",
               ["RequestedQuantity", "RequestedQuantityUnit", "NetAmount",
                "TransactionCurrency"]),
]
ENTITY_TYPES["A_SalesOrderItem"].ui.facets = [
    Facet("General Information", "@UI.FieldGroup#General"),
    Facet("Quantities and Amounts", "@UI.FieldGroup#Quantities"),
]

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

ENTITY_TYPES["A_BusinessPartner"].ui.field_groups = [
    FieldGroup("General", "General Information",
               ["BusinessPartnerCategory", "BusinessPartnerGrouping",
                "BusinessPartnerFullName", "SearchTerm1", "Industry"]),
    FieldGroup("Names", "Names",
               ["OrganizationBPName1", "FirstName", "LastName"]),
]
ENTITY_TYPES["A_BusinessPartner"].ui.facets = [
    Facet("General Information", "@UI.FieldGroup#General"),
    Facet("Names", "@UI.FieldGroup#Names"),
    Facet("Addresses", "to_BusinessPartnerAddress/@UI.LineItem"),
    Facet("Roles", "to_BusinessPartnerRole/@UI.LineItem"),
]

ENTITY_TYPES["A_BusinessPartnerAddress"].ui = UI(
    type_name="Address",
    type_name_plural="Addresses",
    title="CityName",
    description="StreetName",
    line_items=["AddressID", "StreetName", "HouseNumber", "PostalCode", "CityName",
                "Country"],
    identification=["AddressID", "StreetName", "HouseNumber", "PostalCode",
                    "CityName", "Region", "Country", "PhoneNumber", "EmailAddress"],
)

ENTITY_TYPES["A_BusinessPartnerRole"].ui = UI(
    type_name="Role",
    type_name_plural="Roles",
    title="BusinessPartnerRole",
    line_items=["BusinessPartnerRole", "ValidFrom", "ValidTo"],
    identification=["BusinessPartnerRole", "ValidFrom", "ValidTo"],
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

ENTITY_TYPES["A_Product"].ui.field_groups = [
    FieldGroup("General", "General Information",
               ["ProductType", "ProductGroup", "BaseUnit", "Division",
                "ItemCategoryGroup"]),
    FieldGroup("Weights", "Weights",
               ["NetWeight", "GrossWeight", "WeightUnit"]),
]
ENTITY_TYPES["A_Product"].ui.facets = [
    Facet("General Information", "@UI.FieldGroup#General"),
    Facet("Weights", "@UI.FieldGroup#Weights"),
    Facet("Descriptions", "to_Description/@UI.LineItem"),
    Facet("Plants", "to_Plant/@UI.LineItem"),
]

ENTITY_TYPES["A_ProductDescription"].ui = UI(
    type_name="Description",
    type_name_plural="Descriptions",
    title="ProductDescription",
    line_items=["Language", "ProductDescription"],
    identification=["Language", "ProductDescription"],
)

ENTITY_TYPES["A_ProductPlant"].ui = UI(
    type_name="Plant",
    type_name_plural="Plants",
    title="Plant",
    line_items=["Plant", "PurchasingGroup", "ProfitCenter", "MRPType"],
    identification=["Plant", "PurchasingGroup", "ProfitCenter",
                    "AvailabilityCheckType", "MRPType"],
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

ENTITY_TYPES["A_PurchaseOrder"].ui.field_groups = [
    FieldGroup("General", "General Information",
               ["PurchaseOrderType", "CompanyCode", "PurchasingOrganization",
                "PurchasingGroup", "Supplier"]),
    FieldGroup("Terms", "Terms",
               ["DocumentCurrency", "PaymentTerms", "NetPaymentDays",
                "PurchaseOrderDate"]),
]
ENTITY_TYPES["A_PurchaseOrder"].ui.facets = [
    Facet("General Information", "@UI.FieldGroup#General"),
    Facet("Terms", "@UI.FieldGroup#Terms"),
    Facet("Items", "to_PurchaseOrderItem/@UI.LineItem"),
]

ENTITY_TYPES["A_PurchaseOrderItem"].ui = UI(
    type_name="Purchase Order Item",
    type_name_plural="Purchase Order Items",
    title="PurchaseOrderItem",
    description="PurchaseOrderItemText",
    line_items=["PurchaseOrderItem", "Material", "PurchaseOrderItemText",
                "OrderQuantity", "PurchaseOrderQuantityUnit", "NetPriceAmount"],
    identification=["PurchaseOrderItem", "Material", "PurchaseOrderItemText",
                    "OrderQuantity", "NetPriceAmount", "Plant", "StorageLocation"],
)


# The demo service's own UI intent, published as a V2 annotation document
# rather than inside $metadata - see mocksap/annotations.py.

ENTITY_TYPES["BusinessPartner"].ui = UI(
    type_name="Business Partner",
    type_name_plural="Business Partners",
    title="CompanyName",
    description="BusinessPartnerID",
    line_items=["BusinessPartnerID", "CompanyName", "BusinessPartnerRole",
                "EmailAddress", "PhoneNumber", "CurrencyCode"],
    selection_fields=["BusinessPartnerRole", "CompanyName", "LegalForm"],
    identification=["BusinessPartnerID", "CompanyName", "LegalForm",
                    "EmailAddress", "PhoneNumber", "WebAddress", "CurrencyCode"],
    field_groups=[
        FieldGroup("General", "General Information",
                   ["BusinessPartnerID", "CompanyName", "LegalForm",
                    "BusinessPartnerRole", "CurrencyCode"]),
        FieldGroup("Contact", "Contact",
                   ["EmailAddress", "PhoneNumber", "FaxNumber", "WebAddress"]),
    ],
    facets=[
        Facet("General Information", "@UI.FieldGroup#General"),
        Facet("Contact", "@UI.FieldGroup#Contact"),
        Facet("Sales Orders", "ToSalesOrders/@UI.LineItem"),
        Facet("Contacts", "ToContacts/@UI.LineItem"),
    ],
    deletable=False,
)

ENTITY_TYPES["SalesOrder"].ui = UI(
    type_name="Sales Order",
    type_name_plural="Sales Orders",
    title="SalesOrderID",
    description="CustomerName",
    line_items=["SalesOrderID", "CustomerName", "GrossAmount", "CurrencyCode",
                "LifecycleStatusDescription", "DeliveryStatusDescription"],
    selection_fields=["CustomerName", "LifecycleStatus", "BillingStatus"],
    identification=["SalesOrderID", "CustomerID", "CustomerName", "Note",
                    "NetAmount", "TaxAmount", "GrossAmount", "CurrencyCode"],
    field_groups=[
        FieldGroup("General", "General Information",
                   ["SalesOrderID", "CustomerID", "CustomerName", "Note"]),
        FieldGroup("Amounts", "Amounts",
                   ["NetAmount", "TaxAmount", "GrossAmount", "CurrencyCode"]),
        FieldGroup("Status", "Status",
                   ["LifecycleStatusDescription", "BillingStatusDescription",
                    "DeliveryStatusDescription"]),
    ],
    facets=[
        Facet("General Information", "@UI.FieldGroup#General"),
        Facet("Amounts", "@UI.FieldGroup#Amounts"),
        Facet("Status", "@UI.FieldGroup#Status"),
        Facet("Items", "ToLineItems/@UI.LineItem"),
    ],
)

ENTITY_TYPES["SalesOrderLineItem"].ui = UI(
    type_name="Sales Order Item",
    type_name_plural="Sales Order Items",
    title="ItemPosition",
    description="Note",
    line_items=["ItemPosition", "ProductID", "Note", "Quantity", "QuantityUnit",
                "GrossAmount", "CurrencyCode"],
    identification=["ItemPosition", "ProductID", "Note", "Quantity",
                    "NetAmount", "TaxAmount", "GrossAmount", "DeliveryDate"],
)

ENTITY_TYPES["Product"].ui = UI(
    type_name="Product",
    type_name_plural="Products",
    title="Name",
    description="ProductID",
    line_items=["ProductID", "Name", "Category", "SupplierName", "Price",
                "CurrencyCode"],
    selection_fields=["Category", "SupplierName", "TypeCode"],
    identification=["ProductID", "Name", "Description", "Category", "TypeCode",
                    "Price", "CurrencyCode", "MeasureUnit"],
    field_groups=[
        FieldGroup("General", "General Information",
                   ["ProductID", "Name", "Description", "Category", "TypeCode"]),
        FieldGroup("Dimensions", "Dimensions and Weight",
                   ["Width", "Depth", "Height", "DimUnit", "WeightMeasure",
                    "WeightUnit"]),
    ],
    facets=[
        Facet("General Information", "@UI.FieldGroup#General"),
        Facet("Dimensions and Weight", "@UI.FieldGroup#Dimensions"),
    ],
)

ENTITY_TYPES["Contact"].ui = UI(
    type_name="Contact",
    type_name_plural="Contacts",
    title="LastName",
    description="EmailAddress",
    line_items=["Title", "FirstName", "LastName", "EmailAddress", "PhoneNumber"],
    identification=["Title", "FirstName", "MiddleName", "LastName", "Sex",
                    "EmailAddress", "PhoneNumber", "DateOfBirth"],
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
            "A_SalesOrderPartnerAddress": "A_SalesOrderPartnerAddress",
            "A_SalesOrderText": "A_SalesOrderText",
            "A_SalesOrderHeaderPrElement": "A_SalesOrderHeaderPrElement",
            "A_SalesOrderItemPrElement": "A_SalesOrderItemPrElement",
        },
    ),
    Service(
        "API_OUTBOUND_DELIVERY_SRV",
        "API_OUTBOUND_DELIVERY_SRV",
        "Outbound Delivery (A2X)",
        {
            "A_OutbDeliveryHeader": "A_OutbDeliveryHeader",
            "A_OutbDeliveryItem": "A_OutbDeliveryItem",
        },
    ),
    Service(
        "API_BILLING_DOCUMENT_SRV",
        "API_BILLING_DOCUMENT_SRV",
        "Billing Document (A2X)",
        {
            "A_BillingDocument": "A_BillingDocument",
            "A_BillingDocumentItem": "A_BillingDocumentItem",
        },
    ),
    Service(
        "API_JOURNALENTRY_SRV",
        "API_JOURNALENTRY_SRV",
        "Journal Entry (A2X)",
        {
            "A_JournalEntry": "A_JournalEntry",
            "A_JournalEntryItem": "A_JournalEntryItem",
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
        annotations=True,
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
            "SalesOrderPartnerAddress": "A_SalesOrderPartnerAddress",
            "SalesOrderText": "A_SalesOrderText",
            "SalesOrderHeaderPrElement": "A_SalesOrderHeaderPrElement",
            "SalesOrderItemPrElement": "A_SalesOrderItemPrElement",
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
