# 验收记录：AC-01 至 AC-40

更新日期：2026-09-22。对应《实验室平台开发需求文档》v1.0 第 7 节。

自动化用例位置以 `api/` 为根，只在 PostgreSQL 16 上运行（2026-09-23：**297 passed**，无跳过）。PostgreSQL 并发用例用真实独立连接验证幂等锁、资产预约行锁、工作流 `SKIP LOCKED` 与库存并发预留；另有故障注入验证业务写入与幂等响应记录同事务回滚，并断言全新空库迁移只建结构、不生成组织或演示业务数据。端到端链路另由 `scripts/smoke.py` 验证（只走 HTTP，可对部署环境跑）。

「证据」列里写「手工」的项，是无法在单元 / 集成层面证明的操作性要求（迁移演练、备份恢复、真实设备），本文如实标注做了什么、没做什么。**没有把未做的事写成通过。**

## 覆盖表

| AC | 需求 | 证据 | 结论 |
|---|---|---|---|
| AC-01 | 用已有库副本迁移并核对 | 手工：先对 dev 库副本、再对 dev 库本体跑 `scripts/migrate.py upgrade --stamp-baseline`，产出 [migration-report.md](migration-report.md)；`tests/api/test_schema_guard.py::test_migration_reconciliation_report_passes_on_the_seeded_database` | 通过。历史 ID、快照、文件引用保留；差异逐项列出，历史结果标 `legacy_unreviewed` 而非伪造审核结论 |
| AC-02 | 恢复备份后启动兼容版本 | `tests/api/test_schema_guard.py::test_startup_does_not_create_tables_or_seed`、`::test_expected_revision_matches_the_latest_migration`；`scripts/backup-and-recovery-drill.sh`；[本地 PG16 隔离恢复记录](recovery-drill-local-2026-09-22.md) | 通过。本地已验证真实 PG 备份恢复、全量数据哈希一致及当前应用启动兼容；脚本会校验数据库、文件摘要且不改变原有服务启停状态。目标机仍需按 AC-40 留存演练报告 |
| AC-03 | 跨组织读写 | `tests/api/test_access_scope.py::test_cross_organization_objects_are_invisible_everywhere`、`tests/api/test_resources_people.py::test_cross_organization_file_is_not_downloadable` | 通过。列表 / 主键 / 统计 / 附件一律 404，聚合计数不含他组织 |
| AC-04 | 撤销成员、越权账号、后台任务组织 | `test_access_scope.py::test_revoked_membership_blocks_new_requests`、`::test_cannot_switch_to_an_organization_you_are_not_a_member_of`、`::test_disabled_account_cannot_act` | 通过。作用域只由成员关系决定，请求体里的 `organization_id` 被忽略 |
| AC-05 | 未认证 / 停用凭据 / 授权外设备 | `test_access_scope.py::test_device_entries_require_service_credentials`、`::test_service_identity_is_limited_to_authorized_stations`、`::test_disabled_service_identity_is_rejected_immediately` | 通过。无匿名兼容路径 |
| AC-06 | 同一事件重传 | `tests/api/test_analysis.py::test_replayed_event_returns_the_original_result`、`tests/api/test_workflow.py::test_duplicate_events_advance_a_step_only_once`、`tests/api/test_batch_lifecycle.py::test_repeated_command_delivery_executes_once`、`tests/api/test_inventory.py::test_replayed_event_only_posts_once` | 通过。四条路径（结果、推进、指令、台账）各自去重 |
| AC-07 | 同键不同内容 / 失败事务 | `test_batch_lifecycle.py::test_same_key_with_different_body_conflicts`、`::test_create_without_idempotency_key_is_rejected`、`test_analysis.py::test_same_event_with_different_content_is_rejected`、`test_postgres_concurrency.py::test_same_idempotency_key_is_serialized_across_real_connections`、`test_idempotency_atomicity.py::test_failure_before_idempotency_record_rolls_back_business_and_allows_retry`、`test_master_data.py::test_signature_is_one_shot` | 通过。同键并发由 PostgreSQL 专用连接持有 advisory lock，两个 HTTP/DB 会话只写一份；领域服务内部提交在幂等边界内只 flush，业务、审计、签名消费和幂等响应一次提交；故障注入后无半条业务数据且原键可安全重试 |
| AC-08 | 缺事件号 / 错任务 / 未授权指标 | `test_analysis.py::test_missing_event_id_and_wrong_task_are_refused`、`::test_invalid_metric_rejects_the_whole_event`、`::test_missing_value_is_not_treated_as_zero` | 通过。缺值不当 0 处理 |
| AC-09 | 文件上传下载、损坏与暂存清理 | `test_resources_people.py::test_file_upload_download_and_checksum_guard`、`::test_cross_organization_file_is_not_downloadable`、`::test_orphan_file_cleanup_preserves_database_references` | 通过。摘要不符时明确报错；执行器定时清理过期未关联文件并写审计，SOP、资质、校准、结果或报告的数据库正式引用通过反查保护，不依赖可能缺失的辅助标签 |
| AC-10 | 资质过期或被撤销 | `test_resources_people.py::test_expired_qualification_blocks_assignment_and_start`、`tests/domain/test_rules.py::test_expired_qualification_blocks_dispatch` | 通过。分配按预计执行时间判定，开始与恢复再判一次；**运行中不自动急停**（不猜测设备侧策略），按需求要求保持人工处置 |
| AC-11 | 校准在预计区间内失效 | `test_resources_people.py::test_calibration_expiring_inside_the_window_blocks_dispatch`、`tests/domain/test_rules.py::test_calibration_expiring_inside_window_blocks_resource_check` | 通过 |
| AC-12 | 维护 / 人工预约 / 排程竞争同一资产 | `test_resources_people.py::test_bookings_share_asset_capacity_across_stations`、`::test_maintenance_booking_lists_impacted_allocations`、`test_postgres_concurrency.py::test_booking_conflict_rechecks_after_waiting_for_asset_row_lock` | 通过。同资产跨工位共享容量；真实双连接中第二个预约等待资产行锁，醒来后重读占用并返回 409 |
| AC-13 | 设备离线 / 退役 / 联锁 | `test_resources_people.py::test_offline_and_retired_station_blocks_device_action`、`test_failure_paths.py::test_site_interlock_closes_gate_and_blocks_control`、`::test_faulted_station_blocks_scheduling_with_step_reason` | 通过。阻塞原因具体到工位与步骤 |
| AC-14 | 过期 / 开封超期 / 未放行物料 | `tests/api/test_inventory.py::test_expired_and_unopened_lots_are_blocked`、`::test_empty_bom_is_not_blocked_by_material_check`、`test_master_data.py::test_locked_plan_is_immutable_and_lot_release_needs_signature` | 通过。空 BOM 合法实验不被误拦 |
| AC-15 | 100 / 20 / 8 / 12 三量推演 | `test_inventory.py::test_balance_outstanding_available_follow_the_documented_example` | 通过。逐步断言 100/20/80 → 92/12/80 → 92/0/92，精确小数 |
| AC-16 | 重复消耗事件、两次部分投料 | `test_inventory.py::test_replayed_event_only_posts_once`、`::test_consumption_beyond_reservation_is_rejected`、`::test_bom_reservation_equals_the_declared_quantity` | 通过。流水与余额一致；最后一条是回归测试——早期实现把预留量重复计了一次 |
| AC-17 | 并发超量预留 / 终止含已领用物料 | `test_inventory.py::test_issued_material_is_not_auto_released`、`test_failure_paths.py::test_abort_releases_reservations_and_marks_samples`、`::test_material_shortage_blocks_creation_with_reason`、`test_postgres_concurrency.py::test_concurrent_batch_reservations_cannot_overbook_one_lot` | 通过。真实双连接同时申请 7/10 时只有一个事务成功；可用量不为负，已领用未耗用不自动释放 |
| AC-18 | 无批次登记样本、关联运行、分样 | `tests/api/test_samples.py::test_sample_can_be_registered_without_a_batch_or_result`、`::test_split_checks_parent_quantity_and_records_loss` | 通过。父子数量可核对，损耗单独记录 |
| AC-19 | 孔位冲突、重复扫码 | `test_samples.py::test_two_samples_cannot_occupy_the_same_live_slot`、`::test_repeated_receive_scan_does_not_write_twice` | 通过。占位唯一性用部分唯一索引，只约束在途行 |
| AC-20 | 删批次时的样本归属 | `test_samples.py::test_deleting_a_batch_keeps_independently_registered_samples` | 通过。解绑运行分配，不删物理样本与历史 |
| AC-21 | 单条件 / 委托 / 矩阵方案 | `tests/api/test_workflow.py::test_single_condition_plan_needs_no_factor_matrix`、`::test_commissioned_plan_requires_registered_samples_and_released_method`、`test_master_data.py::test_plan_lock_rejects_matrix_larger_than_plate`、`tests/domain/test_rules.py::test_seeded_randomized_layout_is_reproducible` | 通过。非矩阵不强制两因子；矩阵仍校验条件、重复、孔位与随机种子 |
| AC-22 | 批准后修订、转派、重复建批次 | `test_workflow.py::test_revision_reassignment_and_one_batch_per_task` | 通过。修订产生新草稿版本、在跑批次仍指向原批准版本；转派必须写原因并留痕（`history` + 审计）；同一任务第二次建批次 409 |
| AC-23 | 人工步骤缺记录 | `test_workflow.py::test_manual_step_blocks_until_record_is_complete` | 通过。缺项逐条列出；补齐后只创建一个下一步骤 |
| AC-24 | 无浏览器请求等待到期、保持中到期 | `test_workflow.py::test_wait_step_is_advanced_by_the_background_ticker`、`::test_hold_does_not_advance_device_actions` | 通过。推进由后台进程完成，保持中不产生设备动作 |
| AC-25 | 事件重放与并发完成事件 | `test_workflow.py::test_duplicate_events_advance_a_step_only_once`、`test_postgres_concurrency.py::test_workflow_claim_uses_skip_locked_between_workers` | 通过。`StepAdvance` 唯一约束保证一次转换；真实双连接验证已被一个推进器锁定的事件不会阻塞或重复发给另一推进器 |
| AC-26 | 重启且状态可确认 | `test_failure_paths.py::test_restart_reconciliation_holds_mismatched_command`、`::test_hold_then_resume_shifts_downstream` | 通过。复用原命令身份，不重复动作 |
| AC-27 | 回执未落库且无法可靠查询 | `test_failure_paths.py::test_adapter_loss_faults_batch_and_raises_alarm`、`::test_command_without_adapter_is_surfaced_not_silently_queued`、`test_editor_flows.py::test_only_unknown_commands_can_be_sent_to_manual_review` | 通过。进入结果未知 / 人工核查，不盲目重发，资源不提前释放 |
| AC-28 | 加急重排、转运延迟、无工位等待 | `test_workflow.py::test_reschedule_protects_executed_steps`、`tests/domain/test_scheduling.py::test_station_switch_books_transfer_and_clean_buffer`、`::test_hard_window_violation_is_rejected_with_reason`、`tests/domain/test_rules.py::test_pure_manual_flow_marks_device_checks_not_applicable` | 通过。下游不早于转运完成；按实际资源需求校验，纯人工流程不被工位项误拦 |
| AC-29 | 一事件多指标其中一项非法 | `test_analysis.py::test_invalid_metric_rejects_the_whole_event` | 通过。整次拒绝，无部分入账 |
| AC-30 | 分批回传与第二检测任务 | `test_analysis.py::test_partial_ingest_moves_task_to_collecting_only` | 通过。仅对应指标入账；满足要求后才 `collected`，第二任务状态不变 |
| AC-31 | 回传成功但未复核 | `test_batch_lifecycle.py::test_full_run_leaves_results_unassessed_until_review`、`tests/api/test_reports.py::test_unreviewed_result_blocks_report_submission` | 通过。不自动置 valid / approved，不进正式报告 |
| AC-32 | 本人审核、重测、更正 | `test_analysis.py::test_admin_cannot_review_own_entry`、`::test_researcher_cannot_review_results_at_all`、`::test_retest_creates_a_new_task_and_round`、`::test_duplicate_metric_needs_an_explicit_revision`、`::test_approval_requires_a_quality_verdict` | 通过。更正产生新版本，旧记录不覆盖 |
| AC-33 | SOP 修订 / 退役与在途任务 | `test_reports.py::test_sop_lifecycle_and_self_approval_guard`、`::test_sop_training_acknowledgement_gates_dispatch` | 通过。新任务按有效版本校验，历史引用与附件仍可查 |
| AC-34 | 审核通过但质量 invalid | `test_analysis.py::test_approved_but_invalid_result_is_excluded_from_official_statistics`、`test_reports.py::test_official_statistics_exclude_invalid_results_with_reasons`、`tests/domain/test_rules.py::test_statistics_exclude_non_valid_samples` | 通过。默认排除并显示「已审核但质量判定无效」，不进均值与黄金比较 |
| AC-35 | 发布后修订源结果 | `test_reports.py::test_revising_source_results_requires_a_new_report_version` | 通过。原 PDF 与引用不变，新报告标明版本与替代关系 |
| AC-36 | 作者自批、签名票据挪用 | `test_reports.py::test_report_publish_requires_reviewed_results_and_blocks_self_approval`、`test_analysis.py::test_signature_cannot_be_reused_on_another_object`、`test_master_data.py::test_signature_is_one_shot` | 通过。票据绑定动作 + 对象 ID + 对象版本，一次性消费 |
| AC-37 | 真实设备完成实验 | `test_crud_flows.py::test_adapter_configuration_is_versioned_secret_safe_and_testable`、`tests/domain/test_http_json_adapter.py`、`test_failure_paths.py::test_real_async_adapter_is_polled_without_faking_telemetry` | **现场实机未验证。** 已内置 `http_json_v1` HTTPS 网关驱动，并用本地假设备覆盖身份核对、外置凭据、设备端去重、查询、异步完成、保持/终止及超时分类；真实设备断联、物理副作用与恢复仍待 DEC-02 |
| AC-38 | 前端走通首期全流程 | 手工浏览器验收；`scripts/validate-compose-stack.sh` 在隔离 PG16 栈调用 `scripts/smoke.py`，完整完成方案审批 → 任务分配接单 → 批次排程下发 → 人工记录（签名）→ 设备步骤 → 3 秒等待由独立执行器唤醒 → 审核节点（操作员自审被拒、QA 通过）→ 检测任务 → LIMS 服务认证回传 → 8 条数据复核（1 条判无效）→ 报告草稿 / 提交 / 批准 / 发布 PDF、摘要核对与审计链 | 通过。全程只走 HTTP，不改库、不手工补步骤；等待与审核节点未跳过，余额、状态和发布快照联动正确 |
| AC-39 | 原有流程回归 | `tests/api/test_master_data.py`、`test_failure_paths.py`、`test_editor_flows.py`、`test_crud_flows.py`、`tests/domain/test_scheduling.py`、`tests/domain/test_rules.py`、`tests/domain/test_lifecycle.py`、`tests/domain/test_recipe_rules.py` 全部通过；`web` 侧 `npm run build` 通过 | 通过。变更语义（样本→物理样本 + 运行分配、固定指标→版本化指标）有 `0003` 数据迁移 |
| AC-40 | 服务重启、文件恢复、不兼容版本、首次部署 | `test_schema_guard.py`（不兼容库拒绝启动、空库迁移不造业务数据）、`scripts/migrate.py seed` 对正式库默认拒绝、`scripts/check-production-readiness.py`（不执行 env 内容的离线门禁）、`scripts/validate-compose-stack.sh`、[本地 Compose 完整部署记录](compose-validation-local-2026-09-22.md)、`scripts/backup-and-recovery-drill.sh`、[目标机只读探测记录](target-host-probe-2026-09-22.md) | 部分通过。已在隔离环境跑通 PG16、迁移、正式初始化、API/执行器/nginx、登录、非 root 只读容器及完整 HTTP 业务闭环，并覆盖配置占位符/密钥复用/PG 一致性/构建制品检查、停写备份、摘要、隔离恢复与业务核对；目标机 `10.10.106.51:22` 当前连接超时，未进入认证且未产生远端修改，**首次部署与整套恢复演练尚未执行** |

