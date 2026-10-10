"""Generate sarvam/agent_spec.json: the Sarvam agent's prompt, variables and tools, from the code.

The code is the source of truth; the JSON is the reviewable artifact that gets deployed to Sarvam.
The token and public URL stay as ${API_TOKEN} / ${PUBLIC_BASE_URL} placeholders: secrets never go in files.

Usage:
  python -m scripts.sarvam_spec            # write sarvam/agent_spec.json
  python -m scripts.sarvam_spec --check    # exit 1 if the spec on disk is stale or inconsistent
"""

import argparse
import hashlib
import json
import sys

from app.config import PROJECT_ROOT
from app.services.sarvam_service import GENERIC, INPUT_VARIABLES, OUTPUT_VARIABLES, PRECALL_VARIABLES, agent_prompt, prompt_variables

SPEC_PATH = PROJECT_ROOT / "sarvam" / "agent_spec.json"
AUTH = [{"name": "Authorization", "type": "static", "value": "Bearer ${API_TOKEN}"}]


def build_spec() -> dict:
    prompt = agent_prompt()
    variables = {
        "glid": {"value": "", "description": INPUT_VARIABLES["glid"], "needs_initial_value": True},
        # Pre-call variables default to the cautious fallback, which applies when the API is unreachable.
        **{k: {"value": GENERIC.get(k, ""), "description": d, "needs_initial_value": False} for k, d in PRECALL_VARIABLES.items()},
        **{
            k: {"value": "", "type": t, **({"one_of": opts} if opts else {}), "description": k.replace("_", " "),
                "needs_initial_value": False, "update_post_interaction": True, "post_interaction_prompt": p}
            for k, (t, opts, p) in OUTPUT_VARIABLES.items()
        },
    }
    for name, v in variables.items():
        v["name"] = name
    agent_var = lambda n: {"name": n, "type": "agent_variable", "value": {"variable_name": n}}  # noqa: E731
    llm = lambda n, d, **kw: {"name": n, "type": "llm_prompt", "description": d, "isInputProperty": True, **kw}  # noqa: E731
    tools = [
        {"name": "fetch_seller_persona", "tool_type": "webhook_on_start", "connector_config": {
            "url": "${PUBLIC_BASE_URL}/api/v1/sarvam/pre-call", "method": "POST", "lifecycle": "on_start", "timeout_seconds": 8,
            "headers": AUTH, "body": [agent_var("glid")],
            "save_to_variables": [{"response_path": f"variables.{k}", "variable_name": k} for k in PRECALL_VARIABLES]}},
        {"name": "log_persona_switch", "tool_type": "webhook", "connector_config": {
            "url": "${PUBLIC_BASE_URL}/api/v1/sarvam/switch-log", "method": "POST", "lifecycle": "run", "timeout_seconds": 5,
            "headers": AUTH, "body": [
                agent_var("glid"), agent_var("call_ref"),
                llm("signal", "The signal detected in the seller's last turn", param_type="One of a list",
                    enum=["frustration", "confusion", "interest", "language_switch", "rush", "bot_suspicion", "opt_out", "other"]),
                llm("change_made", "The delivery change made, in English", param_type="Text"),
                llm("reason", "Why the change was made, in English, tied to what the seller said", param_type="Text"),
                llm("seller_quote", "The seller's words that showed the signal, as heard", param_type="Text", required=False)]}},
        {"name": "send_call_outcome", "tool_type": "webhook_on_end", "connector_config": {
            "url": "${PUBLIC_BASE_URL}/api/v1/sarvam/post-call", "method": "POST", "lifecycle": "on_end", "timeout_seconds": 8,
            "headers": AUTH, "body": [agent_var(n) for n in ("glid", "call_ref", *OUTPUT_VARIABLES)]}},
    ]
    return {
        "agent_id": "sulabh-im-80199a3e-82ea",
        "prompt_file": "sarvam/agent_prompt.md",
        "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
        "prompt_chars": len(prompt),
        "variables": variables,
        "tools": tools,
        "voicemail_message": "Hello, this is VANI from IndiaMART calling about your business listing and buyer enquiries. "
                             "We will call you back at a better time. Thank you.",
        # The whole first line comes from the API (opening_greeting): "Hello, kya aap <business> se bol rahe hain?" in Hindi,
        # or the general English greeting when no business name is on record. Every language mapping is this placeholder.
        "intro_message": "{{opening_greeting}}",
        "evaluation_criteria": [{"name": "Meeting fixed", "variable": "call_outcome", "operator": "equals", "value": "meeting_fixed"}],
    }


def problems(spec: dict) -> list[str]:
    out = []
    used, declared = prompt_variables(), set(spec["variables"])
    if used - declared:
        out.append(f"prompt uses undeclared variables: {sorted(used - declared)}")
    if set(GENERIC) - set(PRECALL_VARIABLES):
        out.append(f"fallback values for unknown variables: {sorted(set(GENERIC) - set(PRECALL_VARIABLES))}")
    if "${API_TOKEN}" not in json.dumps(spec) or "Bearer vani_" in json.dumps(spec):
        out.append("token must be a placeholder")
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()
    spec = build_spec()
    errs = problems(spec)
    text = json.dumps(spec, indent=2, ensure_ascii=False) + "\n"
    if args.check:
        on_disk = SPEC_PATH.read_text(encoding="utf-8") if SPEC_PATH.exists() else ""
        if on_disk != text:
            errs.append("sarvam/agent_spec.json is stale: run python -m scripts.sarvam_spec")
        for e in errs:
            print("FAIL:", e)
        sys.exit(1 if errs else 0)
    if errs:
        sys.exit("\n".join(errs))
    SPEC_PATH.write_text(text, encoding="utf-8")
    print(f"wrote {SPEC_PATH} ({len(spec['variables'])} variables, {len(spec['tools'])} tools, prompt {spec['prompt_chars']} chars)")


if __name__ == "__main__":
    main()
