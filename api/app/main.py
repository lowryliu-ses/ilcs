"""组装根。这里只做接线：校验库版本、注册路由、翻译领域异常，不含业务逻辑。"""
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from .api.routers import ROUTERS
from .core.config import settings
from .core.db import engine
from .core.errors import DomainError
from .core.schema import EXPECTED_REVISION, SchemaMismatch, verify

API_VERSION = "2.0.0"

STATE = {"schema_revision": None, "ready": False, "error": "", "error_code": ""}


@asynccontextmanager
async def lifespan(app: FastAPI):
    """启动只校验版本。建表、补列与播种都不在这里——见 scripts/migrate.py。"""
    try:
        issues = settings.production_issues()
        if issues:
            STATE["ready"] = False
            STATE["error_code"] = "configuration_error"
            STATE["error"] = "；".join(issues)
            print(f"启动配置校验失败：{STATE['error']}", flush=True)
            yield
            return
        STATE["schema_revision"] = verify(engine)
        STATE["ready"] = True
        STATE["error"] = ""
        STATE["error_code"] = ""
        print(f"数据库版本 {STATE['schema_revision']}，与应用期待一致", flush=True)
    except SchemaMismatch as exc:
        STATE["ready"] = False
        STATE["error_code"] = "schema_mismatch"
        STATE["error"] = str(exc)
        print(f"启动校验失败：{exc}", flush=True)
    yield


app = FastAPI(title=settings.app_name, version=API_VERSION, lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def require_schema(request: Request, call_next):
    """库版本不兼容时只放行健康检查，业务接口一律 503。

    「拒绝进入可用状态」不能只是日志里一行字：否则不兼容的副本照样接流量，
    在一个自己看不懂的结构上读写。
    """
    if not STATE["ready"] and not request.url.path.startswith("/api/health"):
        return JSONResponse(
            status_code=503,
            content={"detail": {"code": STATE["error_code"] or "not_ready", "message": STATE["error"]}},
        )
    return await call_next(request)


@app.exception_handler(DomainError)
async def domain_error_handler(_: Request, exc: DomainError):
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.as_payload()})


@app.get("/api/health")
def health():
    status = "ok" if STATE["ready"] else (STATE["error_code"] or "not_ready")
    detail = STATE["error"]
    if STATE["ready"]:
        try:
            # 启动成功不代表数据库之后一直可达。编排器要能在连接中断时把实例
            # 摘出流量，而不是只看到一份已经过期的启动状态。
            with engine.connect() as connection:
                connection.execute(text("SELECT 1"))
        except SQLAlchemyError as exc:
            status = "database_unavailable"
            detail = f"数据库连接失败：{exc.__class__.__name__}"
    return {
        "status": status,
        "version": API_VERSION,
        "schema_revision": STATE["schema_revision"],
        "expected_schema_revision": EXPECTED_REVISION,
        "detail": detail,
    }


for router in ROUTERS:
    app.include_router(router, prefix="/api")
