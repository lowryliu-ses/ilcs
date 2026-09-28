"""一个方案分多批执行：父任务是整件事，每个子任务一个批次。

- 方案总数可以超过流程每批样品位，照常审批；建任务时按每批容量拆成子任务（缺省最少批数、均分、并行）
- 批次这一层核对容量：一批放不下就拒绝；按数量拆出的子任务只生成本份那么多样本，序号按全局编号
- 矩阵方案按重复拆，每批都包含全部条件；条件数超过每批样品位时方案不能锁定
- 批次终止、样本不合格留下的短缺：补测或签名放弃之前父任务不能结束，也不能作为下游的运行结束依据
- 父任务出一份合并报告：合并统计、分批明细、批次差异；发布后各子任务算完成
- 按样本计通道的工位：一批 8 颗占满 8 个通道，两批不能同时上柜
"""
import uuid

from test_core_chain_review import CAPACITY, _approve, _session


def _plan(researcher, qa, **body) -> str:
    created = researcher.post("/api/plans", {
        "name": f"分批 {uuid.uuid4().hex[:4]}", "recipe_id": "R-205", "plan_type": "single_condition",
        "required_metrics": [CAPACITY], **body,
    })
    assert created.status_code == 201, created.text
    plan_id = created.json()["id"]
    _approve(researcher, qa, plan_id)
    return plan_id


def _create_task(researcher, plan_id: str, **extra) -> dict:
    created = researcher.post("/api/experiment-tasks", {"plan_id": plan_id, **extra})
    assert created.status_code == 201, created.text
    return researcher.get(f"/api/experiment-tasks/{created.json()['id']}").json()


def _batch(operator, plan_id: str, task_id: str) -> dict:
    created = operator.post("/api/batches", {"plan_id": plan_id, "task_id": task_id})
    assert created.status_code == 201, created.text
    return operator.get(f"/api/batches/{created.json()['id']}").json()


def test_a_plan_over_one_batch_is_approved_and_split_when_the_task_is_created(researcher, qa, operator, reset_runtime):
    plan_id = _plan(researcher, qa, sample_count=20)
    plan = researcher.get(f"/api/plans/{plan_id}").json()
    capacity = next(row for row in plan["checks"] if row["key"] == "capacity")
    assert capacity["ok"] and "分 3 批（7/7/6）" in capacity["detail"], "容量是每批的约束，不是方案的"
    assert plan["batch_plan"]["sizes"] == [7, 7, 6]
    bom = next(row for row in plan["materials"] if row["source"].startswith("流程 BOM"))
    assert bom["source"] == "流程 BOM × 3 批" and bom["qty"] == 3.0, "BOM 是每批的，分 3 批要 3 份"

    preview = researcher.post("/api/experiment-tasks/split-preview", {"plan_id": plan_id}).json()
    assert preview["needs_split"] and [row["size"] for row in preview["parts"]] == [7, 7, 6]
    assert [row["label"] for row in preview["parts"]] == ["第 1–7 号", "第 8–14 号", "第 15–20 号"]
    filled = researcher.post("/api/experiment-tasks/split-preview", {"plan_id": plan_id, "chunk_size": 8}).json()
    assert [row["size"] for row in filled["parts"]] == [8, 8, 4]
    too_big = researcher.post("/api/experiment-tasks/split-preview", {"plan_id": plan_id, "chunk_size": 9}).json()
    assert "超过流程每批样品位" in too_big["error"]

    parent = _create_task(researcher, plan_id, title="20 个扣电充放电")
    children = parent["children"]
    assert [row["planned_count"] for row in children] == [7, 7, 6]
    assert [row["portion"]["offset"] for row in children] == [0, 7, 14]
    assert parent["split_mode"] == "parallel" and all(row["depends_on"] == [] for row in children), \
        "缺省并行：同一台设备一次跑一批是资源约束，交给排程"
    assert parent["progress"]["target"] == 20 and parent["progress"]["pending"] == 20

    refused = operator.post("/api/batches", {"plan_id": plan_id, "task_id": parent["id"]})
    assert refused.status_code == 409 and refused.json()["detail"]["code"] == "task_has_children"
    direct = operator.post("/api/batches", {"plan_id": plan_id})
    assert direct.status_code == 409 and direct.json()["detail"]["code"] == "batch_over_capacity", \
        "不经任务直接建批次：一批放不下就拒绝，不再静默生成 20 个样本"

    second = _batch(operator, plan_id, children[1]["id"])
    assert len(second["samples"]) == 7
    assert sorted(row["repeat"] for row in second["samples"]) == list(range(8, 15)), "第 2 批是第 8–14 号"


