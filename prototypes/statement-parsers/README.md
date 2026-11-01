# Midas statement parser and PDF unlocker spike

This isolated spike extracts canonical, owner-relative transactions from Axis, SBI, Slice,
ICICI, IDFC FIRST, HSBC, and YES Bank credit-card statements plus HSBC India composite,
Axis One Glance, HDFC, Kotak, YES Bank, and Slice savings statements. It deliberately stops
at parsed staging entries and does not write to the Midas ledger, database, or
reconciliation workflow.

Supported adapters:

- `axis.credit-card.pdf.v0`
- `sbi.credit-card.pdf.v0`
- `slice.credit-card.pdf.v0`
- `slice.savings-account.pdf.v0`
- `icici.credit-card.pdf.v0`
- `hdfc.savings-account.pdf.v0`
- `yes.savings-account.pdf.v0`
- `idfc-first.credit-card.pdf.v0`
- `hsbc.credit-card.pdf.v0`
- `yes-bank.credit-card.pdf.v0`
- `axis.one-glance.savings.pdf.v0`
- `axis.quick-view.savings.pdf.v0`
- `kotak.savings-account.pdf.v0`
- `bank-statement.hsbc.india.composite.v1`

The prototype also includes a preview-only Gmail inventory. It lists likely PDF, CSV, and
ZIP statement attachments using Gmail metadata but does not download message bodies or
attachment bytes. Inventory review is intentionally required before ingestion.

All transaction output files are written atomically with owner-only (`0600`) permissions.
Multi-account statements use privacy-safe section references such as `axis-section-2`;
raw account identifiers are not copied into those references.

Axis and SBI were recovered from commit `eca983b` on
`codex/prototype-axis-sbi-parsers`; HSBC and the shared PDF password handling were then
consolidated into this same prototype.

It can also create an unencrypted copy of a password-protected PDF. The source is never
overwritten, and the output is verified to be unencrypted with the same page count before
it is published.

## Run

Use a Python 3.11+ virtual environment with `pdfplumber` installed (do not rely on a
system `python3` that may be older):

```bash
PYTHONPATH=src /path/to/python3.11 -m statement_parser.cli statement.pdf --output transactions.json
PYTHONPATH=src /path/to/python3.11 -m statement_parser.cli statement.pdf --format csv --output transactions.csv
```

The original parser invocation remains compatible. `parse` can also be stated explicitly:

```bash
PYTHONPATH=src /path/to/python3.11 -m statement_parser.cli parse statement.pdf --output transactions.json
```

## Parse an encrypted PDF

For persistent local storage, use macOS Keychain as described in
[`PASSWORDS.md`](PASSWORDS.md). After saving an issuer-specific key, parsing can retrieve
the password without exposing it in shell history or source files:

```bash
PYTHONPATH=src /path/to/python3.11 -m statement_parser.cli parse locked.pdf \
  --password-key idfc-bank \
  --output transactions.json
```

For automation, provide the name of an environment variable holding the password. The
password itself is intentionally not accepted as a command-line argument, where it could
be exposed in shell history or process listings.

```bash
export STATEMENT_PDF_PASSWORD='...'
PYTHONPATH=src /path/to/python3.11 -m statement_parser.cli parse locked.pdf \
  --password-env STATEMENT_PDF_PASSWORD \
  --output transactions.json
unset STATEMENT_PDF_PASSWORD
```

For an interactive run, prompt securely without saving an unlocked PDF:

```bash
PYTHONPATH=src /path/to/python3.11 -m statement_parser.cli parse locked.pdf \
  --prompt-password \
  --output transactions.json
```

## Unlock a PDF

Prompt securely for the password:

```bash
PYTHONPATH=src /path/to/python3.11 -m statement_parser.cli unlock locked.pdf \
  --output output/pdf/statement_unlocked.pdf
```

The standalone `unlock` command writes a verified, unencrypted copy when you explicitly
need one. For normal transaction extraction, prefer parsing the encrypted PDF directly.

```bash
export STATEMENT_PDF_PASSWORD='...'
PYTHONPATH=src /path/to/python3.11 -m statement_parser.cli unlock locked.pdf \
  --output output/pdf/statement_unlocked.pdf \
  --password-env STATEMENT_PDF_PASSWORD
unset STATEMENT_PDF_PASSWORD
```

Amounts are signed owner cash flow in minor units: deposits are positive and withdrawals
are negative; credit-card charges are negative while card payments, refunds, and credits
are positive. The JSON result also reports available opening/closing balances, parse
issues, source provenance, and balance reconciliation.

Private statement PDFs are used only as local acceptance inputs and are not copied into
this repository. Generated JSON should also be treated as sensitive financial data.

## Test

```bash
PYTHONPATH=src /path/to/python3.11 -m unittest discover -s tests -v
```

## Inventory Gmail statement candidates

Supply an OAuth access token authorized with the read-only Gmail scope
`https://www.googleapis.com/auth/gmail.readonly`. The token is read only from an
environment variable and is never written to the inventory:

```bash
export GMAIL_ACCESS_TOKEN='...'
PYTHONPATH=src /path/to/python3.11 -m statement_parser.cli gmail-inventory \
  --output output/gmail-statement-inventory.json \
  --max-messages 500
unset GMAIL_ACCESS_TOKEN
```

The output is written atomically with owner-only (`0600`) permissions and is ignored by
Git. It contains sender, subject, Gmail message/thread IDs, and attachment metadata, so it
must still be treated as sensitive. The command uses only Gmail `GET` operations; it does
not modify labels, messages, or mailbox state and does not download attachments.

Use `--query` to narrow the default candidate search or `--token-env` to select a different
environment variable. Access tokens expire; this spike deliberately does not store refresh
tokens or OAuth client credentials.

## Prototype boundaries

- Parsing stops at canonical staging entries; there is no ledger mutation.
- Money uses integer paise and never floating-point arithmetic.
- PDF extraction is a replaceable boundary shared by every adapter.
- Statement boilerplate is excluded by adapter-specific table boundaries.
- Passwords can be prompted or read from an environment variable and are never accepted as
  command-line values. Persistent passwords can be retrieved from macOS Keychain by a
  non-secret key name.
- Gmail discovery is preview-only, bounded to 5,000 messages per invocation, and stores no
  OAuth credentials or attachment bytes.

When the Midas application reaches its import phase, move these adapters behind the planned
Java `SourceAdapter` seam (using a valid package such as `com.midas.importing.adapter`) and
retain the fixture-free acceptance behavior.
