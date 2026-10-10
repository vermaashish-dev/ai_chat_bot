"""Deterministic persona generator (PS04 §3.2-3.5).

Turns one seller's verified data into a persona spec: voice, language, tone, conversation plan and live
adaptation rules. Every setting carries the reason, the seller fields it used, the past-call evidence it
rests on, and a confidence. Personas change delivery only, never facts, pricing or compliance content.
This runs without an LLM so the Sarvam pre-call tool stays fast and works when the LLM is down.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from app.models import Supplier, SupplierConversation
from app.services.packages import recommend

# Opening language by state. Past VANI calls ran in Hindi/Hinglish nationwide; regional languages are
# offered on switch rather than forced, because the data has no regional-language outcome evidence.
STATE_LANGUAGE = {
    "Gujarat": "Gujarati", "Maharashtra": "Marathi", "Goa": "Marathi", "West Bengal": "Bengali", "Tripura": "Bengali",
    "Tamil Nadu": "Tamil", "Puducherry": "Tamil", "Karnataka": "Kannada", "Kerala": "Malayalam", "Telangana": "Telugu",
    "Andhra Pradesh": "Telugu", "Punjab": "Punjabi", "Odisha": "Odia", "Assam": "Bengali",
}
HINDI_BELT = {
    "Uttar Pradesh", "Delhi", "Haryana", "Rajasthan", "Madhya Pradesh", "Bihar", "Jharkhand", "Uttarakhand",
    "Himachal Pradesh", "Chhattisgarh", "Chandigarh", "Jammu & Kashmir", "Jammu and Kashmir",
}
OPEN_IN_ENGLISH = {"Tamil Nadu", "Kerala", "Karnataka", "Puducherry"}  # Hindi is weakest here
HIGH_TURNOVER = {"5 - 25 Cr", "25 - 100 Cr", "100 - 500 Cr", "> 500 Cr"}
MID_TURNOVER = {"40 L - 1.5 Cr", "1.5 - 5 Cr"}
SARVAM_LANG_CODES = {
    "Hindi": "hi-IN", "English": "en-IN", "Gujarati": "gu-IN", "Marathi": "mr-IN", "Bengali": "bn-IN", "Tamil": "ta-IN",
    "Kannada": "kn-IN", "Malayalam": "ml-IN", "Telugu": "te-IN", "Punjabi": "pa-IN", "Odia": "od-IN",
}

OBJECTION_PLAYBOOK = {
    "Seller Unavailability & Scheduling Constraints": "Offer two concrete slots for a 15 to 20 minute meeting and accept the seller's own time. If no slot works, take a callback time.",
    "Callback / Deferred Engagement (Neutral)": "Acknowledge the delay, then ask for one specific day and time instead of a vague later.",
    "Call Quality / Technical Failures": "Keep turns very short, confirm the seller can hear, and offer a callback at once if the line stays poor.",
    "No Seller Response / Engagement Drop": "Lead with the seller's own business detail in the first turn, then make one direct ask.",
    "Explicit Disinterest / Refusal": "Accept the first clear refusal politely; ask once what would make it useful, then close without pressure.",
    "Call Handling / Executive Gaps": "Acknowledge the earlier experience without blaming anyone and offer that the meeting is with a sales executive who will follow up properly.",
    "Existing Engagement / Past Experience": "Acknowledge the existing contact, ask whether the seller wants the meeting with the same executive, and do not repeat the pitch.",
    "Wrong Contact / Business Not Applicable": "Confirm the business name first; if wrong, apologise, ask for the right contact once, and close.",
    "Not Ready / Needs Time / Early-Stage Seller": "Position the meeting as guidance only, with no commitment, and offer a later date the seller picks.",
    "Product / Service / Business Mismatch": "Ask which products the seller actually deals in and tie the meeting to those categories.",
    "Information / Trust / Privacy Concerns": "Explain clearly the call is from IndiaMART about the seller's own listing, share nothing sensitive, and offer the official helpline.",
}


@dataclass
class Choice:
    value: dict
    reason: str
    data_used: list[str]
    evidence: list[str]
    confidence: str  # high | medium | low

    def as_dict(self) -> dict:
        return {**self.value, "reason": self.reason, "data_used": self.data_used, "evidence": self.evidence, "confidence": self.confidence}


def _num(p: dict, key: str) -> float | None:
    v = p.get(key)
    return float(v) if isinstance(v, (int, float)) else None


def _flag(p: dict, key: str) -> bool:
    return _num(p, key) == 1


def _counts(text: str | None) -> dict[str, int]:
    """'A (2); B (1)' -> {'A': 2, 'B': 1}"""
    out: dict[str, int] = {}
    for part in (text or "").split(";"):
        part = part.strip()
        if not part:
            continue
        name, _, n = part.rpartition("(")
        try:
            out[name.strip()] = int(n.rstrip(") "))
        except ValueError:
            out[part] = 1
    return out


def _trait(evidence: dict, name: str) -> dict | None:
    return next((t for t in evidence.get("seller_level", {}).get("traits", []) if t["trait"] == name), None)


def _ev_trait(evidence: dict, name: str) -> list[str]:
    t = _trait(evidence, name)
    if not t:
        return []
    return [f"Sellers with {name.replace('objection:', 'objection ')} (n={t['sellers']}): {t['meeting_share_pct']}% meeting share vs "
            f"{evidence['seller_level']['baseline_meeting_share_pct']}% baseline ({t['strength']} evidence)."]


def _ev_signal(evidence: dict, signal: str) -> str | None:
    s = evidence.get("signal_response", {}).get(signal)
    if not s or s.get("ask_vs_no_ask_pp") is None:
        return None
    a, b = s["next_turn_keeps_ask"], s["next_turn_no_ask"]
    return (f"After a {signal} signal, calls whose next bot turn kept a clear question fixed {a['meeting_fixed_pct']}% meetings (n={a['n']}) "
            f"vs {b['meeting_fixed_pct']}% without one (n={b['n']}); {s['strength']} evidence.")


def _ev_group(evidence: dict, key: str, group: str | None) -> list[str]:
    g = next((x for x in evidence.get(key, []) if x["group"] == group), None)
    if not g:
        return []
    return [f"{key.replace('by_', '').replace('_', ' ').title()} '{group}' (n={g['n']} calls): {g['meeting_fixed_pct']}% meetings vs "
            f"{evidence['baseline']['meeting_fixed_pct']}% baseline ({g['strength']} evidence)."]


def _best_hours(evidence: dict) -> list[str]:
    hours = sorted(evidence.get("by_hour", []), key=lambda g: -g["meeting_fixed_pct"])
    return [g["group"] for g in hours[:4]]


def build_persona(sup: Supplier, dated: list[SupplierConversation], evidence: dict, names: list[str]) -> dict:
    p = sup.profile_json or {}
    ev_ok = evidence.get("available", False)
    objections = _counts(p.get("past_objections"))
    questions = _counts(p.get("past_questions_asked"))
    dispositions = _counts(p.get("past_dispositions_detailed"))
    avg_sec = _num(p, "avg_answered_call_sec")
    answered = _num(p, "bot_answered") or 0
    frustrated = _flag(p, "showed_frustration")
    asked_bot = _flag(p, "asked_if_talking_to_bot")
    in_touch = _flag(p, "already_in_touch_with_executive")
    dnc = _flag(p, "do_not_call_requested") or sup.do_not_call
    has_bot_history = p.get("showed_frustration") is not None
    has_engagement = p.get("eng_answered_calls_90d") is not None
    turnover, btype, state = sup.annual_turnover, sup.business_type, sup.state
    rushed = (avg_sec is not None and avg_sec < 20 and answered >= 1) or "Seller Unavailability & Scheduling Constraints" in objections
    confused = any(q in questions for q in ("Clarification", "Identity Confirmation", "Purpose Reason"))
    price_minded = "Pricing Payment" in questions or "Benefit Value" in questions
    ni_count = int(_num(p, "calls_not_interested") or 0)
    callback_call = next((c for c in dated if c.outcome == "Call Later / Busy" and c.ts_valid), None)
    callback_hist = "callback_requested" in dispositions or callback_call is not None
    met_before = int(_num(p, "calls_meeting_fixed") or 0) > 0
    # Same rule as the 'Fatigued / sceptical' segment: past frustration or 3+ not-interested calls.
    low_tolerance = frustrated or ni_count >= 3

    # ---------------- eligibility
    eligibility_reasons = []
    if dnc:
        eligibility_reasons.append("Seller asked not to be called (do_not_call_requested = 1). Do not place the call.")
    eligibility = {"call_allowed": not dnc, "reasons": eligibility_reasons or ["No recorded contact restriction."]}

    # ---------------- segment (explains distinctness)
    if dnc:
        segment = "Do-not-call"
    elif in_touch:
        segment = "Already with an executive"
    elif frustrated or ni_count >= 3:
        segment = "Fatigued / sceptical"
    elif rushed:
        segment = "Time-pressed"
    elif price_minded:
        segment = "Value-seeking"
    elif callback_hist:
        segment = "Warm callback"
    elif turnover in HIGH_TURNOVER or btype == "Limited Company":
        segment = "Established business"
    elif not has_bot_history and not has_engagement:
        segment = "New / little history"
    else:
        segment = "Small trader, standard"

    # ---------------- language
    regional = STATE_LANGUAGE.get(state or "")
    if state in OPEN_IN_ENGLISH:
        primary, secondary = "English", regional
        lang_reason = f"Seller is in {state}; Hindi is least familiar there, so the call opens in simple English and switches to {regional} if the seller does."
        lang_conf = "medium"
    elif state in HINDI_BELT or regional is None:
        primary, secondary = "Hindi", "English"
        lang_reason = f"Seller is in {state or 'an unknown state'}; past VANI calls in this region ran in Hindi/Hinglish."
        lang_conf = "high" if state in HINDI_BELT else "low"
    else:
        primary, secondary = "Hindi", regional
        lang_reason = f"Seller is in {state}; past VANI calls here ran in Hindi, so the call opens in Hindi and is ready to switch to {regional}."
        lang_conf = "medium"
    if turnover in HIGH_TURNOVER or btype == "Limited Company":
        mix, formality = "high (English business terms, Hindi grammar)", "formal: aap, Sir or Madam, complete sentences"
        mix_reason = f"{btype or 'Business'} with turnover {turnover or 'unknown'}: larger firms use more English business vocabulary."
    elif turnover in MID_TURNOVER:
        mix, formality = "medium (common English words such as order, enquiry, buyer)", "respectful: aap and ji, no slang"
        mix_reason = f"Mid-size {btype or 'business'} (turnover {turnover}): everyday Hinglish with common trade words."
    else:
        mix, formality = "low (simple Hindi, only very common English words)", "warm-respectful: aap and ji, short simple sentences"
        mix_reason = f"Small {btype or 'business'} (turnover {turnover or 'not given'}): simple Hindi keeps the call easy to follow."
    lang_data = ["seller_state", "annual_turnover", "business_type"]
    language = Choice(
        {"primary_language": primary, "switch_ready_language": secondary, "sarvam_language_code": SARVAM_LANG_CODES.get(primary, "hi-IN"),
         "hinglish_mix": mix, "formality": formality},
        f"{lang_reason} {mix_reason}",
        lang_data,
        _ev_group(evidence, "by_state", state) + _ev_group(evidence, "by_turnover", turnover) if ev_ok else [],
        "low" if not turnover or not state else lang_conf,
    )

    # ---------------- voice
    if frustrated:
        pace, pace_val, pitch = "slow and calm", 1.0, "slightly lower, steady"
        pace_reason = "Seller showed frustration on a past call; a calm, unhurried delivery lowers tension."
        pace_ev = _ev_trait(evidence, "showed_frustration")
    elif rushed:
        pace, pace_val, pitch = "brisk", 1.2, "neutral"
        pace_reason = (f"Seller's answered calls average {avg_sec:.0f}s" if avg_sec is not None else "Seller raised scheduling objections") + "; get to the point quickly."
        pace_ev = _ev_trait(evidence, "short_calls_avg_under_20s") + _ev_trait(evidence, "objection:Seller Unavailability & Scheduling Constraints")
    elif confused:
        pace, pace_val, pitch = "measured", 1.05, "neutral, clear"
        pace_reason = "Seller previously asked clarification or identity questions; a measured pace with clear wording helps."
        pace_ev = []
    else:
        pace, pace_val, pitch = "natural conversational", 1.1, "neutral"
        pace_reason = "No pace-related signal in the seller's history; use the standard VANI pace."
        pace_ev = []
    accent = "neutral Indian English" if primary == "English" else (f"Hindi with a light {regional} familiarity" if regional else "neutral North Indian Hindi")
    voice = Choice(
        {"gender": "female", "accent": accent, "pitch": pitch, "pace": pace, "sarvam_pace": pace_val},
        f"{pace_reason} Gender stays female to match the female voice configured on the Sarvam VANI agent: the data has no gender-versus-outcome evidence, so this is a continuity choice.",
        ["avg_answered_call_sec", "past_objections", "past_questions_asked", "showed_frustration"],
        pace_ev if ev_ok else [],
        "medium" if has_bot_history else "low",
    )

    # ---------------- tone
    warmth = "high"
    empathy = "high" if (frustrated or ni_count >= 2 or "Call Handling / Executive Gaps" in objections) else "medium"
    assertive = "low" if (frustrated or ni_count >= 3 or "Explicit Disinterest / Refusal" in objections) else ("medium-high" if (callback_hist or price_minded or met_before) else "medium")
    tone_bits = []
    if frustrated:
        tone_bits.append("past frustration means apologising for the disturbance first")
    if ni_count >= 2:
        tone_bits.append(f"{ni_count} past not-interested calls mean a low-pressure ask")
    if callback_hist:
        tone_bits.append("an earlier callback request allows a confident, direct follow-up")
    if price_minded:
        tone_bits.append("past pricing/benefit questions call for a value-first framing")
    tone = Choice(
        {"warmth": warmth, "empathy": empathy, "assertiveness": assertive},
        ("Tone set because " + "; ".join(tone_bits) + ".") if tone_bits else "No strong history signal; default warm and polite VANI tone.",
        ["showed_frustration", "calls_not_interested", "past_dispositions_detailed", "past_questions_asked"],
        (_ev_trait(evidence, "callback_requested_before") + _ev_trait(evidence, "asked_pricing_question")) if ev_ok else [],
        "medium" if has_bot_history else "low",
    )

    # ---------------- conversation plan
    name = names[0] if names else None
    category = sup.top_category_1 or sup.top_category_group
    enq_90 = _num(p, "eng_enq_received_90d")
    personalisation = []
    if name:
        personalisation.append(f"Confirm the business name {name} before anything else.")
    if sup.city and category:
        personalisation.append(f"Refer to the seller's listing in {category} in {sup.city}.")
    if enq_90 and enq_90 > 0:
        personalisation.append(f"The seller received {int(enq_90)} buyer enquiries on IndiaMART in the last 90 days (verified engagement data).")
    if p.get("nature_of_business"):
        personalisation.append(f"Speak to them as a {p['nature_of_business']}.")

    if in_touch:
        opening = "Confirm identity, acknowledge that the seller is already in touch with an IndiaMART executive, and ask whether a short meeting with that executive would help."
        objective = "Confirm or schedule a follow-up with the existing executive; do not re-pitch."
    elif callback_call is not None:
        opening = f"Confirm identity, then remind the seller that on the call of {callback_call.conversation_ts:%d %b %Y} they asked to be called back, and ask for two minutes now."
        objective = "Fix a 15 to 20 minute meeting with a sales executive; fallback: a specific callback time."
    elif callback_hist:
        opening = "Confirm identity, mention that the seller asked for a callback on an earlier call, and ask for two minutes now."
        objective = "Fix a 15 to 20 minute meeting with a sales executive; fallback: a specific callback time."
    elif met_before:
        opening = "Confirm identity and mention that a meeting with an IndiaMART executive was arranged earlier; ask how it went before proposing a fresh meeting."
        objective = "Re-engage after the earlier meeting and fix a follow-up meeting; fallback: a callback time."
    else:
        # The meeting ask belongs to the flow (two concrete times), not to the opening.
        opening = "Confirm identity, say the call is from IndiaMART about the seller's listing, and ask how buyer enquiries are going for them."
        objective = "Fix a 15 to 20 minute meeting with a sales executive; fallback: a specific callback time."

    playbook = []
    for cat, count in sorted(objections.items(), key=lambda kv: -kv[1]):
        if cat in OBJECTION_PLAYBOOK:
            playbook.append({"objection": cat, "times_raised_before": count, "approach": OBJECTION_PLAYBOOK[cat],
                             "evidence": _ev_trait(evidence, f"objection:{cat}") if ev_ok else [], "source": "seller_file.past_objections"})
    for generic in ("Seller Unavailability & Scheduling Constraints", "Explicit Disinterest / Refusal", "Information / Trust / Privacy Concerns"):
        if generic not in objections:
            playbook.append({"objection": generic, "times_raised_before": 0, "approach": OBJECTION_PLAYBOOK[generic], "evidence": [], "source": "default"})
    playbook.append({"objection": "Price question", "times_raised_before": questions.get("Pricing Payment", 0),
                     "approach": "Name the suggested package and its price exactly as listed by IndiaMART, say GST and final price are confirmed by the sales executive, then return to the meeting; never offer a discount.",
                     "evidence": [], "source": "IndiaMART paid services page (listed prices)"})

    escalation = [
        "Seller asks not to be called again: apologise once, confirm the request is noted, and end the call immediately.",
        "Second sign of frustration: stop pitching, apologise, offer a callback at a time the seller picks, and close.",
        "Seller asks for a human: say a sales executive will call back and close politely.",
        "Complaint about IndiaMART service: acknowledge, give the official customer care route, and close; do not argue.",
        "Wrong person answers: ask whether the named business can be reached on this number, take no business details, and close.",
    ]
    if low_tolerance:
        escalation.insert(1, "This seller was frustrated or uninterested before: treat the first sign of irritation as the limit.")
    guardrails = [
        "Personas change delivery only. Facts, pricing and compliance statements stay the same for every seller.",
        "Quote only package prices and buylead counts exactly as listed on IndiaMART's paid services page; never offer discounts, guarantees or results.",
        "Disclose being IndiaMART's virtual assistant when asked; never claim to be human.",
        "Never mention internal data, scores or that the seller was profiled.",
    ]
    plan = {
        "objective": objective,
        "opening": opening,
        "personalisation": personalisation,
        "objection_playbook": playbook,
        "escalation_rules": escalation,
        "guardrails": guardrails,
        "best_call_hours": _best_hours(evidence) if ev_ok else [],
        "package_recommendation": recommend(turnover, enq_90),
    }

    # ---------------- live adaptation
    adaptation = [
        {"signal": "frustration", "change": "Lower pace, apologise briefly, drop the pitch to one sentence, offer a callback time.",
         "threshold": "first sign" if low_tolerance else "second sign"},
        {"signal": "confusion", "change": "Slow down, restate who is calling and why in one simple sentence with no English jargon, then re-ask."},
        {"signal": "rush", "change": "Skip the explanation and give one short concrete ask: a meeting time or a callback time."},
        {"signal": "interest", "change": "Warm up, answer the question in one line, and immediately propose two meeting slots."},
        {"signal": "language_switch", "change": f"Switch to the seller's language (likely {secondary}) and keep the same plan."},
        {"signal": "bot_suspicion", "change": "Confirm being IndiaMART's virtual assistant in one line, then continue with the pending question."},
    ]
    for a in adaptation:
        e = _ev_signal(evidence, a["signal"]) if ev_ok else None
        a["evidence"] = [e] if e else []
        a["rule"] = "Every adapted turn still ends with one clear question."

    missing = [f for f in ("annual_turnover", "business_type", "nature_of_business", "top_category_1") if p.get(f) is None]
    if not has_bot_history:
        missing.append("bot outcome flags (no evaluator history)")
    if not has_engagement:
        missing.append("engagement history (eng_*)")

    return {
        "segment": segment,
        "call_eligibility": eligibility,
        "voice": voice.as_dict(),
        "language": language.as_dict(),
        "tone": tone.as_dict(),
        "conversation_plan": plan,
        "live_adaptation": adaptation,
        "signals_from_history": {
            "rushed": rushed, "confused_before": confused, "price_minded": price_minded, "showed_frustration": frustrated,
            "asked_if_bot": asked_bot, "already_in_touch_with_executive": in_touch, "callback_requested_before": callback_hist,
            "meeting_fixed_before": met_before, "not_interested_calls": ni_count,
            "callback_requested_on": callback_call.conversation_ts.strftime("%Y-%m-%d") if callback_call else None,
        },
        "missing_fields": missing,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }
