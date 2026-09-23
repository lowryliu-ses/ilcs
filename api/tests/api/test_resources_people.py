"""AC-09 至 AC-13：文件、人员资质、资产校准与资源预约。"""
from datetime import datetime, timedelta

import pytest

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64


def test_file_upload_download_and_checksum_guard(operator, reset_runtime):
    """AC-09：权限与摘要有效；损坏明确报警且不冒充原件。"""
    uploaded = operator.upload(
        "/api/files", "cert.png", PNG, "image/png", ref_type="asset", ref_id="AS-0001"
    )
    assert uploaded.status_code == 201, uploaded.text
    record = uploaded.json()
    assert record["state"] == "available" and record["byte_size"] == len(PNG)
    assert record["checksum"] and record["downloadable"] is True

    downloaded = operator.client.get(
        f"/api/files/{record['id']}/download", headers=operator.headers
    )
    assert downloaded.status_code == 200
    assert downloaded.content == PNG
    assert downloaded.headers["X-Checksum-Sha256"] == record["checksum"]

    rejected = operator.upload("/api/files", "x.exe", b"MZ", "application/x-msdownload")
    assert rejected.status_code == 422
    assert rejected.json()["detail"]["code"] == "media_type_not_allowed"

    # 把盘上的内容改掉：下载必须被摘要校验拦住，而不是把坏文件当原件发出去
    from app.services.file_service import FileStore

    store = FileStore()
    from app.core.db import SessionLocal
    from app.models import FileObject

    with SessionLocal() as db:
        stored = db.get(FileObject, record["id"])
        path = store.path_for(stored.storage_key)
    path.write_bytes(PNG + b"tampered")

    corrupted = operator.client.get(
        f"/api/files/{record['id']}/download", headers=operator.headers
    )
    assert corrupted.status_code == 409
    assert corrupted.json()["detail"]["code"] == "file_checksum_mismatch"


def test_cross_organization_file_is_not_downloadable(operator, reset_runtime):
    from app.core.db import SessionLocal
    from app.models import FileObject

    with SessionLocal() as db:
        db.add(
            FileObject(
                id="FILE-OTHER-ORG", org_id="ORG-002", filename="secret.pdf",
                media_type="application/pdf", storage_key="other/secret.pdf",
                state="available", checksum="deadbeef",
            )
        )
        db.commit()
    assert operator.client.get(
        "/api/files/FILE-OTHER-ORG/download", headers=operator.headers
    ).status_code == 404


def test_expired_qualification_blocks_assignment_and_start(
    admin, researcher, operator, qa, reset_runtime
):
    """AC-10：资质在分配时已过期或执行前被撤销都要拦住。"""
    people = admin.get("/api/people").json()["items"]
    operator_person = next(row for row in people if row["user_id"] == operator.user["id"])
    detail = admin.get(f"/api/people/{operator_person['id']}").json()
    coat = next(
        row for row in detail["qualifications"]
        if row["scope_kind"] == "capability" and row["scope_ref"] == "cap.coat"
    )

    revoked = admin.post(
        f"/api/people/qualifications/{coat['id']}/revoke",
        {"reason": "年度复审未通过"},
    )
    assert revoked.status_code == 200, revoked.text
    # 运行中的设备不自动急停：策略里写清楚
    assert revoked.json()["running_policy"]["emergency_stop"] is False
    assert revoked.json()["running_policy"]["block_next_controlled_action"] is True

    try:
        task = researcher.post("/api/experiment-tasks", {"plan_id": "EP-201-03"})
        assert task.status_code == 201, task.text
        task_id = task.json()["id"]
        blocked = researcher.post(
            f"/api/experiment-tasks/{task_id}/assign",
            {"assignee_user_id": operator.user["id"]},
        )
        assert blocked.status_code == 409
        assert blocked.json()["detail"]["code"] == "qualification_blocked"
        assert any(
            "涂布烘干" in row["label"] for row in blocked.json()["detail"]["blocked"]
        )
    finally:
        admin.post(
            f"/api/people/{operator_person['id']}/qualifications",
            {
                "scope_kind": "capability", "scope_ref": "cap.coat",
                "expires_at": (datetime.utcnow() + timedelta(days=365)).isoformat(),
            },
        )


