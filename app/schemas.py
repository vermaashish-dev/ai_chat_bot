"""Request/response models for the API and for validating LLM output."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator


# ---------------------------------------------------------------- requests

class PersonaRequest(BaseModel):
    business_name: str | None = Field(None, min_length=2, max_length=200, examples=["Wide Digital Systems"])
    glid: int | None = Field(None, gt=0, description="Seller GLID (fk_glusr_usr_id). Takes precedence over business_name.")
    history_limit: int | None = Field(None, ge=1, le=50)
    use_llm: bool = Field(True, description="False skips the LLM and returns the rule-based persona only.")

    @model_validator(mode="after")
    def need_identifier(self):
        if self.glid is None and not (self.business_name and self.business_name.strip()):
            raise ValueError("provide business_name or glid")
        return self


class SarvamPreCallRequest(BaseModel):
    glid: str | int | None = None
    business_name: str | None = Field(None, max_length=200)


class SwitchLogRequest(BaseModel):
    glid: str | int | None = None
    call_ref: str | None = Field(None, max_length=120)
    signal: Literal["frustration", "confusion", "interest", "language_switch", "rush", "bot_suspicion", "opt_out", "other"]
    change_made: str = Field(..., min_length=3, max_length=1000)
    reason: str = Field(..., min_length=3, max_length=1000)
    seller_quote: str | None = Field(None, max_length=500)


class PostCallRequest(BaseModel):
    glid: str | int | None = None
    call_ref: str | None = Field(None, max_length=120)
    call_outcome: str | None = Field(None, max_length=40)
    meeting_time: str | None = Field(None, max_length=120)
    callback_time: str | None = Field(None, max_length=120)
    objections_raised: str | None = Field(None, max_length=2000)
    persona_switches: str | None = Field(None, max_length=4000)
    call_summary: str | None = Field(None, max_length=2000)


class DialCheckRequest(BaseModel):
    glids: list[int] = Field(..., min_length=1, max_length=50000)


class SignalDetectRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=2000)


# ---------------------------------------------------------------- LLM output (validated)

class Cited(BaseModel):
    text: str
    source: str


class Readiness(BaseModel):
    level: Literal["cold", "lukewarm", "warm", "hot", "unknown"]
    evidence: list[str] = []
    uncertainty: str


class PersonaNarrative(BaseModel):
    profile_summary: str
    engagement_level: Literal["high", "medium", "low", "unknown"]
    readiness: Readiness
    concerns: list[str] = []
    confidence: Literal["high", "medium", "low"]
    verified_facts: list[str] = []
    inferences: list[str] = []
    unknowns: list[str] = []


class HistoryInsights(BaseModel):
    objections: list[Cited] = []
    commitments: list[Cited] = []
    unresolved_issues: list[Cited] = []
    follow_ups: list[Cited] = []


class ObjectionResponse(BaseModel):
    objection: str
    response: str


class CallStrategy(BaseModel):
    objective: str
    opening_approach: str
    talking_points: list[str] = []
    objection_responses: list[ObjectionResponse] = []
    commitments_to_revisit: list[str] = []
    next_action: str


class LLMPersonaOutput(BaseModel):
    persona: PersonaNarrative
    history_insights: HistoryInsights
    call_strategy: CallStrategy
    voice_agent_system_prompt: str = Field(..., min_length=400)


# ---------------------------------------------------------------- responses

class NameOut(BaseModel):
    name: str
    source: str
    occurrences: int


class SupplierOut(BaseModel):
    glid: int
    business_names: list[NameOut]
    city: str | None
    state: str | None
    business_type: str | None
    annual_turnover: str | None
    nature_of_business: str | None
    top_categories: list[str]
    category_group: str | None
    gst_registration_year: str | None
    do_not_call: bool


class TurnOut(BaseModel):
    turn_no: int
    speaker: str
    start_sec: float | None
    text: str | None


class ConversationOut(BaseModel):
    timestamp: datetime | None
    timestamp_valid: bool
    channel: str
    conversation_type: str | None
    outcome: str | None
    meeting_fixed: bool | None
    duration_sec: float | None
    summary: str | None
    source_file: str
    source_record_id: str
    transcript: list[TurnOut] | None = None


class ConversationHistoryOut(BaseModel):
    latest: ConversationOut | None
    dated: list[ConversationOut]
    undated: list[ConversationOut]
    total_dated: int
    executive_calls: dict
    bot_call_stats: dict
    past_objections: dict[str, int]
    past_questions_asked: dict[str, int]
    past_dispositions: dict[str, int]


class PersonaOut(BaseModel):
    narrative: PersonaNarrative
    history_insights: HistoryInsights
    spec: dict = Field(description="Rule-based persona spec: voice, language, tone, plan, live adaptation; each with reason and evidence.")


class Metadata(BaseModel):
    matched_glid: int
    match_method: str
    sources: list[str]
    generated_at: datetime
    generation_mode: Literal["llm", "rules"]
    model: str | None
    warnings: list[str]


class PersonaResponse(BaseModel):
    supplier: SupplierOut
    conversation_history: ConversationHistoryOut
    persona: PersonaOut
    call_strategy: CallStrategy
    voice_agent_system_prompt: str
    metadata: Metadata


class ErrorBody(BaseModel):
    code: str
    message: str
    details: dict | list | None = None


class ErrorResponse(BaseModel):
    error: ErrorBody
