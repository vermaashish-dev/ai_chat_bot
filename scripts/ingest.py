"""Initialise the database and run (incremental) ingestion from the command line.

Usage:  python -m scripts.ingest [--force]
"""

import argparse
import json
import logging

from app import db
from app.config import get_settings
from app.services import evidence_service
from app.services.ingestion_service import IngestionService


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true", help="re-read files even if their hash is unchanged")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    db.init_db()
    summary = IngestionService(get_settings(), db.session_factory()).run(force=args.force)
    for f in summary.files:
        print(f"{f.status:18} {f.file_type:16} {f.file_path}  +{f.imported} ~{f.updated} ={f.skipped} !{f.failed}")
        for e in f.errors[:5]:
            print(f"{'':37}- {e}")
    print("missing inputs:", summary.missing_inputs or "none")
    print("aliases rebuilt:", summary.aliases_rebuilt)
    print("totals:", json.dumps(summary.totals()))
    print("took:", summary.finished_at - summary.started_at)
    with db.session_factory()() as s:
        ev = evidence_service.get_evidence(s)  # cached to EVIDENCE_CACHE_PATH for fast first requests
    print("evidence:", ev.get("baseline"))


if __name__ == "__main__":
    main()
