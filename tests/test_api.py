import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import app
from app.schemas import PersonaResponse


@pytest.fixture()
def client(ingested):
    with TestClient(app) as c:
        yield c


def test_health(client):
    body = client.get("/health").json()
    assert body["status"] == "ok" and body["suppliers"] == 8
    assert "anthropic_api_key" not in str(body).lower() and "openai_api_key" not in str(body).lower()


def test_persona_endpoint_schema(client):
    r = client.post("/api/v1/supplier/persona", json={"business_name": "Alpha Traders", "history_limit": 2})
    assert r.status_code == 200
    body = PersonaResponse.model_validate(r.json())
    assert body.metadata.matched_glid == 1001
    assert len(body.conversation_history.dated) == 2
    assert "call_recording_url" not in r.text and "example.invalid" not in r.text


@pytest.mark.parametrize("payload,status,code", [
    ({"business_name": "Common Name Co"}, 409, "supplier_ambiguous"),
    ({"business_name": "Alpha Tradrs"}, 404, "supplier_not_found"),
    ({"glid": 31337}, 404, "supplier_not_found"),
    ({}, 422, "validation_error"),
    ({"business_name": "Alpha Traders", "history_limit": 0}, 422, "validation_error"),
    ({"glid": -5}, 422, "validation_error"),
])
def test_persona_errors(client, payload, status, code):
    r = client.post("/api/v1/supplier/persona", json=payload)
    assert r.status_code == status and r.json()["error"]["code"] == code


def test_404_includes_suggestions_409_includes_candidates(client):
    assert client.post("/api/v1/supplier/persona", json={"business_name": "Alpha Tradrs"}).json()["error"]["details"]["suggestions"][0]["glid"] == 1001
    cands = client.post("/api/v1/supplier/persona", json={"business_name": "Common Name Co"}).json()["error"]["details"]["candidates"]
    assert {c["glid"] for c in cands} == {1005, 1006}


def test_admin_ingest_and_status_local(client):
    r = client.post("/api/v1/admin/ingest", params={"wait": "true"})
    assert r.status_code == 200 and r.json()["imported"] == 0 and r.json()["status"] == "finished"
    st = client.get("/api/v1/admin/ingestion-status").json()
    assert st["last_run"]["failed"] == 0 and len(st["files"]) >= 6


def test_admin_refuses_proxied_requests_without_token(client):
    assert client.post("/api/v1/admin/ingest", headers={"X-Forwarded-For": "1.2.3.4"}).status_code == 403
    assert client.post("/api/v1/sarvam/pre-call", json={"glid": "1001"}, headers={"X-Forwarded-For": "1.2.3.4"}).status_code == 403


def test_tokens_enforced_when_configured(client, monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "adm")
    monkeypatch.setenv("API_TOKEN", "api")
    get_settings.cache_clear()
    try:
        assert client.get("/api/v1/admin/ingestion-status").status_code == 403
        assert client.get("/api/v1/admin/ingestion-status", headers={"Authorization": "Bearer adm"}).status_code == 200
        assert client.post("/api/v1/sarvam/pre-call", json={"glid": "1001"}, headers={"X-Forwarded-For": "1.2.3.4"}).status_code == 403
        ok = client.post("/api/v1/sarvam/pre-call", json={"glid": "1001"}, headers={"Authorization": "Bearer api", "X-Forwarded-For": "1.2.3.4"})
        assert ok.status_code == 200
    finally:
        get_settings.cache_clear()


def test_sarvam_pre_call(client):
    found = client.post("/api/v1/sarvam/pre-call", json={"glid": "1001"}).json()
    v = found["variables"]
    assert found["found"] and v["seller_found"] == "yes" and v["call_allowed"] == "yes" and v["business_name"] == "Alpha Traders"
    assert all(isinstance(x, str) and len(x) <= 600 for x in v.values())
    assert "callback" in v["previous_commitment"]
    assert client.post("/api/v1/sarvam/pre-call", json={"glid": "1003"}).json()["variables"]["call_allowed"] == "no"
    unknown = client.post("/api/v1/sarvam/pre-call", json={"glid": "not-a-number"}).json()
    assert unknown["found"] is False and unknown["variables"]["seller_found"] == "no"
    assert unknown["variables"]["call_allowed"] == "unknown"  # never "yes" without a do-not-call check
    assert unknown["variables"]["frustration_threshold"] == "first sign"
    assert "permission" in unknown["variables"]["call_objective"] and "none" in unknown["variables"]["recommended_package"]
    assert set(unknown["variables"]) == set(v)


def test_switch_log_roundtrip(client):
    r = client.post("/api/v1/sarvam/switch-log", json={"glid": "1001", "signal": "rush", "change_made": "Shortened to one ask",
                                                       "reason": "Seller said busy", "seller_quote": "busy hoon"})
    assert r.status_code == 200
    rows = client.get("/api/v1/switch-log", params={"glid": 1001}).json()
    assert rows[0]["signal"] == "rush" and rows[0]["seller_quote"] == "busy hoon"
    assert client.post("/api/v1/sarvam/switch-log", json={"signal": "panic", "change_made": "x" * 5, "reason": "y" * 5}).status_code == 422


