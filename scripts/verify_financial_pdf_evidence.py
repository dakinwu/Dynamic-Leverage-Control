"""Save four new issuer PDFs, then replay a version-specific semantic spot check.

This is a manually reviewed, layout-specific evidence check, not a general PDF
parser or a historical signal backtest. Existing statement snapshots are reused.
PDF extraction requires pypdf (available in the bundled document runtime).
"""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal as D
from io import BytesIO
from pathlib import Path
from urllib.request import ProxyHandler, Request, build_opener

ROOT = Path(__file__).resolve().parents[1]
DOCUMENTS = {
    "2330-q1": {
        "url": "https://investor.tsmc.com/sites/ir/financial-report/2026/TSMC%202026Q1%20Consolidated%20Financial%20Statements_E.pdf",
        "listing": "https://investor.tsmc.com/english/financial-reports",
        "pages": [5],
    },
    "2330-q2": {
        "url": "https://investor.tsmc.com/sites/ir/financial-report/2026/TSMC%202026Q2%20Consolidated%20Financial%20Statements_E_2.pdf",
        "listing": "https://investor.tsmc.com/english/financial-reports",
        "pages": [5, 6, 36],
    },
    "2801-q2": {
        "url": "https://www.bankchb.com/chb_2a_resource/leap_do/gallery/1787561888598/115Q2data2total.pdf",
        "listing": "https://www.bankchb.com/frontend/financeDetail.jsp?id=84",
        "pages": [8],
    },
    "2881-q2": {
        "url": "https://www.irpro.co/2881/financial/179/EN/20260930180600-3.pdf",
        "listing": "https://fubon.irpro.co/en/financial.php",
        "pages": [4, 10, 453],
    },
}


