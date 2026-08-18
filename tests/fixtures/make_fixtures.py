"""Regenerate ``tests/fixtures/sample.mbox``.

Kept as a script rather than a fixture factory so the mbox is a real file that
can be opened in a mail client and eyeballed when a parser test starts arguing
with reality. Run: ``python tests/fixtures/make_fixtures.py``
"""

from __future__ import annotations

import zlib
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _minimal_pdf(text: str) -> bytes:
    """A one-page PDF with a real text layer, built without a PDF library."""
    content = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode("latin-1")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"\nendstream",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
    xref_at = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_at}\n%%EOF\n"
    ).encode()
    return bytes(out)


PDF = _minimal_pdf("Rechnung Nr 2026-0042 Betrag 1499 EUR faellig 30.04.2026")
PDF_B64 = __import__("base64").encodebytes(PDF).decode("ascii").strip()

MESSAGES: list[bytes] = []


def add(raw: str, charset: str = "utf-8") -> None:
    """Encode each message with the charset it declares.

    Writing the whole file as UTF-8 would turn the ISO-8859-1 message into
    mojibake and quietly test the wrong thing.
    """
    MESSAGES.append(raw.strip("\n").encode(charset))


# 1 — the ordinary case: plain text, a quoted reply, a signature with a phone.
add(
    """From bob@example.com Mon Mar 16 09:14:00 2026
Message-ID: <ordinary-1@example.com>
Date: Mon, 16 Mar 2026 09:14:00 +0000
From: Bob Fischer <bob@example.com>
To: Sam <you@example.com>
Subject: Re: Q2 warehouse lease renewal
Content-Type: text/plain; charset="utf-8"

Hi Sam,

Confirming the warehouse lease renewal at 4200 EUR per month, starting
1 July 2026. The landlord wants a decision by 30 April.

Best regards,
Bob Fischer
+49 30 1234 5678
https://example.com

On Fri, Mar 13, 2026 at 4:02 PM Sam <you@example.com> wrote:
> Could you confirm the renewal figure before I sign anything?
> I have the old contract at 3900.
"""
)

# 2 — German: "Am ... schrieb ...:" opener and a German closing.
add(
    """From anna@beispiel.de Sat Mar 14 10:31:00 2026
Message-ID: <deutsch-2@beispiel.de>
Date: Sat, 14 Mar 2026 10:31:00 +0100
From: Anna Weber <anna@beispiel.de>
To: Sam <you@example.com>
Subject: =?utf-8?B?QW5nZWJvdCBmw7xyIGRpZSBTb2xhcmFubGFnZQ==?=
Content-Type: text/plain; charset="iso-8859-1"

Hallo Sam,

anbei das Angebot f\xfcr die Solaranlage. Der Gesamtpreis betr\xe4gt 18.400 EUR
inklusive Montage. Die Lieferzeit liegt bei acht Wochen.

Mit freundlichen Gr\xfc\xdfen
Anna Weber
Telefon: +49 89 998877
www.beispiel.de

Am 14.03.2026 um 09:12 schrieb Sam <you@example.com>:
> Kannst du mir ein Angebot schicken?
""",
    charset="iso-8859-1",
)

# 3 — German Outlook inline header block rather than an "Am ... schrieb" line.
add(
    """From klaus@beispiel.de Fri Mar 13 08:00:00 2026
Message-ID: <outlook-de-3@beispiel.de>
Date: Fri, 13 Mar 2026 08:00:00 +0100
From: Klaus Meier <klaus@beispiel.de>
To: Sam <you@example.com>
Subject: AW: Rechnung Marz
Content-Type: text/plain; charset="utf-8"

Guten Morgen,

die Rechnung ist bezahlt, Referenznummer XR-99120.

Von: Sam <you@example.com>
Gesendet: Donnerstag, 12. Marz 2026 17:40
An: Klaus Meier
Betreff: Rechnung Marz

alter zitierter text der nicht indexiert werden soll
"""
)

# 4 — HTML only, no text/plain alternative.
add(
    """From newsletter@shop.example Wed Mar 11 12:00:00 2026
Message-ID: <htmlonly-4@shop.example>
Date: Wed, 11 Mar 2026 12:00:00 +0000
From: Shop <newsletter@shop.example>
To: Sam <you@example.com>
Subject: Your order 88213 has shipped
Content-Type: text/html; charset="utf-8"

<html><body><p>Your order <b>88213</b> shipped today by DHL.</p>
<p>Tracking number 00340434161094023456.</p>
<blockquote>Previous message you should not index</blockquote>
</body></html>
"""
)

# 5 — multipart/mixed with a PDF attachment carrying a text layer.
add(
    f"""From billing@lieferant.de Tue Mar 10 07:45:00 2026
Message-ID: <attachment-5@lieferant.de>
Date: Tue, 10 Mar 2026 07:45:00 +0100
From: Buchhaltung <billing@lieferant.de>
To: Sam <you@example.com>
Subject: Rechnung 2026-0042
Content-Type: multipart/mixed; boundary="BOUND5"

--BOUND5
Content-Type: text/plain; charset="utf-8"

Sehr geehrter Herr Reyes,

anbei die Rechnung fuer Maerz.

--BOUND5
Content-Type: application/pdf; name="Rechnung_2026-0042.pdf"
Content-Transfer-Encoding: base64
Content-Disposition: attachment; filename="Rechnung_2026-0042.pdf"

{PDF_B64}

--BOUND5--
"""
)

