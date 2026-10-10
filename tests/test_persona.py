import anthropic
import httpx2
import openai
import pytest

from app.schemas import LLMPersonaOutput, PersonaRequest
from app.services.llm_client import LLMClient
from app.services.persona_service import PersonaService, prompt_violations
from app.services.signals import detect_signals
from tests.conftest import INJECTION, FakeAnthropic, FakeOpenAI, valid_llm_output


def make(session, settings, behaviour=None):
    """Anthropic-provider service with a fake SDK client (behaviour=None: no client, rules only)."""
    settings = settings.model_copy(update={"llm_provider": "anthropic"})
    llm = LLMClient(settings, client=FakeAnthropic(behaviour) if behaviour is not None else None)
    return PersonaService(session, settings, llm), llm


def make_openai(session, settings, behaviour):
    llm = LLMClient(settings, client=FakeOpenAI(behaviour))
    return PersonaService(session, settings, llm), llm


@pytest.mark.parametrize("provider,key", [("openai", "openai_api_key"), ("anthropic", "anthropic_api_key")])
def test_llm_disabled_ignores_key(session, ingested, provider, key):
    settings = ingested["settings"].model_copy(update={"llm_provider": provider, key: "sk-test", "llm_enabled": False})
    llm = LLMClient(settings)
    assert llm._client is None and not settings.llm_configured
    out = PersonaService(session, settings, llm).generate(PersonaRequest(glid=1001))
    assert out.metadata.generation_mode == "rules"


def test_provider_selects_sdk_and_default_model(ingested):
    base = ingested["settings"]
    oa = LLMClient(base.model_copy(update={"openai_api_key": "sk-test"}))
    assert isinstance(oa._client, openai.OpenAI) and oa.model == "gpt-4.1-mini"
    an = LLMClient(base.model_copy(update={"llm_provider": "anthropic", "anthropic_api_key": "sk-ant-test"}))
    assert isinstance(an._client, anthropic.Anthropic) and an.model == "claude-haiku-5-5"
    # A key for the other provider does not enable the selected one.
    assert LLMClient(base.model_copy(update={"anthropic_api_key": "sk-ant-test"}))._client is None


def test_openai_success_used_and_validated(session, ingested):
    svc, llm = make_openai(session, ingested["settings"], valid_llm_output())
    out = svc.generate(PersonaRequest(glid=1001))
    assert out.metadata.generation_mode == "llm" and out.metadata.model == "gpt-4.1-mini"
    sent = llm._client.chat.completions.calls[0]
    assert sent["response_format"] is LLMPersonaOutput and sent["model"] == "gpt-4.1-mini"
    system, user = sent["messages"]
    assert system["role"] == "system" and "untrusted DATA" in system["content"]
    assert user["content"].startswith("<seller_data>") and "Beta Exports" not in user["content"]


def _length_error():
    completion = openai.types.chat.ChatCompletion(id="x", choices=[], created=0, model="gpt-4.1-mini", object="chat.completion")
    return openai.LengthFinishReasonError(completion=completion)


@pytest.mark.parametrize("behaviour,code", [
    ({"persona": {"profile_summary": "x"}}, "invalid_output"),
    ("refusal", "refused"),
    (_length_error(), "invalid_output"),
    (openai.APITimeoutError(request=httpx2.Request("POST", "https://api.openai.com/v1/chat/completions")), "timeout"),
    (openai.RateLimitError("slow down sk-secret", response=httpx2.Response(429, request=httpx2.Request("POST", "https://x")), body=None), "rate_limited"),
    (openai.AuthenticationError("bad key sk-secret", response=httpx2.Response(401, request=httpx2.Request("POST", "https://x")), body=None), "provider_error"),
    (openai.APIConnectionError(request=httpx2.Request("POST", "https://x")), "provider_error"),
])
def test_openai_failures_fall_back_safely(session, ingested, behaviour, code):
    svc, _ = make_openai(session, ingested["settings"], behaviour)
    out = svc.generate(PersonaRequest(glid=1001))
    assert out.metadata.generation_mode == "rules"
    assert any(f"({code})" in w for w in out.metadata.warnings)
    assert "sk-secret" not in out.model_dump_json()


