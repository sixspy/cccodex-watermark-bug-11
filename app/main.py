from pathlib import Path

from blacksheep import Application, Content, Cookie, Request, Response
from blacksheep.server.responses import redirect
from itsdangerous import BadSignature, URLSafeSerializer

from app.config import Config
from app.db import init_db
from app.services import (
    POLICY,
    get_batch,
    get_batch_assessment,
    list_adjudications,
    list_batches,
    list_reports,
    list_templates,
    readiness_board,
    reconcile,
    run_gap_report,
    stamp_batch,
    sweep_quarantine,
    trace_downstream,
)
from app.services.errors import ServiceError

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "static"
serializer = URLSafeSerializer(Config.SECRET, salt="cc-watermark-session")

app = Application()
app.serve_files(str(STATIC), root_path="/static")


@app.on_start
async def _on_start(_app) -> None:
    init_db()
    # 启动即执行齐备裁定：历史遗留的“未齐已落印”立即收进候审只读并留痕。
    sweep_quarantine(operator="system")


def _read_session(request: Request) -> dict:
    raw = request.cookies.get("watermark_session")
    if not raw:
        return {}
    try:
        data = serializer.loads(raw)
        return data if isinstance(data, dict) else {}
    except BadSignature:
        return {}


def require_user(request: Request) -> str:
    user = _read_session(request).get("user")
    if user != Config.ADMIN_USER:
        raise ServiceError("未登录", 401, "UNAUTHENTICATED")
    return user


def json_response(data: dict, status: int = 200) -> Response:
    import json

    return Response(
        status,
        content=Content(
            b"application/json",
            json.dumps(data, ensure_ascii=False).encode("utf-8"),
        ),
    )


def html_response(path: Path) -> Response:
    return Response(
        200,
        content=Content(b"text/html; charset=utf-8", path.read_bytes()),
    )


def _error_response(exc: ServiceError) -> Response:
    return json_response(exc.payload(), exc.status_code)


@app.router.get("/")
async def root(request: Request):
    if _read_session(request).get("user") == Config.ADMIN_USER:
        return redirect("/app/")
    return redirect("/login")


@app.router.get("/login")
async def login_page():
    return html_response(STATIC / "login.html")


@app.router.get("/app/")
async def spa_page(request: Request):
    if _read_session(request).get("user") != Config.ADMIN_USER:
        return redirect("/login")
    return html_response(STATIC / "index.html")


@app.router.post("/api/login")
async def api_login(request: Request):
    body = await request.json()
    user = str(body.get("username", "")).strip()
    pwd = str(body.get("password", ""))
    if user != Config.ADMIN_USER or pwd != Config.ADMIN_PASSWORD:
        return json_response(
            {"ok": False, "error": "账号或密码错误", "reason_code": "BAD_CREDENTIALS"}, 400
        )
    resp = json_response({"ok": True, "user": user})
    resp.set_cookie(
        Cookie(
            "watermark_session",
            serializer.dumps({"user": user}),
            http_only=True,
            path="/",
        )
    )
    return resp


@app.router.post("/api/logout")
async def api_logout():
    resp = json_response({"ok": True})
    resp.unset_cookie("watermark_session")
    return resp


@app.router.get("/api/meta")
async def api_meta(request: Request):
    try:
        require_user(request)
    except ServiceError as exc:
        return _error_response(exc)
    return json_response(
        {
            "lab_name": Config.LAB_NAME,
            "product_line": "cc-watermark-lab",
            "chain": ["水印模板", "批次盖印", "下游追索", "缺口对账"],
            "policy": POLICY,
        }
    )


@app.router.get("/api/templates")
async def api_templates(request: Request):
    try:
        require_user(request)
    except ServiceError as exc:
        return _error_response(exc)
    status = request.query.get("status")
    status_val = status[0] if status else None
    return json_response({"items": list_templates(status=status_val)})


@app.router.get("/api/batches")
async def api_batches(request: Request):
    try:
        require_user(request)
    except ServiceError as exc:
        return _error_response(exc)
    status = request.query.get("status")
    status_val = status[0] if status else None
    return json_response({"items": list_batches(status=status_val)})


@app.router.get("/api/batches/{batch_id}")
async def api_batch_detail(request: Request):
    try:
        require_user(request)
    except ServiceError as exc:
        return _error_response(exc)
    batch_id = int(request.route_values["batch_id"])
    batch = get_batch(batch_id)
    if not batch:
        return json_response(
            {"error": "批次不存在", "reason_code": "BATCH_NOT_FOUND"}, 404
        )
    return json_response({"batch": batch, "assessment": get_batch_assessment(batch_id)})


@app.router.post("/api/batches/{batch_id}/stamp")
async def api_stamp(request: Request):
    try:
        user = require_user(request)
        batch_id = int(request.route_values["batch_id"])
        body = await request.json()
        batch = stamp_batch(
            batch_id, str(body.get("watermark_value", "")), operator=user
        )
    except ServiceError as exc:
        return _error_response(exc)
    return json_response(
        {
            "ok": True,
            "batch": batch,
            "assessment": get_batch_assessment(batch_id),
        }
    )


@app.router.get("/api/readiness")
async def api_readiness(request: Request):
    """齐备席 + 靶表附页 + 血缘边目录：同一份裁定。"""
    try:
        require_user(request)
    except ServiceError as exc:
        return _error_response(exc)
    return json_response(readiness_board())


@app.router.post("/api/readiness/sweep")
async def api_readiness_sweep(request: Request):
    """把遗留空边印收进候审只读（齐备席上的命运动作）。"""
    try:
        user = require_user(request)
    except ServiceError as exc:
        return _error_response(exc)
    moved = sweep_quarantine(operator=user)
    return json_response({"ok": True, "moved": moved, "board": readiness_board()})


@app.router.get("/api/adjudications")
async def api_adjudications(request: Request):
    try:
        require_user(request)
    except ServiceError as exc:
        return _error_response(exc)
    return json_response({"items": list_adjudications()})


@app.router.post("/api/trace")
async def api_trace(request: Request):
    try:
        require_user(request)
        body = await request.json()
        result = trace_downstream(str(body.get("watermark_value", "")))
    except ServiceError as exc:
        return _error_response(exc)
    return json_response({"ok": True, **result})


@app.router.get("/api/gap-reports")
async def api_gap_list(request: Request):
    try:
        require_user(request)
    except ServiceError as exc:
        return _error_response(exc)
    return json_response({"items": list_reports()})


@app.router.post("/api/gap-reports")
async def api_gap_run(request: Request):
    try:
        require_user(request)
    except ServiceError as exc:
        return _error_response(exc)
    body = await request.json()
    report = run_gap_report(str(body.get("as_of_date", "2026-11-16")))
    return json_response({"ok": True, "report": report})


@app.router.get("/api/reconcile")
async def api_reconcile(request: Request):
    """二验用：逐批对账明细，可复核空边印是否还在库。"""
    try:
        require_user(request)
    except ServiceError as exc:
        return _error_response(exc)
    day = request.query.get("as_of_date")
    day_val = day[0] if day else "2026-11-16"
    return json_response(reconcile(as_of_date=day_val))
