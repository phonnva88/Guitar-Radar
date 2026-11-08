"""Authenticated owner API and static web interface."""
from __future__ import annotations

import base64
import binascii
import hmac
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
import psycopg
from starlette.middleware.trustedhost import TrustedHostMiddleware

from radar.config import Settings
from radar.db import Database, Conflict, NotFound, InvalidInput
from scripts.phase0 import group_handle, safe_group_input, RunError

STATIC = Path(__file__).parent / "static"


class GroupCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    url: str = Field(min_length=1, max_length=300)


class GroupUpdate(BaseModel):
    enabled: bool


class DuplicateConfirmation(BaseModel):
    post_ids: list[int] = Field(min_length=2, max_length=30)


def create_app(settings: Settings, database: Database | None = None) -> FastAPI:
    db = database or Database(settings.database_url)
    app = FastAPI(title="Guitar Radar", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.db = db
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(settings.allowed_hosts))

    @app.middleware("http")
    async def private_app(request: Request, call_next):
        if request.url.path == "/healthz":
            return JSONResponse({"status": "ok"})
        credentials = request.headers.get("authorization", "")
        username, password = "", ""
        try:
            scheme, encoded = credentials.split(" ", 1)
            if scheme.lower() == "basic":
                username, password = base64.b64decode(encoded, validate=True).decode("utf-8").split(":", 1)
        except (ValueError, UnicodeError, binascii.Error):
            pass
        valid_user = hmac.compare_digest(username.encode(), settings.username.encode())
        valid_password = hmac.compare_digest(password.encode(), settings.password.encode())
        if not (valid_user and valid_password):
            return JSONResponse({"detail": "Đăng nhập để sử dụng Guitar Radar."}, status_code=401,
                                headers={"WWW-Authenticate": 'Basic realm="Guitar Radar", charset="UTF-8"'})
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            if request.headers.get("x-radar-request") != "1":
                return JSONResponse({"detail": "Yêu cầu không hợp lệ."}, status_code=403)
            origin = request.headers.get("origin")
            if origin and urlsplit(origin).netloc != request.headers.get("host"):
                return JSONResponse({"detail": "Yêu cầu khác nguồn bị từ chối."}, status_code=403)
            try:
                size = int(request.headers.get("content-length", "0"))
            except ValueError:
                size = 65537
            if size > 65536:
                return JSONResponse({"detail": "Yêu cầu quá lớn."}, status_code=413)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = ("default-src 'self'; script-src 'self'; style-src 'self'; "
                                                     "img-src 'self' https:; connect-src 'self'; "
                                                     "frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        return response

    @app.exception_handler(NotFound)
    async def not_found(request, exc):
        return JSONResponse({"detail": "Không tìm thấy dữ liệu."}, status_code=404)

    @app.exception_handler(Conflict)
    async def conflict(request, exc):
        messages = {
            "This group is already configured": "Group này đã được thêm.",
            "This group is disabled": "Group đang tắt. Bật group trước khi thu thập.",
            "This group already has an active collection": "Group đang có lượt chờ hoặc đang chạy.",
            "Collection is paused; resolve the session/security issue and resume manually":
                "Thu thập đang tạm dừng. Xử lý phiên Facebook rồi bấm tiếp tục.",
        }
        return JSONResponse({"detail": messages.get(str(exc), "Không thể thực hiện thao tác ở trạng thái hiện tại.")}, status_code=409)

    @app.exception_handler(InvalidInput)
    async def invalid_input(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=422)

    @app.exception_handler(psycopg.Error)
    async def db_error(request, exc):
        return JSONResponse({"detail": "Chưa kết nối được cơ sở dữ liệu. Kiểm tra dịch vụ PostgreSQL."}, status_code=503)

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/api/status")
    def status():
        return db.state()

    @app.get("/api/groups")
    def groups():
        return db.list_groups()

    @app.post("/api/groups", status_code=201)
    def create_group(body: GroupCreate):
        name = body.name.strip()
        if not name:
            raise HTTPException(422, "Tên group không được để trống.")
        try:
            group_handle(body.url.strip())
        except RunError:
            raise HTTPException(422, "Nhập group ID hoặc URL Facebook /groups/<id-hoặc-alias>.") from None
        return db.create_group(name, safe_group_input(body.url.strip()))

    @app.patch("/api/groups/{group_id}")
    def update_group(group_id: int, body: GroupUpdate):
        return db.update_group(group_id, body.enabled)

    @app.post("/api/groups/{group_id}/collect", status_code=202)
    def collect(group_id: int):
        return db.enqueue(group_id)

    @app.post("/api/collection/resume")
    def resume():
        db.resume_collection()
        return db.state()

    @app.get("/api/posts")
    def posts(q: str = Query("", max_length=200), group_id: int | None = None,
              start: datetime | None = None, end: datetime | None = None,
              has_images: bool | None = None, page: int = Query(1, ge=1),
              page_size: int = Query(20, ge=1, le=100)):
        if (start and start.tzinfo is None) or (end and end.tzinfo is None):
            raise HTTPException(422, "Thời gian cần có múi giờ.")
        if start and end and start > end:
            raise HTTPException(422, "Ngày bắt đầu phải trước ngày kết thúc.")
        return db.feed(q=q, group_id=group_id, start=start, end=end, has_images=has_images, page=page, page_size=page_size)

    @app.get("/api/runs")
    def runs():
        return db.list_runs(limit=50)

    @app.get("/api/duplicates")
    def duplicates():
        return db.duplicate_candidates()

    @app.post("/api/duplicates/confirm")
    def confirm(body: DuplicateConfirmation):
        if len(set(body.post_ids)) != len(body.post_ids):
            raise HTTPException(422, "Danh sách có ID trùng.")
        return db.confirm_duplicate(body.post_ids)

    app.mount("/static", StaticFiles(directory=STATIC, check_dir=False), name="static")
    return app