## 界面可达性（2026-09-22 补）

验收用例覆盖的是后端判据。第一轮交付时有一批后端已实现、界面没有入口的能力——
后端能做不等于用户能做，这一节记录补齐情况。

| 能力 | 后端 | 界面 | 补齐 |
|---|---|---|---|
| 方法关联受控 SOP | `PATCH /recipes/{id}` 收 `sop_version_id` | 编辑器无控件 → SOP 模块无入口 | 方法编辑器「方法属性」里加下拉选择，只列已发布且生效的版本 |
| 生效 SOP 清单 | `GET /sops/effective` | — | 修掉空 `capability_id` 被当成「必须适用于空能力」的过滤 bug（限定适用范围的 SOP 全被滤掉，下拉框永远是空的） |
| 指标定义管理 | `POST/PATCH/retire/revisions /metrics` | 完全没有页面 | 新增「指标定义」页与导航入口：登记、改本版、修订、停用；被引用的版本禁用编辑并提示改走修订 |
| 资产台账与可用性 | `PATCH /assets/{id}` | 只能建不能改 | 编辑对话框：名称/型号/位置、状态（正常/维护/退役）、共享容量、不适用校准+理由，带 `row_version` 乐观并发 |
| 人员档案 | `PATCH /people/{id}` | 只能建不能改 | 编辑对话框：姓名/岗位/联系方式、在岗状态、账号绑定 |
| 账号绑定 | 无接口 | 手填 UUID | 新增 `GET /admin/members` 列本组织有效成员，界面改为下拉，已被占用的账号列出但不可选 |
| SOP 草稿 | `PATCH /sops/{version_id}` | 建完不能改 | 草稿编辑对话框：附件、适用能力、样本类型、培训要求；已发布只读 |
| 资源占用取消 | `POST /resource-bookings/{id}/cancel` | 无按钮 | 占用列表加取消，必填理由 |
| 检测任务取消 | `POST /analysis-tasks/{id}/cancel` | 无按钮 | 检测任务对话框加取消，必填理由；已采集完成的不给取消入口 |

