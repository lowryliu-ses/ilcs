"""动作权限矩阵。静态角色字典 + 动作权限；组织范围、资质和职责分离在服务端另行校验。

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
    "step.submit": ["operator", "researcher", "admin"],
    "step.review": ["qa", "admin"],
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
    "golden.set": ["qa", "admin"],
    "org.admin": ["admin"],
    "service.manage": ["admin"],
}

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


def can(role: str, permission: str) -> bool:
    return role in PERMISSIONS.get(permission, [])


def permissions_of(role: str) -> list[str]:
    return [name for name, roles in PERMISSIONS.items() if role in roles]


def action_name(permission: str) -> str:
    return ACTION_NAMES.get(permission, permission)
