import re

import pytest

from app.services.packages import CATALOG, customer_care_spoken, in_words, price_spoken, recommend
from app.services.persona_service import prompt_violations
from app.services.sarvam_service import GENERIC, PRECALL_VARIABLES, agent_prompt, prompt_variables, render_prompt


@pytest.mark.parametrize("turnover,package", [
    ("0 - 40 L", "Mini Dynamic Catalog (MDC)"),
    ("40 L - 1.5 Cr", "TrustSEAL Pro"),
    ("1.5 - 5 Cr", "Maximiser Pro"),
    ("5 - 25 Cr", "IM Star Pro"),
    ("25 - 100 Cr", "IM Leader Pro"),
    ("> 500 Cr", "IM Industry Leader"),
])
def test_turnover_to_package(turnover, package):
    r = recommend(turnover)
    assert r["package"] == package and r["confidence"] == "medium" and turnover in r["reason"]


def test_unknown_turnover_falls_back_with_low_confidence():
    r = recommend(None)
    assert r["package"] == "Mini Dynamic Catalog (MDC)" and r["confidence"] == "low"


@pytest.mark.parametrize("n,words", [
    (35000, "thirty five thousand"), (72000, "seventy two thousand"), (120000, "one lakh twenty thousand"),
    (170000, "one lakh seventy thousand"), (28, "twenty eight"), (250, "two hundred fifty"), (10500000, "one crore five lakh"),
])
def test_indian_number_words(n, words):
    assert in_words(n) == words


def test_spoken_prices_match_listed_prices():
    assert price_spoken("MDC").startswith("thirty five thousand rupees for one year")
    assert price_spoken("TrustSEAL Pro").endswith("one lakh twenty thousand rupees for three years")
    assert price_spoken("IM Star Pro") == "price on quote from the sales executive"  # never an invented price
    assert all(not v["prices"] or set(v["prices"]) == {1, 2, 3} for v in CATALOG.values())
    assert customer_care_spoken() == "nine six nine six nine six nine six nine six"


def test_agent_prompt_has_no_digit_prices_and_correct_static_facts():
    prompt = agent_prompt()
    assert not re.search(r"\d{2},?\d{3}", prompt), "prices must be in words, never digits"
    assert customer_care_spoken() in prompt
    assert f"{in_words(CATALOG['MDC']['prices'][1])} rupees for one year" in prompt
    assert len(prompt) < 8000


def test_prompt_variables_are_declared_and_have_fallbacks():
    assert prompt_variables() <= set(PRECALL_VARIABLES)
    assert set(GENERIC) <= set(PRECALL_VARIABLES)


def test_rendered_prompt_is_clean():
    out = render_prompt({**GENERIC, "business_name": ""})
    assert "{{" not in out and "(not known)" in out
    assert prompt_violations(out) == []


def test_package_reaches_persona_prompt_and_precall(client_like):
    svc, client = client_like
    out = svc.generate_for(1004)
    assert out.persona.spec["conversation_plan"]["package_recommendation"]["package"] == "IM Star Pro"
    assert "IM Star Pro, price on quote from the sales executive" in out.voice_agent_system_prompt
    assert prompt_violations(out.voice_agent_system_prompt) == []
    v = client.post("/api/v1/sarvam/pre-call", json={"glid": "1001"}).json()["variables"]
    assert v["recommended_package"].startswith("Mini Dynamic Catalog (MDC), thirty five thousand rupees for one year")
    assert not re.search(r"\d", v["recommended_package"])


def test_spec_is_current_and_secret_free():
    from scripts.sarvam_spec import SPEC_PATH, build_spec, problems

    spec = build_spec()
    assert problems(spec) == []
    assert SPEC_PATH.exists(), "run python -m scripts.sarvam_spec"
    import json

    assert json.loads(SPEC_PATH.read_text(encoding="utf-8")) == json.loads(json.dumps(spec)), "spec is stale: run python -m scripts.sarvam_spec"


@pytest.fixture()
def client_like(ingested, session):
    from fastapi.testclient import TestClient

    from app.main import app
    from app.schemas import PersonaRequest
    from app.services.llm_client import LLMClient
    from app.services.persona_service import PersonaService

    svc = PersonaService(session, ingested["settings"], LLMClient(ingested["settings"]))
    svc.generate_for = lambda g: svc.generate(PersonaRequest(glid=g))
    with TestClient(app) as c:
        yield svc, c
