"""测试环境：每次会话在独立的 PostgreSQL 测试库上跑真实迁移，再显式播种。

不再用 `create_all`：那会让测试跑在一份「模型直接建出来」的结构上，
而生产库是迁移建出来的。两者一旦漂移，测试就再也发现不了。

默认连 `scripts/dev-db.sh` 拉起的本机容器里的 `ilcs_test` 库；也可以用
`ILCS_TEST_DATABASE_URL` 指到别的 PostgreSQL。会话开始与结束都会 DROP public schema，
所以库名必须以 `_test` 结尾或以 `test_` 开头，防止误指开发库或正式库。
"""
import os
import subprocess
import sys
import uuid
from pathlib import Path

API_DIR = Path(__file__).resolve().parents[1]
TEST_DATABASE_URL = os.environ.get(
    "ILCS_TEST_DATABASE_URL", "postgresql+psycopg2://ilcs:ilcs-dev@127.0.0.1:55432/ilcs_test"
)
from sqlalchemy.engine import make_url  # noqa: E402

_database_name = make_url(TEST_DATABASE_URL).database or ""
if not TEST_DATABASE_URL.startswith("postgresql"):
    raise RuntimeError("ILCS_TEST_DATABASE_URL 必须是 PostgreSQL 连接串")
if not (_database_name.endswith("_test") or _database_name.startswith("test_")):
    raise RuntimeError("ILCS_TEST_DATABASE_URL 只允许指向名称以 _test 结尾或 test_ 开头的数据库")
os.environ["ILCS_DATABASE_URL"] = TEST_DATABASE_URL
os.environ["ILCS_FILE_ROOT"] = str(API_DIR / "test_files")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

ORG = "ORG-001"
ISOLATED_ORG = "ORG-002"
SERVICE_SOURCE = "executor-sim"
SERVICE_SECRET = "ilcs-executor-dev-secret"
LIMS_SOURCE = "lims-ec"
LIMS_SECRET = "ilcs-lims-dev-secret"


def _clean() -> None:
    import shutil

    from sqlalchemy import create_engine, text
    from sqlalchemy.exc import OperationalError

    cleanup_engine = create_engine(TEST_DATABASE_URL)
    try:
        with cleanup_engine.begin() as connection:
            connection.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
            connection.execute(text("CREATE SCHEMA public"))
    except OperationalError as exc:
        pytest.exit(
            f"连不上测试用 PostgreSQL（{make_url(TEST_DATABASE_URL).render_as_string(hide_password=True)}）："
            f"先执行 bash scripts/dev-db.sh 拉起本机开发库，或设置 ILCS_TEST_DATABASE_URL。\n{exc}",
            returncode=2,
        )
    finally:
        cleanup_engine.dispose()
    files = API_DIR / "test_files"
    if files.exists():
        shutil.rmtree(files)


@pytest.fixture(autouse=True)
def no_resident_executor():
    """测试里没有常驻执行器：存活检查默认关闭，由专门用例显式打开。

    改的是运行时配置对象，不设环境变量——环境变量会泄漏进构造 `Settings()` 的配置门禁用例。
    每个用例都取「当前」模块里的 settings：有用例会卸载并重新导入 app.*，之后进程里的
    settings 就换成了新对象，只在会话开始时改一次会漏掉它。
    """
    import sys

    module = sys.modules.get("app.core.config")
    if module is None:
        from app.core import config as module
    settings = module.settings
    original = settings.executor_stale_sec
    settings.executor_stale_sec = 0
    yield
    settings.executor_stale_sec = original


@pytest.fixture(scope="session", autouse=True)
def fresh_database():
    _clean()
    result = subprocess.run(
        [str(API_DIR / ".venv" / "bin" / "alembic"), "upgrade", "head"],
        cwd=API_DIR, capture_output=True, text=True,
        env={**os.environ, "ILCS_DATABASE_URL": TEST_DATABASE_URL},
    )
    assert result.returncode == 0, result.stderr
    from app.core.db import SessionLocal
    from app.seed import seed

    with SessionLocal() as db:
        seed(db, org_id=ORG)
    yield
    _clean()


@pytest.fixture()
def db():
    from app.core.db import SessionLocal

    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture()
def reset_runtime():
    """交班状态：清退上一用例遗留的在途批次，工位与适配器恢复健康。"""
    from app.core.clock import now
    from app.core.db import SessionLocal
    from app.models import (
        Adapter, Alarm, Allocation, Batch, Recipe, ResourceBooking, Station, StepRun, WorkflowEvent,
    )

    with SessionLocal() as session:
        for batch in session.query(Batch).filter(Batch.state.notin_(["done", "aborted"])).all():
            batch.state = "aborted"
            session.query(Allocation).filter(Allocation.batch_id == batch.id).delete()
            session.query(StepRun).filter(StepRun.batch_id == batch.id).update(
                {"state": "cancelled"}, synchronize_session=False
            )
            session.query(WorkflowEvent).filter(WorkflowEvent.batch_id == batch.id).update(
                {"state": "rejected"}, synchronize_session=False
            )
        for station in session.query(Station).all():
            station.status = "idle"
            station.clean = True
        for adapter in session.query(Adapter).all():
            adapter.connected = True
            adapter.site_interlock = False
            adapter.accepts_commands = True
            adapter.last_heartbeat = now()
            adapter.current_command_id = ""
            adapter.supports_hold = True
            adapter.supports_abort = True
            adapter.supports_query = True
            adapter.supports_dedup = True
        for recipe in session.query(Recipe).all():
            recipe.needs_revision = False
        # 上一用例登记的维护 / 人工占用：排程会绕开它们，不清掉就会把后面用例的批次推迟
        session.query(ResourceBooking).filter(ResourceBooking.state.in_(["pending", "confirmed"])).update(
            {"state": "cancelled"}, synchronize_session=False
        )
        # 上一用例制造的失联 / 联锁条件报警：设备已恢复健康，条件随之复位
        session.query(Alarm).filter(Alarm.condition_key.like("station:%")).update(
            {"condition_active": False, "state": "closed"}, synchronize_session=False
        )
        session.commit()
    yield


