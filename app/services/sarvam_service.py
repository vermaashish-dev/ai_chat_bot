"""Sarvam voice-agent integration.

The agent's on_start API tool posts {glid} here before the first turn and saves the returned
`variables.*` fields into agent variables of the same name. The prompt in the Sarvam agent then reads
them as {{name}}. Values are short plain strings: the platform stores variables as text and the LLM
reads them every turn, so long values cost latency. Rule-based only, so it answers in milliseconds.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime

from sqlalchemy.orm import Session

from app.config import PROJECT_ROOT
from app.services.conversation_repository import ConversationRepository
from app.services.evidence_service import get_evidence
from app.services.persona_rules import build_persona
from app.models import CallSession, Supplier
from app.services.supplier_repository import AmbiguousSupplier, Resolution, SupplierNotFound, SupplierRepository

MAX_LEN = 600

# Input variables the campaign / test call supplies; output variables are captured after the call.
INPUT_VARIABLES = {"glid": "Seller GLID (fk_glusr_usr_id) for this call, supplied by the campaign or test call."}
PRECALL_VARIABLES = {
    "seller_found": "yes when the seller was found in the persona database",
    "call_allowed": "yes, no (seller asked not to be called) or unknown (seller record unavailable)",
    "business_name": "Business name to confirm at the start of the call",
    "opening_greeting": "Exact first line of the call: asks for the business by name, or a general greeting when no name is on record",
    "seller_city": "Seller city",
    "seller_category": "Seller's main product category",
    "opening_language": "Language the call opens in",
    "switch_language": "Most likely language if the seller switches",
    "language_style": "Hinglish mix and formality",
    "delivery_style": "Pace and tone for this seller",
    "call_objective": "Primary and fallback outcome",
    "opening_plan": "How to open this call",
    "seller_facts": "Verified facts the agent may use",
    "previous_commitment": "Earlier callback or meeting to acknowledge",
    "objection_guidance": "Handling for this seller's known objections",
    "frustration_threshold": "first sign or second sign",
    "call_ref": "Unique id for this call; sent back with the switch log and the outcome",
    "recommended_package": "Package suggested for this seller's turnover, with its price in words",
    "package_pitch": "Benefits and reason the suggested package fits",
}
OUTPUT_VARIABLES = {
    "call_outcome": ("enum", ["meeting_fixed", "callback_requested", "not_interested", "already_in_touch", "complaint", "wrong_person",
                              "do_not_call", "language_barrier", "other"],
                     "Classify the final outcome of the call from the transcript. meeting_fixed only when the seller clearly agreed to a "
                     "meeting time. callback_requested when a specific callback day or time was agreed. already_in_touch only when the "
                     "seller says they are already talking to an IndiaMART sales executive about packages or services. complaint when the "
                     "call was mainly about an unresolved complaint, poor service, or the seller being on a call with customer care. "
                     "not_interested only when the refusal was unambiguous. Otherwise choose the closest value or other."),
    "meeting_time": ("string", [], "The meeting date and time the seller agreed to, exactly as agreed, or empty."),
    "callback_time": ("string", [], "The callback date and time the seller asked for, or empty."),
    "objections_raised": ("string", [], "Short list of the objections the seller raised, separated by semicolons, or empty."),
    "persona_switches": ("string", [], "List every point where the agent changed its delivery because of the seller's behaviour (frustration, "
                         "confusion, interest, language switch, rush, bot suspicion). For each: signal detected, the seller's words, change made, "
                         "reason. Separate entries with semicolons. Empty if none."),
    "call_summary": ("string", [], "A short 1-2 line description of what happened on the call."),
}

# Used when the seller cannot be looked up. No do-not-call check was possible, so the call is never
# marked allowed, and the cautious behaviour is carried by the variables the agent reads first
# (objective and opening plan) instead of relying on a conditional rule deep in the prompt.
# Keep in sync with the agent's variable defaults in Sarvam, which apply when this API is unreachable.
CAUTIOUS_OBJECTIVE = (
    "Seller record could not be checked. First get permission: only after a clear yes, ask about buyer enquiries "
    "and offer a 15 to 20 minute meeting with a sales executive. Any other answer: thank the user and close."
)
CAUTIOUS_OPENING = (
    "Confirm the business name, then ask one question only: is the user open to a two minute conversation about "
    "their IndiaMART listing. Stop and wait. Continue only on a clear yes; for maybe, later, not sure or no, "
    "thank the user and end the call. Share no seller details and suggest no package."
)
# A do-not-call seller is blocked before dialling (dial-check). If one is reached anyway, every variable
# the agent reads says to stop: on Sarvam the per-seller variables outweigh a general prompt rule.
DO_NOT_CALL_OBJECTIVE = (
    "This seller asked not to be called. Do not pitch or ask anything. Apologise for the disturbance, "
    "confirm this number will not be called again, and end the call in the same turn."
)
# The first line spoken on the call (the agent's intro message is just {{opening_greeting}}).
GREETING_UNKNOWN_NAME = (
    "Hello, this is VANI calling from IndiaMART about your business listing. Am I speaking with the business owner?"
)


def opening_greeting(business_name: str) -> str:
    """Hello, kya aap <business> se bol rahe hain? when the name is known; otherwise the general greeting."""
    return f"\u0939\u0947\u0932\u094b, \u0915\u094d\u092f\u093e \u0906\u092a {business_name} \u0938\u0947 \u092c\u094b\u0932 \u0930\u0939\u0947 \u0939\u0948\u0902?" if business_name else GREETING_UNKNOWN_NAME


GENERIC = {
    "seller_found": "no", "call_allowed": "unknown", "business_name": "", "opening_greeting": GREETING_UNKNOWN_NAME, "seller_city": "", "seller_category": "",
    "opening_language": "Hindi", "switch_language": "English",
    "language_style": "medium Hinglish with common trade words; respectful, aap and ji",
    "delivery_style": "calm, unhurried pace; warmth high, empathy high, assertiveness low",
    "call_objective": CAUTIOUS_OBJECTIVE,
    "opening_plan": CAUTIOUS_OPENING,
    "seller_facts": "", "previous_commitment": "",
    "objection_guidance": "Any reluctance: accept it at once, thank the user and close.",
    "frustration_threshold": "first sign",
    "recommended_package": "none: do not suggest a package on this call",
    "package_pitch": "none",
}


def _clip(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= MAX_LEN else text[: MAX_LEN - 3].rsplit(" ", 1)[0] + "..."


def precall_variables(session: Session, glid: int | None, business_name: str | None) -> dict:
    repo = SupplierRepository(session)
    try:
        res = repo.resolve(business_name=business_name, glid=glid) if (glid or business_name) else None
    except (SupplierNotFound, AmbiguousSupplier) as exc:
        reason = "ambiguous business name" if isinstance(exc, AmbiguousSupplier) else "seller not found"
        return _start_call(session, glid, False, None, dict(GENERIC), reason)
    if res is None:
        return _start_call(session, glid, False, None, dict(GENERIC), "no glid or business_name supplied")

    variables, segment = build_variables(session, res)
    return _start_call(session, res.supplier.glid, True, segment, variables)


def do_not_call_variables(business_name: str) -> dict:
    return {
        **GENERIC,
        "seller_found": "yes", "call_allowed": "no", "business_name": business_name,
        "opening_greeting": opening_greeting(business_name),
        "call_objective": DO_NOT_CALL_OBJECTIVE,
        "opening_plan": "As soon as the user answers, call end_interaction with a message that says sorry for the disturbance "
                        "and confirms this number will not be called again.",
        "objection_guidance": "None: this call ends at the first user turn.",
        "frustration_threshold": "first sign",
    }


def build_variables(session: Session, res: Resolution) -> tuple[dict, str]:
    """Per-seller agent variables (all short strings) and the seller's segment."""
    sup = res.supplier
    hist = ConversationRepository(session).history(sup.id, limit=5, transcripts_for=0)
    names = [a.business_name for a in res.names]
    spec = build_persona(sup, hist.dated, get_evidence(session, wait=False), names)
    if not spec["call_eligibility"]["call_allowed"]:
        return {k: _clip(v) for k, v in do_not_call_variables(names[0] if names else "").items()}, spec["segment"]
    lang, voice, tone, plan = spec["language"], spec["voice"], spec["tone"], spec["conversation_plan"]
    sig = spec["signals_from_history"]
    commitment = ""
    if sig["callback_requested_on"]:
        when = datetime.strptime(sig["callback_requested_on"], "%Y-%m-%d")
        commitment = f"On the call of {when:%d %B %Y} the seller asked to be called back; this call is that callback."
    elif sig["callback_requested_before"]:
        commitment = "On an earlier call the seller asked to be called back; this call is that callback."
    elif sig["meeting_fixed_before"]:
        commitment = "A meeting with an IndiaMART executive was fixed on an earlier call; ask briefly how it went."
    playbook = [o for o in plan["objection_playbook"] if o["times_raised_before"]] or plan["objection_playbook"][:2]
    frustration = next(a for a in spec["live_adaptation"] if a["signal"] == "frustration")
    pkg = plan["package_recommendation"]
    variables = {
        "seller_found": "yes",
        "call_allowed": "yes" if spec["call_eligibility"]["call_allowed"] else "no",
        "business_name": names[0] if names else "",
        "opening_greeting": opening_greeting(names[0] if names else ""),
        "seller_city": sup.city or "",
        "seller_category": sup.top_category_1 or sup.top_category_group or "",
        "opening_language": lang["primary_language"],
        "switch_language": lang["switch_ready_language"] or "English",
        "language_style": f"{lang['hinglish_mix']}; {lang['formality']}",
        "delivery_style": f"{voice['pace']} pace; warmth {tone['warmth']}, empathy {tone['empathy']}, assertiveness {tone['assertiveness']}",
        # A rule keyed on a variable deep in the prompt was ignored in testing; the objective carries it.
        "call_objective": plan["objective"] + (
            " This seller has little patience for these calls: at the first sign of irritation, say sorry, say IndiaMART "
            "will keep calls to a minimum, and end the call at once." if frustration.get("threshold") == "first sign" else ""),
        "opening_plan": plan["opening"],
        "seller_facts": " ".join(plan["personalisation"]),
        "previous_commitment": commitment,
        "objection_guidance": " ".join(f"{o['objection']}: {o['approach']}" for o in playbook[:3]),
        "frustration_threshold": frustration.get("threshold", "second sign"),
        "recommended_package": f"{pkg['package']}, {pkg['price_spoken']}",
        "package_pitch": f"{pkg['buyleads']}; {', '.join(pkg['benefits'])}. {pkg['reason']}",
    }
    return {k: _clip(v) for k, v in variables.items()}, spec["segment"]


