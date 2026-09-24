from __future__ import annotations

from sqlalchemy.orm import Session

from ..core.clock import now
from ..core.context import AccessContext
from ..core.errors import DomainError, NotFound, PermissionDenied, StateConflict, ValidationFailed
from ..domain.access import service_may_use_station
from ..domain.lifecycle import capability_delete_blockers, station_retire_blockers
from ..domain.recipe_rules import is_valid, validate_steps
from ..domain.steps import normalize
from ..models import Adapter, Capability, Station, User
from ..adapters.base import AdapterError
from ..adapters.registry import adapter_for, catalog_of, describe, reset_cache
from ..repositories.batches import AllocationRepository
from ..repositories.execution import CommandRepository
from ..repositories.recipes import RecipeRepository
from ..repositories.resources import AdapterRepository, CapabilityRepository, IslandRepository, StationRepository
from .audit_service import AuditService
from .gate_service import GateService
from .identity_service import IdentityService


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
        adapters = {a.station_id: a for a in self.adapters.list()}
        gate_state = self.gate.status()
        degraded = " ".join(gate_state["degraded"])
        rows = []
        for station in self.stations.list():
            adapter = adapters.get(station.id)
            rows.append(
                {
                    "id": station.id,
                    "island": station.island,
                    "name": station.name,
                    "model": station.model,
                    "status": station.status,
                    "cal_due": station.cal_due,
                    "positions": station.positions,
                    "channels": station.channels or 1,
                    "clean": station.clean,
                    "limits": station.limits,
                    "retired": station.retired,
                    "asset_id": station.asset_id,
                    "row_version": station.row_version,
                    "retire_blockers": station_retire_blockers(
                        station.status, self._open_allocation_count(station.id)
                    ),
                    "adapter": self._adapter_out(adapter, degraded) if adapter else None,
                }
            )
        return rows

    def _adapter_out(
        self, adapter: Adapter, degraded_text: str, *, include_config: bool = False,
    ) -> dict:
        age = (now() - adapter.last_heartbeat).total_seconds()
        if not adapter.enabled:
            status = "disabled"
        elif not adapter.connected:
            status = "offline"
        elif adapter.station_id in degraded_text:
            status = "degraded"
        else:
            status = "online"
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
        }

    def adapter_detail(self, station_id: str) -> dict:
        self._require_station(station_id)
        adapter = self.adapters.get(station_id)
        if not adapter:
            raise NotFound("适配器未登记")
        degraded = " ".join(self.gate.status()["degraded"])
        return self._adapter_out(adapter, degraded, include_config=True)

    @staticmethod
    def _reject_inline_secrets(value, path: str = "config") -> None:
        """适配器配置可审计可导出，任何秘密原文都不能混在 JSON 里。"""
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
        return [{"id": i.id, "name": i.name} for i in self.islands.list()]

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
        expected_version: int | None = None,
    ) -> dict:
        station = self._require_station(station_id)
        self.stations.check_version(station, expected_version, "工位")
        for capability_id, params in limits.items():
            for name, window in params.items():
                if len(window) != 2 or not window[0] < window[1]:
                    raise DomainError(f"{name} 下限必须小于上限")
        signature = self.identity.consume_signature(signature_id, user, "修改能力极限")
        self.stations.bump(station)
        merged = dict(station.limits or {})
        changed: set[str] = set()
        for capability_id, params in limits.items():
            slot = dict(merged.get(capability_id) or {})
            slot.update(params)
            merged[capability_id] = slot
            changed.add(capability_id)
        station.limits = merged
        broken = self.revalidate_recipes(changed)
        self.audit.record(
            user, "修改能力极限", station_id, sign=True, meaning=signature.meaning,
            signature_id=signature.id, detail=str(limits),
        )
        self.db.commit()
        return {"station": station_id, "limits": station.limits, "broken_recipes": broken}

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
        signature_id: str, user: User,
    ) -> dict:
        if self.capabilities.get(capability_id):
            raise DomainError(f"标识 {capability_id} 已存在")
        signature = self.identity.consume_signature(signature_id, user, "登记新能力")
        self.capabilities.add(Capability(id=capability_id, name=name, params=params, recovery=recovery))
        for station_id in stations:
            station = self.stations.get(station_id)
            if not station:
                continue
            limits = dict(station.limits or {})
            limits[capability_id] = {key: [0, 100] for key in params}
            station.limits = limits
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
        adapter.note = "已重新加入车队，等待任务" if station_id.startswith("AGV") else "重连后已对账最近检查点"
        station = self.stations.get(station_id)
        if station and station.status == "offline":
            station.status = "idle"
        from .monitoring_service import DeviceMonitor

        DeviceMonitor(self.db).evaluate_station(station_id)
        self.audit.record(
            user, "重连设备适配器", station_id, before=before, after="在线",
            detail=f"{adapter.protocol} 健康检查成功：{health}；批次续跑仍需恢复评估",
        )
        self.db.commit()
        return {"station": station_id, "adapter": self._adapter_out(adapter, " ".join(self.gate.status()["degraded"]))}

    # ---------- 工位增改停 ----------

    def create_station(self, payload: dict, signature_id: str, user: User) -> dict:
        # 工位标识是全站主键：别的组织占用了同一标识也要明确拒绝，而不是撞主键报 500。
        # 提示里不说是被哪个组织占用，免得泄露别的组织的台账。
        if self.db.get(Station, payload["id"]) is not None:
            raise StateConflict(f"工位标识 {payload['id']} 已被占用，请换一个标识", code="station_id_taken")
        signature = self.identity.consume_signature(signature_id, user, "登记新工位")
        station = Station(
            id=payload["id"], org_id=self.ctx.org_id, asset_id=payload.get("asset_id", ""),
            island=payload.get("island", 0), name=payload["name"],
            model=payload.get("model", ""), status="idle", cal_due=payload.get("cal_due", ""),
            positions=payload.get("positions", 1), channels=max(1, int(payload.get("channels") or 1)),
            clean=True, limits=payload.get("limits") or {},
        )
        self.stations.add(station)
        if payload.get("protocol"):
            self._reject_inline_secrets(payload.get("adapter_config") or {})
            self._validate_credential_ref(payload.get("credential_ref", ""))
            kind = payload.get("adapter_kind", "simulation")
            driver = (payload.get("adapter_driver") or "").strip()
            if kind == "real" and (not driver or driver == "simulation"):
                raise ValidationFailed("真实设备必须填写已登记的驱动键", code="adapter_driver_required")
            self.adapters.add(Adapter(
                station_id=station.id, protocol=payload["protocol"], version=payload.get("adapter_version", ""),
                note="新登记，等待首次心跳", connected=False, accepts_commands=False,
                kind=kind, driver=driver or "simulation", config=payload.get("adapter_config") or {},
                credential_ref=payload.get("credential_ref", ""),
                supports_hold=payload.get("supports_hold", True),
                supports_abort=payload.get("supports_abort", True),
                supports_query=payload.get("supports_query", True),
                supports_dedup=payload.get("supports_dedup", True),
            ))
        broken = self.revalidate_recipes(set(station.limits or {}))
        self.audit.record(
            user, "登记新工位", station.id, sign=True, meaning=signature.meaning, signature_id=signature.id,
            before="—", after="空闲",
            detail=f"{station.name}；{len(station.limits or {})} 项能力极限"
                   + (f"；重校验影响 {len(broken)} 个流程" if broken else ""),
        )
        self.db.commit()
        return {"id": station.id, "broken_recipes": broken}

    def update_adapter(
        self, station_id: str, changes: dict, expected: int, signature_id: str, user: User,
    ) -> dict:
        self._require_station(station_id)
        adapter = self.adapters.get(station_id)
        if not adapter:
            raise NotFound("适配器未登记")
        if adapter.row_version != expected:
            raise StateConflict(
                "适配器配置已被其他人修改，请刷新后重试",
                {"current_version": adapter.row_version}, code="version_conflict",
            )
        if "config" in changes:
            self._reject_inline_secrets(changes["config"])
        kind = changes.get("kind", adapter.kind)
        driver = (changes.get("driver", adapter.driver) or "").strip()
        if kind == "real" and (not driver or driver == "simulation"):
            raise ValidationFailed("真实设备必须填写已登记的驱动键", code="adapter_driver_required")
        credential_ref = changes.get("credential_ref", adapter.credential_ref)
        self._validate_credential_ref(credential_ref)
        signature = self.identity.consume_signature(
            signature_id, user, "修改设备适配器", object_ref=station_id,
            object_version=adapter.row_version,
        )
        before = {
            "kind": adapter.kind, "driver": adapter.driver, "protocol": adapter.protocol,
            "version": adapter.version, "enabled": adapter.enabled,
            "config_version": adapter.config_version,
        }
        # 逐项记前后值：把 base_url 改到别的主机、换一个凭据引用，审计里都要看得出来。
        # config 已拒绝秘密原文、credential_ref 只是引用，二者都可以留痕。
        diff = self._adapter_diff(adapter, changes)
        for key, value in changes.items():
            setattr(adapter, key, value)
        adapter.config_version += 1
        adapter.row_version += 1
        adapter.updated_at = now()
        # 配置变更后必须重新握手，不能沿用旧连接的“在线”结论。
        adapter.connected = False
        adapter.accepts_commands = False
        reset_cache()
        self.audit.record(
            user, "修改设备适配器", station_id, sign=True, meaning=signature.meaning,
            signature_id=signature.id, before=str(before),
            after=f"{adapter.kind}/{adapter.driver} 配置 v{adapter.config_version}",
            object_version=adapter.row_version,
            detail="；".join(diff) or "无字段变化（仅递增配置版本）",
        )
        self.db.commit()
        return self._adapter_out(adapter, " ".join(self.gate.status()["degraded"]))

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

    def test_adapter(self, station_id: str) -> dict:
        self._require_station(station_id)
        adapter = self.adapters.get(station_id)
        if not adapter:
            raise NotFound("适配器未登记")
        if not adapter.enabled:
            raise StateConflict("适配器已停用，不能测试连接")
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
        if station and reported["reported_model"] and station.model and reported["reported_model"] != station.model:
            warning = f"设备自报型号 {reported['reported_model']} 与台账型号 {station.model} 不一致"
        self.audit.record(
            user, "读取设备方法目录", station_id, before=before,
            after=f"{len(reported['methods'])} 个方法 · 固件 {reported['firmware'] or '—'}",
            detail=(f"来源 {reported['described_from']}；厂商 {reported['vendor'] or '—'}"
                    + (f"；{warning}" if warning else "")),
        )
        self.db.commit()
        return {"station_id": station_id, **catalog_of(adapter), "warning": warning}

    def update_station(self, station_id: str, changes: dict, user: User) -> dict:
        """改台账信息（名称、型号、岛、样品位、校准到期）。能力极限走单独的签名接口。"""
        station = self._require_station(station_id)
        self.stations.check_version(station, changes.pop("row_version", None), "工位")
        allowed = {"name", "model", "island", "positions", "channels", "cal_due", "asset_id"}
        rejected = [k for k in changes if k not in allowed]
        if rejected:
            raise DomainError(f"这些字段不能在这里修改：{'、'.join(rejected)}；能力极限请用极限编辑并签名")
        before = {k: getattr(station, k) for k in changes}
        for key, value in changes.items():
            setattr(station, key, value)
        self.stations.bump(station)
        self.audit.record(
            user, "编辑工位台账", station_id,
            detail="；".join(f"{k}: {before[k]} → {v}" for k, v in changes.items()),
        )
        self.db.commit()
        return {"id": station_id}

    def set_station_retired(self, station_id: str, retired: bool, user: User) -> dict:
        """停用 / 启用工位。一律不删：历史工步分配与检查点都指向它。"""
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
        signature = self.identity.consume_signature(signature_id, user, "修改能力定义")
        before_params = set(capability.params or {})
        for key in ("name", "params", "recovery"):
            if key in changes:
                setattr(capability, key, changes[key])
        removed = before_params - set(capability.params or {})
        if removed:
            # 工位极限里残留已删参数会让匹配永远不通过，顺手清掉
            for station in self.stations.list():
                limits = dict(station.limits or {})
                slot = limits.get(capability_id)
                if not slot:
                    continue
                limits[capability_id] = {k: v for k, v in slot.items() if k not in removed}
                station.limits = limits
        broken = self.revalidate_recipes({capability_id})
        self.audit.record(
            user, "修改能力定义", capability_id, sign=True, meaning=signature.meaning,
            signature_id=signature.id,
            detail=f"{'、'.join(changes)} 已更新"
                   + (f"；移除参数 {'、'.join(removed)}" if removed else "")
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
        station.clean = clean
        station.status = status
        self.stations.bump(station)
        self.audit.record(user, "确认工位就绪状态", station_id, before=before, after=f"{status}, clean={clean}")
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
                "适配器已停用，心跳不被接受；请在工位页启用并完成健康检查", code="adapter_disabled",
            )
        adapter.connected = connected
        adapter.site_interlock = site_interlock
        adapter.accepts_commands = accepts_commands
        adapter.last_heartbeat = now()
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
