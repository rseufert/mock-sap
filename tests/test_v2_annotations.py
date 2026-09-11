"""The V2 annotation document: UI intent for the classic smart controls."""
import unittest
from xml.etree import ElementTree as ET

from support import MockServerCase, SRV

GW = "/sap/opu/odata/IWBEP/GWSAMPLE_BASIC"
V4 = "/sap/opu/odata4/sap/api_salesorder/srvd_a2x/sap/api_salesorder/0001"
EDMX = "{http://docs.oasis-open.org/odata/ns/edmx}"
EDM = "{http://docs.oasis-open.org/odata/ns/edm}"


class AnnotationDocumentCase(MockServerCase):
    def document(self, path=GW + "/annotations"):
        status, headers, raw = self.get(path, raw=True)
        self.assertEqual(status, 200)
        self.assertIn("xml", headers["Content-Type"])
        return ET.fromstring(raw), raw.decode()

    def block_for(self, root, target):
        for block in root.iter(EDM + "Annotations"):
            if block.get("Target") == target:
                return block
        return None

    def term(self, block, name):
        for annotation in block.findall(EDM + "Annotation"):
            if annotation.get("Term") == name:
                return annotation
        return None


class TestTheDocument(AnnotationDocumentCase):
    def test_it_is_discoverable_from_the_service_document(self):
        _, _, body = self.get(GW + "/?$format=json")
        link = body["d"]["__annotations"]
        self.assertTrue(link.endswith(GW + "/annotations"))

        status, _, raw = self.get(GW + "/", headers={"Accept": "application/atomsvc+xml"},
                                  raw=True)
        self.assertEqual(status, 200)
        self.assertIn("SAPData/annotations", raw.decode())
        self.assertIn(GW + "/annotations", raw.decode())

    def test_it_references_what_it_annotates(self):
        root, text = self.document()
        namespaces = set()
        for reference in root.findall(EDMX + "Reference"):
            for include in reference.findall(EDMX + "Include"):
                namespaces.add(include.get("Namespace"))
        self.assertIn("com.sap.vocabularies.UI.v1", namespaces,
                      "the vocabulary the terms come from")
        self.assertIn("GWSAMPLE_BASIC", namespaces,
                      "and the service whose types it targets")
        self.assertIn(GW + "/$metadata", text)

    def test_annotations_blocks_do_not_nest(self):
        root, _ = self.document()
        for block in root.iter(EDM + "Annotations"):
            inner = [b for b in block.iter(EDM + "Annotations") if b is not block]
            self.assertEqual(inner, [], "Annotations sit at schema level in CSDL")

    def test_the_terms_a_list_report_needs(self):
        root, _ = self.document()
        block = self.block_for(root, "GWSAMPLE_BASIC.BusinessPartner")
        self.assertIsNotNone(block)

        header = self.term(block, "UI.HeaderInfo")
        values = {p.get("Property"): p
                  for p in header.find(EDM + "Record").findall(EDM + "PropertyValue")}
        self.assertEqual(values["TypeName"].get("String"), "Business Partner")

        line_item = self.term(block, "UI.LineItem")
        columns = [record.find(EDM + "PropertyValue").get("Path")
                   for record in line_item.find(EDM + "Collection")]
        self.assertEqual(columns[0], "BusinessPartnerID")
        self.assertIn("CompanyName", columns)

        self.assertIsNotNone(self.term(block, "UI.SelectionFields"))
        self.assertIsNotNone(self.term(block, "UI.Facets"))

    def test_every_annotated_path_is_a_real_property(self):
        root, _ = self.document()
        _, _, metadata = self.get(GW + "/$metadata", raw=True)
        edmx = ET.fromstring(metadata)
        edm_v2 = "{http://schemas.microsoft.com/ado/2008/09/edm}"

        properties, navigations = {}, {}
        for entity in edmx.iter(edm_v2 + "EntityType"):
            name = entity.get("Name")
            properties[name] = {p.get("Name") for p in entity.findall(edm_v2 + "Property")}
            navigations[name] = {n.get("Name")
                                 for n in entity.findall(edm_v2 + "NavigationProperty")}

        checked = 0
        for block in root.iter(EDM + "Annotations"):
            target = block.get("Target", "").split(".", 1)[1]
            type_name = target.split("/")[0]
            self.assertIn(type_name, properties, target)
            if "/" in target:                      # a property label
                self.assertIn(target.split("/")[1], properties[type_name], target)
                checked += 1
                continue
            for record in block.iter(EDM + "Record"):
                if record.get("Type") != "UI.DataField":
                    continue
                path = record.find(EDM + "PropertyValue").get("Path")
                self.assertIn(path, properties[type_name], "%s: %s" % (target, path))
                checked += 1
            for path in block.iter(EDM + "PropertyPath"):
                self.assertIn(path.text, properties[type_name], target)
                checked += 1
            for record in block.iter(EDM + "Record"):
                if record.get("Type") != "UI.ReferenceFacet":
                    continue
                value = record.findall(EDM + "PropertyValue")[1].get("AnnotationPath")
                if value.startswith("@UI.FieldGroup#"):
                    continue
                navigation = value.split("/")[0]
                self.assertIn(navigation, navigations[type_name],
                              "%s: %s is not a navigation property" % (target, value))
                checked += 1
        self.assertGreater(checked, 50)

    def test_property_labels_are_there(self):
        root, _ = self.document()
        block = self.block_for(root, "GWSAMPLE_BASIC.BusinessPartner/CompanyName")
        self.assertIsNotNone(block)
        annotation = block.find(EDM + "Annotation")
        self.assertEqual(annotation.get("Term"), "Common.Label")
        self.assertEqual(annotation.get("String"), "Company Name")


class TestTheRoutesStaySeparate(AnnotationDocumentCase):
    def test_v2_metadata_is_unchanged(self):
        status, _, raw = self.get(GW + "/$metadata", raw=True)
        self.assertEqual(status, 200)
        text = raw.decode()
        self.assertIn('sap:label="Company Name"', text)
        self.assertNotIn("UI.LineItem", text,
                         "the V2 terms live in the annotation document, not here")

    def test_v4_services_have_no_annotation_document(self):
        status, _, body = self.get(V4 + "/annotations")
        self.assertEqual(status, 404)
        self.assertIn("$metadata", body["error"]["message"])

        # and the A2X V2 services, which have no UI declarations, likewise
        status, _, _ = self.get(SRV + "/annotations?$format=json")
        self.assertEqual(status, 404)

    def test_the_document_is_read_only(self):
        status, _, _ = self.request("POST", GW + "/annotations", body={},
                                    headers=self.csrf_token())
        self.assertEqual(status, 405)


if __name__ == "__main__":
    unittest.main(verbosity=2)
