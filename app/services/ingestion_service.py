"""Incremental, idempotent ingestion of the PS07 CSVs and optional Markdown transcripts.

File type is decided by the CSV header, not the file name, so renamed or re-split files still load.
A file whose SHA-256 matches the last successful run is skipped. Inside a changed file every row is
compared by hash, so counts distinguish imported / updated / unchanged-or-duplicate / failed rows.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from sqlalchemy import delete, insert, select, update
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.models import (
    ConversationTurn,
    IngestionFile,
    Supplier,
    SupplierAlias,
    SupplierConversation,
)
from app.services.supplier_repository import normalize_name

log = logging.getLogger(__name__)
csv.field_size_limit(10_000_000)

MAX_ERRORS_REPORTED = 20
DERIVED_ALIAS_SOURCES = ("bot_call_summary", "bot_turn_greeting")

FILE_SIGNATURES = {
    # file_type: columns that must all be present
    "seller_file": {"fk_glusr_usr_id", "business_type", "annual_turnover"},
    "bot_calls": {"attempt_id", "fk_glusr_usr_id", "lead_call_summary", "disposition_label"},
    "call_turns": {"attempt_id", "fk_glusr_usr_id", "turn_no", "speaker", "text"},
    "executive_calls": {"click_to_call_id", "fk_glusr_usr_id", "status", "call_start_time"},
}
TYPE_ORDER = ["seller_file", "bot_calls", "call_turns", "executive_calls", "markdown"]
TS_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y")
# Columns kept as text even when they look numeric.
TEXT_COLUMNS = {"seller_pincode", "gst_registration_year"}


@dataclass
class FileResult:
    file_path: str
    file_type: str
    status: str
    imported: int = 0
    updated: int = 0
    skipped: int = 0
    failed: int = 0
    errors: list[str] = field(default_factory=list)

    def error(self, msg: str) -> None:
        self.failed += 1
        if len(self.errors) < MAX_ERRORS_REPORTED:
            self.errors.append(msg)


@dataclass
class IngestionSummary:
    started_at: datetime
    finished_at: datetime | None = None
    files: list[FileResult] = field(default_factory=list)
    missing_inputs: list[str] = field(default_factory=list)
    aliases_rebuilt: int | None = None

    def totals(self) -> dict[str, int]:
        keys = ("imported", "updated", "skipped", "failed")
        return {k: sum(getattr(f, k) for f in self.files) for k in keys}


# ---------------------------------------------------------------- helpers

def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def row_hash(row: dict) -> str:
    return sha256_bytes(json.dumps(row, sort_keys=True, ensure_ascii=False).encode())


def decode(data: bytes) -> tuple[str, str | None]:
    """Decode a file, falling back to cp1252 so a stray byte does not drop the whole file."""
    try:
        return data.decode("utf-8-sig"), None
    except UnicodeDecodeError as exc:
        return data.decode("cp1252", errors="replace"), f"not valid UTF-8 ({exc.reason} at byte {exc.start}); decoded as cp1252"


def parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    value = value.strip()
    for fmt in TS_FORMATS:
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def parse_value(col: str, raw: str | None):
    if raw is None:
        return None
    raw = raw.strip()
    if raw == "":
        return None
    if col in TEXT_COLUMNS:
        return raw[:-2] if raw.endswith(".0") else raw
    try:
        num = float(raw)
    except ValueError:
        return raw
    return int(num) if num.is_integer() and "." not in raw else num


def parse_int_id(raw: str | None) -> int:
    if raw is None or raw.strip() == "":
        raise ValueError("missing id")
    return int(float(raw.strip()))


def to_bool(value) -> bool | None:
    if value is None:
        return None
    return bool(int(float(value)))


def detect_csv_type(header: list[str]) -> str | None:
    cols = set(header)
    for ftype, required in FILE_SIGNATURES.items():
        if required <= cols:
            return ftype
    return None


def iter_csv(text: str, result: FileResult):
    """Yield (line_no, row_dict) and record rows whose field count does not match the header."""
    reader = csv.reader(io.StringIO(text, newline=""))
    try:
        header = next(reader)
    except StopIteration:
        return
    width = len(header)
    for row in reader:
        line_no = reader.line_num
        if not row or (len(row) == 1 and row[0] == ""):
            continue
        if len(row) != width:
            result.error(f"line {line_no}: expected {width} fields, got {len(row)}")
            continue
        yield line_no, dict(zip(header, row, strict=True))


# ---------------------------------------------------------------- name extraction

_NAME = r"(?P<n>[A-Z0-9][A-Za-z0-9&.'()/\- ]{2,70}?)"
_END = (
    r"(?=\.(?:\s|$)|,|\?|;|:|\"|'s\b|\s+(?:regarding|about|but|for|who|which|when|while|or|in|at|on|as|via|"
    r"and (?=[a-z])|"
    r"to (?:schedule|discuss|confirm|offer|inform|ask|arrange|set))\b)"
)
SUMMARY_NAME_PATTERNS = [
    re.compile(r"\b(?:if|whether) (?:the lead|they|he|she|the seller) (?:was|were|is|are) (?:speaking (?:from|with)|from|with) " + _NAME + _END),
    re.compile(r"\b(?:the lead|the seller) from " + _NAME + _END),
]
GREETING_NAME_PATTERN = re.compile(r"\b[Kk]ya aap (?P<n>.+?) se (?:hi |hee )?bol rah")
_PROSE = re.compile(r"\b(?!(?:and|of|the|de|da)\b)[a-z]{2,}\s+(?!(?:and|of|the|de|da)\b)[a-z]{2,}\b")
NAME_STOPLIST = {"indiamart", "india mart", "the company", "their company", "your company", "company", "the business"}


def clean_business_name(name: str | None) -> str | None:
    if not name:
        return None
    name = re.sub(r"\s+", " ", name).strip(" .,'\"-")
    norm = normalize_name(name)
    if len(norm) < 4 or norm in NAME_STOPLIST or len(name) > 80:
        return None
    if re.search(r"(?:^|\s)[A-Za-z]$", name):  # truncated at an initial, e.g. "M.M"
        return None
    # Real names are mostly capitalised; a run of lowercase words means the regex ran into prose.
    if _PROSE.search(name) or ". " in name or re.search(r"\band(?:\s+[a-z]+)?$", name):
        return None
    return name


def extract_name_from_summary(summary: str | None) -> str | None:
    if not summary:
        return None
    for pat in SUMMARY_NAME_PATTERNS:
        m = pat.search(summary)
        if m:
            return clean_business_name(m.group("n"))
    return None


def extract_name_from_greeting(text: str | None) -> str | None:
    if not text:
        return None
    m = GREETING_NAME_PATTERN.search(text)
    return clean_business_name(m.group("n")) if m else None


# ---------------------------------------------------------------- service

class IngestionService:
    def __init__(self, settings: Settings, sessions: sessionmaker[Session]):
        self.settings = settings
        self.sessions = sessions

    # ---- discovery
    def discover(self, summary: IngestionSummary) -> list[tuple[Path, str | None, bytes]]:
        found: list[tuple[Path, str | None, bytes]] = []
        sup_dir, conv_dir = Path(self.settings.supplier_data_dir), Path(self.settings.conversation_data_dir)
        if not sup_dir.is_dir():
            summary.missing_inputs.append(f"SUPPLIER_DATA_DIR not found: {sup_dir}")
        else:
            for p in sorted(sup_dir.glob("*.csv")):
                data = p.read_bytes()
                text, _ = decode(data[:65536])
                header = next(csv.reader(io.StringIO(text)), [])
                found.append((p, detect_csv_type(header), data))
        if not conv_dir.is_dir():
            summary.missing_inputs.append(f"CONVERSATION_DATA_DIR not found: {conv_dir} (Markdown transcripts optional)")
        else:
            for p in sorted(conv_dir.rglob("*.md")):
                found.append((p, "markdown", p.read_bytes()))
        types = {t for _, t, _ in found}
        if "seller_file" not in types:
            summary.missing_inputs.append("No seller file found (CSV with fk_glusr_usr_id, business_type, annual_turnover)")
        if "bot_calls" not in types:
            summary.missing_inputs.append("No bot call file found (CSV with attempt_id, lead_call_summary, disposition_label)")
        order = {t: i for i, t in enumerate(TYPE_ORDER)}
        found.sort(key=lambda x: (order.get(x[1], 99), str(x[0])))
        return found

    # ---- entry point
    def run(self, force: bool = False) -> IngestionSummary:
        summary = IngestionSummary(started_at=datetime.now())
        changed_conversations = False
        for path, ftype, data in self.discover(summary):
            rel = self._rel(path)
            if ftype is None:
                res = FileResult(rel, "unknown", "unrecognized", errors=["header does not match any known PS07 file"])
                self._record(res, sha256_bytes(data))
                summary.files.append(res)
                continue
            content_hash = sha256_bytes(data)
            if not force and self._unchanged(rel, content_hash):
                res = FileResult(rel, ftype, "skipped_unchanged")
                summary.files.append(res)
                continue
            if ftype == "markdown" and changed_conversations:
                # Markdown linking by name needs aliases from the bot data loaded in this run.
                summary.aliases_rebuilt = self.rebuild_aliases()
                changed_conversations = False
            res = self._ingest_file(path, rel, ftype, data)
            self._record(res, content_hash)
            summary.files.append(res)
            log.info("ingested %s [%s] %s", rel, ftype, {k: getattr(res, k) for k in ("status", "imported", "updated", "skipped", "failed")})
            if ftype in ("bot_calls", "call_turns", "seller_file") and res.status != "failed":
                changed_conversations = True
        if changed_conversations:
            summary.aliases_rebuilt = self.rebuild_aliases()
        summary.finished_at = datetime.now()
        return summary

    def _rel(self, path: Path) -> str:
        for base in (Path(self.settings.supplier_data_dir).parent, Path.cwd()):
            try:
                return path.resolve().relative_to(base.resolve()).as_posix()
            except ValueError:
                continue
        return path.as_posix()

    def _unchanged(self, rel: str, content_hash: str) -> bool:
        with self.sessions() as s:
            rec = s.scalar(select(IngestionFile).where(IngestionFile.file_path == rel))
            return bool(rec and rec.content_hash == content_hash and rec.status in ("success", "partial"))

    def _record(self, res: FileResult, content_hash: str) -> None:
        with self.sessions.begin() as s:
            rec = s.scalar(select(IngestionFile).where(IngestionFile.file_path == res.file_path))
            if rec is None:
                rec = IngestionFile(file_path=res.file_path)
                s.add(rec)
            rec.file_type = res.file_type
            # A failed file keeps no hash so the next run retries it.
            rec.content_hash = content_hash if res.status in ("success", "partial") else None
            rec.last_ingested_at = datetime.now()
            rec.status = res.status
            rec.imported, rec.updated, rec.skipped, rec.failed = res.imported, res.updated, res.skipped, res.failed
            rec.error_summary = "\n".join(res.errors) or None

    def _ingest_file(self, path: Path, rel: str, ftype: str, data: bytes) -> FileResult:
        res = FileResult(rel, ftype, "success")
        text, warn = decode(data)
        if warn:
            res.errors.append(warn)
        handler = {
            "seller_file": self._ingest_sellers,
            "bot_calls": self._ingest_bot_calls,
            "call_turns": self._ingest_turns,
            "executive_calls": self._ingest_exec_calls,
            "markdown": self._ingest_markdown,
        }[ftype]
        try:
            with self.sessions.begin() as s:
                handler(s, text, rel, res)
        except Exception as exc:  # whole-file failure rolls back; nothing half-written
            log.exception("ingestion failed for %s", rel)
            return FileResult(rel, ftype, "failed", failed=res.failed, errors=[*res.errors, f"{type(exc).__name__}: {exc}"][:MAX_ERRORS_REPORTED])
        if res.failed:
            res.status = "partial"
        return res

    # ---- shared upsert
    def _apply(self, s: Session, model, inserts: list[dict], updates: list[dict]) -> None:
        bs = self.settings.ingest_batch_size
        for i in range(0, len(inserts), bs):
            s.execute(insert(model), inserts[i : i + bs])
        for i in range(0, len(updates), bs):
            s.execute(update(model), updates[i : i + bs])

    def _glid_map(self, s: Session) -> dict[int, int]:
        return dict(s.execute(select(Supplier.glid, Supplier.id)).all())

    # ---- sellers
    def _ingest_sellers(self, s: Session, text: str, rel: str, res: FileResult) -> None:
        existing = {g: (i, h) for g, i, h in s.execute(select(Supplier.glid, Supplier.id, Supplier.row_hash))}
        seen: set[int] = set()
        inserts, updates = [], []
        now = datetime.now()
        for line_no, row in iter_csv(text, res):
            try:
                glid = parse_int_id(row.get("fk_glusr_usr_id"))
            except ValueError:
                res.error(f"line {line_no}: invalid fk_glusr_usr_id {row.get('fk_glusr_usr_id')!r}")
                continue
            if glid in seen:
                res.skipped += 1  # duplicate within file: first row wins
                continue
            seen.add(glid)
            h = row_hash(row)
            prof = {k: v for k, v in ((k, parse_value(k, v)) for k, v in row.items()) if v is not None}
            values = dict(
                glid=glid,
                city=prof.get("seller_city"),
                state=prof.get("seller_state"),
                business_type=prof.get("business_type"),
                annual_turnover=prof.get("annual_turnover"),
                nature_of_business=prof.get("nature_of_business"),
                top_category_1=prof.get("top_category_1"),
                top_category_group=prof.get("top_category_group"),
                do_not_call=bool(to_bool(prof.get("do_not_call_requested"))),
                profile_json=prof,
                row_hash=h,
                source_file=rel,
                ingested_at=now,
            )
            if glid not in existing:
                inserts.append(values)
                res.imported += 1
            elif existing[glid][1] != h:
                updates.append({"id": existing[glid][0], **values})
                res.updated += 1
            else:
                res.skipped += 1
        self._apply(s, Supplier, inserts, updates)

    # ---- conversations (bot + executive share the upsert path)
    def _upsert_conversations(self, s: Session, channel: str, rows: list[dict], res: FileResult) -> None:
        existing = {
            r: (i, h)
            for r, i, h in s.execute(
                select(SupplierConversation.source_record_id, SupplierConversation.id, SupplierConversation.content_hash).where(
                    SupplierConversation.channel == channel
                )
            )
        }
        inserts, updates, seen = [], [], set()
        for values in rows:
            rid = values["source_record_id"]
            if rid in seen:
                res.skipped += 1
                continue
            seen.add(rid)
            if rid not in existing:
                inserts.append(values)
                res.imported += 1
            elif existing[rid][1] != values["content_hash"]:
                updates.append({"id": existing[rid][0], **values})
                res.updated += 1
            else:
                res.skipped += 1  # already ingested (e.g. same call in the recording sample)
        self._apply(s, SupplierConversation, inserts, updates)

    def _ingest_bot_calls(self, s: Session, text: str, rel: str, res: FileResult) -> None:
        glids = self._glid_map(s)
        rows, now = [], datetime.now()
        for line_no, row in iter_csv(text, res):
            try:
                attempt = str(parse_int_id(row.get("attempt_id")))
                glid = parse_int_id(row.get("fk_glusr_usr_id"))
            except ValueError:
                res.error(f"line {line_no}: invalid attempt_id/fk_glusr_usr_id")
                continue
            ts = parse_ts(row.get("call_start_time"))
            sid = glids.get(glid)
            mf = parse_value("meeting_fixed", row.get("meeting_fixed"))
            rows.append(
                dict(
                    supplier_id=sid,
                    glid=glid,
                    link_status="linked" if sid else "needs_review",
                    channel="bot_call",
                    source_record_id=attempt,
                    conversation_ts=ts,
                    ts_valid=ts is not None,
                    conversation_type=row.get("lead_bot_version") or None,
                    outcome=row.get("disposition_label") or None,
                    meeting_fixed=to_bool(mf) if isinstance(mf, (int, float)) else None,
                    duration_sec=parse_value("d", row.get("lead_call_duration")),
                    summary=(row.get("lead_call_summary") or "").strip() or None,
                    raw_content=None,
                    details_json={
                        k: parse_value(k, row.get(k))
                        for k in ("redis_bucket", "call_attempt_count", "has_turn_transcript", "has_evaluator_output", "call_recording_url")
                    },
                    content_hash=row_hash(row),
                    source_file=rel,
                    ingested_at=now,
                )
            )
        self._upsert_conversations(s, "bot_call", rows, res)

    def _ingest_exec_calls(self, s: Session, text: str, rel: str, res: FileResult) -> None:
        glids = self._glid_map(s)
        rows, now = [], datetime.now()
        for line_no, row in iter_csv(text, res):
            try:
                rid = str(parse_int_id(row.get("click_to_call_id")))
                glid = parse_int_id(row.get("fk_glusr_usr_id"))
            except ValueError:
                res.error(f"line {line_no}: invalid click_to_call_id/fk_glusr_usr_id")
                continue
            ts = parse_ts(row.get("call_start_time"))
            sid = glids.get(glid)
            # fk_employeeid is deliberately not stored: not needed for personas.
            rows.append(
                dict(
                    supplier_id=sid,
                    glid=glid,
                    link_status="linked" if sid else "needs_review",
                    channel="executive_call",
                    source_record_id=rid,
                    conversation_ts=ts,
                    ts_valid=ts is not None,
                    conversation_type=row.get("module") or None,
                    outcome=row.get("status") or None,
                    meeting_fixed=None,
                    duration_sec=parse_value("d", row.get("call_duration_customer")),
                    summary=None,
                    raw_content=None,
                    details_json={k: parse_value(k, row.get(k)) for k in ("call_duration", "ring_duration")},
                    content_hash=row_hash({k: v for k, v in row.items() if k != "fk_employeeid"}),
                    source_file=rel,
                    ingested_at=now,
                )
            )
        self._upsert_conversations(s, "executive_call", rows, res)

    def _ingest_turns(self, s: Session, text: str, rel: str, res: FileResult) -> None:
        existing = {
            (a, v, t): (i, h)
            for a, v, t, i, h in s.execute(
                select(ConversationTurn.attempt_id, ConversationTurn.transcript_variant, ConversationTurn.turn_no, ConversationTurn.id, ConversationTurn.row_hash)
            )
        }
        inserts, updates = [], []
        seen_rows: set[str] = set()
        variant_of: Counter[tuple[str, int]] = Counter()
        for line_no, row in iter_csv(text, res):
            try:
                attempt = str(parse_int_id(row.get("attempt_id")))
                turn_no = parse_int_id(row.get("turn_no"))
                glid = parse_int_id(row.get("fk_glusr_usr_id"))
            except ValueError:
                res.error(f"line {line_no}: invalid attempt_id/turn_no/fk_glusr_usr_id")
                continue
            h = row_hash(row)
            if h in seen_rows:
                res.skipped += 1  # byte-identical duplicate row
                continue
            seen_rows.add(h)
            # A repeated (attempt, turn_no) with different content is a second transcript of the call.
            variant = variant_of[(attempt, turn_no)]
            variant_of[(attempt, turn_no)] += 1
            key = (attempt, variant, turn_no)
            values = dict(
                attempt_id=attempt,
                transcript_variant=variant,
                glid=glid,
                turn_no=turn_no,
                speaker=(row.get("speaker") or "unknown")[:10],
                start_sec=parse_value("s", row.get("start_sec")),
                end_sec=parse_value("e", row.get("end_sec")),
                text=row.get("text"),
                row_hash=h,
            )
            if key not in existing:
                inserts.append(values)
                res.imported += 1
            elif existing[key][1] != h:
                updates.append({"id": existing[key][0], **values})
                res.updated += 1
            else:
                res.skipped += 1
        self._apply(s, ConversationTurn, inserts, updates)

    # ---- markdown
    def _ingest_markdown(self, s: Session, text: str, rel: str, res: FileResult) -> None:
        from app.services.supplier_repository import SupplierRepository

        repo = SupplierRepository(s)
        glids = self._glid_map(s)
        s.execute(delete(SupplierConversation).where(SupplierConversation.channel == "markdown", SupplierConversation.source_file == rel))
        now = datetime.now()
        for idx, conv in enumerate(parse_markdown_conversations(text)):
            meta, body = conv["meta"], conv["body"]
            if not body.strip():
                res.error(f"section {idx}: empty conversation body")
                continue
            content_hash = sha256_bytes(body.strip().encode())
            dup = s.scalar(select(SupplierConversation.id).where(SupplierConversation.content_hash == content_hash).limit(1))
            if dup:
                res.skipped += 1
                continue
            sid, glid, method = None, None, None
            raw_glid = meta.get("glid") or meta.get("fk_glusr_usr_id") or meta.get("supplier_id")
            if raw_glid:
                try:
                    glid = parse_int_id(raw_glid)
                    sid, method = glids.get(glid), "glid"
                except ValueError:
                    res.errors.append(f"section {idx}: unparseable glid {raw_glid!r}")
            if sid is None and meta.get("business_name"):
                matches = repo.find_by_name(meta["business_name"])
                if len(matches) == 1:
                    sid, glid, method = matches[0].id, matches[0].glid, "business_name_exact"
            ts = parse_ts(meta.get("date") or meta.get("timestamp") or meta.get("call_date"))
            s.add(
                SupplierConversation(
                    supplier_id=sid,
                    glid=glid,
                    link_status="linked" if sid else "needs_review",
                    channel="markdown",
                    source_record_id=f"{rel}#{idx}",
                    conversation_ts=ts,
                    ts_valid=ts is not None,
                    conversation_type=meta.get("type") or meta.get("conversation_type"),
                    outcome=meta.get("outcome"),
                    summary=meta.get("summary"),
                    raw_content=body,
                    details_json={"metadata": meta, "link_method": method},
                    content_hash=content_hash,
                    source_file=rel,
                    ingested_at=now,
                )
            )
            res.imported += 1
            if sid is None:
                res.errors.append(f"section {idx}: no confident supplier match; flagged needs_review")

    # ---- aliases
    def rebuild_aliases(self) -> int:
        """Re-derive business-name aliases from bot summaries and opening greetings."""
        counts: Counter[tuple[int, str, str]] = Counter()
        display: dict[tuple[int, str, str], Counter[str]] = defaultdict(Counter)
        first_ref: dict[tuple[int, str, str], str] = {}
        with self.sessions.begin() as s:
            glids = self._glid_map(s)
            q = select(SupplierConversation.supplier_id, SupplierConversation.source_record_id, SupplierConversation.summary).where(
                SupplierConversation.channel == "bot_call", SupplierConversation.supplier_id.is_not(None), SupplierConversation.summary.is_not(None)
            )
            for sid, rid, summary_text in s.execute(q):
                name = extract_name_from_summary(summary_text)
                if name:
                    key = (sid, normalize_name(name), "bot_call_summary")
                    counts[key] += 1
                    display[key][name] += 1
                    first_ref.setdefault(key, f"bot_call:{rid}")
            q2 = select(ConversationTurn.attempt_id, ConversationTurn.glid, ConversationTurn.text).where(
                ConversationTurn.speaker == "bot", ConversationTurn.turn_no <= 2
            )
            for attempt, glid, text in s.execute(q2):
                sid = glids.get(glid)
                name = extract_name_from_greeting(text) if sid else None
                if name:
                    key = (sid, normalize_name(name), "bot_turn_greeting")
                    counts[key] += 1
                    display[key][name] += 1
                    first_ref.setdefault(key, f"call_turn:{attempt}")
            s.execute(delete(SupplierAlias).where(SupplierAlias.source.in_(DERIVED_ALIAS_SOURCES)))
            rows = [
                dict(
                    supplier_id=k[0],
                    normalized_name=k[1],
                    source=k[2],
                    business_name=display[k].most_common(1)[0][0],
                    occurrences=n,
                    first_source_record=first_ref[k],
                )
                for k, n in counts.items()
            ]
            self._apply(s, SupplierAlias, rows, [])
        return len(rows)


# ---------------------------------------------------------------- markdown parsing

_META_LINE = re.compile(r"^\s*[-*]?\s*\**(?P<k>[A-Za-z_ ]{2,30})\**\s*[:=]\s*(?P<v>.+?)\s*$")
_META_KEYS = {"glid", "fk_glusr_usr_id", "supplier_id", "business_name", "date", "timestamp", "call_date", "type", "conversation_type", "outcome", "summary"}


def _read_meta(lines: list[str]) -> dict[str, str]:
    meta: dict[str, str] = {}
    for line in lines:
        m = _META_LINE.match(line)
        if m:
            k = m.group("k").strip().lower().replace(" ", "_")
            if k in _META_KEYS:
                meta[k] = m.group("v").strip()
    return meta


def parse_markdown_conversations(text: str) -> list[dict]:
    """Conservative: one conversation per file, unless several H2 sections each carry their own date/glid."""
    meta: dict[str, str] = {}
    body = text
    fm = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, re.S)
    if fm:
        meta = _read_meta(fm.group(1).splitlines())
        body = text[fm.end():]
    sections = re.split(r"(?m)^##\s+", body)
    head, parts = sections[0], sections[1:]
    part_meta = [_read_meta(p.splitlines()[:8]) for p in parts]
    if len(parts) > 1 and all(("date" in m or "timestamp" in m or "glid" in m) for m in part_meta):
        file_meta = {**meta, **_read_meta(head.splitlines()[:8])}
        return [{"meta": {**file_meta, **pm}, "body": "## " + p} for p, pm in zip(parts, part_meta, strict=True)]
    if not fm:
        meta = _read_meta(body.splitlines()[:12])
    return [{"meta": meta, "body": body}]
