"""Many clients at once: every request is answered as if it were the only one.

The server takes requests on a thread each and has one SQLite connection.
Nothing kept two requests from using it at the same moment, and every handler
is a sequence - read the row, decide, write it back - so concurrent writers
got 409s and 500s neither deserved and left rows behind from requests that
had failed (#91). Eight threads posting orders into the unfixed mock had
fewer than a quarter of them succeed, and counted more orders afterwards than
had been acknowledged.

What is asserted is the arithmetic, not the absence of an exception: every
request acknowledged, and the database holding exactly what was acknowledged.
"""
from __future__ import annotations

import threading
import time
import unittest

from support import MockServerCase, SRV

CLIENTS, EACH = 8, 10
ORDER = {"SalesOrderType": "OR", "SalesOrganization": "1710",
         "SoldToParty": "1000001",
         "to_Item": [{"Material": "TG11", "RequestedQuantity": "2",
                      "NetAmount": "10.00", "RequestedQuantityUnit": "PC"}] * 3}


class ConcurrencyCase(MockServerCase):
    def count(self, entity_set):
        status, _, raw = self.get(SRV + "/%s/$count" % entity_set, raw=True)
        self.assertEqual(status, 200)
        return int(raw)

    def together(self, work, clients=CLIENTS):
        """Run `work(index)` on `clients` threads released at the same moment."""
        gate, results, lock = threading.Barrier(clients), [], threading.Lock()

        def run(index):
            gate.wait()
            try:
                outcome = work(index)
            except Exception as exc:        # a refused connection is a result too
                outcome = [(repr(exc), None)]
            with lock:
                results.extend(outcome)

        threads = [threading.Thread(target=run, args=(index,))
                   for index in range(clients)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(60)
        self.assertFalse([t for t in threads if t.is_alive()], "a client hung")
        return results


class TestWritersAtOnce(ConcurrencyCase):
    def test_every_order_is_created_once_and_whole(self):
        headers = self.csrf_token()
        orders, items = self.count("A_SalesOrder"), self.count("A_SalesOrderItem")

        def post(_):
            out = []
            for _ in range(EACH):
                status, _, body = self.request(
                    "POST", SRV + "/A_SalesOrder", body=ORDER, headers=headers)
                out.append((status, body["d"]["SalesOrder"] if status == 201
                            else body))
            return out

        results = self.together(post)

        self.assertEqual([r for r in results if r[0] != 201], [],
                         "no request was failed by another one")
        numbers = [number for _, number in results]
        self.assertEqual(len(numbers), CLIENTS * EACH)
        self.assertEqual(len(set(numbers)), len(numbers), "no number twice")
        self.assertEqual(self.count("A_SalesOrder"), orders + CLIENTS * EACH)
        self.assertEqual(self.count("A_SalesOrderItem"),
                         items + CLIENTS * EACH * 3,
                         "three items each, and none from a request that failed")

    def test_readers_and_a_changeset_do_not_see_each_others_halves(self):
        """A batch snapshots and restores the whole database; reads carry on."""
        headers = self.csrf_token()
        member = ("--changeset_1\r\n"
                  "Content-Type: application/http\r\n"
                  "Content-Transfer-Encoding: binary\r\n\r\n"
                  "POST A_SalesOrder HTTP/1.1\r\n"
                  "Content-Type: application/json\r\n\r\n%s\r\n")
        good = member % ('{"SalesOrderType":"OR","SalesOrganization":"1710",'
                         '"SoldToParty":"1000001"}')
        bad = member % '{"ThisPropertyIsWrong":"1"}'
        body = ("--batch_test\r\n"
                "Content-Type: multipart/mixed; boundary=changeset_1\r\n\r\n"
                + good + bad + "--changeset_1--\r\n--batch_test--\r\n")
        before = self.count("A_SalesOrder")

        def work(index):
            out = []
            for _ in range(EACH):
                if index % 2:
                    status, _, _ = self.request(
                        "POST", SRV + "/$batch", body=body, raw=True,
                        headers=dict(headers, **{
                            "Content-Type": "multipart/mixed; boundary=batch_test"}))
                    out.append((status, 202))
                else:
                    status, _, _ = self.get(SRV + "/A_SalesOrder?$top=5&$format=json")
                    out.append((status, 200))
            return out

        results = self.together(work)

        self.assertEqual([r for r in results if r[0] != r[1]], [])
        self.assertEqual(self.count("A_SalesOrder"), before,
                         "every changeset failed, and every one was rolled back")


    def test_the_control_plane_reads_while_orders_are_written(self):
        """`/_mock` is on the same connection and waits its turn like the rest."""
        headers = self.csrf_token()

        def work(index):
            out = []
            for _ in range(EACH):
                if index % 2:
                    status, _, _ = self.request(
                        "POST", SRV + "/A_SalesOrder", body=ORDER, headers=headers)
                    out.append((status, 201))
                else:
                    status, _, state = self.get("/_mock/state")
                    out.append((status, 200))
                    status, _, _ = self.get("/_mock/requests?verbose=1")
                    out.append((status, 200))
            return out

        results = self.together(work)

        self.assertEqual([r for r in results if r[0] != r[1]], [])


class TestOneSlowRequestIsNotEveryRequest(ConcurrencyCase):
    config_kwargs = {"slow_ms": 1500}

    def test_a_request_being_made_to_wait_does_not_hold_the_others(self):
        """The delay scenarios are a client waiting, not the mock stopping."""
        took = {}

        def work(index):
            started = time.monotonic()
            headers = {"sap-mock-scenario": "slow"} if index == 0 else {}
            if index:
                time.sleep(0.2)             # let the slow one get going first
            status, _, _ = self.get(SRV + "/A_SalesOrder/$count", raw=True,
                                    headers=headers)
            took[index] = time.monotonic() - started
            return [(status, 200)]

        results = self.together(work, clients=4)

        self.assertEqual([r for r in results if r[0] != r[1]], [])
        self.assertGreaterEqual(took[0], 1.5, "the slow request was slow")
        for index in (1, 2, 3):
            self.assertLess(took[index], 1.2,
                            "and the others did not queue behind it")


class TestTheListenQueue(ConcurrencyCase):
    def test_it_is_longer_than_a_small_client_pool(self):
        """Not behaviour, and stated as a number for that reason.

        The behaviour - no connection refused - is what the tests above see,
        but with the default queue of five they saw it fail in most runs
        rather than every run. This holds the setting; they hold the reason.
        """
        self.assertGreaterEqual(self.httpd.request_queue_size, 64)


if __name__ == "__main__":
    unittest.main()
