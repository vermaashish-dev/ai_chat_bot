from __future__ import annotations

import hmac
import logging
import threading
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import db
from app.config import Settings, get_settings
from app.models import CallOutcome, CallSession, IngestionFile, PersonaSwitchLog
from app.schemas import (
    ErrorResponse,
    DialCheckRequest,
    PersonaRequest,
    PersonaResponse,
    PostCallRequest,
    SarvamPreCallRequest,
    SignalDetectRequest,
    SwitchLogRequest,
)
from app.services import evidence_service
from app.services.ingestion_service import IngestionService
from app.services.llm_client import LLMClient
from app.services.persona_service import PersonaService
from app.services.sarvam_service import dial_check, precall_variables
from app.services.signals import detect_signals
from app.services.supplier_repository import SupplierRepository

router = APIRouter()
_ingest_lock = threading.Lock()
_last_summary: dict | None = None

LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost", "testclient"}
PROXY_HEADERS = ("x-forwarded-for", "forwarded", "x-real-ip", "cf-connecting-ip", "true-client-ip")
ERRORS = {404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}, 403: {"model": ErrorResponse}, 422: {"model": ErrorResponse}}


# ---------------------------------------------------------------- access control

def _is_direct_local(request: Request) -> bool:
    host = request.client.host if request.client else ""
    return host in LOCAL_HOSTS and not any(h in request.headers for h in PROXY_HEADERS)


def _bearer_ok(request: Request, token: str) -> bool:
    auth = request.headers.get("authorization", "")
    return auth.startswith("Bearer ") and hmac.compare_digest(auth[7:].strip(), token)


# Declared only so Swagger UI (/docs) shows an "Authorize" button for the bearer token;
# the checks below read the Authorization header themselves.
_bearer_scheme = HTTPBearer(auto_error=False, description="The API_TOKEN (or ADMIN_TOKEN for /admin) from .env")


def require_admin(request: Request, settings: Settings = Depends(get_settings),
                  _: HTTPAuthorizationCredentials | None = Security(_bearer_scheme)) -> None:
    if settings.admin_token:
        if not _bearer_ok(request, settings.admin_token):
            raise HTTPException(403, "admin token required")
    elif not _is_direct_local(request):
        raise HTTPException(403, "admin endpoints accept direct local requests only (set ADMIN_TOKEN to allow remote access)")


def require_api(request: Request, settings: Settings = Depends(get_settings),
                _: HTTPAuthorizationCredentials | None = Security(_bearer_scheme)) -> None:
    if settings.api_token:
        if not _bearer_ok(request, settings.api_token):
            raise HTTPException(403, "API token required")
    elif not _is_direct_local(request):
        raise HTTPException(403, "set API_TOKEN before exposing the API through a tunnel or proxy")


def get_llm(settings: Settings = Depends(get_settings)) -> LLMClient:
    return LLMClient(settings)


# ---------------------------------------------------------------- health

@router.get("/health", tags=["health"])
def health(session: Session = Depends(db.get_session)) -> dict:
    try:
        db.ping()
        counts = SupplierRepository(session).counts()
        status = "ok" if counts["suppliers"] else "empty"
    except Exception:
        return {"status": "error", "database": "unavailable"}
    return {"status": status, "database": "ok", **counts, "llm_configured": get_settings().llm_configured}


# ---------------------------------------------------------------- persona

@router.post("/api/v1/supplier/persona", response_model=PersonaResponse, responses=ERRORS, tags=["persona"], dependencies=[Depends(require_api)])
def supplier_persona(req: PersonaRequest, session: Session = Depends(db.get_session), llm: LLMClient = Depends(get_llm),
                     settings: Settings = Depends(get_settings)) -> PersonaResponse:
    return PersonaService(session, settings, llm).generate(req)


@router.get("/api/v1/evidence", tags=["persona"], dependencies=[Depends(require_api)])
def evidence(session: Session = Depends(db.get_session)) -> dict:
    return evidence_service.get_evidence(session, wait=False)


@router.post("/api/v1/signals/detect", tags=["persona"], dependencies=[Depends(require_api)])
def signals_detect(req: SignalDetectRequest) -> dict:
    return detect_signals(req.text)


# ---------------------------------------------------------------- Sarvam tools

def _glid(value) -> int | None:
    try:
        return int(str(value).strip()) if value not in (None, "") else None
    except ValueError:
        return None


