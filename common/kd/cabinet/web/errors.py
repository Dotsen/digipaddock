"""Errors of the cabinet's HTTP API: always ``{"error": kind, "message": text}``.

``kind`` is stable for the frontend, which shows its own catalog text (``error.<kind>``); ``message``
is the service's own words for the journal and for developers.
"""

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from kd.cabinet import api

STATUS = {
    "invalid": 400,
    "refused": 403,
    "stepup_required": 403,
    "not_found": 404,
    "conflict": 409,
    "unavailable": 503,
}
DEFAULT_KIND = {
    400: "invalid",
    401: "unauthorized",
    403: "refused",
    404: "not_found",
    405: "invalid",
    409: "conflict",
    410: "expired",
    423: "locked",
    429: "rate_limited",
    503: "unavailable",
}


class Failure(Exception):
    """An HTTP answer with a kind: ``Failure(401, "unauthorized", "wrong code")``."""

    def __init__(self, status: int, kind: str, message: str = ""):
        super().__init__(message or kind)
        self.status, self.kind, self.message = status, kind, message or kind


def body(kind: str, message: str) -> dict:
    return {"error": kind, "message": message}


def install(app: FastAPI):
    @app.exception_handler(Failure)
    async def _failure(request: Request, exc: Failure):
        return JSONResponse(body(exc.kind, exc.message), status_code=exc.status)

    @app.exception_handler(api.ApiError)
    async def _api(request: Request, exc: api.ApiError):
        answer = body(exc.kind, str(exc))
        if exc.reason:  # a refused card action: the page shows flow.reason.<code>
            answer["reason"] = exc.reason
        return JSONResponse(answer, status_code=STATUS.get(exc.kind, 400))

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException):
        kind = DEFAULT_KIND.get(exc.status_code, "invalid")
        return JSONResponse(body(kind, str(exc.detail)), status_code=exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError):
        return JSONResponse(body("invalid", "malformed request"), status_code=400)
