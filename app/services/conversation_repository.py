"""History retrieval for one supplier. Dated records first (latest wins, id breaks ties); undated kept apart."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.models import ConversationTurn, SupplierConversation

CONTENT_CHANNELS = ("bot_call", "markdown")


@dataclass
class History:
    dated: list[SupplierConversation]
    undated: list[SupplierConversation]
    turns: dict[str, list[ConversationTurn]]
    executive: dict
    total_dated: int
    warnings: list[str] = field(default_factory=list)


class ConversationRepository:
    def __init__(self, session: Session):
        self.s = session

    def history(self, supplier_id: int, limit: int, transcripts_for: int = 2, max_turns: int = 40) -> History:
        base = select(SupplierConversation).where(
            SupplierConversation.supplier_id == supplier_id, SupplierConversation.channel.in_(CONTENT_CHANNELS)
        )
        dated = list(
            self.s.scalars(
                base.where(SupplierConversation.ts_valid.is_(True))
                .order_by(SupplierConversation.conversation_ts.desc(), SupplierConversation.id.desc())
                .limit(limit)
            )
        )
        undated = list(self.s.scalars(base.where(SupplierConversation.ts_valid.is_(False)).order_by(SupplierConversation.id).limit(limit)))
        total_dated = self.s.scalar(
            select(func.count(SupplierConversation.id)).where(
                SupplierConversation.supplier_id == supplier_id,
                SupplierConversation.channel.in_(CONTENT_CHANNELS),
                SupplierConversation.ts_valid.is_(True),
            )
        ) or 0
        warnings = []
        if total_dated > len(dated):
            warnings.append(f"history truncated to the latest {len(dated)} of {total_dated} dated conversations")
        if undated:
            warnings.append(f"{len(undated)} conversation(s) have no reliable timestamp and are listed separately")

        # Transcripts for the most recent calls that have them, in one query.
        attempts = [c.source_record_id for c in dated if c.channel == "bot_call"]
        turns: dict[str, list[ConversationTurn]] = defaultdict(list)
        if attempts:
            with_turns = list(
                self.s.scalars(select(ConversationTurn.attempt_id).where(ConversationTurn.attempt_id.in_(attempts)).distinct())
            )
            chosen = [a for a in attempts if a in set(with_turns)][:transcripts_for]
            if chosen:
                for t in self.s.scalars(
                    select(ConversationTurn)
                    .where(ConversationTurn.attempt_id.in_(chosen), ConversationTurn.transcript_variant == 0)
                    .order_by(ConversationTurn.attempt_id, ConversationTurn.turn_no)
                ):
                    if len(turns[t.attempt_id]) < max_turns:
                        turns[t.attempt_id].append(t)
        return History(dated, undated, dict(turns), self.executive_summary(supplier_id), total_dated, warnings)

    def executive_summary(self, supplier_id: int) -> dict:
        c = SupplierConversation
        answered = case((c.outcome == "Answered", 1), else_=0)
        row = self.s.execute(
            select(
                func.count(c.id),
                func.sum(answered),
                func.max(case((c.outcome == "Answered", c.conversation_ts), else_=None)),
                func.max(c.conversation_ts),
            ).where(c.supplier_id == supplier_id, c.channel == "executive_call")
        ).one()
        modules = dict(
            self.s.execute(
                select(c.conversation_type, func.count(c.id)).where(c.supplier_id == supplier_id, c.channel == "executive_call").group_by(c.conversation_type)
            ).all()
        )
        return {
            "total_calls": row[0] or 0,
            "answered_calls": int(row[1] or 0),
            "last_answered_at": row[2].isoformat() if row[2] else None,
            "last_attempt_at": row[3].isoformat() if row[3] else None,
            "modules": modules,
            "coverage_note": "Executive call logs cover Aug-Sep 2026 only (7 and 18 Sep missing).",
        }
