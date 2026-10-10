"""Optional real-LLM test. Runs only with RUN_LLM_INTEGRATION=1 and REAL_OPENAI_API_KEY or REAL_ANTHROPIC_API_KEY (incurs cost)."""

import os

import pytest

from app.config import Settings
from app.schemas import PersonaRequest
from app.services.llm_client import LLMClient
from app.services.persona_service import PersonaService, prompt_violations

PROVIDERS = [("openai", "openai_api_key", "REAL_OPENAI_API_KEY"), ("anthropic", "anthropic_api_key", "REAL_ANTHROPIC_API_KEY")]


@pytest.mark.parametrize("provider,field,env_key", PROVIDERS)
def test_real_llm_generation(session, ingested, provider, field, env_key):
    if os.getenv("RUN_LLM_INTEGRATION") != "1" or not os.getenv(env_key):
        pytest.skip(f"set RUN_LLM_INTEGRATION=1 and {env_key} to run")
    overrides = {"llm_provider": provider, field: os.environ[env_key], "llm_enabled": True,
                 "anthropic_workspace_id": os.getenv("ANTHROPIC_WORKSPACE_ID") or None}
    settings = Settings(**{**ingested["settings"].model_dump(), **overrides})
    out = PersonaService(session, settings, LLMClient(settings)).generate(PersonaRequest(glid=1001))
    assert out.metadata.generation_mode == "llm", out.metadata.warnings
    assert prompt_violations(out.voice_agent_system_prompt) == []