def test_evidence_and_signals_endpoints(client):
    import time

    for _ in range(100):  # computed in the background after startup
        ev = client.get("/api/v1/evidence").json()
        if ev.get("available"):
            break
        assert ev == {"available": False, "warming_up": True}
        time.sleep(0.05)
    assert ev["available"] is True
    assert "rush" in client.post("/api/v1/signals/detect", json={"text": "abhi busy hoon"}).json()["signals"]


def test_post_call_outcome_roundtrip(client):
    r = client.post("/api/v1/sarvam/post-call", json={"glid": "1001", "call_outcome": "callback_requested", "callback_time": "Sunday 8 AM",
                                                      "persona_switches": "rush -> one question -> seller busy", "meeting_time": "{{meeting_time}}"})
    assert r.status_code == 200
    row = client.get("/api/v1/call-outcomes", params={"glid": 1001}).json()[0]
    assert row["call_outcome"] == "callback_requested" and row["callback_time"] == "Sunday 8 AM"
    assert row["meeting_time"] is None  # unfilled template placeholder stored as empty
    assert client.post("/api/v1/sarvam/post-call", json={"glid": "1"}, headers={"X-Forwarded-For": "1.2.3.4"}).status_code == 403


def test_switch_log_drops_repeated_tool_call(client):
    body = {"glid": "1002", "signal": "frustration", "change_made": "Apologised", "reason": "Seller irritated", "seller_quote": "time waste"}
    first = client.post("/api/v1/sarvam/switch-log", json=body).json()
    second = client.post("/api/v1/sarvam/switch-log", json=body).json()
    assert first["logged"] and second == {"logged": False, "duplicate_of": first["id"]}
    other = client.post("/api/v1/sarvam/switch-log", json={**body, "signal": "confusion", "seller_quote": "kya matlab"}).json()
    assert other["logged"]


def test_switch_log_drops_reworded_retry(client):
    base = {"glid": "1003", "signal": "frustration", "change_made": "Apologised", "reason": "Seller irritated"}
    first = client.post("/api/v1/sarvam/switch-log", json={**base, "seller_quote": "you are forcing again and again"}).json()
    retry = client.post("/api/v1/sarvam/switch-log", json={**base, "seller_quote": "you are forcing again and again for the meeting"}).json()
    assert first["logged"] and retry["logged"] is False
    assert client.post("/api/v1/sarvam/switch-log", json={**base, "signal": "rush", "seller_quote": "busy"}).json()["logged"]


def test_dial_check_gate(client):
    r = client.post("/api/v1/campaign/dial-check", json={"glids": [1001, 1003, 424242, 1001]}).json()
    assert r == {"allowed": [1001], "blocked": [{"glid": 1003, "reason": "do_not_call_requested"}], "unknown": [424242]}
    assert client.post("/api/v1/campaign/dial-check", json={"glids": []}).status_code == 422


