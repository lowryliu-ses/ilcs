"""幂等记录与业务变更必须共享最终提交点。"""
import pytest


def test_failure_before_idempotency_record_rolls_back_business_and_allows_retry(
    operator, monkeypatch,
):
    """模拟业务已 flush、幂等响应尚未写入时进程出错，不得留下半条样本。"""
    from app.repositories.governance import IdempotencyRepository

    original = IdempotencyRepository.remember
    attempts = 0

    def fail_once(self, *args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("injected crash before idempotency commit")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(IdempotencyRepository, "remember", fail_once)
    key = "atomic-sample-create"
    payload = {
        "id": "PS-IDEMPOTENCY-ATOMIC",
        "barcode": "BC-IDEMPOTENCY-ATOMIC",
        "sample_type": "故障注入样本",
    }

    with pytest.raises(RuntimeError, match="injected crash"):
        operator.post("/api/samples", payload, idempotency_key=key)

    assert operator.get("/api/samples/PS-IDEMPOTENCY-ATOMIC").status_code == 404

    retried = operator.post("/api/samples", payload, idempotency_key=key)
    assert retried.status_code == 201, retried.text
    assert retried.json()["id"] == payload["id"]

    replayed = operator.post("/api/samples", payload, idempotency_key=key)
    assert replayed.status_code == 201
    assert replayed.json() == retried.json()