**刻意不加界面入口的一项**：`POST /samples/{id}/flag`（过渡期人工质量标记）。它是历史质量
判定，不等于结果审核通过；给它一个按钮会让人拿它当复核用。历史数据处置仍可走接口。

真实浏览器补验（2026-09-22）：在当前 API 与 Vite 页面上分别以操作员和管理员登录，确认工作台、
任务中心、工位/设备适配器台账、账号成员、服务身份和拒绝访问记录可达；权限导航随角色变化。
浏览器控制台发现注销时 `invalidate()` 会在令牌删除后触发当前页面重取，产生一批无意义 401，现改为
注销只清缓存、不通知即将卸载的查询；重新登录/注销验证为 **0 errors**。生产构建通过，`web/dist`
不含演示口令或固定服务密钥。

## 汇总

- 完全由自动化用例覆盖：AC-03 至 AC-27、AC-29 至 AC-36、AC-39（其中 AC-07 含故障注入与 PostgreSQL 真并发，AC-12、AC-17、AC-25 含 PostgreSQL 真并发）。
- 自动化 + 手工共同覆盖：AC-01、AC-02、AC-38、AC-40。
- **未验证**：AC-37，阻塞于 DEC-02。

## 待业务决策（原文 DEC 段）仍未关闭的项

| 编号 | 事项 | 系统当前行为 |
|---|---|---|
| DEC-01 | 组织与项目的层级口径 | 单层组织 + 成员关系；`AccessContext` 可扩展第二层，不需要改仓储 |
| DEC-02 | 真实设备协议与超时语义 | 已有配置模型、密钥引用、驱动注册入口及可运行的 `http_json_v1` 网关契约；若设备不能适配该网关，仍需首台设备资料实现专用驱动并实测 |
| DEC-03 | 物料有效期与开封期的具体天数 | 不猜测天数，按批号上登记的日期判定；未登记即为未知，不放行 |
| DEC-04 | 资质到期预警窗口 | 由 `ILCS_QUALIFICATION_WARN_DAYS` 配置，默认值仅作缺省，不写进判据 |
| DEC-05 | 旧 `delivered_qty` 的口径 | 未迁入 `consumed_qty`，列在 `migrate.py verify` 的待确认清单里 |
| DEC-06 | 报告模板与签署格式 | 固定模板 `fixed-1.0`，模板版本随发布固化 |
| DEC-07 | 外部系统集成方向 | 只实现入向（服务身份 + `event_id` 幂等），不猜出向协议 |