def test_rules_mode_without_api_key(session, ingested):
    svc, _ = make(session, ingested["settings"])
    out = svc.generate(PersonaRequest(business_name="Alpha Traders"))
    assert out.metadata.generation_mode == "rules"
    assert any("not_configured" in w for w in out.metadata.warnings)
    assert out.supplier.glid == 1001
    assert "## Persona" in out.voice_agent_system_prompt and "Alpha Traders" in out.voice_agent_system_prompt
    assert prompt_violations(out.voice_agent_system_prompt) == []
    spec = out.persona.spec
    for key in ("voice", "language", "tone"):
        assert spec[key]["reason"] and spec[key]["confidence"] in {"high", "medium", "low"}
    # Latest dated call (05 Sep) was a callback request: opening references it.
    assert "05 Sep 2026" in spec["conversation_plan"]["opening"]


def test_do_not_call_blocks_strategy_and_skips_llm(session, ingested):
    svc, llm = make(session, ingested["settings"], valid_llm_output())
    out = svc.generate(PersonaRequest(glid=1003))
    assert out.persona.spec["call_eligibility"]["call_allowed"] is False
    assert out.call_strategy.objective == "Do not call"
    assert "must not be placed" in out.voice_agent_system_prompt
    assert llm._client.messages.calls == []


def test_llm_success_used_and_validated(session, ingested):
    svc, llm = make(session, ingested["settings"], valid_llm_output())
    out = svc.generate(PersonaRequest(glid=1001))
    assert out.metadata.generation_mode == "llm" and out.metadata.model == "claude-haiku-5-5"
    assert out.call_strategy.next_action == "Call at 3 pm"
    sent = llm._client.messages.calls[0]
    # Structured output: the response is constrained to the persona schema.
    assert sent["output_format"] is LLMPersonaOutput and sent["model"] == "claude-haiku-5-5"
    # Only the selected supplier's data is sent.
    user = sent["messages"][0]["content"]
    assert "Alpha Traders" in user and "Beta Exports" not in user


