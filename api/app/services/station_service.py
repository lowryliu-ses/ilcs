from __future__ import annotations

from sqlalchemy.orm import Session

from ..core.clock import now
from ..core.context import AccessContext
from ..core.errors import DomainError, NotFound, PermissionDenied, StateConflict, ValidationFailed
from ..domain.access import service_may_use_station
from ..domain.adapter_rules import ACTING_LABELS, busy_blocked_changes
from ..domain.gate import adapter_status
from ..domain.lifecycle import capability_delete_blockers, station_delete_blockers, station_retire_blockers
from ..domain.params import clean_specs, limit_issues, spec_issues, spec_of
from ..domain.recipe_rules import is_valid, validate_steps
from ..domain.steps import normalize
from ..models import Adapter, Capability, Island, Station, User
from ..adapters.base import AdapterError
from ..adapters.registry import adapter_for, catalog_of, describe, reset_cache
from ..repositories.batches import AllocationRepository
from ..repositories.execution import CommandRepository
from ..repositories.recipes import RecipeRepository
from ..repositories.resources import (
    AdapterRepository, AssetRepository, CapabilityRepository, IslandRepository, StationRepository,
    adopt_asset_model, station_model,
)
from ..adapters.catalog import DRIVERS, has_tasks, validate_config
from .acceptance_service import (
    after_config_change, driver_awaiting_approval, driver_drift, gate_out, requeue_if_needed, running_stations,
)
from .template_service import (
    TemplateService, connection_problems, station_template_options, template_brief, template_changes,
)
from .audit_service import AuditService
from .gate_service import GateService
from .identity_service import IdentityService


def _kind(spec: dict) -> str:
    """参数的大类：数值与整数的极限写法相同（区间），选项、程序表各是一种写法。"""
    return "numeric" if spec["type"] in ("number", "integer") else spec["type"]


def _default_window(spec: dict):
    """新接一项能力时的缺省极限：选项型全部允许，程序表不约束列，数值 [0, 100]。"""
    if spec["type"] == "enum":
        return list(spec["options"])
    if spec["type"] == "program":
        return {}
    return [0, 100]


