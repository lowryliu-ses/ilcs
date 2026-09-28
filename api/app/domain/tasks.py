"""任务树与任务间依赖的纯规则。

- 依赖是「完成—开始」：上游任务的批次运行结束，下游任务的批次才能下发；排程把下游的开工
  放在上游计划结束之后。这里只判断图本身（成环、自依赖），满足与否由服务层按批次状态判。
- 父任务的状态由子任务汇总：全部结束才结束，有任何一个在跑就是执行中；样本有短缺时不能结束。
- 一个方案分多批执行：每批的样本数、矩阵每批的重复次数、拆分方式对应的依赖。
"""
from __future__ import annotations

ORDER = ("unassigned", "pending_accept", "accepted", "running", "data_review", "reporting", "done")
# 上游任务到了这些状态，就算「运行已结束」：数据复核与报告不挡下游开工
RUN_FINISHED = {"data_review", "reporting", "done"}

# 上游怎样才算满足。运行结束、数据复核通过、报告发布放行是三件不同的事：下游只需要上游的样品
# 做完就能开工时用第一种；要用上游经过复核的数据做决定时用第二种；要等上游结论正式放行时用第三种。
GATES: dict[str, tuple[str, frozenset[str]]] = {
    "run_completed": ("运行结束", frozenset(RUN_FINISHED)),
    "data_validated": ("数据复核通过", frozenset({"reporting", "done"})),
    "released": ("报告发布放行", frozenset({"done"})),
}
DEFAULT_GATE = "run_completed"


def gate_label(gate: str) -> str:
    return GATES.get(gate or DEFAULT_GATE, GATES[DEFAULT_GATE])[0]


def gate_satisfied(gate: str, upstream_state: str) -> bool:
    return upstream_state in GATES.get(gate or DEFAULT_GATE, GATES[DEFAULT_GATE])[1]


def dependency_issues(task_id: str, wanted: list[str], edges: dict[str, list[str]]) -> list[str]:
    """给 task_id 设上游 wanted 会不会出问题。`edges` 是现有的「任务 → 它的上游」。"""
    issues: list[str] = []
    if task_id in wanted:
        issues.append("任务不能依赖自己")
    graph = {key: list(value) for key, value in edges.items()}
    graph[task_id] = [ref for ref in wanted if ref != task_id]
    # 从每个上游往上走，走回自己就是环
    for start in graph[task_id]:
        stack, seen = [start], set()
        while stack:
            current = stack.pop()
            if current == task_id:
                issues.append(f"依赖 {start} 会形成环：{start} 已经直接或间接依赖本任务")
                stack = []
                break
            if current in seen:
                continue
            seen.add(current)
            stack.extend(graph.get(current, []))
    return issues


def aggregate(states: list[str]) -> str:
    """父任务状态。全部取消 → 取消；全部结束且至少一个完成 → 完成；有进展 → 执行中；否则取最靠前的。

    某个子树待补测（shortfall）时：其余都跑完了就整体待补测，否则整体还在执行中——不能因为别的子任务
    都完成了就把整件事算完成。
    """
    live = [state for state in states if state != "cancelled"]
    if not states:
        return "unassigned"
    if not live:
        return "cancelled"
    if "shortfall" in live:
        others = [state for state in live if state != "shortfall" and state in ORDER]
        finished = all(ORDER.index(state) >= ORDER.index("data_review") for state in others)
        return "shortfall" if finished else "running"
    if all(state == "done" for state in live):
        return "done"
    if any(ORDER.index(state) >= ORDER.index("running") for state in live if state in ORDER):
        stages = [state for state in live if state in ORDER]
        if all(ORDER.index(state) >= ORDER.index("data_review") for state in stages):
            return min(stages, key=ORDER.index)
        return "running"
    return min((state for state in live if state in ORDER), key=ORDER.index, default="unassigned")


def chunks(items: list[str], size: int) -> list[list[str]]:
    if size <= 0:
        raise ValueError("每份数量必须大于 0")
    return [items[start:start + size] for start in range(0, len(items), size)]