def test_split_modes_replicas_and_the_capacity_guard(researcher, qa, reset_runtime):
    plan_id = _plan(researcher, qa, sample_count=20)
    pilot = _create_task(researcher, plan_id, split={"mode": "pilot"})
    first, *rest = pilot["children"]
    for child in rest:
        detail = researcher.get(f"/api/experiment-tasks/{child['id']}").json()
        assert detail["depends_on"] == [first["id"]] and detail["dependency_gate"] == "data_validated", \
            "首批验证：其余等第一批数据复核通过"
    chain = _create_task(researcher, plan_id, split={"mode": "sequential", "chunk_size": 8})
    assert [row["planned_count"] for row in chain["children"]] == [8, 8, 4]
    assert chain["children"][2]["depends_on"] == [chain["children"][1]["id"]]

    small = _plan(researcher, qa, sample_count=6)
    task = _create_task(researcher, small)
    assert task["children"] == [], "一批放得下就不拆"
    oversize = researcher.post(f"/api/experiment-tasks/{task['id']}/decompose", {"chunk_size": 9})
    assert oversize.status_code == 422 and "超过流程每批样品位" in oversize.json()["detail"]["message"]
    replicas = researcher.post(f"/api/experiment-tasks/{task['id']}/decompose", {"parts": 2, "replicate": True})
    assert replicas.status_code == 200, replicas.text
    assert [row["planned_count"] for row in replicas.json()["children"]] == [6, 6], "整体重复：每份按方案整体执行"
    assert [row["portion"]["replica"] for row in replicas.json()["children"]] == [1, 2]


def test_matrix_plans_split_into_complete_blocks(researcher, qa, operator, reset_runtime):
    plan_id = _plan(
        researcher, qa, plan_type="matrix", repeats=10, layout="randomized", seed=7,
        factors=[{"name": "倍率", "unit": "C", "levels": [0.5, 1]}],
    )
    parent = _create_task(researcher, plan_id)
    assert [row["portion"]["repeats"] for row in parent["children"]] == [4, 3, 3]
    assert [row["planned_count"] for row in parent["children"]] == [8, 6, 6]
    samples = _batch(operator, plan_id, parent["children"][1]["id"])["samples"]
    by_group: dict[str, list[int]] = {}
    for row in samples:
        by_group.setdefault(row["condition_group"], []).append(row["repeat"])
    assert {group: sorted(values) for group, values in by_group.items()} == {"C01": [5, 6, 7], "C02": [5, 6, 7]}, \
        "每批都包含全部条件，重复号接着上一批编"

    wide = researcher.post("/api/plans", {
        "name": f"宽矩阵 {uuid.uuid4().hex[:4]}", "recipe_id": "R-205", "plan_type": "matrix", "repeats": 1,
        "factors": [{"name": "A", "levels": [1, 2, 3]}, {"name": "B", "levels": [1, 2, 3, 4]}],
        "required_metrics": [CAPACITY],
    }).json()
    capacity = next(row for row in researcher.get(f"/api/plans/{wide['id']}").json()["checks"] if row["key"] == "capacity")
    assert not capacity["ok"] and "每批放不下全部条件" in capacity["detail"]
    assert researcher.post(f"/api/plans/{wide['id']}/lock").status_code == 409