class Session:
    """测试用的 API 会话封装：登录、签名、带幂等键的写请求。

    幂等键默认自动生成——正式前端对关键写接口一律发稳定键，服务端缺键会拒绝，
    所以测试也必须带上，否则测的就不是真实调用路径。
    """

    def __init__(self, client: TestClient, username: str, password: str = "ilcs1234"):
        self.client = client
        self.counter = 0
        # 每个会话一个唯一前缀：id(self) 会被 CPython 回收复用，
        # 两个用例拿到同一个键会被服务端当成「同键不同内容」而 409
        self.prefix = uuid.uuid4().hex[:12]
        response = client.post("/api/auth/login", json={"username": username, "password": password})
        assert response.status_code == 200, response.text
        payload = response.json()
        self.token = payload["access_token"]
        self.user = payload["user"]
        self.organizations = payload["organizations"]

    @property
    def headers(self) -> dict:
        return {"Authorization": f"Bearer {self.token}"}

    def _key(self) -> str:
        self.counter += 1
        return f"{self.prefix}-{self.counter}"

    def get(self, path: str, **kwargs):
        return self.client.get(path, headers=self.headers, **kwargs)

    def post(self, path: str, json: dict | None = None, idempotency_key: str | None = None):
        headers = dict(self.headers)
        headers["Idempotency-Key"] = idempotency_key or self._key()
        return self.client.post(path, json=json or {}, headers=headers)

    def post_without_key(self, path: str, json: dict | None = None):
        return self.client.post(path, json=json or {}, headers=self.headers)

    def patch(self, path: str, json: dict | None = None):
        return self.client.patch(path, json=json or {}, headers=self.headers)

    def put(self, path: str, json: dict | None = None):
        return self.client.put(path, json=json or {}, headers=self.headers)

    def delete(self, path: str):
        return self.client.delete(path, headers=self.headers)

    def upload(self, path: str, filename: str, content: bytes, media_type: str, **form):
        return self.client.post(
            path, headers=self.headers, files={"file": (filename, content, media_type)}, data=form
        )

    def sign(
        self, meaning: str, action: str = "", target: str = "", password: str = "ilcs1234",
        object_version: int = 0,
    ) -> str:
        response = self.client.post(
            "/api/signatures",
            json={
                "password": password, "meaning": meaning, "action": action, "target": target,
                "object_version": object_version,
            },
            headers=self.headers,
        )
        assert response.status_code == 200, response.text
        return response.json()["signature_id"]


    def sign_recipe(self, meaning: str, recipe_id: str) -> str:
        """方法审批 / 发布按严格模式核对签名：票据必须针对该方法的当前版本。"""
        version = self.get(f"/api/recipes/{recipe_id}").json()["row_version"]
        return self.sign(meaning, target=recipe_id, object_version=version)


class ServiceSession:
    """集成服务会话。设备与回传入口只接受它。"""

    def __init__(self, client: TestClient, source: str = SERVICE_SOURCE, secret: str = SERVICE_SECRET):
        self.client = client
        self.source = source
        self.secret = secret

    @property
    def headers(self) -> dict:
        return {"X-Service-Source": self.source, "X-Service-Secret": self.secret}

    def post(self, path: str, json: dict | None = None):
        return self.client.post(path, json=json or {}, headers=self.headers)

    def get(self, path: str):
        return self.client.get(path, headers=self.headers)


@pytest.fixture(scope="session")
def client(fresh_database):
    from app.main import app

    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def operator(client):
    return Session(client, "operator")


@pytest.fixture()
def qa(client):
    return Session(client, "qa")


@pytest.fixture()
def researcher(client):
    return Session(client, "researcher")


@pytest.fixture()
def admin(client):
    return Session(client, "admin")


@pytest.fixture()
def ehs(client):
    return Session(client, "ehs")


@pytest.fixture()
def device(client):
    return ServiceSession(client)


@pytest.fixture()
def lims(client):
    return ServiceSession(client, LIMS_SOURCE, LIMS_SECRET)


@pytest.fixture()
def executor():
    """执行器一轮。测试里用可控调用代替后台轮询。"""
    from app.core.db import SessionLocal
    from app.services.execution_service import ExecutorLoop

    def run(simulate_heartbeat: bool = True) -> dict:
        with SessionLocal() as session:
            return ExecutorLoop(session).tick(simulate_heartbeat=simulate_heartbeat)

    return run