# ---------- 一个方案分多批执行 ----------
#
# 「每批最多几个样本」是流程的属性（样品位），「一共要做多少」是方案的需求，「分成几批、每批几个」由这两者
# 算出来，落在任务树上：父任务是整件事，每个子任务对应一个批次。样品位与工位通道是两种容量——前者限制
# 一批放几个样本，后者限制同一台设备同时跑几份——这里只管前者。

SPLIT_MODES: dict[str, str] = {
    "parallel": "并行（排程按设备决定先后）",
    "pilot": "首批验证后放行其余",
    "sequential": "逐批顺序",
    "replicate": "整体重复执行",
}
DEFAULT_SPLIT_MODE = "parallel"


def batches_needed(total: int, capacity: int) -> int:
    """装下 total 个样本最少要几批。"""
    if capacity <= 0:
        raise ValueError("流程每批样品位必须大于 0")
    return max(1, -(-max(0, total) // capacity))


def balanced(total: int, parts: int) -> list[int]:
    """把 total 尽量均匀地分成 parts 份，多出来的从前往后各加一个：20 分 3 份 → 7、7、6。"""
    if parts <= 0:
        raise ValueError("份数必须大于 0")
    base, extra = divmod(total, parts)
    return [base + (1 if index < extra else 0) for index in range(parts)]


def split_sizes(total: int, capacity: int, *, per_batch: int | None = None, parts: int | None = None) -> list[int]:
    """每批的样本数。

    缺省用最少的批数、各批尽量一样多（20 个、每批最多 8 个 → 7、7、6）：批数一样，但每批样本数接近，
    按批比较和看批次差异才有意义。给了 `per_batch` 就按它装满（→ 8、8、4），给了 `parts` 就均分成这么多份。
    任何一批都不能超过流程样品位。
    """
    if total <= 0:
        raise ValueError("样本数必须大于 0")
    if capacity <= 0:
        raise ValueError("流程每批样品位必须大于 0")
    if per_batch:
        if per_batch > capacity:
            raise ValueError(f"每批 {per_batch} 个超过流程每批样品位 {capacity}")
        sizes = [per_batch] * (total // per_batch) + ([total % per_batch] if total % per_batch else [])
    elif parts:
        if parts > total:
            raise ValueError(f"{total} 个样本分不成 {parts} 份")
        sizes = balanced(total, parts)
        if max(sizes) > capacity:
            raise ValueError(
                f"分成 {parts} 份时每份最多 {max(sizes)} 个，超过流程每批样品位 {capacity}；"
                f"至少要分 {batches_needed(total, capacity)} 份"
            )
    else:
        sizes = balanced(total, batches_needed(total, capacity))
    return sizes


def matrix_blocks(
    conditions: int, repeats: int, capacity: int, *, per_batch: int | None = None, parts: int | None = None,
) -> list[int]:
    """矩阵方案跨批时每批的重复次数：每一批都包含全部条件（完整区组），批内再随机排布。

    按条件拆（「0.5C 一批、1C 一批」）会让因子的影响与批次的影响分不开，所以只按重复拆：2 个条件各
    10 次、每批最多 8 位 → 每批每个条件最多 4 次 → 4、3、3（每批 8、6、6 个样本）。条件数超过每批
    样品位时，一批放不下全部条件，没有完整区组可言，拒绝。`per_batch` 是每批的样本数上限（会向下取整到
    条件数的整数倍），`parts` 是批数。
    """
    if conditions <= 0 or repeats <= 0:
        raise ValueError("矩阵至少要有一个条件、一次重复")
    if conditions > capacity:
        raise ValueError(
            f"{conditions} 个条件超过流程每批样品位 {capacity}：每批放不下全部条件，跨批会让批次与因子混杂；"
            f"请减少条件，或改用每批样品位更多的流程"
        )
    most = capacity // conditions
    if per_batch:
        if per_batch > capacity:
            raise ValueError(f"每批 {per_batch} 个超过流程每批样品位 {capacity}")
        if per_batch < conditions:
            raise ValueError(f"每批 {per_batch} 个放不下全部 {conditions} 个条件")
        each = per_batch // conditions
        return [each] * (repeats // each) + ([repeats % each] if repeats % each else [])
    if parts:
        if parts > repeats:
            raise ValueError(f"{repeats} 次重复分不成 {parts} 批：每批至少要有每个条件的一次重复")
        blocks = balanced(repeats, parts)
        if max(blocks) > most:
            raise ValueError(
                f"分成 {parts} 批时每批每个条件要做 {max(blocks)} 次（{max(blocks) * conditions} 个样本），"
                f"超过流程每批样品位 {capacity}"
            )
        return blocks
    return balanced(repeats, batches_needed(repeats, most))


def offsets(sizes: list[int]) -> list[int]:
    """每一份第一个样本（或重复）之前已经有多少个：全局编号 = 偏移 + 份内序号。"""
    out, running = [], 0
    for size in sizes:
        out.append(running)
        running += size
    return out


def split_dependencies(ids: list[str], mode: str) -> dict[str, tuple[list[str], str]]:
    """拆分方式对应的任务依赖：{子任务: (上游, 放行条件)}。

    「同一台设备一次只能跑一批」是资源约束，排程按通道数本来就会错开，不需要依赖；依赖只表达真正的工艺
    先后。并行：没有依赖。首批验证：其余每批等第一批数据复核通过（首批不合格就不浪费后面的样本）。
    逐批顺序：每批等上一批运行结束——第二批要等第一批整个流程做完才能开工，不能流水线，只在工艺上确实
    有先后时用。
    """
    if mode not in SPLIT_MODES:
        raise ValueError(f"拆分方式只能是 {'、'.join(SPLIT_MODES)}")
    edges: dict[str, tuple[list[str], str]] = {}
    for index, task_id in enumerate(ids):
        if index == 0 or mode in {"parallel", "replicate"}:
            edges[task_id] = ([], DEFAULT_GATE)
        elif mode == "pilot":
            edges[task_id] = ([ids[0]], "data_validated")
        else:
            edges[task_id] = ([ids[index - 1]], DEFAULT_GATE)
    return edges


def portion_count(portion: dict | None, conditions: int = 1) -> int | None:
    """这一份计划做几个样本；没指定份额（按样本清单或方案整体执行）返回 None。"""
    portion = portion or {}
    if portion.get("groups"):
        return sum(int(value or 0) for value in portion["groups"].values())
    if portion.get("repeats"):
        return int(portion["repeats"]) * max(1, conditions)
    if portion.get("count"):
        return int(portion["count"])
    return None


def shortfall(target: int, valid: int, expected: int, accepted: int) -> int:
    """还差几个样本没有着落：计划量 − 有效完成 − 还在做（或待做、补测中）的 − 已签名放弃的。"""
    return max(0, target - valid - expected - accepted)


def topo_order(ids: list[str], upstream: dict[str, list[str]]) -> list[str]:
    """稳定拓扑序：在原顺序的基础上把上游挪到下游前面。成环时原样返回。"""
    position = {value: index for index, value in enumerate(ids)}
    indegree = {value: len([ref for ref in upstream.get(value, []) if ref in position]) for value in ids}
    ready = [value for value in ids if indegree[value] == 0]
    out: list[str] = []
    while ready:
        ready.sort(key=position.get)
        current = ready.pop(0)
        out.append(current)
        for other in ids:
            if current in upstream.get(other, []):
                indegree[other] -= 1
                if indegree[other] == 0:
                    ready.append(other)
    return out if len(out) == len(ids) else ids


def respects(order: tuple[str, ...] | list[str], upstream: dict[str, list[str]]) -> bool:
    seen: set[str] = set()
    members = set(order)
    for value in order:
        if any(ref in members and ref not in seen for ref in upstream.get(value, [])):
            return False
        seen.add(value)
    return True