def test_a_shortfall_must_be_retested_or_signed_off(researcher, qa, operator, reset_runtime):
    from app.models import Batch, Sample

    plan_id = _plan(researcher, qa, sample_count=20)
    parent = _create_task(researcher, plan_id)
    made = operator.post(f"/api/experiment-tasks/{parent['id']}/batches", {})
    assert made.status_code == 201, made.text
    batch_ids = [row["id"] for row in made.json()["batches"]]
    assert len(batch_ids) == 3
    downstream = _create_task(researcher, _plan(researcher, qa, sample_count=2))
    assert researcher.put(
        f"/api/experiment-tasks/{downstream['id']}/dependencies", {"depends_on": [parent["id"]]},
    ).status_code == 200

    # 前两批跑完（第 2 批有 1 颗不合格），第 3 批终止
    with _session() as db:
        for number, batch_id in enumerate(batch_ids):
            db.get(Batch, batch_id).state = "aborted" if number == 2 else "done"
            rows = db.query(Sample).filter(Sample.batch_id == batch_id).order_by(Sample.position).all()
            for row in rows:
                row.state = "done"
            if number == 1:
                rows[0].state = "failed"
        db.commit()

    detail = researcher.get(f"/api/experiment-tasks/{parent['id']}").json()
    assert detail["state"] == "shortfall", "子任务都跑完了，但 20 个里只有 13 个有效"
    progress = detail["progress"]
    assert (progress["target"], progress["valid"], progress["failed"], progress["shortfall"]) == (20, 13, 7, 7)
    blocked = researcher.get(f"/api/experiment-tasks/{downstream['id']}").json()["blocked_by"]
    assert blocked and parent["id"] in blocked[0]["label"], "待补测不算运行结束，下游照样等"

    retest = researcher.post(f"/api/experiment-tasks/{parent['id']}/retest", {})
    assert retest.status_code == 200, retest.text
    extra = [row for row in retest.json()["children"] if row["purpose"] == "retest"]
    assert len(extra) == 1 and extra[0]["planned_count"] == 7 and extra[0]["portion"]["offset"] == 20
    after = researcher.get(f"/api/experiment-tasks/{parent['id']}").json()
    assert after["state"] == "running" and after["progress"]["shortfall"] == 0 and after["progress"]["target"] == 20

    assert researcher.post(f"/api/experiment-tasks/{extra[0]['id']}/cancel", {"reason": "改为签名放弃"}).status_code == 200
    reopened = researcher.get(f"/api/experiment-tasks/{parent['id']}").json()
    assert reopened["state"] == "shortfall"
    assert reopened["progress"]["retest"] == 0, "没下发就取消的补测补不了短缺，不再算「补测中」"

    unsigned = researcher.post(f"/api/experiment-tasks/{parent['id']}/accept-shortfall", {"reason": "样品用完"})
    assert unsigned.status_code == 400
    version = researcher.get(f"/api/experiment-tasks/{parent['id']}").json()["row_version"]
    accepted = researcher.post(f"/api/experiment-tasks/{parent['id']}/accept-shortfall", {
        "reason": "极片用完，按 13 个有效样本出结论",
        "signature_id": researcher.sign("放弃补测", target=parent["id"], object_version=version),
    })
    assert accepted.status_code == 200, accepted.text
    body = accepted.json()
    assert body["progress"]["shortfall"] == 0 and body["shortfall_decisions"][0]["count"] == 7
    assert body["state"] == "reporting", "签名放弃之后按现有结果进入报告阶段"
    assert researcher.get(f"/api/experiment-tasks/{downstream['id']}").json()["blocked_by"] == []

    pooled = researcher.get(f"/api/experiment-tasks/{parent['id']}/results").json()
    assert [row["state"] for row in pooled["batches"]] == ["done", "done", "aborted"], "终止的批次照样列在分批明细里"
    assert pooled["batch_ids"] == batch_ids[:2], "它的数据不进合并统计：样本已经按失败计了"


def test_migrating_a_split_parent_reapportions_the_children_without_batches(researcher, qa, operator, reset_runtime):
    plan_id = _plan(researcher, qa, sample_count=12)
    parent = _create_task(researcher, plan_id)
    first, second = parent["children"]
    assert (first["planned_count"], second["planned_count"]) == (6, 6)
    _batch(operator, plan_id, first["id"])

    def revise(count: int) -> None:
        assert researcher.post(f"/api/plans/{plan_id}/revisions").status_code == 201
        plan = researcher.get(f"/api/plans/{plan_id}").json()
        assert researcher.patch(f"/api/plans/{plan_id}", {"sample_count": count, "row_version": plan["row_version"]}).status_code == 200
        _approve(researcher, qa, plan_id)

    revise(16)
    refused = researcher.post(f"/api/experiment-tasks/{parent['id']}/migrate-version", {"reason": "改做 16 个"})
    assert refused.status_code == 409 and refused.json()["detail"]["code"] == "task_split_mismatch", \
        "已建批次的占了 6 个，剩下 10 个一批放不下：请按新版本重新建任务"
    assert researcher.get(f"/api/experiment-tasks/{second['id']}").json()["plan_version"] == 1, "拒绝时什么都不改"

    revise(14)
    moved = researcher.post(f"/api/experiment-tasks/{parent['id']}/migrate-version", {"reason": "改做 14 个"})
    assert moved.status_code == 200, moved.text
    after = researcher.get(f"/api/experiment-tasks/{second['id']}").json()
    assert after["plan_version"] == 3 and after["planned_count"] == 8 and after["portion"]["offset"] == 6, \
        "还没建批次的子任务按新版本的总数分剩下的"
    assert researcher.get(f"/api/experiment-tasks/{parent['id']}").json()["progress"]["target"] == 14


