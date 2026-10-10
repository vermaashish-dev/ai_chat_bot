"""Past-call evidence behind the persona rules (PS04 §3.4).

Every figure is computed from the ingested data, with its sample size and a strength label. These are
observational comparisons, not experiments: where a comparison is confounded the note says so, and
persona rules that rest on weak evidence are marked as informed guesses.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
from collections import defaultdict
from pathlib import Path

from sqlalchemy import Integer, case, cast, func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import ConversationTurn, IngestionFile, Supplier, SupplierConversation
from app.services.signals import SIGNAL_CUES, matches

OBJECTION_CATEGORIES = [
    "Seller Unavailability & Scheduling Constraints",
    "Callback / Deferred Engagement (Neutral)",
    "Call Quality / Technical Failures",
    "No Seller Response / Engagement Drop",
    "Explicit Disinterest / Refusal",
    "Call Handling / Executive Gaps",
    "Existing Engagement / Past Experience",
    "Wrong Contact / Business Not Applicable",
    "Not Ready / Needs Time / Early-Stage Seller",
    "Product / Service / Business Mismatch",
    "Information / Trust / Privacy Concerns",
]

log = logging.getLogger(__name__)
_lock = threading.Lock()
_cache: dict | None = None
_refreshing = False


def strength(n: int, diff_pp: float | None) -> str:
    if diff_pp is None:
        return "insufficient"
    if n >= 1000 and abs(diff_pp) >= 3:
        return "strong"
    if n >= 200 and abs(diff_pp) >= 2:
        return "moderate"
    return "weak"


def _pct(x) -> float | None:
    return None if x is None else round(100 * float(x), 1)


def invalidate() -> None:
    global _cache
    with _lock:
        _cache = None


def _fingerprint(session: Session) -> str:
    rows = session.execute(select(IngestionFile.file_path, IngestionFile.content_hash).order_by(IngestionFile.file_path)).all()
    return hashlib.sha256(json.dumps([list(r) for r in rows]).encode()).hexdigest()


def _read_disk(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _compute_and_store(session: Session, path: Path, fp: str) -> dict:
    global _cache
    data = {**compute_evidence(session), "fingerprint": fp}
    with _lock:
        _cache = data
    try:
        path.write_text(json.dumps(data), encoding="utf-8")
    except OSError as exc:
        log.warning("could not write evidence cache: %s", exc)
    return data


def refresh_in_background(cache_path: Path | None = None) -> bool:
    """Start one background recompute; returns False if one is already running."""
    global _refreshing
    path = Path(cache_path or get_settings().evidence_cache_path)
    with _lock:
        if _refreshing:
            return False
        _refreshing = True

    def run() -> None:
        global _refreshing
        from app import db

        try:
            with db.session_factory()() as s:
                _compute_and_store(s, path, _fingerprint(s))
        except Exception:
            log.exception("background evidence refresh failed")
        finally:
            with _lock:
                _refreshing = False

    threading.Thread(target=run, name="evidence-refresh", daemon=True).start()
    return True


def get_evidence(session: Session, cache_path: Path | None = None, wait: bool = True) -> dict:
    """Evidence for the current data: memory, then disk (valid while ingested files are unchanged).

    wait=True computes inline when nothing current exists (CLI, startup, tests).
    wait=False never blocks (request paths, ~30 s otherwise): it returns the last evidence marked
    stale, or {"available": False, "warming_up": True}, and starts one background refresh.
    """
    global _cache
    path = Path(cache_path or get_settings().evidence_cache_path)
    fp = _fingerprint(session)
    with _lock:
        if _cache is not None and _cache.get("fingerprint") == fp:
            return _cache
    disk = _read_disk(path)
    if disk is not None and disk.get("fingerprint") == fp:
        with _lock:
            _cache = disk
        return disk
    if wait:
        return _compute_and_store(session, path, fp)
    refresh_in_background(path)
    previous = _cache or disk
    if previous is not None:
        return {**previous, "stale": True}
    return {"available": False, "warming_up": True}


def compute_evidence(s: Session) -> dict:
    c = SupplierConversation
    bot = (c.channel == "bot_call", c.supplier_id.is_not(None))
    mf = func.avg(cast(c.meeting_fixed, Integer))
    ni = func.avg(case((c.outcome == "Not Interested", 1), else_=0))

    total, base_mf, base_ni = s.execute(select(func.count(c.id), mf, ni).where(*bot)).one()
    if not total:
        return {"available": False, "note": "No bot call data ingested yet."}
    base_mf_pct = _pct(base_mf)

    def rows_to_groups(rows, min_n=50):
        out = []
        for key, n, m, nint in rows:
            if key is None or n < min_n:
                continue
            m_pct = _pct(m)
            out.append({"group": str(key), "n": n, "meeting_fixed_pct": m_pct, "not_interested_pct": _pct(nint),
                        "vs_baseline_pp": round(m_pct - base_mf_pct, 1), "strength": strength(n, m_pct - base_mf_pct)})
        return sorted(out, key=lambda g: -g["n"])

    by_version = rows_to_groups(s.execute(select(c.conversation_type, func.count(c.id), mf, ni).where(*bot).group_by(c.conversation_type)).all(), 50)
    by_turnover = rows_to_groups(
        s.execute(select(Supplier.annual_turnover, func.count(c.id), mf, ni).join(Supplier, Supplier.id == c.supplier_id).where(*bot).group_by(Supplier.annual_turnover)).all()
    )
    by_business_type = rows_to_groups(
        s.execute(select(Supplier.business_type, func.count(c.id), mf, ni).join(Supplier, Supplier.id == c.supplier_id).where(*bot).group_by(Supplier.business_type)).all()
    )
    by_state = rows_to_groups(
        s.execute(select(Supplier.state, func.count(c.id), mf, ni).join(Supplier, Supplier.id == c.supplier_id).where(*bot).group_by(Supplier.state)).all(), 500
    )
    hour = func.strftime("%H", c.conversation_ts) if s.bind.dialect.name == "sqlite" else func.to_char(c.conversation_ts, "HH24")
    by_hour = sorted(
        rows_to_groups(s.execute(select(hour, func.count(c.id), mf, ni).where(*bot, c.ts_valid.is_(True)).group_by(hour)).all(), 200),
        key=lambda g: g["group"],
    )

    seller_level = _seller_level(s)
    signal_response = _signal_response(s)

    personalised = next((g for g in by_version if g["group"] == "personalis"), None)
    main = next((g for g in by_version if g["group"] == "main_vani"), None)
    headline = []
    if personalised and main:
        headline.append(
            f"Personalised-script trial ('personalis', n={personalised['n']}): {personalised['meeting_fixed_pct']}% meetings fixed and "
            f"{personalised['not_interested_pct']}% not-interested vs main_vani (n={main['n']}): {main['meeting_fixed_pct']}% / {main['not_interested_pct']}%. "
            "Disposition labelling differs between bot versions, so the not-interested gap is partly a labelling effect."
        )
    return {
        "available": True,
        "baseline": {"answered_bot_calls": total, "meeting_fixed_pct": base_mf_pct, "not_interested_pct": _pct(base_ni)},
        "headline": headline,
        "by_bot_version": by_version,
        "by_turnover": by_turnover,
        "by_business_type": by_business_type,
        "by_state": by_state,
        "by_hour": by_hour,
        "seller_level": seller_level,
        "signal_response": signal_response,
        "caveats": [
            "Observational data: differences show association, not cause.",
            "Arrowhead calls are mostly labelled 'General', which lowers their not-interested rate artificially.",
            "Turn transcripts exist for ~12.8k calls only; signal-response figures come from that subset.",
        ],
    }


def _seller_level(s: Session) -> dict:
    """Meeting share among sellers with a given trait, from the seller-file bot summaries."""
    p = Supplier.profile_json
    answered = func.sum(p["bot_answered"].as_float())
    meetings = func.sum(p["calls_meeting_fixed"].as_float())
    base_ans, base_meet = s.execute(select(answered, meetings)).one()
    base = 100 * (base_meet or 0) / base_ans if base_ans else None
    out: dict = {"baseline_meeting_share_pct": round(base, 1) if base is not None else None, "traits": []}

    def add(label: str, cond, note: str = "") -> None:
        n, a, m = s.execute(select(func.count(Supplier.id), answered, meetings).where(cond)).one()
        if not n or not a:
            return
        pct = round(100 * m / a, 1)
        diff = round(pct - base, 1) if base is not None else None
        out["traits"].append({"trait": label, "sellers": n, "meeting_share_pct": pct, "vs_baseline_pp": diff, "strength": strength(n, diff), "note": note})

    add("showed_frustration", p["showed_frustration"].as_float() == 1)
    add("asked_if_talking_to_bot", p["asked_if_talking_to_bot"].as_float() == 1)
    add("already_in_touch_with_executive", p["already_in_touch_with_executive"].as_float() == 1)
    add("asked_pricing_question", p["past_questions_asked"].as_string().like("%Pricing Payment%"))
    add("callback_requested_before", p["past_dispositions_detailed"].as_string().like("%callback_requested%"))
    add("short_calls_avg_under_20s", p["avg_answered_call_sec"].as_float() < 20, "short average answered call; seller tends to cut calls early")
    add("long_calls_avg_over_60s", p["avg_answered_call_sec"].as_float() > 60, "long calls partly reflect calls that already went well")
    for cat in OBJECTION_CATEGORIES:
        add(f"objection:{cat}", p["past_objections"].as_string().like(f"%{cat}%"))
    return out


def _signal_response(s: Session) -> dict:
    """After the first seller turn showing a signal, compare outcomes by how the bot's next turn responded."""
    q = (
        select(ConversationTurn.attempt_id, ConversationTurn.speaker, ConversationTurn.text, SupplierConversation.meeting_fixed)
        .join(SupplierConversation, (SupplierConversation.channel == "bot_call") & (SupplierConversation.source_record_id == ConversationTurn.attempt_id))
        .where(ConversationTurn.transcript_variant == 0)
        .order_by(ConversationTurn.attempt_id, ConversationTurn.turn_no)
    )
    calls: dict[str, list[tuple[str, str]]] = defaultdict(list)
    outcome: dict[str, int] = {}
    for attempt, speaker, text, mfix in s.execute(q):
        calls[attempt].append((speaker, text or ""))
        outcome[attempt] = int(bool(mfix))
    result = {"calls_with_transcripts": len(calls)}
    for signal in SIGNAL_CUES:
        buckets = {k: [0, 0] for k in ("next_turn_keeps_ask", "next_turn_no_ask", "next_turn_short_le20w", "next_turn_long_gt20w")}
        for attempt, turns in calls.items():
            for i, (spk, text) in enumerate(turns):
                if spk == "seller" and matches(signal, text):
                    nxt = next((t for sp, t in turns[i + 1 :] if sp == "bot"), None)
                    if nxt is not None:
                        for key in ("next_turn_keeps_ask" if "?" in nxt else "next_turn_no_ask", "next_turn_short_le20w" if len(nxt.split()) <= 20 else "next_turn_long_gt20w"):
                            buckets[key][0] += 1
                            buckets[key][1] += outcome[attempt]
                    break
        stats = {k: {"n": n, "meeting_fixed_pct": round(100 * m / n, 1) if n else None} for k, (n, m) in buckets.items()}
        a, b = stats["next_turn_keeps_ask"], stats["next_turn_no_ask"]
        diff = (a["meeting_fixed_pct"] - b["meeting_fixed_pct"]) if a["n"] and b["n"] else None
        stats["ask_vs_no_ask_pp"] = round(diff, 1) if diff is not None else None
        stats["strength"] = strength(min(a["n"], b["n"]), diff)
        result[signal] = stats
    result["note"] = (
        "Short next turns are often closings, so 'short' vs 'long' is confounded with the bot giving up. "
        "The 'keeps an ask' split (next bot turn ends in a question) is the more actionable comparison."
    )
    return result
