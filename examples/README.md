# Examples

Two things that need nothing but this mock:

- `demo.sh`, a tour of every endpoint in curl.
- `client.py`, a dependency-free client showing the token and cookie flow.

## The integrations moved to mock-acme

`invoice_check.py` and `remittance.py` lived here, with their tests. They are
code that sits *between* this mock and the others, so they now live in one
place, with one copy of each and tests that run against all three mocks:
[mock-acme](https://github.com/rseufert/mock-acme).

| Was here | Is now |
| --- | --- |
| `examples/invoice_check.py` | [`mockacme/invoice_check.py`](https://github.com/rseufert/mock-acme/blob/main/mockacme/invoice_check.py) |
| `examples/test_invoice_check.py` | [`tests/test_invoice_check.py`](https://github.com/rseufert/mock-acme/blob/main/tests/test_invoice_check.py) |
| `examples/remittance.py` | [`mockacme/remittance.py`](https://github.com/rseufert/mock-acme/blob/main/mockacme/remittance.py) |
| `examples/test_remittance.py` | [`tests/test_remittance.py`](https://github.com/rseufert/mock-acme/blob/main/tests/test_remittance.py) |

The last versions kept here are at
[`911ffcd`](https://github.com/rseufert/mock-sap/tree/911ffcd/examples).
