"""Persona orchestration: retrieval -> rule-based spec -> LLM narrative + prompt (validated) -> fallback."""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from pathlib import Path

from sqlalchemy.orm import Session

from app.config import Settings
from app.models import Supplier, SupplierAlias, SupplierConversation
from app.schemas import (
    CallStrategy,
    Cited,
    ConversationHistoryOut,
    ConversationOut,
    HistoryInsights,
    LLMPersonaOutput,
    Metadata,
    NameOut,
    ObjectionResponse,
    PersonaNarrative,
    PersonaOut,
    PersonaRequest,
    PersonaResponse,
    Readiness,
    SupplierOut,
    TurnOut,
)
from app.services.conversation_repository import ConversationRepository, History
from app.services.evidence_service import get_evidence
from app.services.llm_client import LLMClient, LLMUnavailable
from app.services.persona_rules import _counts, build_persona
from app.services.sarvam_service import build_variables, render_prompt
from app.services.supplier_repository import SupplierRepository

log = logging.getLogger(__name__)
PROMPTS = Path(__file__).resolve().parent.parent / "prompts"

# What must never reach the customer-facing prompt.
_INTERNAL = re.compile(r"\b(glid|fk_glusr\w*|profile_json|database|eng_\w+|redis|evaluator|segment|disposition|confidence score)\b", re.I)
# Affirmative offers only; the guardrail sentence "never quotes ... guarantees" must not trip it.
_FABRICATED_OFFER = re.compile(
    r"\d+\s*%\s*(?:off|discount)|\bdiscount of\b|\bguarantee(?:d|s)?\s+(?:\d+|of\s+\d+|leads|orders|business|results|sales|returns)"
    r"|free of cost|\bcashback\b",
    re.I,
)
PROFILE_KEYS_FOR_LLM = (
    "seller_city", "seller_state", "business_type", "annual_turnover", "nature_of_business", "nature_of_business_secondary",
    "gst_registration_year", "top_category_1", "top_category_2", "top_category_3", "top_category_group", "num_categories",
)


def _load(name: str) -> str:
    return (PROMPTS / name).read_text(encoding="utf-8")


def prompt_violations(text: str) -> list[str]:
    issues = []
    if m := _INTERNAL.search(text):
        issues.append(f"internal term '{m.group(0)}'")
    if m := _FABRICATED_OFFER.search(text):
        issues.append(f"unsupported offer/guarantee '{m.group(0)}'")
    return issues


