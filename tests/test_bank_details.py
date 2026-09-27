"""Supplier bank details: the account a payment run pays into.

Where the IBAN lives matters more than it looks. An integration that cannot
read it from SAP keeps its own list of accounts somewhere else, and a payment
then goes to whichever account that list happens to hold.
"""
from __future__ import annotations

import unittest

from mocksap import bank
from support import BP_SRV, MockServerCase

V4_BP = ("/sap/opu/odata4/sap/api_businesspartner/srvd_a2x/sap"
         "/api_businesspartner/0001")
BANK_SET = BP_SRV + "/A_BusinessPartnerBank"


class BankCase(MockServerCase):
    def suppliers_with_accounts(self):
        _, _, body = self.get(BANK_SET + "?$format=json")
        return body["d"]["results"]

    def an_account(self):
        accounts = self.suppliers_with_accounts()
        self.assertTrue(accounts, "the seed gives every supplier an account")
        return accounts[0]


class TestReadingBankDetails(BankCase):
    def test_a_supplier_carries_its_account(self):
        account = self.an_account()
        for field in ("BusinessPartner", "BankIdentification", "BankCountryKey",
                      "SWIFTCode", "BankAccountHolderName"):
            self.assertTrue(account[field], "%s should be filled" % field)

    def test_expanding_the_partner_reaches_the_account(self):
        account = self.an_account()
        _, _, body = self.get(
            BP_SRV + "/A_BusinessPartner('%s')?$expand=to_BusinessPartnerBank"
            "&$format=json" % account["BusinessPartner"])
        accounts = body["d"]["to_BusinessPartnerBank"]["results"]
        self.assertTrue(accounts)
        self.assertEqual(accounts[0]["BankIdentification"],
                         min(a["BankIdentification"] for a in accounts))

    def test_the_same_account_over_v4(self):
        account = self.an_account()
        status, _, body = self.get(
            V4_BP + "/BusinessPartner('%s')?$expand=to_BusinessPartnerBank"
            % account["BusinessPartner"])
        self.assertEqual(status, 200)
        v4 = body["to_BusinessPartnerBank"]
        self.assertEqual(sorted(a["BankIdentification"] for a in v4),
                         sorted(a["BankIdentification"] for a in
                                self.accounts_of(account["BusinessPartner"])))
        self.assertEqual(v4[0]["IBAN"], self.accounts_of(
            account["BusinessPartner"])[0]["IBAN"])

    def accounts_of(self, partner):
        _, _, body = self.get(
            BANK_SET + "?$filter=BusinessPartner%%20eq%%20'%s'&$format=json" % partner)
        return sorted(body["d"]["results"], key=lambda a: a["BankIdentification"])

    def test_a_supplier_can_have_more_than_one_account(self):
        """Which is what catches a client that pays accounts[0] regardless."""
        counted = {}
        for account in self.suppliers_with_accounts():
            counted.setdefault(account["BusinessPartner"], []).append(account)
        several = [a for a in counted.values() if len(a) > 1]
        self.assertTrue(several, "the seed gives one supplier two accounts")
        ids = sorted(a["BankIdentification"] for a in several[0])
        self.assertEqual(ids, ["0001", "0002"])
        self.assertNotEqual(several[0][0]["IBAN"], several[0][1]["IBAN"])

    def test_a_supplier_outside_the_iban_countries_has_no_iban(self):
        without = [a for a in self.suppliers_with_accounts() if not a["IBAN"]]
        self.assertTrue(without, "not every country is on IBAN")
        for account in without:
            self.assertNotIn(account["BankCountryKey"], bank.IBAN_COUNTRIES)
            self.assertTrue(account["BankNumber"], "but it still has a bank")
            self.assertTrue(account["BankAccount"], "and an account number")


class TestTheIbanIsChecked(BankCase):
    def new_account(self, iban, identification="0009", partner=None):
        partner = partner or self.an_account()["BusinessPartner"]
        return self.request("POST", BANK_SET, headers=self.csrf_token(), body={
            "BusinessPartner": partner,
            "BankIdentification": identification,
            "BankCountryKey": "DE",
            "BankName": "Deutsche Bank",
            "SWIFTCode": "DEUTDEFF",
            "IBAN": iban,
            "BankAccountHolderName": "Test Holder",
        })

    def test_a_good_iban_is_accepted(self):
        status, _, body = self.new_account("DE89370400440532013000")
        self.assertEqual(status, 201, body)
        self.assertEqual(body["d"]["IBAN"], "DE89370400440532013000")

    def test_one_wrong_digit_is_refused_and_nothing_is_stored(self):
        before = len(self.suppliers_with_accounts())
        status, _, body = self.new_account("DE89370400440532013001",
                                           identification="0007")
        self.assertEqual(status, 400)
        self.assertIn("check digits", body["error"]["message"]["value"])
        self.assertEqual(len(self.suppliers_with_accounts()), before,
                         "a refused account is not half-written")

    def test_a_patch_that_would_break_the_iban_is_refused(self):
        account = self.an_account()
        while not account["IBAN"]:
            account = [a for a in self.suppliers_with_accounts() if a["IBAN"]][0]
        key = ("(BusinessPartner='%s',BankIdentification='%s')"
               % (account["BusinessPartner"], account["BankIdentification"]))

        status, _, body = self.request(
            "PATCH", BANK_SET + key, body={"IBAN": "DE00370400440532013000"},
            headers=dict(self.csrf_token(), **{"If-Match": "*"}))
        self.assertEqual(status, 400)

        _, _, after = self.get(BANK_SET + key + "?$format=json")
        self.assertEqual(after["d"]["IBAN"], account["IBAN"], "unchanged")

    def test_a_bic_that_is_not_8_or_11_characters_is_refused(self):
        status, _, body = self.request(
            "POST", BANK_SET, headers=self.csrf_token(), body={
                "BusinessPartner": self.an_account()["BusinessPartner"],
                "BankIdentification": "0008", "BankCountryKey": "DE",
                "SWIFTCode": "DEUTDE", "IBAN": ""})
        self.assertEqual(status, 400)
        self.assertIn("BIC", body["error"]["message"]["value"])

    def test_the_mock_holds_its_own_seed_to_the_same_rule(self):
        """Every IBAN the mock seeded is one it would accept from a client."""
        for account in self.suppliers_with_accounts():
            if not account["IBAN"]:
                continue
            self.assertTrue(bank.is_valid_iban(account["IBAN"]), account["IBAN"])


class TestResetRestoresTheAccounts(BankCase):
    def test_reset_brings_the_seeded_accounts_back(self):
        account = self.an_account()
        key = ("(BusinessPartner='%s',BankIdentification='%s')"
               % (account["BusinessPartner"], account["BankIdentification"]))
        status, _, _ = self.request("DELETE", BANK_SET + key,
                                    headers=dict(self.csrf_token(),
                                                 **{"If-Match": "*"}))
        self.assertEqual(status, 204)
        self.assertNotIn(key, [a["BankIdentification"] for a in
                               self.suppliers_with_accounts()])

        self.request("POST", "/_mock/reset")
        status, _, _ = self.get(BANK_SET + key + "?$format=json")
        self.assertEqual(status, 200, "the seeded account is back")


if __name__ == "__main__":
    unittest.main()
