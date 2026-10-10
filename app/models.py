"""ORM tables. CSV/Markdown files are the source of truth; these tables are the indexed retrieval layer.

Mapped from the real PS07 dataset:
- suppliers              <- ps07_seller_files_part*.csv (one row per seller, keyed by fk_glusr_usr_id = GLID)
- supplier_aliases       <- business names IndiaMART used on past bot calls (the seller file has no name column)
- supplier_conversations <- ps07_bot_calls_*.csv / ps07_recording_sample_5000.csv (bot), ps07_executive_calls.csv
                            (executive), and optional Markdown transcripts in CONVERSATION_DATA_DIR
- conversation_turns     <- ps07_call_turns.csv
No package/pricing source exists in the dataset, so there is no package table.
"""

from datetime import datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class Supplier(Base):
    __tablename__ = "suppliers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    glid: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    city: Mapped[str | None] = mapped_column(String(120))
    state: Mapped[str | None] = mapped_column(String(80))
    business_type: Mapped[str | None] = mapped_column(String(60))
    annual_turnover: Mapped[str | None] = mapped_column(String(40))
    nature_of_business: Mapped[str | None] = mapped_column(String(80))
    top_category_1: Mapped[str | None] = mapped_column(String(200))
    top_category_group: Mapped[str | None] = mapped_column(String(200))
    do_not_call: Mapped[bool] = mapped_column(Boolean, default=False)
    # Every source column, parsed, so nothing is lost when the schema is narrower than the file.
    profile_json: Mapped[dict] = mapped_column(JSON)
    row_hash: Mapped[str] = mapped_column(String(64))
    source_file: Mapped[str] = mapped_column(String(300))
    ingested_at: Mapped[datetime] = mapped_column(DateTime)