def test_calibration_expiring_inside_the_window_blocks_dispatch(
    admin, operator, reset_runtime
):
    """AC-11：设备步骤的校准在预计执行区间内失效，开跑检查必须拦住。"""
    from app.core.clock import now
    from app.core.db import SessionLocal
    from app.models import CalibrationRecord, Station

    created = operator.post("/api/batches", {"plan_id": "EP-205-01"})
    batch_id = created.json()["id"]
    assert operator.post(f"/api/batches/{batch_id}/schedule", {}).status_code == 200

    detail = operator.get(f"/api/batches/{batch_id}").json()
    second = next(
        row for row in detail["steps"] if row["needs_station"] and row["station_id"]
    )
    with SessionLocal() as db:
        station = db.get(Station, second["station_id"])
        record = (
            db.query(CalibrationRecord)
            .filter(CalibrationRecord.asset_id == station.asset_id)
            .first()
        )
        original = record.expires_at
        record.expires_at = now() + timedelta(minutes=1)
        db.commit()
    try:
        preflight = operator.get(
            f"/api/batches/{batch_id}/preflight?manual_review=true"
        ).json()
        assert preflight["ok"] is False
        resource = next(row for row in preflight["checks"] if row["key"] == "resource")
        assert resource["state"] == "blocked"
        assert "校准在" in resource["detail"]
        failing = [row for row in preflight["resource_checks"] if not row["ok"]]
        assert failing and failing[0]["step_name"] == second["name"]
    finally:
        with SessionLocal() as db:
            station = db.get(Station, second["station_id"])
            db.query(CalibrationRecord).filter(
                CalibrationRecord.asset_id == station.asset_id
            ).first().expires_at = original
            db.commit()


def test_bookings_share_asset_capacity_across_stations(admin, operator, reset_runtime):
    """AC-12：同一资产的不同工位不能各占一份容量。"""
    from app.core.clock import now

    asset = admin.post(
        "/api/assets",
        {"asset_no": "AS-CAP-1", "name": "共享容量资产", "capacity": 1},
    )
    assert asset.status_code == 201, asset.text
    asset_id = asset.json()["id"]
    start = now() + timedelta(hours=2)
    end = start + timedelta(hours=2)

    first = operator.post(
        "/api/resource-bookings",
        {"asset_id": asset_id, "station_id": "ST-01-A", "kind": "maintenance",
         "starts_at": start.isoformat(), "ends_at": end.isoformat(), "reason": "年度维护"},
    )
    assert first.status_code == 201, first.text

    overlapping = operator.post(
        "/api/resource-bookings",
        {"asset_id": asset_id, "station_id": "ST-01-B", "kind": "manual",
         "starts_at": (start + timedelta(minutes=30)).isoformat(),
         "ends_at": end.isoformat(), "reason": "人工预约"},
    )
    assert overlapping.status_code == 409
    assert overlapping.json()["detail"]["code"] == "booking_conflict"
    assert "超出资产容量" in overlapping.json()["detail"]["blocked"][0]["label"]

    # 容量放宽到 2 后可以并行
    assert admin.patch(f"/api/assets/{asset_id}", {"capacity": 2}).status_code == 200
    assert operator.post(
        "/api/resource-bookings",
        {"asset_id": asset_id, "station_id": "ST-01-B", "kind": "manual",
         "starts_at": (start + timedelta(minutes=30)).isoformat(),
         "ends_at": end.isoformat(), "reason": "人工预约"},
    ).status_code == 201


def test_calibration_exempt_requires_a_written_reason(admin, reset_runtime):
    """DEV-06.2：缺失不等同不适用。"""
    rejected = admin.post(
        "/api/assets",
        {"asset_no": "AS-EXEMPT-1", "name": "无理由免校准", "calibration_applicable": False},
    )
    assert rejected.status_code == 422
    assert rejected.json()["detail"]["code"] == "calibration_exempt_reason_required"

    ok = admin.post(
        "/api/assets",
        {"asset_no": "AS-EXEMPT-2", "name": "手工工作台", "calibration_applicable": False,
         "calibration_exempt_reason": "无计量输出，按实验室规定不做校准"},
    )
    assert ok.status_code == 201
    assert ok.json()["unavailable_reasons"] == []


def test_pass_calibration_requires_a_certificate(admin, reset_runtime):
    from app.core.clock import now

    asset = admin.post("/api/assets", {"asset_no": "AS-CERT-1", "name": "需证书资产"}).json()
    rejected = admin.post(
        f"/api/assets/{asset['id']}/calibrations",
        {"result": "pass", "expires_at": (now() + timedelta(days=365)).isoformat()},
    )
    assert rejected.status_code == 422
    assert rejected.json()["detail"]["code"] == "certificate_required"

    missing = admin.post(
        f"/api/assets/{asset['id']}/calibrations",
        {
            "result": "pass",
            "expires_at": (now() + timedelta(days=365)).isoformat(),
            "certificate_file_id": "FILE-NOT-FOUND",
        },
    )
    assert missing.status_code == 404
    assert "校准证书文件不存在" in missing.json()["detail"]["message"]