# ---------------------------------------------------------------- agent prompt (single source of truth)

PROMPT_PATH = PROJECT_ROOT / "sarvam" / "agent_prompt.md"
_VAR = re.compile(r"\{\{(\w+)\}\}")


def agent_prompt() -> str:
    """The exact prompt deployed to the Sarvam agent."""
    return PROMPT_PATH.read_text(encoding="utf-8")


def prompt_variables(text: str | None = None) -> set[str]:
    return set(_VAR.findall(text if text is not None else agent_prompt()))


def render_prompt(variables: dict) -> str:
    """The agent prompt with this seller's values filled in, as a standalone system prompt."""
    return _VAR.sub(lambda m: (variables.get(m.group(1)) or "").strip() or "(not known)", agent_prompt())


def _start_call(session: Session, glid: int | None, found: bool, segment: str | None, variables: dict, reason: str | None = None) -> dict:
    """Issue the call_ref and keep a snapshot of exactly what the agent was given."""
    call_ref = uuid.uuid4().hex[:16]
    variables = {**variables, "call_ref": call_ref}
    session.add(CallSession(call_ref=call_ref, glid=glid, found=found, segment=segment, variables_json=variables, created_at=datetime.now()))
    session.commit()
    out = {"found": found, "glid": glid, "segment": segment, "call_ref": call_ref, "variables": variables}
    if reason:
        out["reason"] = reason
    return out


def dial_check(session: Session, glids: list[int]) -> dict:
    """Pre-dial gate for campaigns: only sellers on record without a do-not-call flag are dialable.

    Unknown GLIDs are not dialable, because their do-not-call status cannot be checked.
    """
    from sqlalchemy import select

    unique = list(set(glids))
    found: dict[int, bool] = {}
    for i in range(0, len(unique), 5000):  # stay under SQLite's bound-parameter limit
        chunk = unique[i : i + 5000]
        found.update(session.execute(select(Supplier.glid, Supplier.do_not_call).where(Supplier.glid.in_(chunk))).all())
    allowed, blocked, unknown = [], [], []
    for g in dict.fromkeys(glids):  # keep input order, drop repeats
        if g not in found:
            unknown.append(g)
        elif found[g]:
            blocked.append({"glid": g, "reason": "do_not_call_requested"})
        else:
            allowed.append(g)
    return {"allowed": allowed, "blocked": blocked, "unknown": unknown}