class SupplierAlias(Base):
    """A business name tied to a GLID, with where it came from and how often it was seen."""

    __tablename__ = "supplier_aliases"
    __table_args__ = (UniqueConstraint("supplier_id", "normalized_name", "source"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    supplier_id: Mapped[int] = mapped_column(ForeignKey("suppliers.id", ondelete="CASCADE"), index=True)
    business_name: Mapped[str] = mapped_column(String(200), index=True)
    normalized_name: Mapped[str] = mapped_column(String(200), index=True)
    source: Mapped[str] = mapped_column(String(40))  # bot_call_summary | bot_turn_greeting | markdown
    occurrences: Mapped[int] = mapped_column(Integer, default=1)
    first_source_record: Mapped[str | None] = mapped_column(String(80))


class SupplierConversation(Base):
    __tablename__ = "supplier_conversations"
    __table_args__ = (
        UniqueConstraint("channel", "source_record_id"),
        Index("ix_conv_supplier_ts", "supplier_id", "conversation_ts"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # Null when the record could not be confidently tied to a supplier (link_status = needs_review).
    supplier_id: Mapped[int | None] = mapped_column(ForeignKey("suppliers.id", ondelete="SET NULL"))
    glid: Mapped[int | None] = mapped_column(BigInteger, index=True)
    link_status: Mapped[str] = mapped_column(String(20), default="linked")  # linked | needs_review
    channel: Mapped[str] = mapped_column(String(20))  # bot_call | executive_call | markdown
    source_record_id: Mapped[str] = mapped_column(String(120))
    conversation_ts: Mapped[datetime | None] = mapped_column(DateTime)
    ts_valid: Mapped[bool] = mapped_column(Boolean, default=False)
    conversation_type: Mapped[str | None] = mapped_column(String(40))  # bot version / exec module
    outcome: Mapped[str | None] = mapped_column(String(60))
    meeting_fixed: Mapped[bool | None] = mapped_column(Boolean)
    duration_sec: Mapped[float | None] = mapped_column(Float)
    summary: Mapped[str | None] = mapped_column(Text)
    raw_content: Mapped[str | None] = mapped_column(Text)  # Markdown body only; bot transcripts live in turns
    details_json: Mapped[dict] = mapped_column(JSON, default=dict)
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    source_file: Mapped[str] = mapped_column(String(300))
    ingested_at: Mapped[datetime] = mapped_column(DateTime)


class ConversationTurn(Base):
    __tablename__ = "conversation_turns"
    # Some calls were transcribed twice (two evaluator runs); each run is kept as its own variant.
    __table_args__ = (UniqueConstraint("attempt_id", "transcript_variant", "turn_no"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    attempt_id: Mapped[str] = mapped_column(String(40), index=True)
    transcript_variant: Mapped[int] = mapped_column(Integer, default=0)
    glid: Mapped[int | None] = mapped_column(BigInteger, index=True)
    turn_no: Mapped[int] = mapped_column(Integer)
    speaker: Mapped[str] = mapped_column(String(10))
    start_sec: Mapped[float | None] = mapped_column(Float)
    end_sec: Mapped[float | None] = mapped_column(Float)
    text: Mapped[str | None] = mapped_column(Text)
    row_hash: Mapped[str] = mapped_column(String(64))


class IngestionFile(Base):
    __tablename__ = "ingestion_files"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    file_path: Mapped[str] = mapped_column(String(400), unique=True)
    file_type: Mapped[str] = mapped_column(String(30))
    content_hash: Mapped[str | None] = mapped_column(String(64), index=True)
    last_ingested_at: Mapped[datetime | None] = mapped_column(DateTime)
    status: Mapped[str] = mapped_column(String(20))  # success | partial | failed | skipped_unchanged | unrecognized
    imported: Mapped[int] = mapped_column(Integer, default=0)
    updated: Mapped[int] = mapped_column(Integer, default=0)
    skipped: Mapped[int] = mapped_column(Integer, default=0)
    failed: Mapped[int] = mapped_column(Integer, default=0)
    error_summary: Mapped[str | None] = mapped_column(Text)


class CallSession(Base):
    """One row per pre-call lookup: the call_ref that ties the switch log and outcome to this call,
    and the persona variables the agent actually received."""

    __tablename__ = "call_sessions"
    __table_args__ = (Index("ix_session_glid_ts", "glid", "created_at"),)

    call_ref: Mapped[str] = mapped_column(String(40), primary_key=True)
    glid: Mapped[int | None] = mapped_column(BigInteger)
    found: Mapped[bool] = mapped_column(Boolean)
    segment: Mapped[str | None] = mapped_column(String(60))
    variables_json: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime)


class CallOutcome(Base):
    """Post-call record sent by the Sarvam on_end tool. persona_switches is the platform's post-call
    extraction, kept verbatim: it completes the live switch log when a mid-call log call was missed."""

    __tablename__ = "call_outcomes"
    __table_args__ = (Index("ix_outcome_glid_ts", "glid", "created_at"), Index("ix_outcome_call_ref", "call_ref"))

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    glid: Mapped[int | None] = mapped_column(BigInteger)
    call_ref: Mapped[str | None] = mapped_column(String(120))
    call_outcome: Mapped[str | None] = mapped_column(String(40))
    meeting_time: Mapped[str | None] = mapped_column(String(120))
    callback_time: Mapped[str | None] = mapped_column(String(120))
    objections_raised: Mapped[str | None] = mapped_column(Text)
    persona_switches: Mapped[str | None] = mapped_column(Text)
    call_summary: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime)


class PersonaSwitchLog(Base):
    """Mid-call persona switches reported by the Sarvam agent (signal detected, change made, reason)."""

    __tablename__ = "persona_switch_log"
    __table_args__ = (Index("ix_switch_glid_ts", "glid", "created_at"), Index("ix_switch_call_ref", "call_ref"))

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    glid: Mapped[int | None] = mapped_column(BigInteger)
    call_ref: Mapped[str | None] = mapped_column(String(120))
    signal: Mapped[str] = mapped_column(String(30))
    change_made: Mapped[str] = mapped_column(Text)
    reason: Mapped[str] = mapped_column(Text)
    seller_quote: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime)