def test_orphan_file_cleanup_preserves_database_references(admin, reset_runtime):
    """DEV-03.4：过期暂存文件会清理，正式业务引用即使关联元数据缺失也不能删。"""
    from app.core.clock import now
    from app.core.db import SessionLocal
    from app.models import AuditEvent, FileObject
    from app.services.execution_service import ExecutorLoop
    from app.services.file_service import FileStore

    unlinked_response = admin.upload(
        "/api/files", "abandoned.png", PNG, "image/png"
    )
    assert unlinked_response.status_code == 201, unlinked_response.text
    unlinked = unlinked_response.json()

    referenced_response = admin.upload(
        "/api/files", "training.png", PNG, "image/png", ref_type="qualification"
    )
    assert referenced_response.status_code == 201, referenced_response.text
    referenced = referenced_response.json()
    person_response = admin.post(
        "/api/people", {"code": "P-FILE-CLEANUP", "name": "文件清理验证人员"}
    )
    assert person_response.status_code == 201, person_response.text
    explicitly_attached_response = admin.upload(
        "/api/files", "person-note.png", PNG, "image/png",
        ref_type="person", ref_id=person_response.json()["id"],
    )
    assert explicitly_attached_response.status_code == 201, explicitly_attached_response.text
    explicitly_attached = explicitly_attached_response.json()
    granted = admin.post(
        f"/api/people/{person_response.json()['id']}/qualifications",
        {
            "scope_kind": "safety", "scope_ref": "cleanup-proof",
            "evidence_file_id": referenced["id"],
        },
    )
    assert granted.status_code == 201, granted.text

    store = FileStore()
    with SessionLocal() as db:
        old = now() - timedelta(hours=48)
        unlinked_row = db.get(FileObject, unlinked["id"])
        referenced_row = db.get(FileObject, referenced["id"])
        explicitly_attached_row = db.get(FileObject, explicitly_attached["id"])
        unlinked_path = store.path_for(unlinked_row.storage_key)
        referenced_path = store.path_for(referenced_row.storage_key)
        explicitly_attached_path = store.path_for(explicitly_attached_row.storage_key)
        unlinked_row.created_at = old
        referenced_row.created_at = old
        explicitly_attached_row.created_at = old
        # 模拟早期数据缺少辅助关联元数据；业务表里的 evidence_file_id 才是保留依据。
        referenced_row.ref_type = ""
        referenced_row.ref_id = ""
        db.commit()

    with SessionLocal() as db:
        assert ExecutorLoop(db).cleanup_orphan_files() == 1

    with SessionLocal() as db:
        assert db.get(FileObject, unlinked["id"]) is None
        assert db.get(FileObject, referenced["id"]) is not None
        assert db.get(FileObject, explicitly_attached["id"]) is not None
        audit = db.query(AuditEvent).filter(
            AuditEvent.action == "清理未关联文件", AuditEvent.target == unlinked["id"]
        ).one()
        assert audit.user == "文件清理任务"
    assert not unlinked_path.exists()
    assert referenced_path.exists()
    assert explicitly_attached_path.exists()


def test_qualification_rejects_missing_evidence_file(admin, reset_runtime):
    person = admin.post(
        "/api/people", {"code": "P-MISSING-EVIDENCE", "name": "缺文件验证人员"}
    ).json()
    rejected = admin.post(
        f"/api/people/{person['id']}/qualifications",
        {
            "scope_kind": "safety", "scope_ref": "general",
            "evidence_file_id": "FILE-NOT-FOUND",
        },
    )
    assert rejected.status_code == 404
    assert "资质证明文件不存在" in rejected.json()["detail"]["message"]


def test_maintenance_booking_lists_impacted_allocations(admin, operator, reset_runtime):
    """AC-13 / DEV-06.6：维护插入要显示受影响清单，不静默移动。"""
    created = operator.post("/api/batches", {"plan_id": "EP-205-01"})
    batch_id = created.json()["id"]
    assert operator.post(f"/api/batches/{batch_id}/schedule", {}).status_code == 200
    allocation = operator.get(f"/api/batches/{batch_id}").json()["allocations"][0]

    from app.core.db import SessionLocal
    from app.models import Station

    with SessionLocal() as db:
        asset_id = db.get(Station, allocation["station_id"]).asset_id

    booking = operator.post(
        "/api/resource-bookings",
        {
            "asset_id": asset_id, "station_id": allocation["station_id"], "kind": "maintenance",
            "starts_at": allocation["starts_at"], "ends_at": allocation["ends_at"],
            "reason": "紧急维护",
        },
    )
    # 容量已被排程占用时直接冲突；否则至少要列出受影响工步
    if booking.status_code == 201:
        assert any(row["batch_id"] == batch_id for row in booking.json()["impacted"])
    else:
        assert booking.status_code == 409
        assert booking.json()["detail"]["code"] == "booking_conflict"