def _run(operator, batch_id: str, executor) -> None:
    assert operator.post(f"/api/batches/{batch_id}/schedule", {}).status_code == 200
    dispatched = operator.post(
        f"/api/batches/{batch_id}/dispatch",
        {"manual_review": True, "signature_id": operator.sign("批准执行", target=batch_id)},
    )
    assert dispatched.status_code == 200, dispatched.text
    for _ in range(15):
        executor()
    detail = operator.get(f"/api/batches/{batch_id}").json()
    assert detail["state"] == "done", detail["failure_reason"]


def test_the_parent_report_pools_all_batches(researcher, qa, operator, lims, admin, reset_runtime, executor):
    plan_id = _plan(researcher, qa, sample_count=10)
    parent = _create_task(researcher, plan_id)
    assert [row["planned_count"] for row in parent["children"]] == [5, 5]
    made = operator.post(f"/api/experiment-tasks/{parent['id']}/batches", {}).json()["batches"]
    for index, batch in enumerate(made):
        _run(operator, batch["id"], executor)
        samples = operator.get(f"/api/batches/{batch['id']}").json()["samples"]
        # 每个在用样本都要有检测任务，数据复核才算走完
        for number, sample in enumerate(samples):
            task = researcher.post("/api/analysis-tasks", {
                "sample_id": sample["id"], "physical_sample_id": sample["physical_sample_id"],
                "method": "电性能测试", "method_version": "EC-02 v2", "required_metrics": [CAPACITY],
            })
            assert task.status_code == 201, task.text
            ingested = lims.post("/api/integrations/results", {
                "event_id": f"split-{batch['id']}-{number}", "task_id": task.json()["id"],
                "parser_version": "ec-parser 2.1",
                "metrics": [{"metric_version_id": CAPACITY, "value": 200.0 + index * 5 + number, "unit": "mAh/g"}],
            })
            assert ingested.status_code == 200, ingested.text
            value = ingested.json()["results"][0]
            reviewed = qa.post(f"/api/result-values/{value['id']}/review", {
                "conclusion": "approved", "quality": "valid", "reason": "曲线正常",
                "signature_id": qa.sign("复核", target=value["id"], object_version=1),
            })
            assert reviewed.status_code == 200, reviewed.text

    view = researcher.get(f"/api/experiment-tasks/{parent['id']}/results").json()
    block = next(row for row in view["metrics"] if row["metric_id"] == CAPACITY)
    assert block["summary"]["included"] == 10 and block["comparable"] is True
    assert [row["n_included"] for row in block["by_batch"]] == [5, 5]
    assert block["batch_effect"]["df1"] == 1 and block["batch_effect"]["significant"] is True, \
        "第 2 批整体高 5：批次差异要标出来，合并前先确认原因"
    assert len(view["batches"]) == 2 and view["progress"]["target"] == 10

    created = admin.post("/api/reports", {"task_id": parent["id"]})
    assert created.status_code == 201, created.text
    report = created.json()
    content = report["content"]
    assert content["batch_ids"] == [row["id"] for row in made] and content["batch_id"] == ""
    assert "2 批合并" in content["title"] and len(content["batches"]["rows"]) == 2
    assert content["statistics"][0]["included"] == 10
    from app.services.report_pdf import render

    assert render(content).startswith(b"%PDF")
    single = admin.post("/api/reports", {"batch_id": made[0]["id"]}).json()["content"]
    assert "batches" not in single, "单批报告没有「分批情况」"

    version_id = report["id"]
    assert admin.post(f"/api/reports/{version_id}/submit").status_code == 200
    approved = qa.post(f"/api/reports/{version_id}/approve", {
        "conclusion": "approved", "signature_id": qa.sign("批准报告", target=version_id),
    })
    assert approved.status_code == 200, approved.text
    published = qa.post(f"/api/reports/{version_id}/publish", {
        "signature_id": qa.sign("发布报告", target=version_id, object_version=approved.json()["row_version"]),
    })
    assert published.status_code == 200, published.text
    assert len(published.json()["publish_snapshot"]["result_versions"]) == 10

    after = researcher.get(f"/api/experiment-tasks/{parent['id']}").json()
    assert all(row["state"] == "done" for row in after["children"]), "父任务的合并报告覆盖了各子任务的批次"
    assert after["state"] == "done"