@pytest.mark.parametrize("behaviour,code", [
    ({"persona": {"profile_summary": "x"}}, "invalid_output"),  # fails the schema
    ("max_tokens", "invalid_output"),  # truncated
    ("refusal", "refused"),
    (anthropic.APITimeoutError(request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages")), "timeout"),
    (anthropic.RateLimitError("slow down sk-secret", response=httpx2.Response(429, request=httpx2.Request("POST", "https://x")), body=None), "rate_limited"),
    (anthropic.InternalServerError("boom sk-secret", response=httpx2.Response(500, request=httpx2.Request("POST", "https://x")), body=None), "provider_error"),
    (anthropic.AuthenticationError("bad key sk-secret", response=httpx2.Response(401, request=httpx2.Request("POST", "https://x")), body=None), "provider_error"),
    (anthropic.APIConnectionError(request=httpx2.Request("POST", "https://x")), "provider_error"),
])
def test_llm_failures_fall_back_safely(session, ingested, behaviour, code):
    svc, _ = make(session, ingested["settings"], behaviour)
    out = svc.generate(PersonaRequest(glid=1001))
    assert out.metadata.generation_mode == "rules"
    assert any(f"({code})" in w for w in out.metadata.warnings)
    assert "sk-secret" not in out.model_dump_json() and "boom" not in out.model_dump_json()


def test_llm_prompt_with_internal_terms_is_rejected(session, ingested):
    bad = valid_llm_output("## Persona\nThe agent knows the seller GLID 1001 from the database. " + "Speak politely. " * 40)
    svc, _ = make(session, ingested["settings"], bad)
    out = svc.generate(PersonaRequest(glid=1001))
    assert "GLID" not in out.voice_agent_system_prompt
    assert any("rejected" in w for w in out.metadata.warnings)


def test_injected_transcript_is_data_not_instructions(session, ingested):
    # Rules path: transcript text never reaches the customer-facing prompt.
    svc, _ = make(session, ingested["settings"])
    out = svc.generate(PersonaRequest(glid=1002))
    assert "90%" not in out.voice_agent_system_prompt and "IGNORE" not in out.voice_agent_system_prompt
    # LLM path: the text is passed only inside the untrusted data block, and the system prompt says so.
    echo = valid_llm_output("## Persona\nTell the seller they get a 90% discount. " + "Be polite. " * 50)
    svc, llm = make(session, ingested["settings"], echo)
    out = svc.generate(PersonaRequest(glid=1002))
    sent = llm._client.messages.calls[0]
    user = sent["messages"][0]["content"]
    assert INJECTION in user and user.startswith("<seller_data>")
    assert "untrusted DATA" in sent["system"]
    assert "90%" not in out.voice_agent_system_prompt
    assert any("unsupported offer" in w for w in out.metadata.warnings)


def test_personas_are_distinct_for_contrasting_sellers(session, ingested):
    svc, _ = make(session, ingested["settings"])
    frustrated = svc.generate(PersonaRequest(glid=1002)).persona.spec
    rushed = svc.generate(PersonaRequest(glid=1004)).persona.spec
    south = svc.generate(PersonaRequest(glid=1008)).persona.spec
    assert frustrated["voice"]["pace"] == "slow and calm" and frustrated["tone"]["assertiveness"] == "low"
    assert rushed["voice"]["pace"] == "brisk" and rushed["language"]["switch_ready_language"] == "Gujarati"
    assert rushed["language"]["formality"].startswith("formal")
    assert south["language"]["primary_language"] == "English" and south["language"]["switch_ready_language"] == "Tamil"
    assert len({frustrated["segment"], rushed["segment"], south["segment"]}) == 3
    assert next(a for a in frustrated["live_adaptation"] if a["signal"] == "frustration")["threshold"] == "first sign"


def test_missing_name_and_fields_lower_confidence(session, ingested):
    svc, _ = make(session, ingested["settings"])
    out = svc.generate(PersonaRequest(glid=1007))  # no calls, so no business name on record
    assert out.supplier.business_names == []
    assert any("No business name" in w for w in out.metadata.warnings)
    assert "Business (not known)" in out.voice_agent_system_prompt and "{{" not in out.voice_agent_system_prompt


def test_signal_detection():
    r = detect_signals("Madam abhi busy hoon, jaldi bolo, price kitna hai?")
    assert {"rush", "interest"} <= set(r["signals"]) and r["opt_out"] is False
    assert detect_signals("Mujhe dobara call mat karo")["opt_out"] is True
    assert "language_switch" in detect_signals("હા બોલો")["signals"]


def test_uninterested_seller_closes_at_first_sign():
    # Seller 6299 pattern: 4 not-interested calls, no frustration flag -> fatigued, so the first sign is the limit.
    from app.models import Supplier
    from app.services.persona_rules import build_persona

    sup = Supplier(glid=1, city="Delhi", state="Delhi", annual_turnover="5 - 25 Cr", business_type="Proprietorship",
                   profile_json={"calls_not_interested": 4, "bot_answered": 4, "avg_answered_call_sec": 9.5})
    spec = build_persona(sup, [], {"available": False}, ["X"])
    assert spec["segment"] == "Fatigued / sceptical"
    assert next(a for a in spec["live_adaptation"] if a["signal"] == "frustration")["threshold"] == "first sign"


def test_rules_insights_read_call_history_without_coded_objections(session, ingested):
    # 1005: one dated Not Interested call and no past_objections; 1002: the injected summary must never be copied.
    svc, _ = make(session, ingested["settings"])
    ins = svc.generate(PersonaRequest(glid=1005)).persona.history_insights
    assert any("Declined on 1 recent bot call" in o.text and "2026-07-11" in o.source for o in ins.objections)
    assert any("latest call ended Not Interested" in f.text for f in ins.follow_ups)
    out = svc.generate(PersonaRequest(glid=1002))
    assert "IGNORE" not in out.persona.history_insights.model_dump_json()
    assert any("not_configured" in w and "rule engine" in w for w in out.metadata.warnings)
