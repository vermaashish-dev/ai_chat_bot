"""Export persona specs (JSON + readable Markdown) for chosen sellers, or auto-pick contrasting demo sellers.

Usage:
  python -m scripts.export_personas --glid 1494419 --glid 8622507
  python -m scripts.export_personas --demo            # one seller per contrasting segment
"""

import argparse
from pathlib import Path

from sqlalchemy import select

from app import db
from app.config import PROJECT_ROOT, get_settings
from app.models import Supplier, SupplierAlias
from app.schemas import PersonaRequest
from app.services.llm_client import LLMClient
from app.services.persona_service import PersonaService

DEMO_SEGMENTS = ["Time-pressed", "Fatigued / sceptical", "Warm callback", "Established business", "Value-seeking"]


def pick_demo(session, per_segment: int = 1) -> list[int]:
    from app.services.conversation_repository import ConversationRepository
    from app.services.evidence_service import get_evidence
    from app.services.persona_rules import build_persona

    ev = get_evidence(session)
    named = select(SupplierAlias.supplier_id).where(SupplierAlias.occurrences >= 2).distinct()
    found: dict[str, list[int]] = {}
    for sup in session.scalars(select(Supplier).where(Supplier.id.in_(named), Supplier.do_not_call.is_(False)).order_by(Supplier.glid).limit(4000)):
        hist = ConversationRepository(session).history(sup.id, 5, transcripts_for=0)
        if len(hist.dated) < 2:
            continue
        seg = build_persona(sup, hist.dated, ev, ["x"])["segment"]
        if seg in DEMO_SEGMENTS and len(found.setdefault(seg, [])) < per_segment:
            found[seg].append(sup.glid)
        if all(len(found.get(s, [])) >= per_segment for s in DEMO_SEGMENTS):
            break
    return [g for s in DEMO_SEGMENTS for g in found.get(s, [])]


def to_markdown(r) -> str:
    sp, sup = r.persona.spec, r.supplier
    name = sup.business_names[0].name if sup.business_names else f"GLID {sup.glid}"
    lines = [f"# Persona spec: {name}", "", f"GLID {sup.glid} | {sup.city}, {sup.state} | {sup.business_type} | {sup.annual_turnover} | {sup.nature_of_business}",
             f"Segment: **{sp['segment']}** | Call allowed: **{sp['call_eligibility']['call_allowed']}** | Mode: {r.metadata.generation_mode}", ""]
    for part in ("voice", "language", "tone"):
        c = sp[part]
        settings = {k: v for k, v in c.items() if k not in ("reason", "data_used", "evidence", "confidence")}
        lines += [f"## {part.title()} ({c['confidence']} confidence)", "", "| Setting | Value |", "|---|---|"]
        lines += [f"| {k} | {v} |" for k, v in settings.items()]
        lines += ["", f"**Why:** {c['reason']}", f"**Data used:** {', '.join(c['data_used'])}"]
        lines += [f"- Evidence: {e}" for e in c["evidence"]] or ["- Evidence: none specific; informed default."]
        lines.append("")
    plan = sp["conversation_plan"]
    lines += ["## Conversation plan", "", f"**Objective:** {plan['objective']}", "", f"**Opening:** {plan['opening']}", "", "**Personalisation:**"]
    lines += [f"- {x}" for x in plan["personalisation"]] or ["- none available"]
    lines += ["", "**Objection playbook:**"]
    for o in plan["objection_playbook"]:
        lines.append(f"- *{o['objection']}* (raised {o['times_raised_before']}x, source {o['source']}): {o['approach']}")
        lines += [f"  - Evidence: {e}" for e in o["evidence"]]
    lines += ["", "**Escalation rules:**"] + [f"- {x}" for x in plan["escalation_rules"]]
    lines += ["", "**Guardrails:**"] + [f"- {x}" for x in plan["guardrails"]]
    lines += ["", "## Live adaptation", "", "| Signal | Change | Evidence |", "|---|---|---|"]
    lines += [f"| {a['signal']} | {a['change']} | {'; '.join(a['evidence']) or 'informed guess'} |" for a in sp["live_adaptation"]]
    lines += ["", "## Warnings", ""] + [f"- {w}" for w in r.metadata.warnings]
    lines += ["", "## Voice agent system prompt", "", "```text", r.voice_agent_system_prompt, "```", ""]
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--glid", type=int, action="append", default=[])
    ap.add_argument("--demo", action="store_true")
    ap.add_argument("--no-llm", action="store_true")
    ap.add_argument("--out", type=Path, default=PROJECT_ROOT / "outputs" / "persona_specs")
    args = ap.parse_args()
    db.init_db()
    settings = get_settings()
    args.out.mkdir(parents=True, exist_ok=True)
    with db.session_factory()() as s:
        glids = args.glid + (pick_demo(s) if args.demo else [])
        svc = PersonaService(s, settings, LLMClient(settings))
        for g in glids:
            r = svc.generate(PersonaRequest(glid=g, use_llm=not args.no_llm))
            (args.out / f"{g}.json").write_text(r.model_dump_json(indent=2), encoding="utf-8")
            (args.out / f"{g}.md").write_text(to_markdown(r), encoding="utf-8")
            print(f"{g}: {r.persona.spec['segment']} -> {args.out / f'{g}.md'}")


if __name__ == "__main__":
    main()