def test_build_dial_list_script(ingested, tmp_path, monkeypatch):
    import csv
    import sys

    from scripts import build_dial_list

    src = tmp_path / "contacts.csv"
    src.write_text(
        "glid,phone\n1001,+910000000001\n1003,+910000000003\n424242,+910000000009\nabc,+91x\n1001,+910000000001\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(sys, "argv", ["build_dial_list", str(src)])
    build_dial_list.main()
    ok = list(csv.DictReader((tmp_path / "contacts_dialable.csv").open(encoding="utf-8")))
    bad = {r["glid"] + ":" + r["excluded_reason"] for r in csv.DictReader((tmp_path / "contacts_excluded.csv").open(encoding="utf-8"))}
    assert [r["glid"] for r in ok] == ["1001"]
    assert bad == {"1003:do_not_call_requested", "424242:glid_not_on_record", "abc:invalid_glid", "1001:duplicate_glid"}


def test_dial_check_handles_large_lists(client):
    glids = list(range(5_000_000, 5_040_000)) + [1001]  # beyond SQLite's parameter limit
    r = client.post("/api/v1/campaign/dial-check", json={"glids": glids}).json()
    assert r["allowed"] == [1001] and len(r["unknown"]) == 40_000


def test_call_ref_ties_switches_and_outcome_to_one_call(client):
    pre = client.post("/api/v1/sarvam/pre-call", json={"glid": "1001"}).json()
    ref = pre["call_ref"]
    assert ref and pre["variables"]["call_ref"] == ref
    other = client.post("/api/v1/sarvam/pre-call", json={"glid": "1001"}).json()["call_ref"]
    assert other != ref

    sw = {"glid": "1001", "signal": "rush", "change_made": "One short ask", "reason": "Seller busy", "seller_quote": "busy hoon"}
    assert client.post("/api/v1/sarvam/switch-log", json={**sw, "call_ref": ref}).json()["logged"]
    assert client.post("/api/v1/sarvam/switch-log", json={**sw, "call_ref": ref}).json()["logged"] is False  # retry in same call
    assert client.post("/api/v1/sarvam/switch-log", json={**sw, "call_ref": other}).json()["logged"]  # same words, other call
    client.post("/api/v1/sarvam/post-call", json={"glid": "1001", "call_ref": ref, "call_outcome": "callback_requested", "callback_time": "Monday 5 PM"})

    call = client.get(f"/api/v1/calls/{ref}").json()
    assert call["found"] and call["persona_variables"]["business_name"] == "Alpha Traders"
    assert [s["signal"] for s in call["live_switch_log"]] == ["rush"]
    assert call["outcome"]["callback_time"] == "Monday 5 PM"
    assert {c["call_ref"] for c in client.get("/api/v1/calls", params={"glid": 1001}).json()} >= {ref, other}
    assert client.get("/api/v1/calls/doesnotexist").status_code == 404


def test_unknown_seller_still_gets_call_ref(client):
    pre = client.post("/api/v1/sarvam/pre-call", json={"glid": "424242"}).json()
    assert pre["found"] is False and pre["call_ref"] and pre["variables"]["call_allowed"] == "unknown"
    assert client.get(f"/api/v1/calls/{pre['call_ref']}").json()["found"] is False


def test_admin_ingest_runs_in_background(client):
    import time

    r = client.post("/api/v1/admin/ingest")
    assert r.status_code == 202 and r.json()["status"] == "running"
    for _ in range(100):
        last = client.get("/api/v1/admin/ingestion-status").json()["last_run"]
        if last["status"] != "running":
            break
        time.sleep(0.05)
    assert last["status"] == "finished" and last["failed"] == 0


def test_requests_never_block_on_cold_evidence(ingested, session, monkeypatch):
    import threading
    import time

    from app.services import evidence_service
    from app.services.sarvam_service import precall_variables

    gate = threading.Event()
    real = evidence_service.compute_evidence
    monkeypatch.setattr(evidence_service, "compute_evidence", lambda s: (gate.wait(5), real(s))[1])
    evidence_service.invalidate()
    t = time.perf_counter()
    out = precall_variables(session, 1001, None)  # cache is cold and the compute is held open
    assert time.perf_counter() - t < 1 and out["found"] and out["variables"]["business_name"] == "Alpha Traders"
    assert evidence_service.get_evidence(session, wait=False) == {"available": False, "warming_up": True}
    gate.set()
    for _ in range(100):
        ev = evidence_service.get_evidence(session, wait=False)
        if ev.get("available"):
            break
        time.sleep(0.05)
    assert ev["available"] and not ev.get("stale")


def test_do_not_call_seller_variables_all_say_stop(client):
    v = client.post("/api/v1/sarvam/pre-call", json={"glid": "1003"}).json()["variables"]
    assert v["call_allowed"] == "no" and v["seller_found"] == "yes"
    # Every variable the agent acts on says to stop; none carries a pitch or a package.
    assert "Do not pitch" in v["call_objective"] and "end_interaction" in v["opening_plan"]
    assert "none" in v["recommended_package"] and v["seller_facts"] == "" and v["frustration_threshold"] == "first sign"


def test_low_tolerance_seller_objective_carries_first_sign_close(client):
    v = client.post("/api/v1/sarvam/pre-call", json={"glid": "1002"}).json()["variables"]  # frustrated before
    assert v["frustration_threshold"] == "first sign" and "first sign of irritation" in v["call_objective"]
    v = client.post("/api/v1/sarvam/pre-call", json={"glid": "1001"}).json()["variables"]
    assert v["frustration_threshold"] == "second sign" and "irritation" not in v["call_objective"]


def test_swagger_offers_bearer_authorize(client):
    spec = client.get("/openapi.json").json()
    assert spec["components"]["securitySchemes"]["HTTPBearer"]["scheme"] == "bearer"
    assert spec["paths"]["/api/v1/supplier/persona"]["post"]["security"] == [{"HTTPBearer": []}]


def test_opening_greeting_uses_name_or_general_greeting(client):
    # Known name: "Hello, kya aap <name> se bol rahe hain?" (Hindi). No name: the general English greeting, never a blank name.
    v = client.post("/api/v1/sarvam/pre-call", json={"glid": "1001"}).json()["variables"]
    assert v["opening_greeting"] == "\u0939\u0947\u0932\u094b, \u0915\u094d\u092f\u093e \u0906\u092a Alpha Traders \u0938\u0947 \u092c\u094b\u0932 \u0930\u0939\u0947 \u0939\u0948\u0902?"
    for glid in ("1007", "424242"):  # on record without a name; not on record
        v = client.post("/api/v1/sarvam/pre-call", json={"glid": glid}).json()["variables"]
        assert v["business_name"] == "" and v["opening_greeting"] == (
            "Hello, this is VANI calling from IndiaMART about your business listing. Am I speaking with the business owner?")


def test_root_redirects_to_docs(client):
    r = client.get("/", follow_redirects=False)
    assert r.status_code in (302, 307) and r.headers["location"] == "/docs"
