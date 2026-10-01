#!/usr/bin/env python3
"""Build the RAG acceptance PDF fixtures.

Four documents with different audiences and, more importantly, **different semantics**, so the
ACL check cannot pass by accident:

* ``delivery-terms.pdf`` — ordinary delivery terms, readable by the sales desk (``manager``);
* ``zetetic-memo.pdf`` — a board restructuring memorandum, ``director`` only, carrying a
  distinctive marker ("ZETETIC");
* ``warehouse-receiving.pdf`` — the receiving procedure (``warehouse``);
* ``finance-closing.pdf`` — the month-end checklist (``accountant``, ``director``).

A question about the ZETETIC restructuring is therefore the *best semantic match* for whoever
can see the second document. A manager asking it must not receive it — which tests that the
filter runs before ranking, not that one document happened to rank above another.

The roles are **not** encoded here: they are an explicit `--roles` argument at ingestion time
(see ingest/README.md). This script only produces the files.

Written with the standard library rather than a PDF library: this is a fixture generator, and a
dependency added only to produce test input is not worth carrying in the image. The PDFs are
plain, uncompressed, extractable text.

    uv run --group dev python scripts/make_rag_fixtures.py [outdir]
"""

from __future__ import annotations

import sys
from pathlib import Path

MANAGER_LINES = [
    "MONI AI - Delivery Terms (Operations)",
    "",
    "Scope: standard delivery terms for customer orders handled by the sales desk.",
    "",
    "1. Delivery windows. Standard orders ship within five business days of confirmation.",
    "2. Partial shipments. A partial shipment is allowed when stock is short, and the",
    "   customer is informed by email before dispatch.",
    "3. Carrier selection. The warehouse selects the carrier; the sales desk may request a",
    "   specific carrier at least one day before dispatch.",
    "4. Delays. A delay must be reported to the account manager the same day it is found.",
    "5. Delivery notes. The delivery note travels with the shipment and is recorded against",
    "   the sale order in Odoo.",
]

DIRECTOR_LINES = [
    "MONI AI - ZETETIC Restructuring Memorandum",
    "",
    "Classification: board only. Not for distribution outside the board.",
    "",
    "This memorandum concerns the ZETETIC restructuring of the delivery organisation.",
    "",
    "1. Background. The ZETETIC review concluded that the current delivery structure carries",
    "   duplicated cost across two regions.",
    "2. Proposal. Consolidate the two regional delivery desks into a single ZETETIC unit",
    "   reporting to the operations director.",
    "3. Financial impact. The ZETETIC consolidation is expected to reduce fixed cost.",
    "4. Timeline. Subject to board approval, the ZETETIC transition starts next quarter.",
    "5. Confidentiality. This ZETETIC memorandum is restricted to board members.",
]


WAREHOUSE_LINES = [
    "MONI AI - Warehouse Receiving Procedure (Operations)",
    "",
    "Audience: warehouse and production staff.",
    "",
    "1. Unloading. Count every pallet against the delivery note before signing it.",
    "2. Discrepancies. Record a short delivery in Odoo the same day and photograph the",
    "   pallet before it is put away.",
    "3. Quarantine. Damaged goods go to the QUARANTINE bay and are not booked into stock.",
    "4. Put-away. The receiving operator picks the location; the system confirms it.",
    "5. Escalation. Any discrepancy above ten units is reported to the warehouse manager.",
]

ACCOUNTANT_LINES = [
    "MONI AI - Month-End Closing Checklist (Finance)",
    "",
    "Audience: finance. Contains no client-identifying detail by design.",
    "",
    "1. Cut-off. All delivery notes dated in the period must be invoiced before closing.",
    "2. Accruals. Unbilled deliveries are accrued at cost, not at sales value.",
    "3. Reconciliation. Bank, ledger and the Odoo stock valuation must agree before the",
    "   period is locked; an unexplained difference is escalated, never adjusted away.",
    "4. FX. Foreign-currency balances are revalued at the closing rate.",
    "5. Lock. The period is locked after review, and later entries need an approval.",
]


def _escape(text: str) -> str:
    """Escape the three characters that are special inside a PDF literal string."""
    return text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")


def build_pdf(lines: list[str]) -> bytes:
    """A minimal, valid, text-extractable single-page A4 PDF."""
    content = ["BT", "/F1 11 Tf", "14 TL", "50 780 Td"]
    for line in lines:
        content.append(f"({_escape(line)}) Tj")
        content.append("T*")
    content.append("ET")
    stream = "\n".join(content).encode("latin-1", "replace")

    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
        b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]

    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"

    xref_at = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_at}\n%%EOF\n"
    ).encode()
    return bytes(out)


def main(argv: list[str]) -> int:
    outdir = Path(argv[1]) if len(argv) > 1 else Path("_fixtures")
    outdir.mkdir(parents=True, exist_ok=True)
    # Four documents with four distinct audiences, so the acceptance run exercises a real
    # corpus rather than a single pair. The role for each is passed to `moni_ingest add
    # --roles` (see the fixture table in ingest/README.md); nothing infers it.
    fixtures = (
        ("delivery-terms", MANAGER_LINES),
        ("zetetic-memo", DIRECTOR_LINES),
        ("warehouse-receiving", WAREHOUSE_LINES),
        ("finance-closing", ACCOUNTANT_LINES),
    )
    for name, lines in fixtures:
        target = outdir / f"{name}.pdf"
        target.write_bytes(build_pdf(lines))
        print(f"wrote {target} ({target.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
