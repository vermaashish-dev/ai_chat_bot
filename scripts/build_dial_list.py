"""Filter a campaign contact CSV to sellers that may be dialled, before uploading it to Sarvam.

Usage:  python -m scripts.build_dial_list contacts.csv [--glid-column glid]
Writes  contacts_dialable.csv  (rows safe to dial) and  contacts_excluded.csv  (row + reason).
Rows are excluded when the seller asked not to be called, or the GLID is not on record (unchecked).
"""

import argparse
import csv
from pathlib import Path

from app import db
from app.services.sarvam_service import dial_check


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("contacts", type=Path)
    ap.add_argument("--glid-column", default="glid")
    args = ap.parse_args()

    with args.contacts.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        if args.glid_column not in (reader.fieldnames or []):
            raise SystemExit(f"column '{args.glid_column}' not found; columns: {reader.fieldnames}")
        fields, rows = reader.fieldnames, list(reader)

    def glid_of(row):
        try:
            return int(float(row[args.glid_column]))
        except (TypeError, ValueError):
            return None

    glids = [g for g in (glid_of(r) for r in rows) if g is not None]
    with db.session_factory()() as s:
        result = dial_check(s, glids)
    allowed = set(result["allowed"])
    reasons = {b["glid"]: b["reason"] for b in result["blocked"]} | {g: "glid_not_on_record" for g in result["unknown"]}

    ok_path = args.contacts.with_name(args.contacts.stem + "_dialable.csv")
    bad_path = args.contacts.with_name(args.contacts.stem + "_excluded.csv")
    seen: set[int] = set()
    with ok_path.open("w", newline="", encoding="utf-8") as ok, bad_path.open("w", newline="", encoding="utf-8") as bad:
        w_ok = csv.DictWriter(ok, fieldnames=fields)
        w_bad = csv.DictWriter(bad, fieldnames=[*fields, "excluded_reason"])
        w_ok.writeheader()
        w_bad.writeheader()
        for row in rows:
            g = glid_of(row)
            if g is None:
                w_bad.writerow({**row, "excluded_reason": "invalid_glid"})
            elif g in seen:
                w_bad.writerow({**row, "excluded_reason": "duplicate_glid"})
            elif g in allowed:
                w_ok.writerow(row)
            else:
                w_bad.writerow({**row, "excluded_reason": reasons[g]})
            if g is not None:
                seen.add(g)
    print(f"dialable: {ok_path}\nexcluded: {bad_path}\nallowed={len(allowed)} blocked={len(result['blocked'])} unknown={len(result['unknown'])}")


if __name__ == "__main__":
    main()
