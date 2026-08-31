# PROTOTYPE - Axis and SBI credit-card statement parsers

This throwaway spike answers one question: **can the two supplied Indian
credit-card PDF layouts be converted into Midas's canonical, owner-relative
statement entries without allowing statement boilerplate to become financial
transactions?**

It deliberately stops at parsed staging entries. It does not deduplicate,
normalize merchants, propose postings, write a database, or mutate the ledger.
The checked-in tests use synthetic statement text; the private reference PDFs
and their personal data are not committed.

## Run

```bash
python3 -m venv .venv
.venv/bin/pip install -r prototypes/statement-parsers/requirements.txt
.venv/bin/python prototypes/statement-parsers/parse_statement.py /path/to/statement.pdf
```

The command emits canonical JSON locally. Treat that output as sensitive.

Run the tests with:

```bash
python3 -m unittest discover -s prototypes/statement-parsers/tests -v
```

## Prototype boundaries

- Adapter IDs end in `v0` to make their experimental status explicit.
- Debit/card charges become negative owner cash flow; payments, refunds, and
  credits become positive owner cash flow.
- Money is parsed with `Decimal` and emitted as integer paise, never float.
- SBI continuation rows such as tax lines inherit the preceding dated row's
  date only while the transaction table is active.
- Marketing, legal, schedule-of-charge, and worked-example pages are ignored.
- PDF text extraction is a replaceable boundary; the parsers consume extracted
  text rather than coupling canonicalization to a PDF library.
- Local PDF input is capped at 25 MiB and 100 pages. This prototype is not a
  hardened upload service and must not be exposed to untrusted remote input.
