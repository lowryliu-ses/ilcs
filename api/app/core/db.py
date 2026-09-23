from collections.abc import Iterator
from decimal import Decimal

from sqlalchemy import Numeric, TypeDecorator, create_engine, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import settings

engine = create_engine(
    settings.database_url,
    pool_pre_ping=True,
    pool_size=settings.db_pool_size,
    max_overflow=settings.db_max_overflow,
    pool_timeout=settings.db_pool_timeout_sec,
    pool_recycle=settings.db_pool_recycle_sec,
)

IDEMPOTENCY_DEFER_COMMIT = "idempotency_defer_commit"
# advisory lock 的业务命名空间（两参数形式的第一个键）
ADVISORY_NAMESPACE = 7317


class ManagedSession(Session):
    """允许 HTTP 幂等边界把领域服务里的 commit 收敛成一次最终提交。

    旧服务为了可独立调用会在用例末尾 `commit()`。幂等写接口若先提交业务、再写
    IdempotencyKey，进程恰好在两者之间退出，重试会重复业务动作。守卫开启本标志后，
    这些 commit 只 flush；幂等记录加入后由 `commit_idempotent()` 一次提交全部数据。
    """

    def commit(self) -> None:
        if self.info.get(IDEMPOTENCY_DEFER_COMMIT):
            self.flush()
            return
        super().commit()

    def begin_idempotent(self) -> None:
        self.info[IDEMPOTENCY_DEFER_COMMIT] = True

    def commit_idempotent(self) -> None:
        try:
            super().commit()
        except Exception:
            self.rollback()
            raise
        finally:
            self.info.pop(IDEMPOTENCY_DEFER_COMMIT, None)

    def abort_idempotent(self) -> None:
        if self.info.pop(IDEMPOTENCY_DEFER_COMMIT, None):
            self.rollback()


SessionLocal = sessionmaker(
    bind=engine, autoflush=False, autocommit=False, class_=ManagedSession
)

QUANTUM = Decimal("0.000001")


class Quantity(TypeDecorator):
    """十进制数量：NUMERIC(18,6)，读写一律是 6 位小数的 Decimal。

    库存账实差额不允许浮点误差，也不设"容差"——差一点就算相等会把真实短少掩盖掉。
    """

    impl = Numeric(18, 6)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        return Decimal(str(value)).quantize(QUANTUM)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return Decimal(str(value)).quantize(QUANTUM)


def dec(value) -> Decimal:
    """把请求里的字符串/数字统一成 6 位小数的 Decimal。"""
    if value is None:
        return Decimal("0").quantize(QUANTUM)
    return Decimal(str(value)).quantize(QUANTUM)


class Base(DeclarativeBase):
    pass


def serialize(db: Session, key: str) -> None:
    """事务级互斥：同一个 key 的写事务排队执行，提交或回滚时自动释放。

    用两参数形式的 advisory xact lock，与幂等守卫的单参数会话锁不共用编号空间。
    """
    db.execute(
        text("SELECT pg_advisory_xact_lock(:namespace, hashtext(:key))"),
        {"namespace": ADVISORY_NAMESPACE, "key": key},
    )


def get_db() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
