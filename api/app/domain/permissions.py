"""动作权限矩阵。角色 → 动作权限；组织范围、资质和职责分离在服务端另行校验。

`PERMISSIONS` 是出厂默认值。组织管理员可以在「系统治理」里按角色改矩阵（存库、版本化、
签名留痕）；没改过的组织沿用默认值。系统管理员角色恒有全部权限，矩阵里改不动它。
一个账号可以同时挂多个角色，有效权限取各角色的并集。

这张表只回答「这个角色能不能发起这个动作」。它不回答：
- 对象在不在当前组织范围内（`domain/access.py` 与 ScopedRepository）
- 执行人有没有资质（`services/people_service.py`）
- 是不是在审自己录入的东西（`domain/access.same_person`）
管理员有相应动作权限，但一样不得绕过「本人不能审批本人」的限制。
"""

PERMISSIONS: dict[str, list[str]] = {
    # ---------- 方法与方案 ----------
    "recipe.edit": ["researcher", "admin"],
    "recipe.submit": ["researcher", "admin"],
    "recipe.approve": ["qa", "admin"],
    "recipe.release": ["qa", "admin"],
    "method.edit": ["researcher", "admin"],
    "method.release": ["qa", "admin"],
    "plan.edit": ["researcher", "admin"],
    "plan.submit": ["researcher", "admin"],
    "plan.approve": ["qa", "admin"],
    # ---------- 任务与执行 ----------
    "task.create": ["researcher", "admin"],
    "task.assign": ["researcher", "admin"],
    "task.accept": ["operator", "researcher", "admin"],
    "task.cancel": ["researcher", "admin"],
    "batch.create": ["operator", "admin"],
    "batch.schedule": ["operator", "admin"],
    "batch.control": ["operator", "admin"],
    "batch.recover": ["operator", "admin"],
    "labware.move": ["operator", "admin"],
    "step.submit": ["operator", "researcher", "admin"],
    "step.review": ["qa", "admin"],
    "batch.signal": ["operator", "researcher", "admin"],
    # ---------- 样本 ----------
    "sample.register": ["operator", "researcher", "admin"],
    "sample.transfer": ["operator", "researcher", "admin"],
    "sample.dispose": ["operator", "admin"],
    # ---------- 物料与库存 ----------
    "material.edit": ["ehs", "operator", "admin"],
    "material.release": ["qa", "ehs", "admin"],
    "inventory.post": ["operator", "researcher", "admin"],
    "inventory.reverse": ["qa", "admin"],
    # ---------- 资源 ----------
    "station.edit": ["admin"],
    "location.edit": ["admin"],
    "asset.edit": ["admin"],
    "booking.edit": ["operator", "admin"],
    "maintenance.edit": ["operator", "admin"],
    # ---------- 人员与资质 ----------
    "person.edit": ["admin"],
    "qualification.edit": ["admin", "ehs"],
    # ---------- 数据与审核 ----------
    "metric.edit": ["researcher", "admin"],
    "analysis.create": ["researcher", "operator", "admin"],
    "result.enter": ["researcher", "operator", "admin"],
    "result.review": ["qa", "admin"],
    "result.flag": ["researcher", "qa", "admin"],
    # ---------- SOP、报告与治理 ----------
    "sop.edit": ["researcher", "qa", "admin"],
    "sop.approve": ["qa", "admin"],
    "report.edit": ["researcher", "admin"],
    "report.submit": ["researcher", "admin"],
    "report.approve": ["qa", "admin"],
    "report.publish": ["qa", "admin"],
    "file.upload": ["researcher", "operator", "qa", "ehs", "admin"],
    "alarm.ack": ["operator", "ehs", "admin"],
    "alarm.shelve": ["ehs", "admin"],
    "alarm.close": ["operator", "ehs", "admin"],
    "exception.handle": ["operator", "qa", "admin"],
    "exception.rules": ["admin"],
    "golden.set": ["qa", "admin"],
    "org.admin": ["admin"],
    "service.manage": ["admin"],
    "integration.manage": ["admin"],
}

ADMIN = "admin"
# 可分配给账号的角色（设备事件、后台推进器是系统主体，不是账号角色）
ASSIGNABLE_ROLES = ("researcher", "qa", "operator", "ehs", "admin")

ROLE_NAMES = {
    "researcher": "研究员",
    "qa": "QA 负责人",
    "operator": "操作员",
    "ehs": "EHS 专员",
    "admin": "系统管理员",
    "device": "设备事件",
    "system": "后台推进器",
}

# 中文动作名，给「当前角色无权限：xxx」这类提示用
ACTION_NAMES = {
    "plan.approve": "批准实验方案",
    "step.review": "审核人工步骤",
    "result.review": "复核检测结果",
    "report.approve": "批准报告",
    "report.publish": "发布报告",
    "sop.approve": "批准 SOP",
    "inventory.post": "库存入账",
    "qualification.edit": "维护资质",
}


