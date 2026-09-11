"""UI vocabulary annotations: what a Fiori elements app reads from $metadata."""
import unittest
from xml.etree import ElementTree as ET

from support import MockServerCase, SRV

EDMX = "{http://docs.oasis-open.org/odata/ns/edmx}"
EDM = "{http://docs.oasis-open.org/odata/ns/edm}"
SERVICES = {
    "api_salesorder": ("SalesOrder", "SalesOrderType"),
    "api_businesspartner": ("BusinessPartner", "BusinessPartnerType"),
    "api_product": ("Product", "ProductType"),
    "api_purchaseorder": ("PurchaseOrder", "PurchaseOrderType"),
}


class AnnotationCase(MockServerCase):
    def path_of(self, service):
        return "/sap/opu/odata4/sap/%s/srvd_a2x/sap/%s/0001" % (service, service)

    def edmx(self, service):
        status, _, raw = self.get(self.path_of(service) + "/$metadata", raw=True)
        self.assertEqual(status, 200)
        return ET.fromstring(raw)

    def csdl(self, service):
        status, _, body = self.get(self.path_of(service) + "/$metadata?$format=json")
        self.assertEqual(status, 200)
        return body

    def annotations_for(self, root, target):
        for block in root.iter(EDM + "Annotations"):
            if block.get("Target", "").endswith(target):
                return block
        return None

    def term(self, block, name):
        for annotation in block.findall(EDM + "Annotation"):
            if annotation.get("Term") == name:
                return annotation
        return None


class TestVocabularyReferences(AnnotationCase):
    def test_every_vocabulary_used_is_referenced(self):
        root = self.edmx("api_salesorder")
        namespaces = set()
        for reference in root.findall(EDMX + "Reference"):
            self.assertTrue(reference.get("Uri", "").startswith("https://"))
            for include in reference.findall(EDMX + "Include"):
                namespaces.add(include.get("Namespace"))
                self.assertTrue(include.get("Alias"))
        self.assertIn("com.sap.vocabularies.UI.v1", namespaces)
        self.assertIn("com.sap.vocabularies.Common.v1", namespaces)
        self.assertIn("Org.OData.Capabilities.V1", namespaces)

    def test_the_json_encoding_references_them_too(self):
        references = self.csdl("api_salesorder")["$Reference"]
        included = {entry["$Namespace"]
                    for value in references.values() for entry in value["$Include"]}
        self.assertIn("com.sap.vocabularies.UI.v1", included)
        for value in references.values():
            for entry in value["$Include"]:
                self.assertTrue(entry["$Alias"])


class TestUiAnnotations(AnnotationCase):
    def test_header_line_items_and_filters(self):
        root = self.edmx("api_salesorder")
        block = self.annotations_for(root, "SalesOrderType")
        self.assertIsNotNone(block)

        header = self.term(block, "UI.HeaderInfo")
        record = header.find(EDM + "Record")
        values = {p.get("Property"): p for p in record.findall(EDM + "PropertyValue")}
        self.assertEqual(values["TypeName"].get("String"), "Sales Order")
        self.assertEqual(values["TypeNamePlural"].get("String"), "Sales Orders")
        title = values["Title"].find(EDM + "Record").find(EDM + "PropertyValue")
        self.assertEqual(title.get("Path"), "SalesOrder")

        line_item = self.term(block, "UI.LineItem")
        columns = [record.find(EDM + "PropertyValue").get("Path")
                   for record in line_item.find(EDM + "Collection")]
        self.assertEqual(columns[0], "SalesOrder")
        self.assertIn("TotalNetAmount", columns)

        selection = self.term(block, "UI.SelectionFields")
        filters = [p.text for p in selection.find(EDM + "Collection")]
        self.assertIn("SalesOrganization", filters)

        self.assertIsNotNone(self.term(block, "UI.Identification"))

    def test_every_annotated_path_is_a_real_property(self):
        """An annotation pointing at a property that does not exist renders an
        empty column in a Fiori app and explains nothing about why."""
        for service, (set_name, type_name) in SERVICES.items():
            root = self.edmx(service)
            properties = set()
            for entity in root.iter(EDM + "EntityType"):
                if entity.get("Name") != type_name:
                    continue
                properties = {p.get("Name") for p in entity.findall(EDM + "Property")}
            self.assertTrue(properties, service)

            block = self.annotations_for(root, "." + type_name)
            self.assertIsNotNone(block, service)
            paths = [record.find(EDM + "PropertyValue").get("Path")
                     for record in block.iter(EDM + "Record")
                     if record.get("Type") == "UI.DataField"]
            paths += [p.text for p in block.iter(EDM + "PropertyPath")]
            self.assertTrue(paths, service)
            for path in paths:
                self.assertIn(path, properties, "%s: %s" % (service, path))

    def test_properties_carry_a_label(self):
        root = self.edmx("api_salesorder")
        labelled = 0
        for entity in root.iter(EDM + "EntityType"):
            for prop in entity.findall(EDM + "Property"):
                annotation = prop.find(EDM + "Annotation")
                if annotation is not None:
                    self.assertEqual(annotation.get("Term"), "Common.Label")
                    self.assertTrue(annotation.get("String"))
                    labelled += 1
        self.assertGreater(labelled, 10)

    def test_capabilities_follow_the_declaration(self):
        root = self.edmx("api_salesorder")
        block = self.annotations_for(root, "_Container/SalesOrder")
        self.assertIsNotNone(block)
        deletable = self.term(block, "Capabilities.DeleteRestrictions")
        value = deletable.find(EDM + "Record").find(EDM + "PropertyValue")
        self.assertEqual(value.get("Bool"), "true")

        # a business partner is not deletable, and says so
        root = self.edmx("api_businesspartner")
        block = self.annotations_for(root, "_Container/BusinessPartner")
        deletable = self.term(block, "Capabilities.DeleteRestrictions")
        self.assertEqual(
            deletable.find(EDM + "Record").find(EDM + "PropertyValue").get("Bool"),
            "false")

    def test_the_json_encoding_carries_the_same_content(self):
        for service, (set_name, type_name) in SERVICES.items():
            csdl = self.csdl(service)
            namespace = [key for key in csdl
                         if key.startswith("com.sap.gateway")][0]
            schema = csdl[namespace]
            entity = schema[type_name]
            self.assertEqual(entity["@UI.HeaderInfo"]["$Type"], "UI.HeaderInfoType")
            self.assertTrue(entity["@UI.LineItem"])
            for column in entity["@UI.LineItem"]:
                self.assertEqual(column["$Type"], "UI.DataField")
                self.assertIn(column["Value"]["$Path"], entity)
            for path in entity["@UI.SelectionFields"]:
                self.assertIn(path["$PropertyPath"], entity)

            container = schema[service + "_Container"][set_name]
            self.assertIn("@Capabilities.InsertRestrictions", container)
            self.assertIsInstance(
                container["@Capabilities.DeleteRestrictions"]["Deletable"], bool)

    def test_metadata_still_parses_everywhere(self):
        for service in SERVICES:
            self.edmx(service)          # raises if the XML is malformed
            self.csdl(service)

        # and the V2 services are untouched: they carry sap: attributes instead
        status, _, raw = self.get(SRV + "/$metadata", raw=True)
        self.assertEqual(status, 200)
        text = raw.decode()
        self.assertIn('sap:label="Sales Order"', text)
        self.assertNotIn("UI.LineItem", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
