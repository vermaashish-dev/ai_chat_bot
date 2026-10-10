"""Export the live mid-call switch log (written by VANI's log_persona_switch tool) as JSON + readable Markdown.

Usage:
  python -m scripts.export_switch_log                 # -> outputs/switch_log.json and outputs/switch_log.md
"""

import argparse
import json
from collections import Counter
from pathlib import Path

from sqlalchemy import select

from app import db
from app.config import PROJECT_ROOT, get_settings
from app.models import PersonaSwitchLog

FIELDS = ["created_at", "glid", "call_ref", "signal", "change_made", "reason", "seller_quote"]


def _cell(value) -> str:
    return str(value if value is not None else "").replace("|", "/").replace("\n", " ")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, default=PROJECT_ROOT / "outputs")
    args = ap.parse_args()

    db.configure(get_settings().database_url)
    with db.session_factory()() as session:
        logs = session.scalars(select(PersonaSwitchLog).order_by(PersonaSwitchLog.created_at)).all()
        rows = [{f: getattr(r, f) for f in FIELDS} for r in logs]
    for r in rows:
        r["created_at"] = str(r["created_at"])[:19]

    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "switch_log.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")

    signals = Counter(r["signal"] for r in rows)
    lines = [
        "# Live switch log", "",
        f"{len(rows)} mid-call persona switches across {len({r['call_ref'] for r in rows})} calls, logged by VANI "
        "through the log_persona_switch tool while talking on Sarvam (voice calls and chat tests).", "",
        "Signals: " + ", ".join(f"{s} {n}" for s, n in signals.most_common()), "",
        "| Time | Seller (GLID) | Call | Signal | Change made | Reason | Seller said |",
        "|---|---|---|---|---|---|---|",
    ]
    lines += [f"| {r['created_at']} | {r['glid']} | {_cell(r['call_ref'])[:8]} | {r['signal']} | {_cell(r['change_made'])} "
              f"| {_cell(r['reason'])} | {_cell(r['seller_quote'])} |" for r in rows]
    (args.out / "switch_log.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"{len(rows)} switches -> {args.out / 'switch_log.md'}; signals: {dict(signals)}")


if __name__ == "__main__":
    main()