@router.post("/api/v1/sarvam/pre-call", tags=["sarvam"], dependencies=[Depends(require_api)])
def sarvam_pre_call(req: SarvamPreCallRequest, session: Session = Depends(db.get_session)) -> dict:
    """on_start tool: always 200 so the call proceeds; unknown sellers get a generic persona with seller_found=no."""
    return precall_variables(session, _glid(req.glid), (req.business_name or "").strip() or None)


@router.post("/api/v1/campaign/dial-check", tags=["sarvam"], dependencies=[Depends(require_api)])
def campaign_dial_check(req: DialCheckRequest, session: Session = Depends(db.get_session)) -> dict:
    """Run before creating a Sarvam campaign: dial only `allowed`; `blocked` asked not to be called."""
    return dial_check(session, req.glids)


@router.post("/api/v1/sarvam/switch-log", tags=["sarvam"], dependencies=[Depends(require_api)])
def sarvam_switch_log(req: SwitchLogRequest, session: Session = Depends(db.get_session)) -> dict:
    # The platform sometimes repeats a tool call within seconds; keep one entry per switch.
    # With a call_ref, a retry is the same call + signal + quote (a reworded retry lands within 10 s).
    # Without one (API was unreachable at call start), fall back to seller + time window.
    now = datetime.now()
    call_ref = _clean(req.call_ref)
    scope = PersonaSwitchLog.call_ref == call_ref if call_ref else PersonaSwitchLog.glid == _glid(req.glid)
    dup = session.scalar(
        select(PersonaSwitchLog.id).where(
            scope, PersonaSwitchLog.signal == req.signal,
            (PersonaSwitchLog.seller_quote == req.seller_quote) | (PersonaSwitchLog.created_at >= now - timedelta(seconds=10)),
            PersonaSwitchLog.created_at >= now - timedelta(seconds=60),
        ).limit(1)
    )
    if dup:
        return {"logged": False, "duplicate_of": dup}
    entry = PersonaSwitchLog(glid=_glid(req.glid), call_ref=call_ref, signal=req.signal, change_made=req.change_made,
                             reason=req.reason, seller_quote=req.seller_quote, created_at=now)
    session.add(entry)
    session.commit()
    return {"logged": True, "id": entry.id}


@router.get("/api/v1/switch-log", tags=["sarvam"], dependencies=[Depends(require_api)])
def switch_log(glid: int | None = None, call_ref: str | None = None, limit: int = Query(50, ge=1, le=500),
               session: Session = Depends(db.get_session)) -> list[dict]:
    q = select(PersonaSwitchLog).order_by(PersonaSwitchLog.created_at.desc(), PersonaSwitchLog.id.desc()).limit(limit)
    if glid is not None:
        q = q.where(PersonaSwitchLog.glid == glid)
    if call_ref:
        q = q.where(PersonaSwitchLog.call_ref == call_ref)
    return [
        {"id": e.id, "glid": e.glid, "call_ref": e.call_ref, "signal": e.signal, "change_made": e.change_made,
         "reason": e.reason, "seller_quote": e.seller_quote, "created_at": e.created_at.isoformat()}
        for e in session.scalars(q)
    ]


def _clean(v: str | None) -> str | None:
    v = (v or "").strip()
    return None if not v or (v.startswith("{{") and v.endswith("}}")) else v  # unfilled template -> empty


@router.post("/api/v1/sarvam/post-call", tags=["sarvam"], dependencies=[Depends(require_api)])
def sarvam_post_call(req: PostCallRequest, session: Session = Depends(db.get_session)) -> dict:
    """on_end tool: stores the call outcome and the platform's post-call switch summary."""
    rec = CallOutcome(glid=_glid(req.glid), call_ref=_clean(req.call_ref), call_outcome=_clean(req.call_outcome),
                      meeting_time=_clean(req.meeting_time), callback_time=_clean(req.callback_time),
                      objections_raised=_clean(req.objections_raised), persona_switches=_clean(req.persona_switches),
                      call_summary=_clean(req.call_summary), created_at=datetime.now())
    session.add(rec)
    session.commit()
    return {"stored": True, "id": rec.id}


@router.get("/api/v1/call-outcomes", tags=["sarvam"], dependencies=[Depends(require_api)])
def call_outcomes(glid: int | None = None, limit: int = Query(50, ge=1, le=500), session: Session = Depends(db.get_session)) -> list[dict]:
    q = select(CallOutcome).order_by(CallOutcome.created_at.desc(), CallOutcome.id.desc()).limit(limit)
    if glid is not None:
        q = q.where(CallOutcome.glid == glid)
    cols = ("id", "glid", "call_ref", "call_outcome", "meeting_time", "callback_time", "objections_raised", "persona_switches", "call_summary")
    return [{**{c: getattr(r, c) for c in cols}, "created_at": r.created_at.isoformat()} for r in session.scalars(q)]