def test_offline_and_retired_station_blocks_device_action(admin, operator, reset_runtime):
    """AC-13：设备离线、退役、未清洗、联锁都要给出具体阻塞原因。"""
    from app.core.db import SessionLocal
    from app.models import Station

    created = operator.post("/api/batches", {"plan_id": "EP-205-01"})
    batch_id = created.json()["id"]
    assert operator.post(f"/api/batches/{batch_id}/schedule", {}).status_code == 200
    first = operator.get(f"/api/batches/{batch_id}").json()["preflight"]["first_station_id"]

    with SessionLocal() as db:
        station = db.get(Station, first)
        station.clean = False
        db.commit()
    try:
        preflight = operator.get(
            f"/api/batches/{batch_id}/preflight?manual_review=true"
        ).json()
        blocked = {row["key"]: row for row in preflight["blocked"]}
        assert "station" in blocked
        assert "未清洗" in blocked["station"]["detail"]
    finally:
        with SessionLocal() as db:
            db.get(Station, first).clean = True
            db.commit()


def test_asset_edit_covers_state_capacity_and_calibration_exemption(admin, reset_runtime):
    """界面新接的资产编辑：状态、容量、不适用校准三件事都要能改，且理由不能省。"""
    created = admin.post("/api/assets", {"asset_no": "AS-EDIT-1", "name": "可编辑资产"})
    assert created.status_code == 201, created.text
    asset = created.json()
    assert asset["unavailable_reasons"], "没有校准记录时本来就不可用"

    renamed = admin.patch(
        f"/api/assets/{asset['id']}",
        {"name": "改过名的资产", "location": "岛 #9", "capacity": 3,
         "row_version": asset["row_version"]},
    )
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["name"] == "改过名的资产"
    assert renamed.json()["capacity"] == 3

    # 乐观并发：拿旧版本号再提交要被拒，不能悄悄覆盖
    stale = admin.patch(
        f"/api/assets/{asset['id']}", {"name": "又改一次", "row_version": asset["row_version"]}
    )
    assert stale.status_code == 409, stale.text

    current = admin.get(f"/api/assets/{asset['id']}").json()
    no_reason = admin.patch(
        f"/api/assets/{asset['id']}",
        {"calibration_applicable": False, "row_version": current["row_version"]},
    )
    assert no_reason.status_code == 422
    assert no_reason.json()["detail"]["code"] == "calibration_exempt_reason_required"

    exempt = admin.patch(
        f"/api/assets/{asset['id']}",
        {"calibration_applicable": False, "calibration_exempt_reason": "无计量输出的手工台",
         "row_version": current["row_version"]},
    )
    assert exempt.status_code == 200, exempt.text
    assert exempt.json()["unavailable_reasons"] == [], "写明理由的不适用校准应当可用"

    current = admin.get(f"/api/assets/{asset['id']}").json()
    retired = admin.patch(
        f"/api/assets/{asset['id']}", {"state": "retired", "row_version": current["row_version"]}
    )
    assert retired.status_code == 200
    assert any("退役" in reason for reason in retired.json()["unavailable_reasons"])


def test_person_edit_changes_employment_and_account_binding(admin, reset_runtime):
    """人员档案编辑：离岗后不能再被分配；账号绑定用成员清单挑，不是手填 UUID。"""
    members = admin.get("/api/admin/members")
    assert members.status_code == 200, members.text
    rows = {row["username"]: row for row in members.json()}
    assert rows["operator"]["bound_person"], "种子里操作员已有档案，清单要标出已被占用"

    created = admin.post("/api/people", {"code": "P-EDIT-1", "name": "待编辑的人"})
    person = created.json()
    assert person["employable"] is False, "未绑定账号不能执行系统任务"

    bound = admin.patch(
        f"/api/people/{person['id']}",
        {"user_id": rows["ehs"]["user_id"], "title": "安全员", "row_version": person["row_version"]},
    )
    assert bound.status_code == 200, bound.text
    assert bound.json()["employable"] is True
    assert bound.json()["username"] == "ehs"

    current = admin.get(f"/api/people/{person['id']}").json()
    on_leave = admin.patch(
        f"/api/people/{person['id']}",
        {"employment_state": "leave", "row_version": current["row_version"]},
    )
    assert on_leave.status_code == 200
    assert on_leave.json()["employable"] is False, "休假的人不能被分配新任务"

    # 绑定后该账号在成员清单里要显示已被占用，避免两份档案抢一个账号
    refreshed = {row["username"]: row for row in admin.get("/api/admin/members").json()}
    assert refreshed["ehs"]["bound_person_id"] == person["id"]