def test_per_sample_channels_keep_full_batches_apart(admin, operator, researcher, qa, reset_runtime):
    """ST-07 改成按样本计通道（8 个）：两批 8 颗不能同时上柜，第二批排在第一批之后。"""
    from app.models import Station

    stations = {row["id"]: row for row in admin.get("/api/stations").json()}
    switched = admin.patch("/api/stations/ST-07", {"channel_unit": "sample", "row_version": stations["ST-07"]["row_version"]})
    assert switched.status_code == 200, switched.text
    try:
        plan_id = _plan(researcher, qa, sample_count=8)
        windows = []
        for _ in range(2):
            batch = _batch(operator, plan_id, _create_task(researcher, plan_id)["id"])
            assert operator.post(f"/api/batches/{batch['id']}/schedule", {}).status_code == 200
            detail = operator.get(f"/api/batches/{batch['id']}").json()
            windows.append(next(
                row for row in detail["allocations"] if row["station_id"] == "ST-07" and row["kind"] == "work"
            ))
        assert all(row["units"] == 8 for row in windows), "一批 8 颗占 8 份通道"
        assert windows[1]["starts_at"] >= windows[0]["ends_at"], "8 通道同时只放得下一批"
        busy = admin.patch("/api/stations/ST-07", {
            "channel_unit": "batch", "row_version": admin.get("/api/stations").json()[
                [row["id"] for row in admin.get("/api/stations").json()].index("ST-07")]["row_version"],
        })
        assert busy.status_code == 409 and busy.json()["detail"]["code"] == "station_has_open_allocations"
    finally:
        with _session() as db:
            db.get(Station, "ST-07").channel_unit = "batch"
            db.commit()


def test_assigning_and_accepting_the_parent_carries_the_children(researcher, operator, qa, reset_runtime):
    """分配父任务就是分配整件事：还没分配的子任务一并分配给同一个人；执行人接父任务，子任务一并接单。
    之后补测出来的子任务沿用父任务的执行人。"""
    plan_id = _plan(researcher, qa, sample_count=20)
    parent = _create_task(researcher, plan_id)
    assigned = researcher.post(f"/api/experiment-tasks/{parent['id']}/assign", {
        "assignee_user_id": operator.user["id"], "row_version": parent["row_version"],
    })
    assert assigned.status_code == 200, assigned.text
    children = [researcher.get(f"/api/experiment-tasks/{row['id']}").json() for row in parent["children"]]
    assert all(row["assignee_user_id"] == operator.user["id"] and row["state"] == "pending_accept" for row in children)

    accepted = operator.post(f"/api/experiment-tasks/{parent['id']}/accept")
    assert accepted.status_code == 200, accepted.text
    assert all(row["state"] == "accepted" for row in accepted.json()["children"]), "接父任务就是接整件事"

    from app.models import Batch

    made = operator.post(f"/api/experiment-tasks/{parent['id']}/batches", {}).json()["batches"]
    with _session() as db:
        for batch in made:
            db.get(Batch, batch["id"]).state = "aborted"
        db.commit()
    retest = researcher.post(f"/api/experiment-tasks/{parent['id']}/retest", {}).json()
    extras = [row for row in retest["children"] if row["purpose"] == "retest"]
    assert [row["portion"]["offset"] for row in extras] == [20, 27, 34], "20 个都要补：分 3 个补测子任务，接着第 21 号编"
    extra = extras[0]
    detail = researcher.get(f"/api/experiment-tasks/{extra['id']}").json()
    assert detail["assignee_user_id"] == operator.user["id"] and detail["state"] == "accepted", \
        "补测子任务沿用父任务的执行人"
