"""Two answers a run may choose: the rollback's type, a refused option's status (#170).

Both went in as judgement calls - #167 made a rollback that could not undo
anything answer `E`, #165 made `$search` a 501 and its two neighbours a 400 -
and which is right depends on how the client under test reads them. So each
is a flag. What neither flag offers is the behaviour it replaced, and the last
class here is the proof of that.
"""
from __future__ import annotations

import contextlib
import io
import unittest

from support import MockServerCase, SRV

from mocksap.__main__ import build_parser
from mocksap.server import Config

V4 = "/sap/opu/odata4/sap/api_salesorder/srvd_a2x/sap/api_salesorder/0001"
ORDER = {
    "ORDER_HEADER_IN": {"DOC_TYPE": "OR", "SALES_ORG": "1710",
                        "DISTR_CHAN": "10", "DIVISION": "00", "CURRENCY": "EUR"},
    "ORDER_PARTNERS": [{"PARTN_ROLE": "AG", "PARTN_NUMB": "0001000001"}],
    "ORDER_ITEMS_IN": [{"ITM_NUMBER": "000010", "MATERIAL": "TG11",
                        "REQ_QTY": "10", "COND_VALUE": "5000.00"}],
}
REFUSED = ("$search=zzzzzz", "$skiptoken=5", "$format=xml")


class ChoiceCase(MockServerCase):
    def rollback_after_a_write(self):
        headers = self.csrf_token()
        _, _, created = self.request(
            "POST", "/sap/bc/rfc/BAPI_SALESORDER_CREATEFROMDAT2",
            headers=headers, body=ORDER)
        self.assertTrue(created["SALESDOCUMENT"])
        _, _, body = self.request(
            "POST", "/sap/bc/rfc/BAPI_TRANSACTION_ROLLBACK", headers=headers,
            body={})
        return body["RETURN"]

    def statuses(self):
        """The status each refused option gets, on both OData versions."""
        got = {}
        for option in REFUSED:
            v2, _, _ = self.get(SRV + "/A_SalesOrder?$top=1&" + option, raw=True)
            v4, _, _ = self.get(V4 + "/SalesOrder?$top=1&" + option, raw=True)
            self.assertEqual(v2, v4, option)
            got[option] = v2
        return got

    def health(self):
        return self.get("/_mock/health")[2]


class TestLeftAlone(ChoiceCase):
    def test_the_rollback_is_an_error(self):
        self.assertEqual(self.rollback_after_a_write()["TYPE"], "E")

    def test_search_is_a_501_and_the_other_two_a_400(self):
        self.assertEqual(self.statuses(), {"$search=zzzzzz": 501,
                                           "$skiptoken=5": 400,
                                           "$format=xml": 400})

    def test_health_says_which_were_chosen(self):
        health = self.health()

        self.assertEqual(health["rollbackType"], "E")
        self.assertIsNone(health["unsupportedOptionStatus"])


class TestARollbackThatWarns(ChoiceCase):
    config_kwargs = {"rollback_type": "W"}

    def test_it_is_a_warning_with_the_same_words(self):
        answer = self.rollback_after_a_write()

        self.assertEqual(answer["TYPE"], "W")
        self.assertIn("Nothing was rolled back", answer["MESSAGE"])
        self.assertIn("BAPI_SALESORDER_CREATEFROMDAT2", answer["MESSAGE"])

    def test_with_nothing_written_it_is_still_a_success(self):
        self.request("POST", "/_mock/reset")
        _, _, body = self.request(
            "POST", "/sap/bc/rfc/BAPI_TRANSACTION_ROLLBACK",
            headers=self.csrf_token(), body={})

        self.assertEqual(body["RETURN"]["TYPE"], "S",
                         "the choice is about what could not be undone")

    def test_health_says_so(self):
        self.assertEqual(self.health()["rollbackType"], "W")

    def test_it_does_not_touch_the_option_statuses(self):
        self.assertEqual(self.statuses()["$search=zzzzzz"], 501)


class TestEveryRefusedOptionA400(ChoiceCase):
    config_kwargs = {"unsupported_option_status": 400}

    def test_all_three_match(self):
        self.assertEqual(set(self.statuses().values()), {400})

    def test_the_words_have_not_changed(self):
        _, _, body = self.get(SRV + "/A_SalesOrder?$format=json&$search=x")

        self.assertIn("$search is not implemented",
                      body["error"]["message"]["value"])

    def test_health_says_so(self):
        self.assertEqual(self.health()["unsupportedOptionStatus"], 400)

    def test_it_does_not_touch_the_rollback(self):
        self.assertEqual(self.rollback_after_a_write()["TYPE"], "E")


class TestEveryRefusedOptionA501(ChoiceCase):
    config_kwargs = {"unsupported_option_status": 501}

    def test_all_three_match(self):
        self.assertEqual(set(self.statuses().values()), {501})

    def test_a_request_without_one_is_not_refused(self):
        status, _, _ = self.get(SRV + "/A_SalesOrder?$top=1&$format=json")

        self.assertEqual(status, 200)


class TestWhatIsNotOnOffer(unittest.TestCase):
    """No setting brings back the behaviour either fix replaced."""

    def test_a_rollback_cannot_be_told_to_answer_success(self):
        for value in ("S", "A", "error", ""):
            with self.subTest(value=value):
                if value == "":
                    self.assertEqual(Config(rollback_type=value).rollback_type, "E",
                                     "unset is the default, not a third answer")
                    continue
                with self.assertRaises(ValueError) as refused:
                    Config(rollback_type=value)
                self.assertIn("rollback_type is E or W", str(refused.exception))

    def test_lower_case_is_the_same_choice(self):
        self.assertEqual(Config(rollback_type="w").rollback_type, "W")

    def test_a_refused_option_cannot_be_given_a_status_that_accepts_it(self):
        for value in (200, 204, 404, "400", 0):
            with self.subTest(value=value):
                with self.assertRaises(ValueError) as refused:
                    Config(unsupported_option_status=value)
                self.assertIn("unsupported_option_status is 400 or 501",
                              str(refused.exception))

    def test_the_command_line_offers_the_same_and_no_more(self):
        chosen = build_parser().parse_args(
            ["--rollback-type", "W", "--unsupported-option-status", "400"])
        self.assertEqual((chosen.rollback_type, chosen.unsupported_option_status),
                         ("W", 400))
        config = Config(**vars(chosen))
        self.assertEqual((config.rollback_type, config.unsupported_option_status),
                         ("W", 400))

        untouched = build_parser().parse_args([])
        self.assertEqual(
            (untouched.rollback_type, untouched.unsupported_option_status),
            ("E", None))

        for bad in (["--rollback-type", "S"],
                    ["--unsupported-option-status", "200"]):
            # argparse explains itself on stderr before it exits
            with self.subTest(argv=bad), contextlib.redirect_stderr(io.StringIO()), \
                    self.assertRaises(SystemExit):
                build_parser().parse_args(bad)


if __name__ == "__main__":
    unittest.main()