def _switch_row(e: PersonaSwitchLog) -> dict:
    return {"signal": e.signal, "change_made": e.change_made, "reason": e.reason, "seller_quote": e.seller_quote,
            "created_at": e.created_at.isoformat()}


@router.get("/api/v1/calls", tags=["sarvam"], dependencies=[Depends(require_api)])
def list_calls(glid: int | None = None, limit: int = Query(50, ge=1, le=500), session: Session = Depends(db.get_session)) -> list[dict]:
    q = select(CallSession).order_by(CallSession.created_at.desc()).limit(limit)
    if glid is not None:
        q = q.where(CallSession.glid == glid)
    return [{"call_ref": c.call_ref, "glid": c.glid, "found": c.found, "segment": c.segment, "created_at": c.created_at.isoformat()}
            for c in session.scalars(q)]


@router.get("/api/v1/calls/{call_ref}", tags=["sarvam"], dependencies=[Depends(require_api)])
def get_call(call_ref: str, session: Session = Depends(db.get_session)) -> dict:
    """One call end to end: persona the agent received, live switch log, and post-call outcome."""
    call = session.get(CallSession, call_ref)
    if call is None:
        raise HTTPException(404, f"no call with call_ref {call_ref}")
    switches = session.scalars(select(PersonaSwitchLog).where(PersonaSwitchLog.call_ref == call_ref).order_by(PersonaSwitchLog.created_at)).all()
    outcome = session.scalar(select(CallOutcome).where(CallOutcome.call_ref == call_ref).order_by(CallOutcome.created_at.desc()).limit(1))
    return {
        "call_ref": call.call_ref, "glid": call.glid, "found": call.found, "segment": call.segment,
        "started_at": call.created_at.isoformat(), "persona_variables": call.variables_json,
        "live_switch_log": [_switch_row(e) for e in switches],
        "outcome": None if outcome is None else {c: getattr(outcome, c) for c in (
            "call_outcome", "meeting_time", "callback_time", "objections_raised", "persona_switches", "call_summary")},
    }


# ---------------------------------------------------------------- admin

@router.post("/api/v1/admin/ingest", tags=["admin"], dependencies=[Depends(require_admin)])
def admin_ingest(response: Response, force: bool = False, wait: bool = False, settings: Settings = Depends(get_settings)) -> dict:
    """A full load takes over a minute, so by default it runs in the background (202); poll
    /admin/ingestion-status. wait=true blocks and returns the counts. Evidence then refreshes in
    the background while requests keep using the previous evidence."""
    global _last_summary
    if not _ingest_lock.acquire(blocking=False):
        raise HTTPException(409, "ingestion already running")
    started = datetime.now().isoformat()

    def run() -> dict:
        global _last_summary
        try:
            summary = IngestionService(settings, db.session_factory()).run(force=force)
            _last_summary = {
                "status": "finished",
                "started_at": summary.started_at.isoformat(),
                "finished_at": summary.finished_at.isoformat() if summary.finished_at else None,
                **summary.totals(),
                "missing_inputs": summary.missing_inputs,
                "aliases_rebuilt": summary.aliases_rebuilt,
                "files": [vars(f) for f in summary.files],
            }
            evidence_service.refresh_in_background()
        except Exception:
            logging.getLogger(__name__).exception("ingestion failed")
            _last_summary = {"status": "failed", "started_at": started, "finished_at": datetime.now().isoformat()}
        finally:
            _ingest_lock.release()
        return _last_summary

    if wait:
        return run()
    _last_summary = {"status": "running", "started_at": started}
    threading.Thread(target=run, name="ingest", daemon=True).start()
    response.status_code = 202
    return _last_summary


@router.get("/api/v1/admin/ingestion-status", tags=["admin"], dependencies=[Depends(require_admin)])
def ingestion_status(session: Session = Depends(db.get_session)) -> dict:
    files = session.scalars(select(IngestionFile).order_by(IngestionFile.file_path)).all()
    return {
        "last_run": _last_summary and {k: v for k, v in _last_summary.items() if k != "files"},
        "files": [
            {"file_path": f.file_path, "file_type": f.file_type, "status": f.status,
             "last_ingested_at": f.last_ingested_at.isoformat() if f.last_ingested_at else None,
             "imported": f.imported, "updated": f.updated, "skipped": f.skipped, "failed": f.failed, "error_summary": f.error_summary}
            for f in files
        ],
    }