class PersonaService:
    def __init__(self, session: Session, settings: Settings, llm: LLMClient):
        self.s = session
        self.settings = settings
        self.llm = llm

    # ------------------------------------------------------------ main
    def generate(self, req: PersonaRequest) -> PersonaResponse:
        limit = min(req.history_limit or self.settings.default_history_limit, self.settings.max_history_limit)
        res = SupplierRepository(self.s).resolve(business_name=req.business_name, glid=req.glid)
        sup = res.supplier
        hist = ConversationRepository(self.s).history(sup.id, limit)
        evidence = get_evidence(self.s, wait=False)
        names = [a.business_name for a in res.names]
        spec = build_persona(sup, hist.dated, evidence, names)
        warnings = [*res.warnings, *hist.warnings]
        if not evidence.get("available"):
            warnings.append("Past-call evidence is still being computed; evidence references are omitted from this persona.")
        elif evidence.get("stale"):
            warnings.append("Past-call evidence is from the previous data load; a refresh is running.")
        if not names:
            warnings.append("No business name on record for this GLID; the agent must ask the user to confirm the business name.")
        if spec["missing_fields"]:
            warnings.append("Missing seller fields (lower-confidence choices): " + ", ".join(spec["missing_fields"]))

        narrative, insights, strategy, prompt = self._rules_output(sup, hist, spec, names, res)
        mode, model = "rules", None
        if not spec["call_eligibility"]["call_allowed"]:
            warnings.append("Do-not-call restriction recorded: LLM generation skipped and no call strategy is offered.")
        elif req.use_llm:
            try:
                out = self._llm_output(sup, res.names, hist, spec, evidence)
                narrative, insights, strategy = out.persona, out.history_insights, out.call_strategy
                mode, model = "llm", self.llm.model
                issues = prompt_violations(out.voice_agent_system_prompt)
                if issues:
                    warnings.append("LLM system prompt rejected (" + "; ".join(issues) + "); rule-based prompt used instead.")
                else:
                    prompt = out.voice_agent_system_prompt
            except LLMUnavailable as exc:
                if exc.code == "not_configured":  # switched off on purpose (LLM_ENABLED=false or no key): not an error
                    log.info("LLM not used (%s): %s", exc.code, exc.detail)
                    warnings.append("LLM not used (not_configured); persona generated from the seller's data by the rule engine.")
                else:
                    log.warning("LLM unavailable (%s): %s", exc.code, exc.detail)
                    warnings.append(f"LLM unavailable ({exc.code}); rule-based persona returned.")

        sources = sorted({c.source_file for c in hist.dated + hist.undated} | {sup.source_file})
        return PersonaResponse(
            supplier=self._supplier_out(sup, res.names),
            conversation_history=self._history_out(sup, hist),
            persona=PersonaOut(narrative=narrative, history_insights=insights, spec=spec),
            call_strategy=strategy,
            voice_agent_system_prompt=prompt,
            metadata=Metadata(
                matched_glid=sup.glid, match_method=res.match_method, sources=sources, generated_at=datetime.now(),
                generation_mode=mode, model=model, warnings=warnings,
            ),
        )

    # ------------------------------------------------------------ LLM
    def _llm_output(self, sup: Supplier, aliases: list[SupplierAlias], hist: History, spec: dict, evidence: dict) -> LLMPersonaOutput:
        p = sup.profile_json or {}
        context = {
            "seller": {
                "business_names_on_record": [a.business_name for a in aliases[:3]],
                "profile": {k: p.get(k) for k in PROFILE_KEYS_FOR_LLM if p.get(k) is not None},
                "engagement": {k: v for k, v in p.items() if k.startswith("eng_")},
                "bot_history_summary": {k: p.get(k) for k in (
                    "bot_attempts", "bot_answered", "calls_meeting_fixed", "calls_not_interested", "calls_general", "calls_call_later",
                    "avg_answered_call_sec", "past_objections", "past_questions_asked", "past_dispositions_detailed",
                    "asked_if_talking_to_bot", "showed_frustration", "already_in_touch_with_executive", "do_not_call_requested") if p.get(k) is not None},
            },
            "conversations_latest_first": [
                {"date": c.conversation_ts.strftime("%Y-%m-%d %H:%M") if c.ts_valid else None, "channel": c.channel, "outcome": c.outcome,
                 "duration_sec": c.duration_sec, "summary": c.summary or (c.raw_content or "")[:1500]}
                for c in hist.dated
            ],
            "undated_conversations": [{"summary": c.summary or (c.raw_content or "")[:1500]} for c in hist.undated],
            "recent_transcripts": {a: [f"{t.speaker}: {t.text}" for t in turns] for a, turns in hist.turns.items()},
            "executive_calls": hist.executive,
            "rule_based_persona_spec": {k: spec[k] for k in ("segment", "call_eligibility", "voice", "language", "tone", "conversation_plan", "live_adaptation")},
            "past_call_evidence": {k: evidence.get(k) for k in ("baseline", "headline", "caveats")},
        }
        user = "<seller_data>\n" + json.dumps(context, ensure_ascii=False, default=str, separators=(",", ":")) + "\n</seller_data>\nReturn the JSON object now."
        return self.llm.generate(_load("persona_prompt.txt"), user, LLMPersonaOutput)

    # ------------------------------------------------------------ rules fallback
    def _rules_output(self, sup: Supplier, hist: History, spec: dict, names: list[str], res):
        p = sup.profile_json or {}
        sig = spec["signals_from_history"]
        plan = spec["conversation_plan"]
        allowed = spec["call_eligibility"]["call_allowed"]

        enq = p.get("eng_enq_received_90d")
        mod = p.get("eng_products_modified_90d")
        if p.get("eng_answered_calls_90d") is None and enq is None:
            engagement = "unknown"
        elif (enq or 0) >= 10 or (mod or 0) >= 5:
            engagement = "high"
        elif (enq or 0) > 0 or (mod or 0) > 0 or (p.get("eng_pns_answered_90d") or 0) > 0:
            engagement = "medium"
        else:
            engagement = "low"

        ev = []
        if sig["meeting_fixed_before"]:
            ev.append(f"{int(p.get('calls_meeting_fixed') or 0)} past bot call(s) ended with a meeting fixed (seller_file.calls_meeting_fixed).")
        if sig["callback_requested_before"]:
            ev.append("Seller asked for a callback before (past dispositions / latest call outcome).")
        if sig["price_minded"]:
            ev.append("Seller asked pricing or benefit questions before (seller_file.past_questions_asked).")
        if sig["not_interested_calls"]:
            ev.append(f"{sig['not_interested_calls']} past call(s) labelled Not Interested (seller_file.calls_not_interested).")
        if sig["showed_frustration"]:
            ev.append("Seller showed frustration on a past call (seller_file.showed_frustration).")
        if not allowed:
            level = "cold"
        elif not hist.dated and not p.get("bot_answered"):
            level = "unknown"
        elif sig["showed_frustration"] or sig["not_interested_calls"] >= 3:
            level = "cold"
        elif sig["meeting_fixed_before"] and (sig["callback_requested_before"] or sig["price_minded"]):
            level = "warm"
        elif sig["callback_requested_before"] or sig["price_minded"] or sig["meeting_fixed_before"]:
            level = "lukewarm"
        else:
            level = "cold" if sig["not_interested_calls"] else "unknown"

        objections = _counts(p.get("past_objections"))
        facts = [f"Business located in {sup.city}, {sup.state}." if sup.city else None,
                 f"Main category: {sup.top_category_1}." if sup.top_category_1 else None,
                 f"Business type: {sup.business_type}; turnover band {sup.annual_turnover}." if sup.business_type and sup.annual_turnover else None,
                 f"{int(enq)} buyer enquiries received in the last 90 days." if enq else None]
        narrative = PersonaNarrative(
            profile_summary=self._profile_summary(sup, names),
            engagement_level=engagement,
            readiness=Readiness(level=level, evidence=ev, uncertainty="Rule-based estimate from recorded call outcomes only; actual intent is unknown until the seller speaks."),
            concerns=list(objections),
            confidence="medium" if p.get("bot_answered") else "low",
            verified_facts=[f for f in facts if f],
            inferences=[f"Delivery chosen for segment '{spec['segment']}' (see spec reasons)."],
            unknowns=["Package, pricing and purchase history (not in the dataset).", *(["Business name (not on record)."] if not names else [])],
        )

        commitments, unresolved, follow_ups, obj_cited = [], [], [], []
        for cat, n in objections.items():
            obj_cited.append(Cited(text=f"{cat} (raised {n}x)", source="seller_file.past_objections"))
        for c in hist.dated:
            d = c.conversation_ts.strftime("%Y-%m-%d")
            if c.outcome == "Call Later / Busy":
                commitments.append(Cited(text="Seller asked to be called back later.", source=f"bot call {d}"))
            if c.meeting_fixed:
                commitments.append(Cited(text="A meeting with a sales executive was fixed.", source=f"bot call {d}"))
                unresolved.append(Cited(text="Whether that meeting took place is not recorded.", source=f"bot call {d}"))
        latest = hist.dated[0] if hist.dated else None
        if latest and latest.outcome == "Call Later / Busy":
            follow_ups.append(Cited(text="Follow up on the callback the seller requested on the latest call.", source=f"bot call {latest.conversation_ts:%Y-%m-%d}"))
        # Patterns read from the call history itself, so sellers without coded objections still get insights.
        # Summaries are never copied here (they are untrusted text); only outcomes, dates and durations are used.
        declined = [c for c in hist.dated if c.outcome == "Not Interested"]
        if declined and not objections:
            obj_cited.append(Cited(text=f"Declined on {len(declined)} recent bot call(s); no reason was recorded.",
                                   source="bot calls " + ", ".join(f"{c.conversation_ts:%Y-%m-%d}" for c in declined[:4])))
        brief = [c for c in hist.dated if c.duration_sec is not None and c.duration_sec < 15]
        if len(brief) >= 2:
            unresolved.append(Cited(text=f"{len(brief)} recent calls ended within 15 seconds; the seller has not engaged beyond the identity check.",
                                    source="bot call durations " + ", ".join(f"{c.conversation_ts:%Y-%m-%d}" for c in brief[:4])))
        if len(hist.dated) >= 3:
            days = (hist.dated[0].conversation_ts.date() - hist.dated[-1].conversation_ts.date()).days + 1
            if days <= 7:
                follow_ups.append(Cited(text=f"{len(hist.dated)} calls within {days} day(s): risk of call fatigue; space out the next attempt and keep it to one low-pressure ask.",
                                        source="bot call dates"))
        if latest and latest.outcome == "Not Interested":
            follow_ups.append(Cited(text="The latest call ended Not Interested: open with a short, low-pressure question and accept a first clear refusal.",
                                    source=f"bot call {latest.conversation_ts:%Y-%m-%d}"))
        if asked := _counts(p.get("past_questions_asked")):
            follow_ups.append(Cited(text="Be ready to answer what the seller asked before: " + ", ".join(asked) + ".", source="seller_file.past_questions_asked"))
        if sig.get("already_in_touch") or p.get("already_in_touch_with_executive"):
            follow_ups.append(Cited(text="Seller is already in touch with an IndiaMART executive: offer a meeting with that executive.",
                                    source="seller_file.already_in_touch_with_executive"))
        insights = HistoryInsights(objections=obj_cited, commitments=commitments[:6], unresolved_issues=unresolved[:4], follow_ups=follow_ups)

        if not allowed:
            strategy = CallStrategy(objective="Do not call", opening_approach="No call: the seller asked not to be called.",
                                    talking_points=[], objection_responses=[], commitments_to_revisit=[],
                                    next_action="Keep the seller on the do-not-call list; no outbound bot call.")
            prompt = ("## Persona\nThe agent is VANI, IndiaMART's virtual assistant.\n\n## Objective\nThis seller has asked not to be called. "
                      "The call must not be placed. If a call connects by mistake, the agent apologises for the disturbance, confirms the "
                      "number will not be called again, and calls end_interaction at once without any pitch.")
            return narrative, insights, strategy, prompt

        strategy = CallStrategy(
            objective=plan["objective"],
            opening_approach=plan["opening"],
            talking_points=(plan["personalisation"] or ["Ask what the seller deals in before proposing the meeting."])
            + [f"Suggested package: {plan['package_recommendation']['package']} ({plan['package_recommendation']['price']}); "
               f"{plan['package_recommendation']['reason']}"],
            objection_responses=[ObjectionResponse(objection=o["objection"], response=o["approach"]) for o in plan["objection_playbook"]],
            commitments_to_revisit=[c.text + f" ({c.source})" for c in commitments[:3]],
            next_action=(f"Call during {', '.join(h + ':00' for h in plan['best_call_hours'])} hours where possible; "
                         if plan["best_call_hours"] else "") + "Log the outcome and any mid-call switches.",
        )
        # Same prompt as the live Sarvam agent, filled with this seller's variables.
        return narrative, insights, strategy, render_prompt(build_variables(self.s, res)[0])

    @staticmethod
    def _profile_summary(sup: Supplier, names: list[str]) -> str:
        who = names[0] if names else "Seller (name not on record)"
        parts = [who, f"a {sup.nature_of_business}" if sup.nature_of_business else None,
                 f"in {sup.city}, {sup.state}" if sup.city else None,
                 f"dealing in {sup.top_category_1}" if sup.top_category_1 else None,
                 f"({sup.business_type}, turnover {sup.annual_turnover})" if sup.business_type and sup.annual_turnover else None]
        return " ".join(x for x in parts if x) + "."

    # ------------------------------------------------------------ response shaping
    @staticmethod
    def _supplier_out(sup: Supplier, aliases: list[SupplierAlias]) -> SupplierOut:
        p = sup.profile_json or {}
        return SupplierOut(
            glid=sup.glid,
            business_names=[NameOut(name=a.business_name, source=a.source, occurrences=a.occurrences) for a in aliases[:5]],
            city=sup.city, state=sup.state, business_type=sup.business_type, annual_turnover=sup.annual_turnover,
            nature_of_business=sup.nature_of_business,
            top_categories=[p[k] for k in ("top_category_1", "top_category_2", "top_category_3") if p.get(k)],
            category_group=sup.top_category_group,
            gst_registration_year=str(p["gst_registration_year"]) if p.get("gst_registration_year") is not None else None,
            do_not_call=bool(sup.do_not_call),
        )

    @staticmethod
    def _conv_out(c: SupplierConversation, turns=None) -> ConversationOut:
        return ConversationOut(
            timestamp=c.conversation_ts if c.ts_valid else None, timestamp_valid=c.ts_valid, channel=c.channel,
            conversation_type=c.conversation_type, outcome=c.outcome, meeting_fixed=c.meeting_fixed, duration_sec=c.duration_sec,
            summary=c.summary or ((c.raw_content or "")[:2000] or None), source_file=c.source_file, source_record_id=c.source_record_id,
            transcript=[TurnOut(turn_no=t.turn_no, speaker=t.speaker, start_sec=t.start_sec, text=t.text) for t in turns] if turns else None,
        )

    def _history_out(self, sup: Supplier, hist: History) -> ConversationHistoryOut:
        p = sup.profile_json or {}
        dated = [self._conv_out(c, hist.turns.get(c.source_record_id)) for c in hist.dated]
        return ConversationHistoryOut(
            latest=dated[0] if dated else None,
            dated=dated,
            undated=[self._conv_out(c) for c in hist.undated],
            total_dated=hist.total_dated,
            executive_calls=hist.executive,
            bot_call_stats={k: p.get(k) for k in ("bot_attempts", "bot_answered", "bot_answer_rate", "calls_meeting_fixed",
                                                   "calls_not_interested", "calls_general", "calls_call_later", "avg_answered_call_sec")},
            past_objections=_counts(p.get("past_objections")),
            past_questions_asked=_counts(p.get("past_questions_asked")),
            past_dispositions=_counts(p.get("past_dispositions_detailed")),
        )