# --- deliberately broken from here on ------------------------------------

# 6 — no Date header at all.
add(
    """From nodate@example.com Thu Jan  1 00:00:00 2026
Message-ID: <nodate-6@example.com>
From: No Date <nodate@example.com>
To: Sam <you@example.com>
Subject: Missing date header entirely
Content-Type: text/plain

This message has no Date header. Ingestion must still index the porcupine.
"""
)

# 7 — an unparseable Date header.
add(
    """From baddate@example.com Thu Jan  1 00:00:00 2026
Message-ID: <baddate-7@example.com>
Date: yesterday afternoon-ish
From: Bad Date <baddate@example.com>
To: Sam <you@example.com>
Subject: Date header is prose
Content-Type: text/plain

The date header above is not a date. Index the marmoset anyway.
"""
)

# 8 — a charset that does not exist.
add(
    """From badcharset@example.com Thu Jan  1 00:00:00 2026
Message-ID: <badcharset-8@example.com>
Date: Mon, 09 Mar 2026 11:00:00 +0000
From: Bad Charset <badcharset@example.com>
To: Sam <you@example.com>
Subject: Charset is invented
Content-Type: text/plain; charset="klingon-8"

Declared charset does not exist. The word to find here is axolotl.
"""
)

# 9 — multipart declaring a boundary that never appears.
add(
    """From badmime@example.com Thu Jan  1 00:00:00 2026
Message-ID: <badmime-9@example.com>
Date: Sun, 08 Mar 2026 11:00:00 +0000
From: Broken MIME <badmime@example.com>
To: Sam <you@example.com>
Subject: Boundary never appears
Content-Type: multipart/mixed; boundary="NEVER-APPEARS"

This body sits outside any part because the boundary is missing. Capybara.
"""
)

# 10 — headers and nothing else.
add(
    """From empty@example.com Thu Jan  1 00:00:00 2026
Message-ID: <empty-10@example.com>
Date: Sat, 07 Mar 2026 11:00:00 +0000
From: Empty <empty@example.com>
To: Sam <you@example.com>
Subject: Completely empty body
Content-Type: text/plain

"""
)

# 11 — no Subject, no From, base64 body whose padding is wrong.
add(
    """From  Thu Jan  1 00:00:00 2026
Message-ID: <headerless-11@example.com>
Date: Fri, 06 Mar 2026 11:00:00 +0000
To: Sam <you@example.com>
Content-Type: text/plain; charset="utf-8"
Content-Transfer-Encoding: base64

VGhpcyBiYXNlNjQgaXMgdHJ1bmNhdGVkIGFuZCB0aGUgd29yZCBpcyBva2FwaQ
"""
)

# 12 — an attachment with no filename and a bogus content type.
add(
    """From weird@example.com Thu Jan  1 00:00:00 2026
Message-ID: <noname-12@example.com>
Date: Thu, 05 Mar 2026 11:00:00 +0000
From: Weird <weird@example.com>
To: Sam <you@example.com>
Subject: Nameless attachment
Content-Type: multipart/mixed; boundary="BOUND12"

--BOUND12
Content-Type: text/plain

Body mentioning a wildebeest.

--BOUND12
Content-Type: application/x-not-a-real-type
Content-Transfer-Encoding: base64

////AAAA

--BOUND12--
"""
)

# 13 — a "From " line inside the body, which naive mbox splitting would break on.
add(
    """From tricky@example.com Thu Jan  1 00:00:00 2026
Message-ID: <fromline-13@example.com>
Date: Wed, 04 Mar 2026 11:00:00 +0000
From: Tricky <tricky@example.com>
To: Sam <you@example.com>
Subject: Body contains a From line
Content-Type: text/plain

Quoting something below, watch out:

>From the manual, page 12: the setting is optional.

That is why the pangolin setting stays switched on for now.
"""
)

# 14 — sent by me, so counterparty must be the recipient.
add(
    """From you@example.com Tue Mar 03 11:00:00 2026
Message-ID: <outbound-14@example.com>
Date: Tue, 03 Mar 2026 11:00:00 +0000
From: Sam <you@example.com>
To: Chidi Okafor <chidi@partner.example>
Subject: Statement of work draft
Content-Type: text/plain

Sending over the SOW draft for the numbat migration.
"""
)

# 15 — long enough to need several chunks.
_para = (
    "Section {n}. The transition plan covers migration of the ledger, "
    "reconciliation of open invoices, and handover of the vendor relationships. "
    "Each workstream has a named owner and a completion date, and the steering "
    "group reviews progress fortnightly against the agreed milestones. "
)
add(
    """From longmail@example.com Mon Mar 02 11:00:00 2026
Message-ID: <long-15@example.com>
Date: Mon, 02 Mar 2026 11:00:00 +0000
From: Long <longmail@example.com>
To: Sam <you@example.com>
Subject: Transition plan, full text
Content-Type: text/plain

"""
    + "\n\n".join(_para.format(n=i) for i in range(1, 40))
    + "\n\nThe codeword for the final section is quetzal.\n"
)


def write(path: Path) -> None:
    body = b"\n\n".join(MESSAGES) + b"\n"
    path.write_bytes(body)
    print(f"wrote {path} ({len(MESSAGES)} messages, {len(body)} bytes, crc {zlib.crc32(body):08x})")


if __name__ == "__main__":
    write(HERE / "sample.mbox")