class StationService:
    def __init__(self, db: Session, ctx: AccessContext):
        self.db = db
        self.ctx = ctx
        self.stations = StationRepository(db, ctx)
        self.capabilities = CapabilityRepository(db)
        self.adapters = AdapterRepository(db)
        self.islands = IslandRepository(db)
        self.recipes = RecipeRepository(db, ctx)
        self.commands = CommandRepository(db, ctx)
        self.allocations = AllocationRepository(db)
        self.audit = AuditService(db, ctx)
        self.identity = IdentityService(db, ctx)
        self.gate = GateService(db)

    # ---------- 读 ----------

    def list_stations(self) -> list[dict]:
        """工位台账。实物属性（型号、校准、资产状态）从关联资产带出、只读，不在工位上另存一份。"""
        from .asset_service import AssetService

        adapters = {a.station_id: a for a in self.adapters.list()}
        gate_state = self.gate.status()
        stations = self.stations.list()
        assets = self.stations.assets_by_id(stations)
        asset_service = AssetService(self.db, self.ctx)
        briefs: dict[str, dict] = {}
        rows = []
        for station in stations:
            adapter = adapters.get(station.id)
            asset = assets.get(station.asset_id)
            if asset is not None and asset.id not in briefs:
                briefs[asset.id] = asset_brief(asset_service.asset_out(asset))
            model = station_model(station, asset)
            rows.append(
                {
                    "id": station.id,
                    "island": station.island,
                    "name": station.name,
                    "model": model,
                    # 型号从哪来：asset 资产登记（关联了资产）/ station 工位自己登记（没关联资产）
                    "model_source": "asset" if asset is not None else "station",
                    # 工位上早先登记、与资产不一致的型号。设备方法已按资产型号匹配，界面提示人核对
                    "model_conflict": (
                        station.model if asset is not None and station.model and station.model != model else ""
                    ),
                    "status": station.status,
                    "channels": station.channels or 1,
                    "channel_unit": station.channel_unit or "batch",
                    "clean": station.clean,
                    "dirty_batch_id": station.dirty_batch_id,
                    "limits": station.limits,
                    "retired": station.retired,
                    "asset_id": station.asset_id,
                    "asset": briefs.get(asset.id) if asset is not None else None,
                    "row_version": station.row_version,
                    "retire_blockers": station_retire_blockers(
                        station.status, self._open_allocation_count(station.id)
                    ),
                    "adapter": self._adapter_out(adapter, gate_state) if adapter else None,
                }
            )
        return rows

    def _adapter_out(
        self, adapter: Adapter, gate_state: dict, *, include_config: bool = False,
    ) -> dict:
        age = (now() - adapter.last_heartbeat).total_seconds()
        status = adapter_status(gate_state, adapter.station_id, adapter.enabled, adapter.connected)
        return {
            "protocol": adapter.protocol,
            "driver": adapter.driver,
            "version": adapter.version,
            # 连接地址与凭据引用只通过 station.edit 保护的详情接口返回。
            "config": (adapter.config or {}) if include_config else {},
            "credential_ref": adapter.credential_ref if include_config else "",
            "credential_configured": bool(adapter.credential_ref),
            "config_version": adapter.config_version,
            "enabled": adapter.enabled,
            "row_version": adapter.row_version,
            "updated_at": adapter.updated_at.isoformat(timespec="seconds"),
            "status": status,
            "connected": adapter.connected,
            "accepts_commands": adapter.accepts_commands,
            "site_interlock": adapter.site_interlock,
            "dedup_count": adapter.dedup_count,
            "last_heartbeat": adapter.last_heartbeat.isoformat(timespec="seconds"),
            "heartbeat_age_sec": round(age, 1),
            "current_command_id": adapter.current_command_id,
            "note": adapter.note,
            # 适配器契约：真实设备不支持的能力在界面上禁用并说明原因，不假装通用支持
            "kind": adapter.kind,
            # 参与自动流程（接指令）？映射驱动只配了点表的是「只读写点位」：能读点、手动写，不接指令
            "tasks": adapter.kind != "real" or has_tasks(adapter.driver, adapter.config),
            # SiLA 设备服务的点表在设备那一侧（PointAccess），读的时候才知道有没有
            "points": adapter.kind == "real" and (bool((adapter.config or {}).get("points")) or adapter.driver == "sila2_v1"),
            "capabilities": {
                "hold": adapter.supports_hold,
                "abort": adapter.supports_abort,
                "query": adapter.supports_query,
                "dedup": adapter.supports_dedup,
            },
            "unsupported_note": "、".join(
                label for label, supported in (
                    ("保持", adapter.supports_hold), ("终止", adapter.supports_abort),
                    ("状态查询", adapter.supports_query), ("设备端去重", adapter.supports_dedup),
                ) if not supported
            ),
            # 驱动自报的设备身份与方法目录
            "catalog": catalog_of(adapter),
            # 配置变更后的接入验收闸门：还欠什么级别、最近一次满足要求的验收
            "acceptance": gate_out(adapter),
            # 驱动在 ILCS 之外的设备服务：最近一次报的插件与配置摘要、接入验收批准的那份，两者对不上就是待验收的驱动改动
            "driver_info": adapter.driver_info or {},
            "approved_driver": adapter.approved_driver or {},
            "driver_changed": bool(driver_drift(adapter, adapter.driver_info or {})),
            # 这次驱动变更还没有人签名批准（批准过的等接入验收出结论）；最近一次签名批准的记录
            "driver_awaiting_approval": driver_awaiting_approval(adapter),
            "driver_approval": adapter.driver_approval or {},
            # 套用的设备接入模板（哪一版、有没有更新的发布版）与这台设备自己的连接参数
            "template": template_brief(self.db, adapter.template_id),
            "template_connection": (adapter.template_connection or {}) if include_config else {},
        }

    def adapter_detail(self, station_id: str) -> dict:
        self._require_station(station_id)
        adapter = self.adapters.get(station_id)
        if not adapter:
            raise NotFound("适配器未登记")
        return self._adapter_out(adapter, self.gate.status(), include_config=True)

    @staticmethod
    def _reject_inline_secrets(value, path: str = "config") -> None:
        """适配器配置可审计可导出，任何秘密原文都不能混在 JSON 里：按键名，也看 URL 里有没有带口令。"""
        from .template_service import URL_PASSWORD

        if isinstance(value, str) and URL_PASSWORD.search(value):
            raise ValidationFailed(
                f"{path} 的地址里带了口令（user:password@）：请去掉口令、改填 credential_ref",
                code="inline_adapter_secret_forbidden",
            )
        forbidden = {
            "password", "passwd", "secret", "token", "api_key", "apikey", "private_key",
            "authorization", "cookie", "client_secret",
        }
        if isinstance(value, dict):
            for key, nested in value.items():
                normalized = str(key).lower().replace("-", "_")
                if normalized in forbidden:
                    raise ValidationFailed(
                        f"{path}.{key} 不能保存秘密原文；请改填 credential_ref",
                        code="inline_adapter_secret_forbidden",
                    )
                StationService._reject_inline_secrets(nested, f"{path}.{key}")
        elif isinstance(value, list):
            for index, nested in enumerate(value):
                StationService._reject_inline_secrets(nested, f"{path}[{index}]")

    @staticmethod
    def _validate_credential_ref(value: str) -> None:
        if value and not value.startswith(("vault://", "env://", "file://")):
            raise ValidationFailed(
                "credential_ref 只能使用 vault://、env:// 或 file:// 引用",
                code="credential_ref_invalid",
            )

    def list_capabilities(self) -> list[dict]:
        stations = self.stations.list()
        usage = self._capability_usage()
        rows = []
        for capability in self.capabilities.list():
            implemented = [s.id for s in stations if capability.id in (s.limits or {})]
            recipe_ids = usage.get(capability.id, [])
            rows.append(
                {
                    "id": capability.id,
                    "name": capability.name,
                    "params": capability.params,
                    "param_specs": capability.param_specs or {},
                    "recovery": capability.recovery,
                    "stations": implemented,
                    "retired": capability.retired,
                    "recipes": recipe_ids,
                    "delete_blockers": capability_delete_blockers(implemented, recipe_ids),
                }
            )
        return rows

    def _capability_usage(self) -> dict[str, list[str]]:
        """哪些流程的步骤在用这个能力。停用与删除都要先回答这个问题。"""
        usage: dict[str, list[str]] = {}
        for recipe in self.recipes.list():
            for step in recipe.steps or []:
                capability_id = step.get("cap")
                if capability_id and recipe.id not in usage.setdefault(capability_id, []):
                    usage[capability_id].append(recipe.id)
        return usage

    def _open_allocation_count(self, station_id: str) -> int:
        return self.allocations.open_count_for_station(station_id)

    def list_islands(self) -> list[dict]:
        """实验区：登记了名称的，加上工位用着、还没起名的岛号（名称为空），各带在用工位数。岛号 0 是「未分区」。"""
        names = {island.id: island.name for island in self.islands.list()}
        counts: dict[int, int] = {}
        for station in self.stations.list():
            if station.island and not station.retired:
                counts[station.island] = counts.get(station.island, 0) + 1
        return [
            {"id": island_id, "name": names.get(island_id, ""), "stations": counts.get(island_id, 0)}
            for island_id in sorted({*names, *counts})
        ]

    def name_island(self, island_id: int, name: str, user: User) -> dict:
        """给实验区起名字（有就改，没有就登记）。只是给人看的名称：工位、排程、执行都按岛号，改名不影响任何运行。"""
        if not 1 <= island_id <= 9999:
            raise ValidationFailed("实验区编号必须是 1–9999 的整数", code="island_id_invalid")
        name = name.strip()
        if not name:
            raise ValidationFailed("实验区名称不能为空", code="island_name_required")
        island = self.db.get(Island, island_id)
        before = island.name if island is not None else ""
        if island is None:
            island = Island(id=island_id, name=name)
            self.db.add(island)
        else:
            island.name = name
        self.audit.record(
            user, "修改实验区名称" if before else "登记实验区", f"实验区 #{island_id}",
            before=before or "—", after=name,
        )
        self.db.commit()
        return {"id": island_id, "name": name}

    def command_ledger(self, limit: int = 50) -> list[dict]:
        return [
            {
                "id": command.id,
                "batch_id": command.batch_id,
                "station_id": command.station_id,
                "type": command.type,
                "state": command.state,
                "step_index": command.step_index,
                "checkpoint_id": command.checkpoint_id,
                "step_run_id": command.step_run_id,
                "delivery_state": command.delivery_state,
                "error": command.error,
                "created_at": command.created_at.isoformat(timespec="seconds"),
                "updated_at": command.updated_at.isoformat(timespec="seconds"),
            }
            for command in self.commands.recent(limit)
        ]

    # ---------- 写 ----------

    def update_limits(
        self, station_id: str, limits: dict, signature_id: str, user: User,
        expected_version: int | None = None, remove: list[str] | None = None,
    ) -> dict:
        """改能力极限：`limits` 只列要改的能力（合并写入），`remove` 列这台工位不再承接的能力（整项移除）。"""
        station = self._require_station(station_id)
        self.stations.check_version(station, expected_version, "工位")
        problems = self._limit_problems(limits)
        if problems:
            raise DomainError("；".join(problems))
        removed = list(dict.fromkeys(remove or []))
        both = [capability_id for capability_id in removed if capability_id in limits]
        if both:
            raise DomainError(f"同一项能力不能既改范围又移除：{'、'.join(both)}")
        missing = [capability_id for capability_id in removed if capability_id not in (station.limits or {})]
        if missing:
            raise DomainError(f"工位 {station_id} 没有登记能力 {'、'.join(missing)}，无从移除")
        if removed:
            self._require_capabilities_idle(station_id, set(removed))
        signature = self.identity.consume_signature(signature_id, user, "修改能力极限")
        self.stations.bump(station)
        merged = dict(station.limits or {})
        changed: set[str] = set(removed)
        for capability_id in removed:
            merged.pop(capability_id, None)
        for capability_id, params in limits.items():
            slot = dict(merged.get(capability_id) or {})
            slot.update(params)
            merged[capability_id] = slot
            changed.add(capability_id)
        station.limits = merged
        broken = self.revalidate_recipes(changed)
        self.audit.record(
            user, "修改能力极限", station_id, sign=True, meaning=signature.meaning,
            signature_id=signature.id,
            detail="；".join(filter(None, [
                f"移除能力 {'、'.join(removed)}" if removed else "", str(limits) if limits else "",
            ])),
        )
        self.db.commit()
        return {"station": station_id, "limits": station.limits, "broken_recipes": broken}

    def _limit_problems(self, limits: dict) -> list[str]:
        """能力极限的写法按参数规格核：数值参数 [下限, 上限]，选项型参数写允许的选项（登记选项的子集）。"""
        specs = self.capabilities.specs()
        problems: list[str] = []
        for capability_id, params in (limits or {}).items():
            capability = specs.get(capability_id)
            if not isinstance(params, dict):
                problems.append(f"能力 {capability_id} 的极限格式不正确")
                continue
            for name, window in params.items():
                problems.extend(limit_issues(spec_of(capability, name), window, name))
        return problems

    def _require_capabilities_idle(self, station_id: str, capabilities: set[str]) -> None:
        """工位上还有未结束批次的时间窗在用这些能力时不能移除：已排下的工步会落到一台不再承接它的工位上。"""
        mine: dict[str, list[str]] = {}
        others = 0
        for allocation, batch in self.allocations.open_for_station(station_id):
            steps = normalize((batch.recipe_snapshot or {}).get("steps") or [])
            step = steps[allocation.step_index] if allocation.step_index < len(steps) else {}
            capability = step.get("cap")
            if capability not in capabilities:
                continue
            if batch.org_id == self.ctx.org_id:
                if batch.id not in mine.setdefault(capability, []):
                    mine[capability].append(batch.id)
            else:
                others += 1
        if not mine and not others:
            return
        # 工位是跨组织共享的实物：别的组织的批次只报条数，不报编号
        rows = [{"key": capability, "label": f"{capability}：{'、'.join(batch_ids)}"} for capability, batch_ids in mine.items()]
        if others:
            rows.append({"key": "other_org", "label": f"另有 {others} 段其他组织批次的时间窗"})
        raise StateConflict(
            f"工位 {station_id} 上还有未结束批次的时间窗在用 {'、'.join(sorted(set(mine) or capabilities))}："
            "请先让这些批次结束，或取消排程后再移除",
            {"blocked": rows}, code="capability_in_use_on_station",
        )

    def revalidate_recipes(self, capability_ids: set[str] | None = None) -> list[str]:
        """极限变更后重校验；已发布流程不通过则进入需修订。"""
        specs = self.stations.specs()
        specs_by_capability = self.capabilities.specs()
        broken = []
        for recipe in self.recipes.reviewable_states():
            if capability_ids and not any(step.get("cap") in capability_ids for step in recipe.steps or []):
                continue
            bad = not is_valid(
                validate_steps(normalize(recipe.steps or []), specs, specs_by_capability)
            )
            if recipe.state == "released":
                recipe.needs_revision = bad
            if bad:
                broken.append(recipe.id)
        return broken

    def register_capability(
        self, capability_id: str, name: str, params: dict, recovery: dict, stations: list[str],
        signature_id: str, user: User, param_specs: dict | None = None,
    ) -> dict:
        if self.capabilities.get(capability_id):
            raise DomainError(f"标识 {capability_id} 已存在")
        problems = spec_issues(params, param_specs or {})
        if problems:
            raise ValidationFailed("参数规格不正确", {"issues": problems})
        signature = self.identity.consume_signature(signature_id, user, "登记新能力")
        self.capabilities.add(Capability(
            id=capability_id, name=name, params=params, recovery=recovery,
            param_specs=clean_specs(params, param_specs),
        ))
        # 界面上的能力极限只在工位上填（编辑极限）；这里给接口调用方留着一次登记到位的写法，
        # 照样递增工位行版本，别让手里拿着旧版本的极限编辑悄悄盖过去
        for station_id in stations:
            station = self.stations.get(station_id)
            if not station:
                continue
            limits = dict(station.limits or {})
            spec = {"params": params, "param_specs": clean_specs(params, param_specs)}
            limits[capability_id] = {key: _default_window(spec_of(spec, key)) for key in params}
            station.limits = limits
            self.stations.bump(station)
        self.audit.record(
            user, "登记新能力", f"{capability_id} {name}", sign=True, meaning=signature.meaning,
            signature_id=signature.id, detail=f"{len(params)} 个参数 · 实现工位 {' '.join(stations) or '无'}",
        )
        self.db.commit()
        return {"id": capability_id, "name": name}

    def _require_station(self, station_id: str) -> Station:
        station = self.stations.get(station_id)
        if not station:
            raise NotFound("工位不存在")
        return station

    def authorize_device(self, station_id: str) -> Station:
        """设备入口的授权校验。

        来源由认证确定；服务身份只能操作自己被授权的设备，未声明范围的一律拒绝。
        """
        station = self.stations.get(station_id)
        if not station:
            # 跨组织的工位在这里就是「不存在」，不泄漏它的存在
            raise NotFound("工位不存在")
        if self.ctx.is_service and not service_may_use_station(self.ctx.scopes, station_id):
            raise PermissionDenied(
                f"该服务凭据未被授权操作工位 {station_id}", code="station_not_authorized"
            )
        return station

    def mark_command_manual(self, command_id: str, note: str, user: User) -> dict:
        """结果未知的指令转人工核查。不自动重试是刻意的：设备实态只能由人现场确认。"""
        command = self.commands.get(command_id)
        if not command:
            raise NotFound("指令不存在")
        if command.state != "unknown":
            raise DomainError("只有结果未知的指令需要转人工核查")
        command.state = "manual"
        command.error = note or f"{user.display_name} 转人工核查：需现场确认 {command.station_id} 实际状态与检查点一致后再续跑"
        command.updated_at = now()
        self.audit.record(
            user, "指令转人工核查", command_id, before="结果未知", after="人工核查中",
            command_id=command_id, detail=f"{command.batch_id} · {command.station_id} · 第 {command.step_index + 1} 步",
        )
        self.db.commit()
        return {"id": command.id, "state": command.state, "note": command.error}

    def reconnect_adapter(self, station_id: str, user: User) -> dict:
        """重新握手并对账最近检查点。握手成功不等于批次可续跑，恢复评估另走一套前置。"""
        # 适配器按工位 ID 全局登记，组织范围由工位决定：别的组织的设备当作不存在
        self._require_station(station_id)
        adapter = self.adapters.get(station_id)
        if not adapter:
            raise NotFound("适配器未登记")
        if not adapter.enabled:
            raise StateConflict("适配器已停用，不能重连", code="adapter_disabled")
        self._require_no_acceptance_running(station_id, "重连")
        before = "在线" if adapter.connected else "离线"
        try:
            implementation = adapter_for(adapter)
            health = implementation.healthcheck()
        except NotImplementedError as exc:
            raise StateConflict(
                str(exc), {"blocked": [{"key": "driver", "label": str(exc)}]},
                code="adapter_driver_unavailable",
            ) from exc
        except Exception as exc:
            raise StateConflict(
                f"适配器健康检查失败：{exc}",
                {"blocked": [{"key": "connection", "label": str(exc)}]},
                code="adapter_healthcheck_failed",
            ) from exc
        adapter.connected = True
        adapter.accepts_commands = True
        adapter.last_heartbeat = now()
        adapter.awaiting_handshake_since = None
        adapter.note = "已重新加入车队，等待任务" if station_id.startswith("AGV") else "重连后已对账最近检查点"
        station = self.stations.get(station_id)
        # 还欠验收、上次自动验收因为连不上没通过：设备回来了，再排一次
        requeue_if_needed(self.db, adapter, station.org_id if station else self.ctx.org_id)
        if station and station.status == "offline":
            station.status = "idle"
        from .monitoring_service import DeviceMonitor

        DeviceMonitor(self.db).evaluate_station(station_id)
        self.audit.record(
            user, "重连设备适配器", station_id, before=before, after="在线",
            detail=f"{adapter.protocol} 健康检查成功：{health}；批次续跑仍需恢复评估",
        )
        self.db.commit()
        return {"station": station_id, "adapter": self._adapter_out(adapter, self.gate.status())}

    # ---------- 工位增改停 ----------

    def create_station(self, payload: dict, signature_id: str, user: User) -> dict:
        # 工位标识是全站主键：别的组织占用了同一标识也要明确拒绝，而不是撞主键报 500。
        # 提示里不说是被哪个组织占用，免得泄露别的组织的台账。
        if self.db.get(Station, payload["id"]) is not None:
            raise StateConflict(f"工位标识 {payload['id']} 已被占用，请换一个标识", code="station_id_taken")
        self._require_channels_fit(
            payload["id"], max(1, int(payload.get("channels") or 1)), payload.get("asset_id", ""),
        )
        problems = self._limit_problems(payload.get("limits") or {})
        if problems:
            raise DomainError("；".join(problems))
        signature = self.identity.consume_signature(signature_id, user, "登记新工位")
        station = Station(
            id=payload["id"], org_id=self.ctx.org_id, asset_id=payload.get("asset_id", ""),
            island=payload.get("island", 0), name=payload["name"],
            model=payload.get("model", ""), status="idle",
            channels=max(1, int(payload.get("channels") or 1)),
            channel_unit=payload.get("channel_unit") or "batch",
            clean=True, limits=payload.get("limits") or {},
        )
        model_note = ""
        if station.asset_id:
            # 关联了资产就以资产型号为准，工位上不另存一份会分叉的型号
            asset = AssetRepository(self.db, self.ctx).get(station.asset_id)
            had_model = bool(asset.model)
            model_note, _ = adopt_asset_model(station, asset)
            if not had_model and asset.model:
                AssetRepository.bump(asset)
        self.stations.add(station)
        if payload.get("protocol"):
            adapter = self.adapters.add(self._new_adapter(station, {
                "protocol": payload["protocol"], "version": payload.get("adapter_version", ""),
                "kind": payload.get("adapter_kind", "simulation"), "driver": payload.get("adapter_driver") or "",
                "config": payload.get("adapter_config") or {}, "credential_ref": payload.get("credential_ref", ""),
                **{f"supports_{key}": payload.get(f"supports_{key}", True) for key in ("hold", "abort", "query", "dedup")},
            }))
            # 新登记的真实设备第一次上线：先过接入验收（自动排一次只读级；真实设备还要动作级）
            after_config_change(self.db, adapter, {"kind": "", "driver": ""}, org_id=station.org_id,
                                requested_by=user.display_name, requested_by_id=user.id)
        broken = self.revalidate_recipes(set(station.limits or {}))
        self.audit.record(
            user, "登记新工位", station.id, sign=True, meaning=signature.meaning, signature_id=signature.id,
            before="—", after="空闲",
            detail=f"{station.name}；{len(station.limits or {})} 项能力极限"
                   + (f"；{model_note}" if model_note else "")
                   + (f"；重校验影响 {len(broken)} 个流程" if broken else ""),
        )
        self.db.commit()
        return {"id": station.id, "broken_recipes": broken}

    def _new_adapter(self, station: Station, payload: dict) -> Adapter:
        """按登记内容造一份适配器（调用方负责入库）：手工给驱动与配置，或套一份已发布的设备接入模板。

        登记新工位时一起建的、给已有工位补接的走同一套检查：真实设备要用登记过的驱动、配置按驱动检查、
        凭据只收引用。新接入的设备先离线、不接指令，等执行器握手与接入验收。
        """
        adapter = Adapter(
            station_id=station.id, protocol="", driver="simulation", version="", kind="simulation", config={},
            credential_ref="", enabled=True, note="新登记，等待首次心跳", connected=False, accepts_commands=False,
            awaiting_handshake_since=now(),
            supports_hold=True, supports_abort=True, supports_query=True, supports_dedup=True,
            template_id="", template_connection={},
        )
        changes = {key: value for key, value in payload.items() if value is not None}
        if not changes.get("template_id"):
            changes.pop("template_id", None)
            if changes.pop("template_connection", None):
                raise ValidationFailed("连接参数要和设备接入模板一起给", code="template_required")
        changes = self._template_changes(adapter, changes)
        if "config" in changes:
            self._reject_inline_secrets(changes["config"])
        self._validate_credential_ref(changes.get("credential_ref", ""))
        kind = changes.get("kind", "simulation")
        driver = (changes.get("driver") or "").strip()
        if kind == "real" and (not driver or driver == "simulation"):
            raise ValidationFailed("真实设备必须填写已登记的驱动键", code="adapter_driver_required")
        protocol = (changes.get("protocol") or "").strip()
        if not protocol:
            raise ValidationFailed("请填写协议名称（给人看的，如 Modbus TCP、串口命令）", code="adapter_protocol_required")
        if kind == "real":
            check = validate_config(driver, changes.get("config") or {}, changes.get("credential_ref", ""), protocol=protocol)
            if check.problems:
                raise ValidationFailed(
                    f"适配器配置有 {len(check.problems)} 处问题：{'；'.join(check.problems)}",
                    {"problems": check.problems, "warnings": check.warnings}, code="adapter_config_invalid",
                )
        for key, value in changes.items():
            setattr(adapter, key, value)
        adapter.driver = driver or "simulation"
        adapter.protocol = protocol
        return adapter

    def create_adapter(self, station_id: str, payload: dict, signature_id: str, user: User) -> dict:
        """给还没接设备的工位登记适配器：登记工位时没填协议的，之后在这里接入。已经接入的改走修改接口。"""
        station = self._require_station(station_id)
        # 锁住工位行：两个人同时接入同一台工位，只能成一份
        self.db.query(Station).filter(Station.id == station.id).with_for_update().one()
        if self.adapters.get(station.id) is not None:
            raise StateConflict(f"工位 {station.id} 已经接入设备，要改连接请到连接配置里修改", code="adapter_exists")
        if station.retired:
            raise StateConflict(f"工位 {station.id} 已停用：先启用再接入设备", code="station_retired")
        adapter = self._new_adapter(station, payload)
        signature = self.identity.consume_signature(signature_id, user, "登记设备适配器", object_ref=station.id)
        self.adapters.add(adapter)
        # 与登记新工位时一起建的同一口径：新接入的真实设备先过接入验收
        required = after_config_change(self.db, adapter, {"kind": "", "driver": ""}, org_id=station.org_id,
                                       requested_by=user.display_name, requested_by_id=user.id)
        template = template_brief(self.db, adapter.template_id)
        self.audit.record(
            user, "登记设备适配器", station.id, sign=True, meaning=signature.meaning, signature_id=signature.id,
            before="未接入", after=f"{adapter.kind}/{adapter.driver} 配置 v{adapter.config_version}"
                                 + (f"，待接入验收（{gate_out(adapter)['required_label']}）" if required else ""),
            object_version=adapter.row_version,
            detail="；".join(filter(None, [
                f"协议 {adapter.protocol}",
                f"套用模板 {template['code']} r{template['revision']}" if template else "",
                f"凭据引用 {adapter.credential_ref}" if adapter.credential_ref else "",
            ])),
        )
        self.db.commit()
        return self._adapter_out(adapter, self.gate.status())

    def update_adapter(
        self, station_id: str, changes: dict, expected: int, signature_id: str, user: User,
    ) -> dict:
        self._require_station(station_id)
        # 锁住适配器行直到提交：与执行器领取指令互斥（它在领取前锁同一行），也不和验收收尾互相覆盖闸门
        adapter = (
            self.db.query(Adapter).filter(Adapter.station_id == station_id)
            .with_for_update().populate_existing().one_or_none()
        )
        if not adapter:
            raise NotFound("适配器未登记")
        if adapter.row_version != expected:
            raise StateConflict(
                "适配器配置已被其他人修改，请刷新后重试",
                {"current_version": adapter.row_version}, code="version_conflict",
            )
        changes = self._template_changes(adapter, changes)
        if "config" in changes:
            self._reject_inline_secrets(changes["config"])
        kind = changes.get("kind", adapter.kind)
        driver = (changes.get("driver", adapter.driver) or "").strip()
        if kind == "real" and (not driver or driver == "simulation"):
            raise ValidationFailed("真实设备必须填写已登记的驱动键", code="adapter_driver_required")
        credential_ref = changes.get("credential_ref", adapter.credential_ref)
        self._validate_credential_ref(credential_ref)
        self._require_quiet_for(adapter, changes)
        self._require_valid_config(adapter, changes)
        signature = self.identity.consume_signature(
            signature_id, user, "修改设备适配器", object_ref=station_id,
            object_version=adapter.row_version,
        )
        before = {
            "kind": adapter.kind, "driver": adapter.driver, "protocol": adapter.protocol,
            "version": adapter.version, "enabled": adapter.enabled,
            "config_version": adapter.config_version, "tasks": has_tasks(adapter.driver, adapter.config),
        }
        # 逐项记前后值：把 base_url 改到别的主机、换一个凭据引用，审计里都要看得出来。
        # config 已拒绝秘密原文、credential_ref 只是引用，二者都可以留痕。
        diff = self._adapter_diff(adapter, changes)
        # 只改了说明、协议名、版本或超时：不改变连谁、怎么判结论。连接状态照旧（设备可能正在动作，保持 / 终止要能立刻下发），
        # 也不新欠验收
        current = {key: getattr(adapter, key) for key in changes if hasattr(adapter, key)}
        light = not busy_blocked_changes(current, {k: v for k, v in changes.items() if k != "enabled"}) and (
            "enabled" not in changes or changes["enabled"] == adapter.enabled
        )
        for key, value in changes.items():
            setattr(adapter, key, value)
        adapter.config_version += 1
        adapter.row_version += 1
        adapter.updated_at = now()
        if not light:
            # 配置变更后必须重新握手，不能沿用旧连接的“在线”结论。等握手的这段时间不是失联：监控宽限期内不报警
            adapter.connected = False
            adapter.accepts_commands = False
            adapter.awaiting_handshake_since = now()
        reset_cache()
        station = self.stations.get(station_id)
        required = after_config_change(
            self.db, adapter, before, org_id=station.org_id if station else self.ctx.org_id,
            requested_by=user.display_name, requested_by_id=user.id, light=light,
        )
        self.audit.record(
            user, "修改设备适配器", station_id, sign=True, meaning=signature.meaning,
            signature_id=signature.id, before=str(before),
            after=f"{adapter.kind}/{adapter.driver} 配置 v{adapter.config_version}"
                  + (f"，待接入验收（{gate_out(adapter)['required_label']}）" if required else ""),
            object_version=adapter.row_version,
            detail="；".join(diff) or "无字段变化（仅递增配置版本）",
        )
        self.db.commit()
        return self._adapter_out(adapter, self.gate.status())

    def _template_changes(self, adapter: Adapter, changes: dict) -> dict:
        """套用 / 换版本 / 脱离设备接入模板。套用时驱动、协议、支持标志与完整配置都由「模板 + 连接参数」算出来。"""
        changes = dict(changes)
        template_id = changes.pop("template_id", None)
        connection = changes.pop("template_connection", None)
        if template_id is None and connection is not None:
            if not adapter.template_id:
                raise ValidationFailed("这台设备没有套用设备接入模板：连接参数要和模板一起给", code="template_required")
            template_id = adapter.template_id  # 只改连接参数：按当前模板重新合并
        if template_id:
            template = TemplateService(self.db, self.ctx).released(template_id)
            connection = dict(connection if connection is not None else (adapter.template_connection or {}))
            self._reject_inline_secrets(connection, "template_connection")
            problems = connection_problems(template.driver, template.config or {}, connection)
            if problems:
                # 工位只填连接参数：映射（点表、命令、状态码）归模板，改映射要改模板、另一个人发布
                raise ValidationFailed(f"模板连接参数有问题：{'；'.join(problems)}", {"problems": problems},
                                       code="template_connection_invalid")
            derived = template_changes(template, connection)
            if "config" in changes and changes["config"] != derived["config"]:
                raise ValidationFailed(
                    "套用模板时不能同时改完整配置：这台设备自己的连接参数写在 template_connection 里",
                    code="template_config_conflict",
                )
            changes.update(derived, template_id=template.id, template_connection=connection)
        elif template_id == "" or changes.get("kind") == "simulation":
            if adapter.template_id:
                changes["template_id"] = ""  # 不再按模板管理：配置原样保留
        elif adapter.template_id and "config" in changes and changes["config"] != (adapter.config or {}):
            changes["template_id"] = ""  # 手工改了完整配置，和模板对不上了：不再按模板管理
        return changes

    def _require_valid_config(self, adapter: Adapter, changes: dict) -> None:
        """真实设备的配置保存前按驱动检查一遍（见 adapters/catalog.validate_config），配错了当场说清楚。

        只在驱动、配置、凭据引用真的变了时检查：适配器已经坏了的时候，停用它、改说明不能被挡住。
        """
        kind = changes.get("kind", adapter.kind)
        relevant = ("kind", "driver", "config", "credential_ref", "protocol")
        if kind != "real" or not any(key in changes and changes[key] != getattr(adapter, key) for key in relevant):
            return
        check = validate_config(
            (changes.get("driver", adapter.driver) or "").strip(), changes.get("config", adapter.config) or {},
            changes.get("credential_ref", adapter.credential_ref) or "", protocol=changes.get("protocol", adapter.protocol),
        )
        if check.problems:
            raise ValidationFailed(
                f"适配器配置有 {len(check.problems)} 处问题：{'；'.join(check.problems)}",
                {"problems": check.problems, "warnings": check.warnings}, code="adapter_config_invalid",
            )

    def check_adapter_config(self, station_id: str, payload: dict) -> dict:
        """保存之前先检查一份配置：不保存、不连设备。"""
        self._require_station(station_id)
        return validate_config(
            payload.get("driver") or "", payload.get("config") or {}, payload.get("credential_ref") or "",
            protocol=payload.get("protocol") or "",
        ).as_dict()

    def drivers(self, station_id: str | None = None) -> list[dict]:
        """已登记的驱动与各自的配置说明、起步模板（按工位能力极限生成）。

        `capability_examples`：每项能力在这个驱动里的起步写法（点表驱动的设定值 / 实测点 / 启动信号、命令驱动的
        设定命令……），按能力字典的参数生成——表单里加一项能力时照它起步。给了工位时以工位的能力极限为准。"""
        limits = (self._require_station(station_id).limits or {}) if station_id else {}
        dictionary = {
            key: {param: None for param in (spec.get("params") or {})}
            for key, spec in self.capabilities.specs().items() if not spec.get("retired")
        }
        rows = []
        for info in DRIVERS.values():
            row = info.as_dict(limits)
            examples = info.template({**dictionary, **limits}).get("capabilities")
            row["capability_examples"] = examples if isinstance(examples, dict) else {}
            rows.append(row)
        return rows

    def template_options(self, station_id: str) -> list[dict]:
        station = self._require_station(station_id)
        asset = AssetRepository(self.db, self.ctx).get(station.asset_id) if station.asset_id else None
        return station_template_options(self.db, self.ctx, station, station_model(station, asset))

    def _require_quiet_for(self, adapter: Adapter, changes: dict) -> None:
        """设备上还有可能在动作的指令时，不放行会让驱动查不回它的修改（见 domain/adapter_rules）。"""
        acting = self.commands.acting_on_station(adapter.station_id)
        if not acting:
            return
        current = {key: getattr(adapter, key) for key in changes if hasattr(adapter, key)}
        blocked = busy_blocked_changes(current, changes)
        if not blocked:
            return
        # 工位是跨组织共享的实物：别的组织的指令只报条数，不报编号
        mine = [command for command in acting if command.org_id == self.ctx.org_id]
        rows = [
            {"key": command.id, "label": f"{command.id} · {command.batch_id} · {ACTING_LABELS.get(command.state, command.state)}"}
            for command in mine
        ]
        if len(acting) > len(mine):
            rows.append({"key": "other_org", "label": f"另有 {len(acting) - len(mine)} 条其他组织的指令"})
        raise StateConflict(
            f"工位 {adapter.station_id} 上还有 {len(acting)} 条可能仍在动作的指令，这时不能改{'、'.join(blocked)}："
            "改完之后新的驱动实例查不回这些指令。请等它们结束，或保持 / 终止并完成现场核查后再改；"
            "只改说明、停用、超时与探测周期可以照常保存",
            {"blocked": rows, "fields": blocked}, code="adapter_busy",
        )

    @staticmethod
    def _adapter_diff(adapter: Adapter, changes: dict) -> list[str]:
        rows: list[str] = []
        for key, value in changes.items():
            current = getattr(adapter, key, None)
            if key == "config" and isinstance(value, dict):
                old_config = current or {}
                for sub in sorted(set(old_config) | set(value)):
                    if old_config.get(sub) != value.get(sub):
                        rows.append(f"config.{sub}: {old_config.get(sub, '—')} → {value.get(sub, '—')}")
            elif current != value:
                rows.append(f"{key}: {current if current not in (None, '') else '—'} → {value if value not in (None, '') else '—'}")
        return rows

    def _require_no_acceptance_running(self, station_id: str, doing: str) -> None:
        """接入验收正在驱动这台设备：界面上的测试、读目录、重连会和它抢同一台设备（同一个串口）。"""
        if station_id in running_stations(self.db):
            raise StateConflict(f"{station_id} 的接入验收正在执行，结束后再{doing}", code="acceptance_running")

    def test_adapter(self, station_id: str) -> dict:
        self._require_station(station_id)
        adapter = self.adapters.get(station_id)
        if not adapter:
            raise NotFound("适配器未登记")
        if not adapter.enabled:
            raise StateConflict("适配器已停用，不能测试连接")
        self._require_no_acceptance_running(station_id, "测试连接")
        try:
            implementation = adapter_for(adapter)
        except (NotImplementedError, AdapterError) as exc:
            raise StateConflict(
                str(exc), {"blocked": [{"key": "driver", "label": str(exc)}]},
                code="adapter_driver_unavailable",
            ) from exc
        try:
            health = implementation.healthcheck()
        except Exception as exc:
            raise StateConflict(
                f"适配器健康检查失败：{exc}",
                {"blocked": [{"key": "connection", "label": str(exc)}]},
                code="adapter_healthcheck_failed",
            ) from exc
        return {
            "ok": True, "station_id": station_id,
            "contract": implementation.contract.as_dict(), "health": health,
        }

    def describe_adapter(self, station_id: str, user: User) -> dict:
        """读驱动自报的厂商、固件、型号、方法目录与指令类型，存到适配器上。

        工位匹配据此判断能不能按某条设备方法执行：方法的设备端程序不在目录里就不排给这台设备。
        没报过目录（空）不据此排除。
        """
        self._require_station(station_id)
        adapter = self.adapters.get(station_id)
        if not adapter:
            raise NotFound("适配器未登记")
        if not adapter.enabled:
            raise StateConflict("适配器已停用，不能读取设备目录")
        self._require_no_acceptance_running(station_id, "读取设备目录")
        try:
            implementation = adapter_for(adapter)
            reported = describe(implementation, adapter)
        except (NotImplementedError, AdapterError) as exc:
            raise StateConflict(
                str(exc), {"blocked": [{"key": "driver", "label": str(exc)}]}, code="adapter_driver_unavailable",
            ) from exc
        except Exception as exc:
            raise StateConflict(
                f"读取设备身份失败：{exc}", {"blocked": [{"key": "connection", "label": str(exc)}]},
                code="adapter_describe_failed",
            ) from exc
        before = f"{len(adapter.methods or [])} 个方法 · 固件 {adapter.firmware or '—'}"
        for key, value in reported.items():
            setattr(adapter, key, value)
        adapter.described_at = now()
        station = self.stations.get(station_id)
        warning = ""
        asset = AssetRepository(self.db, self.ctx).get(station.asset_id) if station and station.asset_id else None
        registered = station_model(station, asset) if station else ""
        if reported["reported_model"] and registered and reported["reported_model"] != registered:
            source = f"资产 {asset.asset_no} 登记的型号" if asset is not None else "工位登记的型号"
            warning = f"设备自报型号 {reported['reported_model']} 与{source} {registered} 不一致"
        self.audit.record(
            user, "读取设备方法目录", station_id, before=before,
            after=f"{len(reported['methods'])} 个方法 · 固件 {reported['firmware'] or '—'}",
            detail=(f"来源 {reported['described_from']}；厂商 {reported['vendor'] or '—'}"
                    + (f"；{warning}" if warning else "")),
        )
        self.db.commit()
        return {"station_id": station_id, **catalog_of(adapter), "warning": warning}

    def update_station(self, station_id: str, changes: dict, user: User) -> dict:
        """改台账信息（名称、岛、通道；没关联资产时的型号）。能力极限走单独的签名接口。

        校准、型号这类实物属性归资产档案：关联了资产的工位不另存型号，`model` 只接受空值或与资产
        登记一致的值，都按「清掉工位上的旧登记」处理；要改型号请改资产。
        """
        station = self._require_station(station_id)
        self.stations.check_version(station, changes.pop("row_version", None), "工位")
        allowed = {"name", "model", "island", "channels", "channel_unit", "asset_id"}
        rejected = [k for k in changes if k not in allowed]
        if rejected:
            raise DomainError(f"这些字段不能在这里修改：{'、'.join(rejected)}；能力极限请用极限编辑并签名")
        asset_id = changes.get("asset_id", station.asset_id)
        asset = AssetRepository(self.db, self.ctx).get(asset_id) if asset_id else None
        if asset_id and asset is None:
            raise NotFound("资产不存在")
        if "model" in changes and asset is not None:
            if (changes["model"] or "") not in {"", asset.model or ""}:
                raise StateConflict(
                    f"工位 {station.id} 关联了资产 {asset.asset_no}，型号以资产登记的 {asset.model or '（未登记）'} 为准："
                    "要改型号请到「仪器设备」修改资产",
                    code="model_owned_by_asset",
                )
            # 「以资产型号为准」：清掉工位上早先登记的型号，同一件事只留资产上一份
            changes["model"] = ""
        if (changes.get("asset_id", station.asset_id) or "") != (station.asset_id or "") and self._open_allocation_count(station.id):
            # 与资产详情里关联、取消关联同一口径：容量与校准按关联的资产计，已排下的时间窗和新口径对不上
            raise StateConflict(
                f"工位 {station.id} 上还有未结束批次的时间窗：容量与校准按关联的资产计，"
                "改关联前请先让这些批次结束，或取消排程后再改",
                code="station_has_open_allocations",
            )
        if "channels" in changes or "asset_id" in changes:
            self._require_channels_fit(
                station.id, int(changes.get("channels", station.channels) or 1), asset_id,
            )
        if "channel_unit" in changes:
            if changes["channel_unit"] not in {"batch", "sample"}:
                raise DomainError("通道计法只能是 batch（按批次）或 sample（按样本）")
            if changes["channel_unit"] != (station.channel_unit or "batch") and self._open_allocation_count(station.id):
                # 已排的时间窗是按原来的计法算的份数：换计法会让它们和新排的对不上
                raise StateConflict(
                    f"工位 {station.id} 上还有未结束批次的时间窗，改通道计法前请先让它们结束，或取消排程后按新计法重排",
                    code="station_has_open_allocations",
                )
        previous = (
            AssetRepository(self.db, self.ctx).get(station.asset_id)
            if "asset_id" in changes and station.asset_id and station.asset_id != asset_id else None
        )
        before = {k: getattr(station, k) for k in changes}
        for key, value in changes.items():
            setattr(station, key, value)
        model_note = ""
        if "asset_id" in changes and "model" not in changes:
            if asset is not None and before["asset_id"] != asset_id:
                # 从这里关联资产与在资产详情里关联同一口径：型号归资产
                had_model = bool(asset.model)
                model_note, _ = adopt_asset_model(station, asset)
                if not had_model and asset.model:
                    AssetRepository.bump(asset)
            elif asset is None and previous is not None and not station.model:
                # 取消关联：工位不能因此没了型号，把资产登记的抄回工位
                station.model = previous.model or ""
        self.stations.bump(station)
        # 型号与关联资产决定设备方法能不能落到这台工位：变了就重校验引用它能力的流程
        broken = (
            self.revalidate_recipes(set(station.limits or {}))
            if {"model", "asset_id"} & set(changes) else []
        )
        self.audit.record(
            user, "编辑工位台账", station_id,
            detail="；".join(f"{k}: {before[k]} → {v}" for k, v in changes.items())
            + (f"；{model_note}" if model_note else "")
            + (f"；重校验后 {len(broken)} 个流程不再通过" if broken else ""),
        )
        self.db.commit()
        return {"id": station_id, "broken_recipes": broken}

    def _require_channels_fit(self, station_id: str, channels: int, asset_id: str) -> None:
        """工位通道数不能超过所属资产容量：资产同一时刻最多承接 capacity 份作业，排程与执行都按它计。"""
        if not asset_id:
            return
        asset = AssetRepository(self.db, self.ctx).get(asset_id)
        if asset is None:
            raise NotFound("资产不存在")
        if channels > max(1, asset.capacity):
            raise StateConflict(
                f"工位 {station_id} 有 {channels} 个并行通道，超过资产 {asset.asset_no} 的容量 {asset.capacity}："
                f"资产同一时刻最多承接 {asset.capacity} 份作业，请先调大资产容量或减少通道数",
                code="channels_exceed_asset_capacity",
            )

    def _station_references(self, station_id: str) -> dict[str, int]:
        """有哪些记录引用了这个工位（跨组织计数，只报条数）。有一条就说明它进过某段历史，不能删。"""
        from sqlalchemy import String, cast

        from ..models import (
            AcceptanceRun, AdapterExecution, Alarm, Allocation, Command, ExceptionEvent, Location, ResourceBooking,
            ResultValue, Sample, ScheduleProposal, ServiceIdentity, StepRun, Telemetry,
        )

        def count(model, *conditions) -> int:
            return self.db.query(model).filter(*conditions).count()

        return {
            "工步时间窗": count(Allocation, Allocation.station_id == station_id),
            "设备指令": count(Command, Command.station_id == station_id)
            + count(Command, cast(Command.assist_station_ids, String).like(f'%"{station_id}"%')),
            "步骤执行": count(StepRun, StepRun.station_id == station_id),
            "适配器执行记录": count(AdapterExecution, AdapterExecution.station_id == station_id),
            "接入验收记录": count(AcceptanceRun, AcceptanceRun.station_id == station_id),
            "遥测数据": count(Telemetry, Telemetry.station_id == station_id),
            "结果数据": count(ResultValue, ResultValue.station_id == station_id),
            "样本位置": count(Sample, Sample.station_id == station_id),
            "放置位": count(Location, Location.station_id == station_id),
            "资源占用": count(ResourceBooking, ResourceBooking.station_id == station_id),
            "排程建议": count(ScheduleProposal, ScheduleProposal.station_id == station_id),
            "异常事件": count(ExceptionEvent, ExceptionEvent.station_id == station_id),
            "报警": count(Alarm, Alarm.source_type == "station", Alarm.source_id == station_id),
            "服务身份授权": sum(
                1 for row in self.db.query(ServiceIdentity).all() if service_may_use_station(row.scopes or {}, station_id)
            ),
        }

    def delete_blockers(self, station_id: str) -> dict:
        station = self._require_station(station_id)
        return {"id": station.id, "blockers": station_delete_blockers(station.retired, self._station_references(station.id))}

    def delete_station(self, station_id: str, user: User) -> dict:
        """删掉登记错了、从没用过的工位（连同它的适配器）。用过的只能停用：历史工步分配、指令与验收记录都指向它。"""
        station = self._require_station(station_id)
        # 锁住工位行、在锁内重查判据：与接入设备（同样锁这一行）不交错；只删已停用的，它已退出排程匹配
        self.db.query(Station).filter(Station.id == station.id).with_for_update().one()
        blockers = station_delete_blockers(station.retired, self._station_references(station.id))
        if blockers:
            raise StateConflict(
                f"工位 {station.id} 不能删除，请改用停用", {"blocked": [{"key": "station", "label": b} for b in blockers]},
                code="station_in_use",
            )
        adapter = self.adapters.get(station.id)
        detail = [f"{station.name}；{len(station.limits or {})} 项能力极限"]
        if station.asset_id:
            asset = AssetRepository(self.db, self.ctx).get(station.asset_id)
            detail.append(f"原关联资产 {asset.asset_no if asset else station.asset_id}")
        if adapter is not None:
            detail.append(f"连同适配器 {adapter.kind}/{adapter.driver}（{adapter.protocol}）")
            self.db.delete(adapter)
            self.db.flush()
        capabilities = set(station.limits or {})
        self.db.delete(station)
        self.db.flush()
        broken = self.revalidate_recipes(capabilities) if capabilities else []
        if broken:
            detail.append(f"重校验后 {len(broken)} 个流程不再通过")
        self.audit.record(user, "删除工位", station_id, before="已停用", after="已删除", detail="；".join(detail))
        self.db.commit()
        return {"id": station_id, "deleted": True, "broken_recipes": broken}

    def set_station_retired(self, station_id: str, retired: bool, user: User) -> dict:
        """停用 / 启用工位。用过的工位不删：历史工步分配与检查点都指向它（从没用过的见 `delete_station`）。"""
        station = self._require_station(station_id)
        if retired:
            blockers = station_retire_blockers(station.status, self._open_allocation_count(station_id))
            if blockers:
                raise StateConflict(
                    "工位不可停用", {"blocked": [{"key": "station", "label": b} for b in blockers]}
                )
        station.retired = retired
        # 退役即离线；重新启用要把它放回空闲，否则工位会永远显示离线且排不进去。
        # 适配器的连通性是另一条线（adapters 表），不受这里影响。
        station.status = "offline" if retired else "idle"
        broken = self.revalidate_recipes(set(station.limits or {}))
        self.audit.record(
            user, "停用工位" if retired else "启用工位", station_id,
            before="在用" if retired else "已停用", after="已停用" if retired else "在用",
            detail=f"重校验后 {len(broken)} 个流程不再通过" if broken else "流程重校验无影响",
        )
        self.db.commit()
        return {"id": station_id, "retired": retired, "broken_recipes": broken}

    # ---------- 能力改停删 ----------

    def update_capability(self, capability_id: str, changes: dict, signature_id: str, user: User) -> dict:
        """改能力定义。参数增删会改变所有引用流程的校验结果，所以要签名并当场重校验。"""
        capability = self.capabilities.get(capability_id)
        if not capability:
            raise NotFound("能力不存在")
        params_after = changes["params"] if "params" in changes else (capability.params or {})
        specs_after = changes["param_specs"] if "param_specs" in changes else (capability.param_specs or {})
        problems = spec_issues(params_after, specs_after) if "param_specs" in changes else []
        if problems:
            raise ValidationFailed("参数规格不正确", {"issues": problems})
        signature = self.identity.consume_signature(signature_id, user, "修改能力定义")
        before_params = set(capability.params or {})
        before_specs = dict(capability.param_specs or {})
        for key in ("name", "params", "recovery"):
            if key in changes:
                setattr(capability, key, changes[key])
        # 删掉的参数连同规格一起删；规格只在这里收成规范写法（单位别名、缺省值不存）
        capability.param_specs = clean_specs(capability.params or {}, specs_after)
        removed = before_params - set(capability.params or {})
        spec_changed = sorted(
            key for key in set(before_specs) | set(capability.param_specs)
            if before_specs.get(key) != capability.param_specs.get(key)
        )
        before_spec = {"params": {**{key: key for key in before_params}}, "param_specs": before_specs}
        after_spec = {"params": capability.params or {}, "param_specs": capability.param_specs}
        retyped = {
            key for key in spec_changed if key in (capability.params or {}) and key in before_params
            and _kind(spec_of(before_spec, key)) != _kind(spec_of(after_spec, key))
        }
        narrowed = {key for key in spec_changed if spec_of(after_spec, key)["type"] == "enum"} - retyped
        reshaped = {key for key in spec_changed if spec_of(after_spec, key)["type"] == "program"} - retyped
        if removed or retyped or narrowed or reshaped:
            # 工位极限里残留已删参数会让匹配永远不通过，顺手清掉；数值与选项互换了的参数，原来的区间或选项
            # 对新类型没有意义，清掉让工位重填（不替工位放宽能做的范围）；选项减少了的，去掉已经没有的选项
            for station in self.stations.list():
                limits = dict(station.limits or {})
                slot = limits.get(capability_id)
                if not slot:
                    continue
                kept = {k: v for k, v in slot.items() if k not in removed and k not in retyped}
                for key in narrowed & set(kept):
                    options = spec_of(after_spec, key)["options"]
                    window = [item for item in kept[key] if item in options] if isinstance(kept[key], list) else []
                    if window:
                        kept[key] = window
                    else:
                        kept.pop(key)
                for key in reshaped & set(kept):
                    # 程序表删了的列，它的列极限一并去掉
                    columns = {column.get("key") for column in spec_of(after_spec, key)["columns"]}
                    kept[key] = {column: window for column, window in (kept[key] or {}).items() if column in columns} \
                        if isinstance(kept[key], dict) else {}
                if kept != slot:
                    limits[capability_id] = kept
                    station.limits = limits
                    self.stations.bump(station)
        broken = self.revalidate_recipes({capability_id})
        self.audit.record(
            user, "修改能力定义", capability_id, sign=True, meaning=signature.meaning,
            signature_id=signature.id,
            detail=f"{'、'.join(changes)} 已更新"
                   + (f"；移除参数 {'、'.join(removed)}" if removed else "")
                   + (f"；参数规格变更 {'、'.join(spec_changed)}" if spec_changed else "")
                   + (f"；重校验后 {len(broken)} 个流程不再通过" if broken else ""),
        )
        self.db.commit()
        return {"id": capability_id, "broken_recipes": broken}

    def set_capability_retired(self, capability_id: str, retired: bool, user: User) -> dict:
        """停用能力：新流程不能再选它，已有流程与批次快照不受影响。"""
        capability = self.capabilities.get(capability_id)
        if not capability:
            raise NotFound("能力不存在")
        capability.retired = retired
        self.audit.record(
            user, "停用能力" if retired else "启用能力", f"{capability_id} {capability.name}",
            before="在用" if retired else "已停用", after="已停用" if retired else "在用",
            detail="已有流程与批次快照不受影响，新建步骤时不再可选" if retired else "恢复可选",
        )
        self.db.commit()
        return {"id": capability_id, "retired": retired}

    def delete_capability(self, capability_id: str, user: User) -> dict:
        """没有工位实现、也没有流程引用的能力可以删掉；否则只能停用。"""
        capability = self.capabilities.get(capability_id)
        if not capability:
            raise NotFound("能力不存在")
        implemented = [s.id for s in self.stations.list() if capability_id in (s.limits or {})]
        recipe_ids = self._capability_usage().get(capability_id, [])
        blockers = capability_delete_blockers(implemented, recipe_ids)
        if blockers:
            raise StateConflict("能力不可删除", {"blocked": [{"key": "capability", "label": b} for b in blockers]})
        self.audit.record(
            user, "删除能力", f"{capability_id} {capability.name}", before="在用", after="已删除",
            detail="无工位实现、无流程引用",
        )
        self.db.delete(capability)
        self.db.commit()
        return {"id": capability_id, "deleted": True}

    def set_readiness(
        self, station_id: str, clean: bool, status: str, user: User,
        expected_version: int | None = None,
    ) -> dict:
        station = self._require_station(station_id)
        self.stations.check_version(station, expected_version, "工位")
        if status not in {"idle", "running", "fault", "offline"}:
            raise DomainError("工位状态无效")
        before = f"{station.status}, clean={station.clean}"
        used_by = station.dirty_batch_id
        station.clean = clean
        station.status = status
        if clean:
            # 清洗确认：这台设备重新可以给别的批次用，等清洗的排队动作随即投递
            station.dirty_batch_id = ""
            from .alarm_service import AlarmService

            AlarmService(self.db, self.ctx).resolve_condition(
                f"station:{station_id}:awaiting_clean", f"{user.display_name} 确认 {station_id} 已清洗",
            )
        self.stations.bump(station)
        self.audit.record(
            user, "确认工位就绪状态", station_id, before=before, after=f"{status}, clean={clean}",
            detail=f"批次 {used_by} 用后的清洗已确认" if clean and used_by else "",
        )
        self.db.commit()
        return {
            "id": station_id, "status": station.status, "clean": station.clean,
            "row_version": station.row_version,
        }

    def heartbeat(
        self, station_id: str, connected: bool, site_interlock: bool, accepts_commands: bool,
        instrument_serial: str = "",
    ) -> dict:
        """设备心跳。来源由服务认证确定；序列号只作业务数据校验。"""
        station = self.authorize_device(station_id)
        adapter = self.adapters.get(station_id)
        if not adapter:
            raise NotFound("适配器未登记")
        if instrument_serial:
            # 序列号是业务数据，和资产档案核对；不一致就拒绝，不照单写入
            from ..repositories.resources import AssetRepository

            asset = (
                AssetRepository(self.db, self.ctx).get(station.asset_id)
                if station.asset_id else None
            )
            if asset is not None and asset.serial and asset.serial != instrument_serial:
                raise StateConflict(
                    f"仪器序列号 {instrument_serial} 与工位 {station_id} 关联资产登记的 "
                    f"{asset.serial} 不一致",
                    code="serial_mismatch",
                )
        if not adapter.enabled:
            # 停用的适配器不能靠一条心跳重新上线：必须由人启用并通过健康检查
            raise StateConflict(
                "适配器已停用，心跳不被接受；请在「工位与接入 → 设备连接」启用并完成健康检查", code="adapter_disabled",
            )
        came_back = connected and not adapter.connected
        adapter.connected = connected
        adapter.site_interlock = site_interlock
        adapter.accepts_commands = accepts_commands
        adapter.last_heartbeat = now()
        if connected:
            adapter.awaiting_handshake_since = None
        if came_back:
            # 推心跳的设备恢复在线：还欠验收、上次自动验收因为连不上没通过的，再排一次
            requeue_if_needed(self.db, adapter, station.org_id)
        from .monitoring_service import DeviceMonitor

        # 联锁 / 失联在心跳到达的这一刻就报警或复位，不等执行器下一轮
        DeviceMonitor(self.db).evaluate_station(station_id)
        self.db.commit()
        return self.gate.status()

    def command_ack(
        self, command_id: str, outcome: str, payload: dict,
    ) -> dict:
        """设备回执入口。必须绑定原 command_id，重复回执回放原结论。

        回执与执行器轮询走同一套落库逻辑（`ExecutionService.settle`）：写检查点、结束指令、
        释放工位的「当前指令」、再交给推进器。只发事件不结束指令，不支持查询的设备会让
        指令永远停在执行中，下一条指令一到同一工位就被对账判成不一致。
        """
        from datetime import datetime

        from ..adapters.base import CommandResult
        from ..core.clock import as_utc
        from ..core.context import system_context
        from ..models import Batch
        from .execution_service import ExecutionService

        command = self.commands.get(command_id)
        if not command:
            raise NotFound("指令不存在")
        self.authorize_device(command.station_id)
        if outcome not in {"accepted", "done", "failed"}:
            raise DomainError("回执结论只能是 accepted、done 或 failed")
        quality = payload.get("quality") or "good"
        if quality not in {"good", "bad", "uncertain"}:
            raise ValidationFailed("回执 quality 只能是 good、bad 或 uncertain")
        execution = ExecutionService(self.db, system_context(command.org_id, "设备回执"))
        ledger = execution.executions.get(command.id)
        if ledger is None:
            raise StateConflict("该指令还没有交给设备，不接受回执", code="command_not_delivered")
        if command.state in {"done", "cancelled"} or ledger.state in {"done", "failed", "rejected"}:
            # 重复回执：回放已落库的结论，不产生第二个检查点，也不再推进
            return {"command_id": command.id, "state": command.state, "replayed": True}
        if command.state in {"unknown", "manual"} and outcome == "accepted":
            return {"command_id": command.id, "state": command.state, "replayed": True}
        batch = self.db.get(Batch, command.batch_id)
        record = self.adapters.get(command.station_id)
        device_ts = payload.get("device_ts")
        if isinstance(device_ts, str) and device_ts:
            device_ts = datetime.fromisoformat(device_ts.replace("Z", "+00:00"))
        result = CommandResult(
            command_id=command.id,
            state=outcome,
            device_ts=as_utc(device_ts) if device_ts else now(),
            quality=quality,
            delivered=payload.get("delivered") or {},
            error=payload.get("error") or "",
            origin=(
                f"real:{record.driver}" if record is not None and record.kind == "real"
                else "simulation"
            ),
        )
        before = ledger.state
        command.delivery_state = "delivered"
        execution.settle(batch, command, ledger, record, result)
        self.audit.record(
            None, "设备回执", command.id, before=before, after=command.state,
            command_id=command.id, detail=f"{command.station_id} 回执 {outcome}；质量 {quality}",
        )
        self.db.commit()
        return {"command_id": command.id, "state": command.state, "replayed": False}

    def ingest_telemetry(self, station_id: str, payload: dict) -> dict:
        """设备遥测入库。

        来源由服务认证确定；同一 (工位, event_id, 指标) 只入库一次，重发回放计数。设备时间
        超前服务器 5 分钟以上视为时钟错误整批拒收——带着错时钟的数据进了曲线就再也分不清。
        """
        from datetime import timedelta

        from sqlalchemy.dialects.postgresql import insert

        from ..core.clock import as_utc
        from ..core.config import settings
        from ..models import Batch, Command, Telemetry
        from ..models.base import uid

        self.authorize_device(station_id)
        adapter = self.adapters.get(station_id)
        if adapter is None:
            raise NotFound("适配器未登记")
        points = payload.get("points") or []
        if len(points) > settings.telemetry_max_points_per_request:
            raise ValidationFailed(
                f"单次最多上报 {settings.telemetry_max_points_per_request} 个点，请分批", code="too_many_points",
            )
        moment = now()
        future = [p for p in points if as_utc(p["device_ts"]) > moment + timedelta(minutes=5)]
        if future:
            raise ValidationFailed(
                f"{len(future)} 个点的设备时间超前服务器 5 分钟以上，请先校准设备时钟", code="device_clock_skew",
            )
        command_id = payload.get("command_id") or adapter.current_command_id or ""
        batch_id = ""
        command = batch = None
        if command_id:
            command = self.db.get(Command, command_id)
            if command is not None and command.station_id == station_id:
                batch = self.db.get(Batch, command.batch_id)
                batch_id = batch.id if batch is not None else ""
            else:
                command = None
        origin = f"real:{adapter.driver}" if adapter.kind == "real" else "simulation"
        from .telemetry import context as telemetry_context

        rows = [
            {
                "id": uid(),
                "station_id": station_id, "batch_id": batch_id, "metric": point["metric"],
                "setpoint": point.get("setpoint"), "value": point["value"],
                "quality": point.get("quality") or "good", "origin": origin,
                "device_ts": as_utc(point["device_ts"]), "event_id": payload["event_id"],
                "received_at": moment,
                # 归属：指令、步骤、值守人；设备按孔位或样本号报时归到样本
                **telemetry_context(
                    self.db, batch, command, well=point.get("well") or "", sample_id=point.get("sample_id") or "",
                ),
            }
            for point in points
        ]
        statement = insert(Telemetry).values(rows).on_conflict_do_nothing(
            index_elements=["station_id", "event_id", "metric"],
            index_where=Telemetry.event_id != "",
        )
        accepted = self.db.execute(statement).rowcount or 0
        self.db.commit()
        return {
            "event_id": payload["event_id"], "accepted": accepted,
            "duplicates": len(rows) - accepted, "batch_id": batch_id,
        }


def asset_brief(row: dict) -> dict:
    """工位台账上显示的资产摘要：校准、状态、容量都只读，改动去「仪器设备」。"""
    return {
        key: row.get(key)
        for key in (
            "id", "asset_no", "name", "model", "state", "capacity", "calibration_applicable",
            "calibration_exempt_reason", "calibration_valid", "calibration_due", "unavailable_reasons",
        )
    }
