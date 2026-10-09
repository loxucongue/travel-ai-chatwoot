import logging
import sqlite3
import uuid

from fastapi import FastAPI, Request
from fastapi.exceptions import HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy.exc import OperationalError

from app.api import router
from app.ops_api import router as ops_router
from app.automation_api import router as automation_router
from app.advisor_api import router as advisor_router
from app.config import settings

logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s")

app = FastAPI(title="China2Go AI Operations API", version="0.1.0")
app.add_middleware(CORSMiddleware, allow_origins=settings.origins, allow_credentials=True, allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"], allow_headers=["Content-Type", "X-CSRF-Token", "X-Request-ID"])


@app.middleware("http")
async def request_id(request: Request, call_next):
    value = request.headers.get("X-Request-ID") or f"req_{uuid.uuid4().hex}"
    request.state.request_id = value
    response = await call_next(request)
    response.headers["X-Request-ID"] = value
    return response


@app.exception_handler(HTTPException)
async def http_error(request: Request, exc: HTTPException):
    detail = exc.detail if isinstance(exc.detail, dict) else {"code": "http_error", "message": str(exc.detail)}
    return JSONResponse(status_code=exc.status_code, content={"error": {**detail, "request_id": getattr(request.state, "request_id", "")}})


@app.exception_handler(OperationalError)
async def database_error(request: Request, exc: OperationalError):
    # Handle inside CORS so a database failure is readable by the console,
    # instead of becoming an opaque browser "Failed to fetch" error.
    full = getattr(exc.orig, 'sqlite_errorcode', None) == sqlite3.SQLITE_FULL
    logging.getLogger(__name__).error('Database request failed: %s',
                                     getattr(request.state, 'request_id', ''), exc_info=exc)
    return JSONResponse(status_code=507 if full else 503, content={"error": {
        "code": "storage_full" if full else "database_unavailable",
        "message": "服务器存储空间不足，暂时无法保存，请联系管理员。" if full else "数据库暂时不可用，请稍后重试。",
        "request_id": getattr(request.state, "request_id", ""),
    }})


app.include_router(router)
app.include_router(ops_router)
app.include_router(automation_router)
app.include_router(advisor_router)
