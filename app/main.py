import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse

from app import db
from app.api.routes import router
from app.services import evidence_service
from app.services.supplier_repository import AmbiguousSupplier, SupplierNotFound

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("app")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    db.init_db()
    # Load or compute evidence off the request path; requests never wait for it.
    with db.session_factory()() as s:
        evidence_service.get_evidence(s, wait=False)
    yield


app = FastAPI(
    title="IndiaMART Supplier Voice Persona API",
    version="1.0.0",
    description="Per-seller VANI persona specs, call strategies and Sarvam pre-call variables from the PS07 dataset.",
    lifespan=lifespan,
)
app.include_router(router)


@app.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    """The API has no page of its own; send browsers to the interactive docs."""
    return RedirectResponse(url="/docs")


def _error(status: int, code: str, message: str, details=None) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": {"code": code, "message": message, "details": details}})


@app.exception_handler(SupplierNotFound)
async def not_found(_req: Request, exc: SupplierNotFound):
    return _error(404, "supplier_not_found", f"No supplier matches '{exc.query}'.",
                  {"suggestions": [vars(c) for c in exc.suggestions]})


@app.exception_handler(AmbiguousSupplier)
async def ambiguous(_req: Request, exc: AmbiguousSupplier):
    return _error(409, "supplier_ambiguous", f"'{exc.query}' matches {len(exc.candidates)} suppliers; retry with a glid.",
                  {"candidates": [vars(c) for c in exc.candidates]})


@app.exception_handler(RequestValidationError)
async def invalid(_req: Request, exc: RequestValidationError):
    details = [{"loc": list(e["loc"]), "msg": e["msg"]} for e in exc.errors()]
    return _error(422, "validation_error", "Request validation failed.", details)


@app.exception_handler(HTTPException)
async def http_error(_req: Request, exc: HTTPException):
    code = {403: "forbidden", 404: "not_found", 409: "conflict"}.get(exc.status_code, "http_error")
    return _error(exc.status_code, code, str(exc.detail))


@app.exception_handler(Exception)
async def unhandled(_req: Request, exc: Exception):
    log.exception("unhandled error")
    return _error(500, "internal_error", "Internal server error.")