def sha(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def write_new(path: Path, value: dict):
    with path.open("x", encoding="utf-8") as target:
        json.dump(value, target, ensure_ascii=False, indent=2)


def capture(folder: Path):
    folder.mkdir(parents=True, exist_ok=False)
    # Only this read-only process bypasses the stale environment proxy. No system
    # setting changes, authentication, or access-control bypass is performed.
    opener = build_opener(ProxyHandler({}))
    observations = []
    for name, specification in DOCUMENTS.items():
        request = Request(specification["url"], headers={"User-Agent": "ResearchEvidence/1.0"})
        with opener.open(request, timeout=30) as response:
            body = response.read(30_000_001)
            received = datetime.now(UTC).isoformat()
            content_type, final_url = response.headers.get("Content-Type"), response.url
        if len(body) > 30_000_000 or not body.startswith(b"%PDF-"):
            raise ValueError(f"expected bounded PDF body: {name}")
        filename = f"{name}.pdf"
        with (folder / filename).open("xb") as target:
            target.write(body)
        observations.append(
            {
                "document_id": name,
                **specification,
                "final_url": final_url,
                "raw_file": filename,
                "body_sha256": sha(body),
                "received_at": received,
                "recorded_at": datetime.now(UTC).isoformat(),
                "content_type": content_type,
                "published_at": None,
            }
        )
    write_new(
        folder / "observations.json",
        {"schema": "issuer-pdf-evidence-v1", "observations": observations},
    )


def contained_file(folder: Path, filename: str) -> Path:
    path = (folder / filename).resolve()
    if not path.is_relative_to(folder.resolve()):
        raise ValueError("evidence path outside capture")
    return path


def verify(evidence: Path, statements: Path, output: Path):
    if output.exists():
        raise FileExistsError("use a new acceptance output")
    from pypdf import PdfReader

    manifest = json.loads((evidence / "observations.json").read_bytes())
    documents, excerpts = {}, {}
    for entry in manifest["observations"]:
        body = contained_file(evidence, entry["raw_file"]).read_bytes()
        if sha(body) != entry["body_sha256"]:
            raise ValueError("PDF fingerprint mismatch")
        name = entry["document_id"]
        if name not in DOCUMENTS or entry["url"] != DOCUMENTS[name]["url"]:
            raise ValueError("unexpected evidence identity")
        reader = PdfReader(BytesIO(body))
        excerpts[name] = {
            page: reader.pages[page - 1].extract_text() for page in DOCUMENTS[name]["pages"]
        }
        documents[name] = {**entry, "page_count": len(reader.pages)}
    if set(documents) != set(DOCUMENTS):
        raise ValueError("all four reviewed documents required")
    raw_manifest = json.loads((statements / "observations.json").read_bytes())
    rows, raw_sources = {}, {}
    for suffix, company in (("ci", "2330"), ("basi", "2801"), ("fh", "2881")):
        entry = next(
            x
            for x in raw_manifest["observations"]
            if x["source_url"].endswith(f"t187ap06_L_{suffix}")
        )
        body = contained_file(statements, entry["raw_file"]).read_bytes()
        if sha(body) != entry["body_sha256"]:
            raise ValueError("statement fingerprint mismatch")
        matches = [
            x
            for x in json.loads(body)
            if x["公司代號"] == company and x["年度"] == "115" and x["季別"] == "2"
        ]
        if len(matches) != 1:
            raise ValueError("unique reviewed row required")
        rows[company], raw_sources[company] = (
            matches[0],
            {
                key: entry[key]
                for key in ("source_id", "source_url", "body_sha256", "received_at", "recorded_at")
            },
        )
    checks = []

    def check(name, passed, **details):
        checks.append({"check": name, "passed": bool(passed), **details})

    def tokens(document, page, *expected):
        # Require the exact manually reviewed field/page tokens; this is a
        # regression check against saved PDFs, not an inferred table mapping.
        text = "".join(excerpts[document][page].split())
        return all("".join(str(token).split()) in text for token in expected)

    def amount(company, field):
        return D(rows[company][field].replace(",", ""))

    general_fields = {
        "營業收入": "2404483690",
        "營業毛利（毛損）淨額": "1611606116",
        "營業利益（損失）": "1425568793",
        "本期淨利（淨損）": "1279582227",
    }
    check(
        "2330-reviewed-half-year-fields",
        all(amount("2330", key) == D(value) for key, value in general_fields.items())
        and tokens(
            "2330-q2", 5, "2,404,483,690", "1,611,606,116", "1,425,568,793", "1,279,582,227"
        ),
    )
    check(
        "2330-period-unit-scope",
        tokens(
            "2330-q2",
            5,
            "Six Months Ended June 30",
            "2026",
            "Thousands of New Taiwan Dollars",
            "CONSOLIDATED",
        ),
    )
    q2_revenue = amount("2330", "營業收入") - D("1134103440")
    q2_gross = amount("2330", "營業毛利（毛損）淨額") - D("751295421")
    check(
        "2330-quarter-amount-difference",
        tokens("2330-q1", 5, "1,134,103,440", "751,295,421")
        and tokens("2330-q2", 5, "1,270,380,250", "860,310,695")
        and q2_revenue == D("1270380250")
        and q2_gross == D("860310695"),
        revenue=str(q2_revenue),
        gross_profit=str(q2_gross),
        gross_margin=str(q2_gross / q2_revenue),
        signal_available=False,
    )
    bank_fields = {
        "利息淨收益": "15221502",
        "利息以外淨損益": "10607584",
        "營業費用": "11225331",
        "繼續營業單位稅前淨利（淨損）": "13113055",
        "本期淨利（淨損）": "11314353",
    }
    check(
        "2801-reviewed-half-year-fields",
        all(amount("2801", key) == D(value) for key, value in bank_fields.items())
        and tokens("2801-q2", 8, "15,221,502", "10,607,584", "25,829,086", "11,314,353"),
    )
    check("2801-period-unit-scope", tokens("2801-q2", 8, "合併", "仟元", "115", "1月1日至6月30日"))
    check(
        "2801-accounting-identities",
        amount("2801", "利息淨收益") + amount("2801", "利息以外淨損益") == D("25829086")
        and D("25829086") - D("1490700") - D("11225331") == D("13113055")
        and D("13113055") - D("1798702") == D("11314353"),
        sign_convention="expense_magnitude",
    )
    mismatch = (
        amount("2881", "淨收益") == D("5446990")
        and amount("2881", "利息以外淨收益") == D("172468262")
        and tokens("2881-q2", 10, "5,446,990", "172,468,262", "Bad debt", "Net revenue")
    )
    check(
        "2881-field-mismatch-detected-and-quarantined",
        mismatch,
        raw_field="淨收益",
        raw_value=str(amount("2881", "淨收益")),
        verified_meaning="bad-debt-expense",
        normalization_allowed=False,
    )
    check(
        "2881-net-revenue-cross-check",
        tokens("2881-q2", 453, "112,076,678", "60,391,584", "172,468,262")
        and D("112076678") + D("60391584") == D("172468262"),
    )
    result = {
        "schema": "issuer-financial-semantics-spot-check-v1",
        "verified_at": datetime.now(UTC).isoformat(),
        "method": "manual-page-semantic-review-plus-fixed-token-and-arithmetic-replay",
        "documents": documents,
        "statement_sources": raw_sources,
        "checks": checks,
        "acceptance_passed": all(c["passed"] for c in checks),
        "reviewed_compatible_rows": ["2330/115Q2/ci", "2801/115Q2/basi"],
        "quarantined_rows": ["2881/115Q2/fh"],
        "period_basis": "year-to-date",
        "amount_unit": "TWD-thousand",
        "scope": "consolidated",
        "eps_amount_unit": "TWD",
        "eps_differencing_allowed": False,
        "financial_period_semantics_verified": False,
        "normalized_quarter_signals": 0,
        "historical_publication_time_verified": False,
        "profitability_demonstrated": False,
        "limitations": [
            "only selected fields of three exact source rows reviewed",
            "token checks do not automatically infer field mappings",
            "no normalized source/context sequence registered",
            "no full industry, historical universe, or revision acceptance",
        ],
    }
    write_new(output, result)
    print(
        json.dumps(
            {"acceptance_passed": result["acceptance_passed"], "checks": checks}, ensure_ascii=True
        )
    )
    if not result["acceptance_passed"]:
        raise ValueError("financial evidence spot check failed; do not promote semantics")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument(
        "--statements", type=Path, default=ROOT / "data/private/fundamental-evidence-20261005-v2"
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--capture", action="store_true", help="capture the four new PDFs in a new directory"
    )
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError("acceptance output already exists")
    if args.capture:
        capture(args.evidence)
    verify(args.evidence, args.statements, args.output)