# 权限目录：界面上按分组展示的中文名。新增权限必须在这里登记，测试会核对两边一致。
PERMISSION_CATALOG: list[tuple[str, list[tuple[str, str]]]] = [
    ("方法与方案", [
        ("recipe.edit", "编辑方法"), ("recipe.submit", "提交方法评审"), ("recipe.approve", "批准方法"),
        ("recipe.release", "发布方法"), ("method.edit", "起草与修订设备方法"), ("method.release", "发布与退役设备方法"),
        ("plan.edit", "编辑实验方案"), ("plan.submit", "提交方案评审"),
        ("plan.approve", "批准实验方案"),
    ]),
    ("任务与执行", [
        ("task.create", "建立实验任务"), ("task.assign", "分配任务"), ("task.accept", "接单"),
        ("task.cancel", "取消任务"), ("batch.create", "建立批次"), ("batch.schedule", "排程"),
        ("batch.control", "下发与保持 / 终止批次"), ("batch.recover", "异常恢复与现场核查"),
        ("labware.move", "载具登记、绑定与扫码放置"),
        ("step.submit", "提交人工步骤记录"), ("step.review", "流程审核与质检判定"),
        ("batch.signal", "发出批次业务事件"),
    ]),
    ("样本", [
        ("sample.register", "登记样本"), ("sample.transfer", "样本转移"), ("sample.dispose", "样本处置"),
    ]),
    ("物料与库存", [
        ("material.edit", "维护物料与批号"), ("material.release", "物料放行"),
        ("inventory.post", "库存入账"), ("inventory.reverse", "库存冲销"),
    ]),
    ("资源", [
        ("station.edit", "维护工位与适配器"), ("location.edit", "维护放置位与板库"),
        ("asset.edit", "维护仪器设备与校准"),
        ("booking.edit", "资源预约"), ("maintenance.edit", "维护工单"),
    ]),
    ("人员与资质", [("person.edit", "维护人员档案"), ("qualification.edit", "维护资质")]),
    ("数据与审核", [
        ("metric.edit", "维护指标定义"), ("analysis.create", "建立检测任务"), ("result.enter", "录入检测结果"),
        ("result.review", "复核检测结果"), ("result.flag", "标记可疑结果"),
    ]),
    ("SOP、报告与报警", [
        ("sop.edit", "编辑 SOP"), ("sop.approve", "批准 SOP"), ("report.edit", "编辑报告"),
        ("report.submit", "提交报告审核"), ("report.approve", "批准报告"), ("report.publish", "发布报告"),
        ("file.upload", "上传附件"), ("alarm.ack", "确认报警"), ("alarm.shelve", "搁置报警"),
        ("alarm.close", "关闭报警"), ("golden.set", "设定金标批次"),
        ("exception.handle", "处理异常事件"), ("exception.rules", "维护异常策略库"),
    ]),
    ("系统治理", [
        ("org.admin", "账号、角色与权限管理"), ("service.manage", "服务身份管理"),
        ("integration.manage", "出向事件订阅（Webhook）管理"),
    ]),
]
PERMISSION_LABELS = {key: label for _, rows in PERMISSION_CATALOG for key, label in rows}
# 只能由系统管理员持有：放给其他角色就等于把「谁能授权」也放出去了
ADMIN_ONLY = frozenset({"org.admin", "service.manage"})


def default_matrix() -> dict[str, list[str]]:
    """出厂默认的角色权限矩阵（不含系统管理员，它恒为全集）。"""
    return {
        role: sorted(name for name, roles in PERMISSIONS.items() if role in roles)
        for role in ASSIGNABLE_ROLES if role != ADMIN
    }


def normalize_matrix(raw: dict) -> tuple[dict[str, list[str]], list[str]]:
    """校验管理员提交的矩阵。返回（规范化矩阵, 问题清单）；有问题时矩阵不应保存。"""
    issues: list[str] = []
    matrix: dict[str, list[str]] = {}
    for role, perms in (raw or {}).items():
        if role == ADMIN:
            issues.append("系统管理员恒有全部权限，不能在矩阵里修改")
            continue
        if role not in ASSIGNABLE_ROLES:
            issues.append(f"未知角色 {role}")
            continue
        unknown = sorted(set(perms or []) - set(PERMISSIONS))
        if unknown:
            issues.append(f"{ROLE_NAMES.get(role, role)}：未知权限 {', '.join(unknown)}")
        reserved = sorted(set(perms or []) & ADMIN_ONLY)
        if reserved:
            issues.append(f"{ROLE_NAMES.get(role, role)}：{', '.join(reserved)} 只能由系统管理员持有")
        matrix[role] = sorted(set(perms or []) & set(PERMISSIONS) - ADMIN_ONLY)
    for role in default_matrix():
        matrix.setdefault(role, [])
    return matrix, issues


def effective_permissions(roles, matrix: dict[str, list[str]] | None = None) -> list[str]:
    """账号的有效权限：各角色权限的并集。系统管理员恒为全集。"""
    roles = set(roles or [])
    if ADMIN in roles:
        return sorted(PERMISSIONS)
    source = matrix if matrix is not None else default_matrix()
    granted: set[str] = set()
    for role in roles:
        granted.update(source.get(role, []))
    return sorted(granted & set(PERMISSIONS))


def can(role: str, permission: str) -> bool:
    """出厂默认矩阵下单个角色的判断。服务层应改用 `AccessContext.has`（按组织矩阵与多角色）。"""
    return permission in effective_permissions([role])


def permissions_of(role: str) -> list[str]:
    return effective_permissions([role])


def roles_label(roles) -> str:
    return "、".join(ROLE_NAMES.get(role, role) for role in roles)


def action_name(permission: str) -> str:
    return ACTION_NAMES.get(permission, permission)
