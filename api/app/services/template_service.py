"""设备接入模板：起草 → 发布 → 修订 / 退役；导入导出；套用到工位。

模板是「一类设备怎么接」：驱动 + 映射配置（点表、命令、状态码、参数对应）+ 连接参数示例 + 支持标志 + 验收缺省。
工位 = 模板的某一版 + 自己的连接参数（地址、证书、设备编号）。同型号的几台设备共用一份模板，不再各抄一份 JSON。

- 发布走职责分离：起草与改过草稿的人都不能发布它（测试环境管理员自审开关例外，且留审计），发布要电子签名；
- 发布后内容冻结（数据库触发器），要改就新建修订；新修订发布时同编号的旧发布版退役；
- 升级不自动推给工位：套用旧版的工位照常运行，界面列出它们，由人逐台切换（签名、配置版本递增、重新验收）；
- 导出成文件（`ilcs-device-template/1`，带内容摘要），设备模块的 `profile.json` 就是这个文件；导入一律成草稿，
  摘要对不上（文件被改过）直接拒绝。
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
from typing import Any

from sqlalchemy.orm import Session

from ..adapters.catalog import DRIVERS, merge_config, validate_config
from ..core.clock import now
from ..core.context import AccessContext
from ..core.errors import NotFound, PermissionDenied, StateConflict, ValidationFailed
from ..domain.access import same_person
from ..models import DeviceTemplate, Station, User
from ..repositories.resources import StationRepository
from ..repositories.templates import DeviceTemplateRepository
from .audit_service import AuditService
from .identity_service import IdentityService, admin_self_approval

FORMAT = "ilcs-device-template/1"
EDITABLE = ("name", "model", "vendor", "driver", "protocol", "version", "config", "connection", "supports",
            "acceptance", "note")
STATE_LABEL = {"draft": "草稿", "released": "已发布", "retired": "已退役"}
SUPPORT_KEYS = ("hold", "abort", "query", "dedup")
CODE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{1,63}$")
# 地址里带了口令：postgresql://user:password@host、https://user:password@host
URL_PASSWORD = re.compile(r"[a-z][a-z0-9+.-]*://[^/@:\s]+:[^/@\s]+@", re.IGNORECASE)
SECRET_KEYS = {
    "password", "passwd", "secret", "token", "api_key", "apikey", "private_key", "authorization", "cookie",
    "client_secret",
}


def template_digest(template: Any) -> str:
    """内容摘要：决定设备怎么被驱动的那些字段。名称、说明改了不算内容变化。"""
    get = template.get if isinstance(template, dict) else lambda key, default=None: getattr(template, key, default)
    content = {key: get(key) or ({} if key in {"config", "connection", "supports", "acceptance"} else "")
               for key in ("driver", "protocol", "version", "config", "connection", "supports", "acceptance")}
    text = json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(text.encode()).hexdigest()


def template_changes(template: DeviceTemplate, connection: dict) -> dict[str, Any]:
    """套用模板时适配器要改成什么：驱动、协议、支持标志与合并后的完整配置（驱动照旧只读 config）。"""
    info = DRIVERS[template.driver]
    supports = {**info.supports, **(template.supports or {})}
    return {
        "kind": "real", "driver": template.driver, "protocol": template.protocol or info.protocol,
        "version": template.version or f"{template.code} r{template.revision}",
        "config": merge_config(template.config or {}, connection or {}),
        **{f"supports_{key}": bool(supports.get(key, True)) for key in SUPPORT_KEYS},
    }


def secrets_in(value: Any, path: str) -> list[str]:
    """秘密原文出现在哪：键名像凭据的，以及地址里带了口令的。"""
    found: list[str] = []
    if isinstance(value, str) and URL_PASSWORD.search(value):
        found.append(f"{path}（地址里带了口令）")
    if isinstance(value, dict):
        for key, nested in value.items():
            if str(key).lower().replace("-", "_") in SECRET_KEYS:
                found.append(f"{path}.{key}")
            found.extend(secrets_in(nested, f"{path}.{key}"))
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            found.extend(secrets_in(nested, f"{path}[{index}]"))
    return found


def connection_problems(driver: str, config: dict, connection: dict) -> list[str]:
    """工位填的连接参数只能是驱动登记的连接参数：映射（点表、命令、状态码）归模板，改映射要改模板、另一个人发布。

    组合工位按路由名给各路由的连接参数：`routes: [{name, config: {连接参数…}, credential_ref}]`。
    """
    info = DRIVERS.get(driver)
    if info is None:
        return [f"驱动 {driver} 没有登记"]
    problems = []
    for key, value in (connection or {}).items():
        if driver == "composite_v1" and key == "routes":
            names = {str(route.get("name")): route for route in config.get("routes") or [] if isinstance(route, dict)}
            for item in value if isinstance(value, list) else [None]:
                if not isinstance(item, dict) or str(item.get("name")) not in names:
                    problems.append("routes 里每一项都要写模板里已有的路由名")
                    continue
                route_info = DRIVERS.get(str(names[str(item["name"])].get("driver") or ""))
                allowed = set(route_info.connection_keys) if route_info else set()
                for sub in item.get("config") or {}:
                    if sub not in allowed:
                        problems.append(f"路由 {item['name']} 的 {sub} 不是连接参数，归模板管")
                extra = set(item) - {"name", "config", "credential_ref"}
                if extra:
                    problems.append(f"路由 {item['name']} 只能填 config 与 credential_ref，不能填 {'、'.join(sorted(extra))}")
        elif key not in info.connection_keys:
            problems.append(f"{key} 不是 {info.label} 的连接参数，归模板管")
    return problems


def template_check(template: Any) -> dict[str, Any]:
    """模板能不能发布：字段缺项与类型是问题；按连接示例构造驱动时发现的只算提醒（套用到工位时再核一次）。"""
    get = template.get if isinstance(template, dict) else lambda key, default=None: getattr(template, key, default)
    driver, config, connection = get("driver") or "", get("config") or {}, get("connection") or {}
    problems: list[str] = []
    warnings: list[str] = []
    if not isinstance(config, dict) or not isinstance(connection, dict):
        return {"ok": False, "problems": ["映射配置与连接参数示例都必须是 JSON 对象"], "warnings": []}
    leaked = secrets_in(config, "config") + secrets_in(connection, "connection")
    if leaked:
        problems.append(f"模板不能带秘密原文：{'、'.join(leaked)}；凭据在工位上填 credential_ref")
    supports = get("supports") or {}
    if not isinstance(supports, dict) or any(key not in SUPPORT_KEYS or not isinstance(value, bool)
                                             for key, value in supports.items()):
        problems.append("支持标志只能是 hold / abort / query / dedup 的是否")
    acceptance = get("acceptance") or {}
    if not isinstance(acceptance, dict) or not isinstance(acceptance.get("params", {}), dict):
        problems.append("验收缺省必须是 {capability, params}")
    info = DRIVERS.get(driver)
    if info is not None:
        mixed = sorted(key for key in config if key in info.connection_keys)
        if mixed:
            warnings.append(f"映射配置里带了连接参数 {'、'.join(mixed)}：套用时以工位填写的为准")
    check = validate_config(driver, merge_config(config, connection), template=True)
    return {"ok": not (problems or check.problems), "problems": problems + check.problems,
            "warnings": warnings + check.warnings}


class TemplateService:
    def __init__(self, db: Session, ctx: AccessContext):
        self.db = db
        self.ctx = ctx
        self.templates = DeviceTemplateRepository(db, ctx)
        self.stations = StationRepository(db, ctx)
        self.audit = AuditService(db, ctx)
        self.identity = IdentityService(db, ctx)

    # ---------- 读 ----------

    def _require(self, template_id: str) -> DeviceTemplate:
        template = self.templates.get(template_id)
        if template is None:
            raise NotFound("设备接入模板不存在")
        return template

    def released(self, template_id: str) -> DeviceTemplate:
        """套用到工位只接受已发布的版本：草稿没人审过，退役的已被新修订取代。"""
        template = self._require(template_id)
        if template.state != "released":
            raise StateConflict(
                f"模板 {template.code} r{template.revision} 处于{STATE_LABEL.get(template.state, template.state)}，"
                "只有已发布的模板可以套用到工位", code="template_not_released",
            )
        if template.driver not in DRIVERS:
            raise StateConflict(f"模板的驱动 {template.driver} 在当前版本里没有登记", code="template_driver_unavailable")
        return template

    def _stations_using(self, code: str) -> list[dict[str, Any]]:
        versions = {row.id: row for row in self.templates.versions(code)}
        latest = self.templates.latest_released(code)
        rows = []
        for adapter in self.templates.adapters_using(list(versions)):
            station = self.stations.get(adapter.station_id)
            if station is None:
                continue  # 别的组织的工位
            used = versions[adapter.template_id]
            rows.append({
                "station_id": station.id, "station_name": station.name, "template_id": used.id,
                "revision": used.revision, "latest_revision": latest.revision if latest else None,
                "outdated": latest is not None and used.revision < latest.revision,
                "acceptance_required": adapter.acceptance_required if adapter.kind == "real" else "",
                "config_version": adapter.config_version,
            })
        return rows

    def out(self, template: DeviceTemplate, *, full: bool = False, usage: list[dict] | None = None) -> dict[str, Any]:
        info = DRIVERS.get(template.driver)
        stations = usage if usage is not None else self._stations_using(template.code)
        mine = [row for row in stations if row["template_id"] == template.id]
        row = {
            "id": template.id, "code": template.code, "revision": template.revision, "name": template.name,
            "model": template.model, "vendor": template.vendor, "driver": template.driver,
            "driver_label": info.label if info else template.driver, "protocol": template.protocol,
            "version": template.version, "state": template.state,
            "state_label": STATE_LABEL.get(template.state, template.state), "digest": template.digest,
            "connection_keys": list(info.connection_keys) if info else [], "note": template.note,
            "created_by_name": template.created_by_name, "released_by_name": template.released_by_name,
            "created_at": template.created_at.isoformat(timespec="seconds") if template.created_at else None,
            "released_at": template.released_at.isoformat(timespec="seconds") if template.released_at else None,
            "row_version": template.row_version, "source": template.source or {},
            "usage": {"stations": len(mine), "outdated": sum(1 for item in stations if item["outdated"])},
        }
        if full:
            row.update(
                config=template.config or {}, connection=template.connection or {}, supports=template.supports or {},
                acceptance=template.acceptance or {}, check=template_check(template),
                stations=stations,
                revisions=[{"id": item.id, "revision": item.revision, "state": item.state,
                            "state_label": STATE_LABEL.get(item.state, item.state),
                            "released_at": item.released_at.isoformat(timespec="seconds") if item.released_at else None}
                           for item in self.templates.versions(template.code)],
            )
        return row

    def list(self, state: str | None = None, driver: str | None = None) -> list[dict[str, Any]]:
        usage: dict[str, list[dict]] = {}
        rows = []
        for template in self.templates.list(state, driver):
            if template.code not in usage:
                usage[template.code] = self._stations_using(template.code)
            rows.append(self.out(template, usage=usage[template.code]))
        return rows

    def get(self, template_id: str) -> dict[str, Any]:
        return self.out(self._require(template_id), full=True)

    # ---------- 写 ----------

    def _clean(self, payload: dict[str, Any]) -> dict[str, Any]:
        values = {key: copy.deepcopy(payload[key]) for key in EDITABLE if key in payload and payload[key] is not None}
        for key in ("name", "model", "vendor", "driver", "protocol", "version", "note"):
            if key in values:
                values[key] = str(values[key]).strip()
        if "driver" in values and values["driver"] not in DRIVERS:
            raise ValidationFailed(f"驱动 {values['driver'] or '（未填）'} 没有登记；可选 {', '.join(sorted(DRIVERS))}",
                                   code="template_driver_invalid")
        for key in ("config", "connection", "supports", "acceptance"):
            if key in values and not isinstance(values[key], dict):
                raise ValidationFailed(f"{key} 必须是 JSON 对象", code="template_invalid")
        leaked = secrets_in(values.get("config") or {}, "config") + secrets_in(values.get("connection") or {}, "connection")
        if leaked:
            raise ValidationFailed(f"模板不能带秘密原文：{'、'.join(leaked)}；凭据在工位上填 credential_ref",
                                   code="inline_adapter_secret_forbidden")
        return values

    def create(self, payload: dict[str, Any], user: User, *, source: dict | None = None,
               revision: int | None = None) -> dict[str, Any]:
        code = str(payload.get("code") or "").strip()
        if not CODE.fullmatch(code):
            raise ValidationFailed("模板编号 2–64 位，字母数字开头，只含字母、数字、点、横线、下划线", code="template_code_invalid")
        values = self._clean(payload)
        if not values.get("name") or not values.get("driver"):
            raise ValidationFailed("模板要有名称与驱动", code="template_invalid")
        versions = self.templates.versions(code)
        if versions and revision is None:
            raise StateConflict(f"模板编号 {code} 已存在：要改请在它上面新建修订", code="template_code_taken")
        if any(row.state == "draft" for row in versions):
            raise StateConflict(f"{code} 已有一个修订草稿，请先完成或删除它", code="template_draft_exists")
        template = DeviceTemplate(
            org_id=self.ctx.org_id, code=code, revision=revision or 1, state="draft", source=source or {"kind": "manual"},
            created_by=user.id, created_by_name=user.display_name, created_at=now(), updated_at=now(), editors=[user.id],
            **{key: values.get(key, "" if key in {"model", "vendor", "protocol", "version", "note"} else {})
               for key in EDITABLE if key not in {"name", "driver"}},
            name=values["name"], driver=values["driver"],
        )
        template.digest = template_digest(template)
        self.templates.add(template)
        self.audit.record(user, "新建设备接入模板", template.id, after=f"{code} r{template.revision} 草稿",
                          detail=f"{template.name}；驱动 {template.driver}；来源 {(source or {}).get('kind', 'manual')}",
                          object_version=template.row_version)
        self.db.commit()
        return self.get(template.id)

    def update(self, template_id: str, changes: dict[str, Any], expected: int | None, user: User) -> dict[str, Any]:
        template = self._require(template_id)
        self.templates.check_version(template, expected, "设备接入模板")
        if template.state != "draft":
            raise StateConflict("只有草稿可以修改；发布过的模板要新建修订", code="template_not_draft")
        values = self._clean(changes)
        for key, value in values.items():
            setattr(template, key, value)
        template.digest = template_digest(template)
        template.updated_at = now()
        # 改过草稿的人和起草人一样不能发布它：否则 B 改了 A 的草稿再自己发布，职责分离就空了
        if user.id not in (template.editors or []):
            template.editors = [*(template.editors or []), user.id]
        self.templates.bump(template)
        self.audit.record(user, "修改设备接入模板", template.id, after=f"{template.code} r{template.revision} 草稿",
                          detail="、".join(sorted(values)) or "无字段变化", object_version=template.row_version)
        self.db.commit()
        return self.get(template.id)

    def release(self, template_id: str, expected: int | None, signature_id: str | None, user: User) -> dict[str, Any]:
        template = self._require(template_id)
        self.templates.check_version(template, expected, "设备接入模板")
        if template.state != "draft":
            raise StateConflict("只有草稿可以发布", code="template_not_draft")
        check = template_check(template)
        if not check["ok"]:
            raise StateConflict(
                "模板定义有问题，不能发布", {"blocked": [{"key": "definition", "label": item} for item in check["problems"]]},
                code="template_invalid",
            )
        authors = {template.created_by, *(template.editors or [])}
        if any(same_person(author, user.id) for author in authors) and not admin_self_approval(
            self.db, self.ctx, user, template.id, "发布本人起草或改过的设备接入模板",
        ):
            raise PermissionDenied("不能发布本人起草或改过的设备接入模板：请由另一位有发布权限的人发布", code="self_approval")
        signature = self.identity.consume_signature(
            signature_id, user, "发布设备接入模板", object_ref=template.id, object_version=template.row_version,
            strict=True,
        )
        retired = []
        for other in self.templates.versions(template.code):
            if other.id != template.id and other.state == "released":
                other.state = "retired"
                self.templates.bump(other)
                retired.append(f"r{other.revision}")
        template.state = "released"
        template.released_by, template.released_by_name, template.released_at = user.id, user.display_name, now()
        template.digest = template_digest(template)
        self.templates.bump(template)
        outdated = [row["station_id"] for row in self._stations_using(template.code) if row["template_id"] != template.id]
        self.audit.record(
            user, "发布设备接入模板", template.id, sign=True, meaning=signature.meaning, signature_id=signature.id,
            before="草稿", after="已发布", object_version=template.row_version,
            detail=f"{template.code} r{template.revision} {template.name}；摘要 {template.digest[:19]}"
                   + (f"；旧版本 {'、'.join(retired)} 退役" if retired else "")
                   + (f"；套用旧版本的工位 {'、'.join(outdated)} 照常运行，需逐台切换" if outdated else ""),
        )
        self.db.commit()
        return self.get(template.id)

    def revise(self, template_id: str, user: User) -> dict[str, Any]:
        source = self._require(template_id)
        versions = self.templates.versions(source.code)
        payload = {"code": source.code, **{key: copy.deepcopy(getattr(source, key)) for key in EDITABLE}}
        return self.create(payload, user, source={"kind": "revise", "from": f"r{source.revision}"},
                           revision=max(row.revision for row in versions) + 1)

    def retire(self, template_id: str, expected: int | None, user: User) -> dict[str, Any]:
        template = self._require(template_id)
        self.templates.check_version(template, expected, "设备接入模板")
        if template.state != "released":
            raise StateConflict("只有已发布的模板可以退役", code="template_not_released")
        template.state = "retired"
        self.templates.bump(template)
        using = [row["station_id"] for row in self._stations_using(template.code) if row["template_id"] == template.id]
        self.audit.record(user, "退役设备接入模板", template.id, before="已发布", after="已退役",
                          object_version=template.row_version,
                          detail=f"{template.code} r{template.revision}；套用它的工位 {len(using)} 个照常运行，不能再新套用")
        self.db.commit()
        return self.get(template.id)

    def delete(self, template_id: str, user: User) -> dict[str, Any]:
        template = self._require(template_id)
        if template.state != "draft":
            raise StateConflict("只有草稿可以删除；发布过的模板只能退役", code="template_not_draft")
        self.audit.record(user, "删除设备接入模板草稿", template.id, before="草稿", after="已删除",
                          detail=f"{template.code} r{template.revision}")
        self.db.delete(template)
        self.db.commit()
        return {"id": template_id, "deleted": True}

    # ---------- 导入导出 ----------

    def export(self, template_id: str) -> dict[str, Any]:
        template = self._require(template_id)
        return {
            "format": FORMAT, "code": template.code, "revision": template.revision, "name": template.name,
            "model": template.model, "vendor": template.vendor, "driver": template.driver, "protocol": template.protocol,
            "version": template.version, "config": template.config or {}, "connection": template.connection or {},
            "supports": template.supports or {}, "acceptance": template.acceptance or {}, "note": template.note,
            "state": template.state, "digest": template_digest(template),
            "exported_at": now().isoformat(timespec="seconds") + "Z",
        }

    def import_file(self, document: Any, filename: str, user: User) -> dict[str, Any]:
        """导入一律成草稿：别的部署发布过，不等于本部署审过。编号已有就作为它的新修订。"""
        if not isinstance(document, dict) or document.get("format") != FORMAT:
            raise ValidationFailed(f"不是 ILCS 设备接入模板文件（format 应为 {FORMAT}）", code="template_file_invalid")
        declared = str(document.get("digest") or "")
        if declared and declared != template_digest(document):
            raise ValidationFailed("文件内容与摘要不一致：文件在导出之后被改过", code="template_digest_mismatch")
        code = str(document.get("code") or "").strip()
        versions = self.templates.versions(code) if code else []
        source = {"kind": "import", "file": filename, "digest": declared or template_digest(document),
                  "revision": document.get("revision")}
        return self.create(document, user, source=source,
                           revision=(max(row.revision for row in versions) + 1) if versions else None)


def template_brief(db: Session, template_id: str) -> dict[str, Any] | None:
    """工位适配器上显示的模板信息：哪一版、是不是有更新的发布版。"""
    if not template_id:
        return None
    template = db.get(DeviceTemplate, template_id)
    if template is None:
        return {"id": template_id, "missing": True}
    latest = (
        db.query(DeviceTemplate).filter(
            DeviceTemplate.org_id == template.org_id, DeviceTemplate.code == template.code,
            DeviceTemplate.state == "released",
        ).order_by(DeviceTemplate.revision.desc()).first()
    )
    return {
        "id": template.id, "code": template.code, "revision": template.revision, "name": template.name,
        "state": template.state, "state_label": STATE_LABEL.get(template.state, template.state),
        "latest_id": latest.id if latest else None, "latest_revision": latest.revision if latest else None,
        "outdated": latest is not None and latest.revision > template.revision,
    }


def station_template_options(db: Session, ctx: AccessContext, station: Station, model: str = "") -> list[dict[str, Any]]:
    """这台工位能套用的模板：已发布的，适用型号与工位一致的排在前面。"""
    rows = DeviceTemplateRepository(db, ctx).list(state="released")
    ordered = sorted(rows, key=lambda row: (0 if model and row.model == model else 1, row.code))
    return [{"id": row.id, "code": row.code, "revision": row.revision, "name": row.name, "model": row.model,
             "driver": row.driver, "matches_model": bool(model and row.model == model),
             "connection_keys": list(DRIVERS[row.driver].connection_keys) if row.driver in DRIVERS else [],
             "connection": row.connection or {}}
            for row in ordered]
