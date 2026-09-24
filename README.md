# ILCS 实验室平台

在原有批次中控系统上按《实验室平台开发需求文档》v1.0 增量扩展出的实验室平台：组织与人员资质、仪器设备台账、库存台账与三量分离、样本与检测任务、实验任务与六类流程节点（可按依赖图并行）、载具位置与可执行转运、数据复核与正式统计、报告审签发布、SOP 受控版本、统计与报表。分层与关键机制见 [ARCHITECTURE.md](ARCHITECTURE.md)。

原有能力全部保留：主数据与权限、方法配方与图形化编辑、批次事务、步骤级排程、执行门、开跑检查、电子签名、检查点与恢复、遥测曲线。

## 跑起来

```bash
cd ilcs/api
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 数据库：只支持 PostgreSQL。本机开发与测试用 Docker 拉起一个 PG 16（幂等，可重复执行）
cd ilcs && bash scripts/dev-db.sh
```

应用默认连 `postgresql+psycopg2://ilcs:ilcs-dev@127.0.0.1:55432/ilcs`，测试默认连同一实例的
`ilcs_test` 库；别的库用 `ILCS_DATABASE_URL` / `ILCS_TEST_DATABASE_URL` 指定。非 PostgreSQL
连接串在配置加载时就被拒绝——行锁、`SKIP LOCKED`、advisory lock、部分唯一索引与精确小数
都依赖 PostgreSQL，开发与测试必须跑在和正式环境同一种库上。

**建结构与播种是独立步骤，不由应用启动代劳。** 应用启动只校验库版本，不一致就拒绝进入可用状态（`/api/health` 返回 `schema_mismatch`，业务接口一律 503）。这是刻意的：多副本发布时让应用自己改结构，会出现两个版本各改一半。

```bash
cd ilcs
api/.venv/bin/python scripts/migrate.py upgrade --stamp-baseline   # 空库从头建；老库先打基线再升级
api/.venv/bin/python scripts/migrate.py seed --force               # 开发/演示：含演示资质、校准、方案与报警
api/.venv/bin/python scripts/migrate.py seed --force --master-only # 正式环境：只建组织、实验室、管理员
api/.venv/bin/python scripts/migrate.py status                     # 当前版本 vs 应用期待
api/.venv/bin/python scripts/migrate.py verify                     # 迁移结果核对，输出待人工确认项
```

```bash
# API
cd ilcs/api && .venv/bin/uvicorn app.main:app --reload --port 8011

# Web（另开终端）
cd ilcs/web && npm install && npm run dev

# 执行器 + 后台推进器（再开终端）；ILCS_AUTO_RESCHEDULE=1 时未下发批次的重排建议自动应用
cd ilcs && api/.venv/bin/python executor/main.py
```

打开 http://127.0.0.1:5174 ，OpenAPI 在 http://127.0.0.1:8011/docs 。

**执行器同一时刻只允许一个在工作。** PostgreSQL 上执行器启动时争用一把会话级 advisory lock，
拿不到的副本待命、主副本退出或断线后自动接管；不要为了"提高吞吐"并行跑多个执行器。
吞吐靠进程内按工位并发（`ILCS_EXECUTOR_WORKERS`，默认 8）：一台设备网关卡住只拖住它自己的线程。
新指令入队经 PostgreSQL `LISTEN/NOTIFY` 立即唤醒执行器，轮询周期只是兜底；界面经 `/api/stream`
接收变更推送，顶栏显示「实时 / 轮询」。

**CP-SAT 求解器默认启用。** 多批次优化会把 CP-SAT 求出的顺序作为候选，与内置顺序搜索（≤ 6 个批次穷举，
更多用局部搜索）的结果一起比较，时间窗仍由排程器生成。`ortools` 9.15 要求 protobuf < 6.34，所以
`requirements.txt` 把 gRPC 栈固定在 1.81 / protobuf 6.33.6（同时满足 SiLA 2 驱动与 OR-Tools 的最高版本）；
不要单独升级 grpcio 或 protobuf。`ILCS_SCHEDULER_BACKEND=search` 可关闭 CP-SAT。

**执行器进程不是可选的。** 它同时承担设备执行与工作流推进：到期的等待节点由它唤醒并推进下一步。不跑它，等待节点会一直停在 `waiting`——刷新界面也没用，因为推进不是由浏览器请求触发的。

开发演示种子的账号口令一律 `ilcs1234`；正式 `--master-only` 初始化只创建一个管理员，从
`ILCS_INITIAL_PASSWORD` 取得临时口令，并强制首次登录修改。首次播种完成后应立即从
`deploy/.env` 删除或清空 `ILCS_INITIAL_PASSWORD`，它不是运行期配置，也不应长期留在服务器。
角色决定可执行的写操作。下表是出厂默认；管理员在 **系统管理 · 用户与权限** 里可以给账号分配多个角色（权限取并集），并在「角色与权限」矩阵里按角色勾选权限（签名保存、留审计、版本化）。系统管理员恒有全部权限。矩阵不改变职责分离——同一个人仍不能批准、复核自己提交或录入的内容；执行类动作仍要看人员资质。测试环境可在 `deploy/.env` 设 `ILCS_ADMIN_SELF_APPROVAL=1`，只对系统管理员放行自审，每次记「测试环境管理员自审」审计；正式环境开启即判配置不合格。

| 账号 | 角色 | 能做的关键动作（默认） |
|---|---|---|
| `operator` | 操作员 | 接单、建批次、排程、下发、人工记录、保持、恢复、终止 |
| `researcher` | 研究员 | 实验方案、方法配方、实验任务分配、检测任务、报告起草 |
| `qa` | QA 负责人 | 方案 / 方法 / SOP / 报告审批与发布、流程审核节点、数据复核、批号放行 |
| `ehs` | EHS 专员 | 报警处置、危废换桶 |
| `admin` | 系统管理员 | 全部权限；独有：账号、角色与权限管理、服务身份 |

设备与检测系统不用人账号，走服务身份（`X-Service-Source` + `X-Service-Secret`）。种子里有两个：`executor-sim`（工位与检测任务全量）与 `lims-ec`（电性能检测）。口令明文只在 `POST /service-identities` 的签发响应里出现一次，库里只存摘要，日志不记录明文。生产环境用 `/service-identities` 重新签发（或 `/service-identities/{id}/rotate` 轮换），别用种子口令。

受控文件起草可直接使用 [SOP 模板](docs/SOP模板.md)：包含安全、资质、设备校准、物料放行、逐步参数、数据完整性、异常恢复、清洗危废、变更历史和发布前核对。真实设备接入按 [设备适配器配置模板](docs/设备适配器配置模板.md) 填写，协议示例、秘密引用、驱动契约与实机核对项均在其中。
正式切换前由业务、QA、设备安全与运维共同填写 [上线前输入与授权清单](docs/上线前输入与授权清单.md)，明确选择“全新正式库”还是“沿用现有 PostgreSQL 库升级”，两条路径不得混用。

## 功能地图

| 导航组 | 页面 | 承载的需求 |
|---|---|---|
| 工作台 | 工作台 / 任务中心 | 待办聚合、执行门、报警、到期资质与批号；运行指标（完成数、自动化成功率、异常与平均恢复时长、设备实际利用率与计划负荷）；实验任务分配与接单、人工待办、流程审核 |
| 实验设计 | 实验方案 / 实验流程 / 设备方法 / SOP 规程 | 条件矩阵、方案审批版本、流程节点（设备 / 人工 / 等待 / 审核 / 质检关卡 / 样本拆分 / 条件分支 / 子流程）与前驱依赖、回环、业务事件等待、步骤级超时与可跳过、设备方法版本与参数范围、SOP 受控版本与校验和 |
| 执行与监控 | 样本管理 / 批次管理 / 排程 / 现场监控 / 报警处理 / 异常处理 | 物理样本接收与运行分配、批次创建与运行、步骤级排程与多批次优化（优化 / 截止时间 / 优先级 / 先进先出）、重排建议、工位实时状态与载具位置、扫码放置、依赖图执行、报警确认与搁置、异常事件与处理策略库 |
| 数据与报告 | 数据审核 / 结果分析 / 报告管理 / 指标与规则 | 逐条数据复核、正式统计与排除说明、报告审签发布、指标版本与前后逻辑规则 |
| 资源管理 | 仪器设备 / 工位配置 / 试剂耗材 / 人员与资质 / 环境监测 | 设备台账与可用性、工位能力极限与设备适配器、库存三量与台账、资质与到期预警、环境读数与人工抄录 |
| 系统管理 | 用户与权限 / 集成与通知 / 审计日志 | 账号创建、多角色分配、角色权限矩阵、成员状态、首次改密与口令重置；服务身份签发、授权编辑、密钥轮换、停用；拒绝访问日志；Webhook 订阅、签名密钥、企业微信 / 钉钉机器人与邮件通知、投递记录与重投；全量审计日志 |

## 验证

```bash
cd ilcs && bash scripts/dev-db.sh                   # 确保本机 PG 在跑
cd ilcs/api && .venv/bin/pytest -q                  # 领域单测 + API 集成测试 + PG 并发用例
cd ilcs && api/.venv/bin/python scripts/smoke.py    # 端到端闭环（api 与 executor 需在跑）
cd ilcs && api/.venv/bin/python scripts/export-openapi.py # 接口变更后刷新契约快照

# 指到别的 PostgreSQL（测试库名必须以 _test 结尾或 test_ 开头；会话前后 DROP public schema）
cd ilcs/api && ILCS_TEST_DATABASE_URL=postgresql+psycopg2://... .venv/bin/pytest -q
```

`scripts/smoke.py` 只走 HTTP，因此也能对着部署环境跑；它刻意断言几条容易被做反的规则（锁定≠已审批、缺项不推进、提交人不能自审、采集完成≠质量有效）。默认联调方法的等待节点为 3 秒，冒烟会等待独立执行器真正唤醒它，再继续 QA 流程审核、检测回传与报告发布；若改用更长的正式方法，超出 45 秒会明确失败，不再跳过节点后仍报告全链路通过。

主要验收点：

| 用例 | 预期 |
|---|---|
| 应用对着落后 / 超前的库启动 | 拒绝进入可用状态，`/api/health` 说明版本差异，业务接口 503 |
| 重跑同一版迁移 | 幂等，不重复建列与索引；`downgrade` 到数据迁移那一版直接拒绝 |
| 跨组织读写别的组织的对象 | 404（不是 403——403 会泄露"该对象存在"） |
| 请求体里塞 `organization_id` | 被忽略，作用域只由成员关系决定 |
| 锁定但未审批的方案建批次 | 409 `plan_not_approved`；矩阵锁定不等于已审批 |
| 作者审批自己的方案 / 报告 / SOP / 数据 | 403 |
| 人工节点缺必填项或未勾核对 | 409，逐条列出缺哪项 |
| 等待节点到期 | 后台推进器唤醒并推进下一步，无需任何浏览器请求 |
| 审核节点被驳回 | 回到被审核的那一步重做，中间的设备步骤不被跳过 |
| 空 BOM / 无设备步骤的流程 | 正常放行，不被"没有 BOM""没有可承接工位"误拦 |
| 20 g BOM 的批次预留 | 占用 20 g，不是 40 g（预留行与台账事件不重复计） |
| 库存预留 / 消耗 / 归还 | 账面、占用、可用三量独立；台账只追加，精确小数，不按步骤比例推算消耗 |
| 改已放行批号的数量 / 调到低于已预留量 | 409，走盘点调整 |
| 无服务凭据回传结果 | 401，没有匿名兼容路径 |
| 同一 `event_id` 重复回传 | 回放原结果，不产生第二条结果版本 |
| 结果采集完成但未复核 | 不进正式统计；报告里以排除说明列出 |
| 审核通过但质量判定无效 | 排除并标注"已审核但质量判定无效" |
| 发布报告 | 固化结果版本 / 算法版本 / 模板版本 / PDF 摘要 / 签名，之后只读 |
| 重复创建 / 下发（同 `Idempotency-Key`） | 返回同一业务结果，设备不二次动作 |
| 执行器重启后设备实态不一致 | 指令 `unknown`、批次 `fault`，转人工核查，不自动重试 |
| 安全联锁或执行器停止 | 全站执行门关闭，排程、下发与续跑返回 423；队列里的动作指令不投递 |
| 单台设备失联或心跳超时 | 只挡用到它的批次：排程绕开、下发与续跑 423 并指明设备；其他批次照常 |
| 执行门关闭时请求保持 / 终止 | 照常执行：安全动作不受执行门限制 |
| 设备报「不接受指令」同时报联锁 | 联锁照样关闭执行门，不因设备停用或拒动作而被忽略 |
| 保持时首条动作还在队列 | 撤回该指令、设备侧不动作；续跑按首次下发，设备只执行一次 |
| 保持 / 终止时设备有在途动作 | 发保持 / 终止指令并以设备回执为准；不覆盖工位的在途指令、不触发对账误判 |
| 正式环境里工位仍是模拟适配器 | 下发 409 `simulation_adapter`，执行器也拒绝投递；正式环境不允许模拟心跳 |
| 推进事件遇到数据库抖动 | 人工记录与签名保留，事件退避重试；超过次数判失败并对批次报警 |
| 保持中走完最后一步 | 批次仍为保持，恢复评估确认后才结束 |
| 转运车忙把开工推迟到硬时限之外 | 排程拒绝，硬时限按推迟后的开工时间判定 |
| 两人同时排同一工位 | 排程写入串行化，后到者在锁内重读时间线，不产生重叠占用 |
| 复校不合格 / 合格校准未填有效期 | 最近一次校准为准，不合格即阻塞；合格校准必须填有效期 |
| 作者或提交人批准自己的方法（含管理员） | 403 `self_approval_denied`；审批签名必须针对该方法的当前版本 |
| 删掉步骤后再加一步 / 删掉修订后再建修订 | 步骤 ID、修订号、修订版本号都不复用；修订版发布即退役来源版本 |
| 96 孔板、全因子矩阵的物料预览 | 按 8×12 编号；物料需求乘上其他因子的组合数 |
| 「业务事件」等待节点 | 由批次信号唤醒（人：`batch.signal`；服务身份：`batch_signals` 授权事件名）；早到的信号先登记，节点开出时消费；同一 `event_id` 只唤醒一次 |
| 条件分支 | 按上游测量值 / 人工记录字段 / 人工选择取出口；没走的路径记为「未走此分支」并归还时间窗；判据缺失且无默认出口时保持，QA 签名选出口 |
| 分支回环 | 回环体作废后从目标重做，到 `max_loops` 转人工；回环体有通往体外的后继时方法校验不通过 |
| 步骤超时 | 报警 / 判失败 / 自动跳过（需可跳过）；设备步骤只允许报警 |
| 运行时跳过 / 从指定节点重做 | 跳过只限方法标了可跳过的步骤，签名；设备已收到指令或结果未知时拒绝。重做只在保持或故障且无在途指令时可用，审计列出将重新执行的设备步骤 |
| 指令没离开系统就被拒（失联、心跳超时） | 有改派策略时改派到等价工位继续、有重试策略时延时重下，次数到上限转人工；没有策略照旧故障转人工。设备可能已收到的指令、安全联锁一律转人工 |
| 工位失联 | 登记影响面；策略要求改派时挪走未开始的时间窗，否则生成重排建议待调度确认；建议生成后时间线被改过则 409 `proposal_stale` |
| 紧急插单 | 优先级 1 的批次按现有时间线晚于交付期时，生成让低优先级未下发批次让路的重排建议 |
| 任务拆分与依赖 | 父任务不绑定批次、状态由子任务汇总；上游没排程时下游排程 409 `dependency_unscheduled`，下游排在上游结束之后；上游运行没结束时下游开跑检查「上游任务」阻塞；优化只接受保持依赖顺序的候选 |
| 出向事件 | 与业务写入同一事务进发件箱，保存点回滚的不发；HMAC 签名、事件编号去重、指数退避、死信；只向允许清单里的主机投递 |
| 子流程 | 只能引用已发布方法；建批次时展开进快照，BOM 合计并入；循环引用、嵌套超过 3 层、引用失效都拒绝 |
| 从某一步重排、期望时间早于上一步结束 | 尾段从上一步实际 / 计划结束起排，换工位排转运；已结束批次 409 |
| 资质在计划执行时段中途到期 | 分配、下发、恢复都按整段时间判定并拦下 |
| 设备回执 done / failed | 写检查点、结束指令、释放工位后推进；重复回执只回放 |
| 结果未知的指令 | 现场核查三选一并签名：已执行（写人工核实检查点）/ 未执行（可重试）/ 部分执行（只能终止） |
| 设备离线时终止 | 终止指令结果未知；现场确认已安全停机并签名后批次终止 |
| 软件生成的报警 | 操作员写明原因并签名清除条件；设备侧报警只接受设备上报恢复 |
| 设备失联 / 心跳超时 / 联锁 / 校准临期 | 按条件去重报警，条件消除后自动复位；停用的适配器心跳 409 |
| 执行器停止上报存活 | 执行门关闭（`ILCS_EXECUTOR_STALE_SEC`，默认 60 s） |
| 设备指令迟迟没有结论 | 超过预计时长 1.5 倍 + 5 min 报警；超过 3 倍 + 5 min（或能力 `maxRunMin`）转结果未知、人工核查 |
| 两人同时改工位台账 / 方法草稿 | 后提交者 409，不做后写覆盖前写 |
| 资产有维护 / 校准预约、处于维护状态、多工位共享容量 | 排程绕开维护时段并按资产容量排；维护中的资产不承接排程 |
| 比计划提前 15 分钟以上下发 | 开跑检查拦下；设备指令到时间窗前 15 分钟才投递 |
| 上游提前完成 / 晚开工 | 提前：不与别的批次冲突就把剩余时间窗整体提前；晚：整体顺延，冲突只报警不挤占 |
| 矩阵因子声明了作用参数 | 方案校验逐水平核对工位参数范围；一条设备指令带全部孔位参数（`params.wells`） |
| 设备回执里报了实际消耗 | 按指令号去重直接入库存消耗；超预留或对不上预留不入账并报警；偏差超 5% 入账并报警待复核 |
| 设备遥测上报 | 同一 `event_id` 只入库一次；设备时钟超前 5 分钟整批拒收；保留 1 年 |
| 维护工单 | 建单即登记维护占用；开工资产转维护状态；完工写记录签名，不合格资产保持维护状态 |
| 工位接到 SiLA 2（`sila2_v1`）/ Modbus TCP（`modbus_tcp_v1`）/ OPC UA（`opcua_v1`）设备 | 执行器主动探测在线；联锁 / 参数非法 / 忙为明确失败，断连 / 超时 / 回执丢失为结果未知，不重发 |
| Modbus 指令带孔位矩阵、未映射的参数或能力 | 驱动直接拒绝，不写触发寄存器 |
| Modbus 写了触发等不到应答 / 执行器重启 | 结果未知不重写触发；新实例从设备当前的触发与应答序号里较大的一个接着编号 |
| OPC UA 未钉住服务器证书、客户端证书不受信任、正式环境用 None 安全策略 | 配置或握手阶段拒绝 |
| 正式环境接入自报为模拟器的设备（任一协议） | 健康检查拒绝 |
| 质检关卡测量值超限 | 按方法配置返工（超过次数转 QA）/ 报废 / 保持待 QA 签名判定；取不到数值一律不放行 |
| 逐孔位质检 | 不合格样本单独剔除，其余继续 |
| 样本拆分节点 | 每个样本拆出 N 个子样本，谱系指向母样，继承条件分组 |
| 多通道设备（`channels`） | 同一工位最多 N 个时间窗重叠；样品位不影响并行 |
| 外部优化器提案 | 须落在已批准方案的设计空间内；接受只生成下一轮方案草稿，仍需 QA 批准；拒绝的也留档 |
| 训练数据导出 | 整个实验活动各轮，只含复核通过、质量有效的当前结果版本 |
| 硬时限无法满足 | 排程拒绝并指出是第几步 |
| 编辑器里把某步时长改成 0 | 校验清单指出是第几步，提交评审 409 |
| 不可中断能力请求保持 | 409，给出能力侧的副作用说明 |
| 未下发即终止 | 立即终止并归还工位时间窗与预留 |
| 已下发但设备侧没有在途动作时终止 | 撤回队列指令并立即终止，不等待不存在的设备确认 |
| 资质过期的人员被分配任务 | 拒绝，并说明按预计执行时间判定 |
| 删除出过批次的方法 / 已锁定方案 / 已下发批次 | 409，列出具体引用与替代动作（退役 / 解锁 / 终止） |

验收用例 AC-01 至 AC-40 与自动化用例的对应关系、以及哪几项只有手工证据，见 [docs/acceptance-record.md](docs/acceptance-record.md)。

未验证项：**现场真实设备试点（AC-37）**。系统已内置 `http_json_v1`（HTTPS 网关）、`sila2_v1`、`modbus_tcp_v1`、`opcua_v1` 四个真实驱动，覆盖设备身份核对、凭据外置、命令去重、异步状态查询、保持、终止、超时分类和真实遥测；与各协议外部模拟设备的联调测试已通过。具体仪器仍需依据 DEC-02 提供厂商协议或网关并完成断联、重复回执与物理副作用实测。未注册驱动会明确拒绝，不回落到模拟器。

## 目录

```
api/         FastAPI 服务：core / models / domain / repositories / services / adapters / api
api/alembic/ 版本化迁移：0001 基线 → 0002 结构 → 0003 历史映射 → 0004 适配器配置 → 0005 样本关联 → 0006 服务身份并发版本 → 0007 账号生命周期 → 0008 推进事件重试计数 → 0009 运行加固 → 0010 按时开工 / 遥测 / 维护工单 → 0011 并行通道 / 设计空间 / 闭环提案 → 0012 角色权限 → 0013 队列索引 → 0014 执行器明细 → 0015 载具与位置 → 0016 流程控制 → 0017 任务树 → 0018 异常引擎 → 0019 重排建议 → 0020 出向事件
executor/    设备执行器 + 工作流推进器；接真实设备实现 adapters/ 契约
simulators/  外部模拟设备：SiLA 2 / Modbus TCP / OPC UA / HTTPS 网关，同一套设备行为与故障注入，见 simulators/README.md
web/         React 前端：shared 基础设施 + features 页面
scripts/     migrate.py（迁移入口）/ smoke.py（端到端冒烟）/ reset-demo.sh（演示环境重置）
contracts/   OpenAPI 快照；设备侧任务契约：sila2/（SiLA 2 特性）、modbus/（任务寄存器表）、opcua/（节点与方法）
docs/        需求文档与迁移报告
```

## 部署（10.10.106.51:8090）

入口覆盖原来的静态原型容器 `ilcs-console`（先停不删，原型文件仍在 `/opt/ilcs-console`）。PostgreSQL、API、执行器、前端由 `deploy/docker-compose.yml` 拉起，另有一个一次性的 `migrate` 任务。上传文件落在 `/opt/ilcs/data`，数据库落在 Compose 的 `ilcs_postgres` 持久卷。

**不要碰 80/443 上的公共网关**：本系统只占 8090，自带 nginx 容器。

部署前可在开发机执行 `bash scripts/validate-compose-stack.sh`。它用临时 PG 卷和动态回环端口
完整验证镜像构建、迁移、正式主数据初始化、API、执行器、nginx、首次登录与容器安全参数，
退出时自动清理；不会读取正式 `.env`、挂载仓库运行数据或连接目标服务器。

```bash
# 在开发机
cd ilcs/web && npm run build

# 先备份 PostgreSQL 与上传文件；pg_dump 是逻辑一致快照，不直接复制 PG 数据目录
ssh 10.10.106.51 'cd /opt/ilcs/deploy && sudo mkdir -p /opt/ilcs-backup && sudo docker compose exec -T db sh -c '\''pg_dump -U "$POSTGRES_USER" "$POSTGRES_DB"'\'' | sudo tee /opt/ilcs-backup/ilcs-$(date +%Y%m%d-%H%M%S).sql >/dev/null'
ssh 10.10.106.51 'sudo cp -a /opt/ilcs/data/files /opt/ilcs-backup/files-$(date +%Y%m%d-%H%M%S)'

cd ilcs && rsync -av --delete \
  --exclude '.venv' --exclude 'node_modules' --exclude '__pycache__' \
  --exclude '*.db' --exclude '*.db-shm' --exclude '*.db-wal' \
  --exclude '.pytest_cache' --exclude '.playwright-cli' --exclude '.DS_Store' \
  --exclude 'web/src' --exclude 'web/tsconfig.tsbuildinfo' --exclude 'api/tests' \
  --exclude 'data' --exclude 'secrets' --exclude 'deploy/.env' --exclude '.git' --exclude 'output' \
  ./  10.10.106.51:/opt/ilcs/

# 首次部署：从样例生成配置，把密钥改掉
ssh 10.10.106.51 'test -f /opt/ilcs/deploy/.env || cp /opt/ilcs/deploy/.env.example /opt/ilcs/deploy/.env'

# API/执行器使用固定非特权 UID 10001；持久目录和设备凭据按该 UID 授权。
# 真实设备凭据目录不进代码同步；文件示例见 docs/设备适配器配置模板.md
ssh 10.10.106.51 'sudo install -d -m 0770 -o 10001 -g 10001 /opt/ilcs/data /opt/ilcs/data/files && sudo install -d -m 0700 -o 10001 -g 10001 /opt/ilcs/secrets'

ssh 10.10.106.51 'cd /opt/ilcs/deploy && docker compose build'
# 在迁移或启动前离线检查正式配置；不连接数据库，也不会输出秘密原文
ssh 10.10.106.51 'cd /opt/ilcs/deploy && docker compose run --rm migrate python scripts/check-production-readiness.py --from-environment --skip-artifacts --initial-seed'
# 迁移先跑、单独跑：它会输出迁移报告到 /opt/ilcs/data/migration-report.md
ssh 10.10.106.51 'cd /opt/ilcs/deploy && docker compose run --rm migrate'
# 全新正式库再执行一次：只创建组织、默认实验室和管理员，不写任何演示业务数据
ssh 10.10.106.51 'cd /opt/ilcs/deploy && docker compose run --rm migrate python scripts/migrate.py seed --force --master-only'
ssh 10.10.106.51 'cd /opt/ilcs/deploy && docker compose up -d'
```

开发机也可以直接检查准备同步的配置与前端制品：

```bash
api/.venv/bin/python scripts/check-production-readiness.py \
  --env-file deploy/.env --initial-seed
```

首次播种成功并完成管理员改密后，删除 `.env` 中的 `ILCS_INITIAL_PASSWORD`，再不带
`--initial-seed` 复查；如果临时口令仍留在配置里，检查器会持续报警。检查器按文本解析
`KEY=VALUE`，不会 `source` 或执行配置文件中的 shell 内容。

顺序不能换：`api` 起来时库版本必须已经是当前版本，否则健康检查判为不健康、`executor` 也不会启动（它 `depends_on: api healthy`）。迁移失败就停在那一步，不会有一个"跑着但结构不对"的中间状态。

API、执行器和迁移任务在镜像内以 UID/GID `10001` 运行，根文件系统只读，只开放独立
`/tmp`，并删除全部 Linux capabilities。设备凭据文件应使用
`install -m 0400 -o 10001 -g 10001 <源文件> /opt/ilcs/secrets/<文件名>` 安装；不要为了省事
把密钥目录改成全员可读。若宿主已有数据目录，升级前先把 `/opt/ilcs/data` 的属主调整为
`10001:10001`，否则上传和迁移报告会因权限被拒绝。
前端 nginx 同样以非特权 UID/GID `101` 运行，容器内监听 8080，宿主仍映射为 8090；
它使用只读根文件系统和独立运行 tmpfs，也不保留 Linux capabilities。
Compose 将网络拆成 `frontend` 与 `backend`：nginx 只能访问 API，PostgreSQL 不加入前端
网络；API 作为唯一双网服务连接入口与数据库，避免前端容器直接探测数据库端口。
全部容器的本地 JSON 日志按单文件 10 MiB、最多 5 个文件轮转，避免长期运行耗尽宿主磁盘；
正式监控平台接管日志后可在 Compose 中替换驱动，但不要取消容量上限。

`ILCS_ENVIRONMENT=production` 时还有配置硬门禁：数据库必须是 PostgreSQL，令牌密钥与
口令 pepper 都必须是至少 32 位的非占位随机值，CORS 不能是通配来源，真实设备网关
必须用 `ILCS_ADAPTER_ALLOWED_HOSTS` 明确列白名单（禁止 `*`）。任一不满足时
`/api/health` 返回 `configuration_error`，业务接口 503。首次初始化后先用管理员临时口令
登录并改密，再到“用户与权限”创建或调整账号、签发设备/LIMS 凭据。若管理员无法登录，可在
停写维护窗口用 `ILCS_RESET_PASSWORD=... docker compose run --rm migrate python scripts/reset-account-password.py admin`
重置；新口令仍会被标为首次登录必须修改。

账号口令使用带独立随机盐的版本化 PBKDF2 摘要；旧库无盐摘要可继续登录，并在首次成功
登录时自动升级。nginx 对登录入口做共享限流并返回 429，API 容器不映射宿主端口，不能绕过
入口直连。`/api/health` 每次检查数据库连通性，运行中断库会返回
`database_unavailable`，不再只报告过期的启动状态。正式入口仍须由现有公共网关提供 HTTPS；
8090 的内网 HTTP 端口不应直接暴露到不受信网络。

`--exclude 'data'`、`--exclude 'secrets'` 与 `--exclude 'deploy/.env'` 不能省：本地没有这些运行数据，`--delete` 会连生产库、上传文件、设备凭据和容器配置一起删掉。
`ILCS_FILE_MAX_BYTES` 与 nginx 的 `client_max_body_size` 必须一起改：网关小于应用上限时，大文件会在网关被截断而应用侧看不到任何错误。
上传后尚未形成正式业务引用的文件默认保留 24 小时，执行器每小时分组织清理并写审计；
`ILCS_FILE_ORPHAN_RETENTION_HOURS`、`ILCS_FILE_CLEANUP_INTERVAL_SEC` 和
`ILCS_FILE_CLEANUP_BATCH_SIZE` 可调整窗口与批量。清理前会从 SOP、资质、校准、结果及报告表
反查引用，不能只凭 `ref_type/ref_id` 辅助字段判断；正式环境把保留期设为 0 会被配置门禁拒绝。

Compose 项目名固定为 `ilcs`。不要加 `--remove-orphans`，以免碰到同机其他 `deploy-*` 容器。

### 试点：外部 SiLA 2 模拟设备

真机到位前，可随 `ilcs` 项目启动两台外部 SiLA 2 模拟设备（配液工作站 `sila-sim-lh`、8 通道充放电柜
`sila-sim-cycler`），只在后端网络可见、不占宿主端口：

```bash
sudo install -d -m 0700 -o 10001 -g 10001 /opt/ilcs/secrets/sila     # 证书目录，模拟设备首次启动写入自签证书
# deploy/.env：ILCS_ADAPTER_ALLOWED_HOSTS 追加 sila-sim-lh,sila-sim-cycler
cd /opt/ilcs/deploy && docker compose --profile pilot up -d sila-sim-lh sila-sim-cycler
```

然后在「工位配置」页把试点工位的适配器改成 `kind=real`、`driver=sila2_v1`，配置示例见
[设备适配器配置模板](docs/设备适配器配置模板.md)；在线状态由执行器探测。模拟设备自报为模拟器，
`ILCS_ENVIRONMENT=production` 时会被拒绝接入。故障注入与验收用法见 [simulators/README.md](simulators/README.md)。

其他协议同样有外部模拟设备（同一 `pilot` profile），和 SiLA 2 那两台共用设备行为与故障注入：

| 服务 | 协议 / 驱动 | 试点工位 | 凭据目录 |
|---|---|---|---|
| `modbus-sim-mixer`（SIM-MIX-01） | Modbus TCP / `modbus_tcp_v1` | ST-02 中试匀浆罐 | 无（明文 Modbus，只在后端网络） |
| `opcua-sim-calender`（SIM-CAL-01） | OPC UA / `opcua_v1`，Basic256Sha256 + SignAndEncrypt | ST-04 辊压冲切机 | `secrets/opcua/` |
| `gateway-sim-coater`（SIM-COAT-01） | HTTPS JSON / `http_json_v1`，TLS + Bearer 令牌 | ST-03 涂布烘干线 | `secrets/gateway/` |

```bash
sudo install -d -m 0700 -o 10001 -g 10001 /opt/ilcs/secrets/opcua /opt/ilcs/secrets/gateway
# deploy/.env：ILCS_ADAPTER_ALLOWED_HOSTS 再追加 modbus-sim-mixer,opcua-sim-calender,gateway-sim-coater
docker compose --profile pilot up -d modbus-sim-mixer opcua-sim-calender gateway-sim-coater
docker compose exec api python ../scripts/configure-pilot-adapters.py apply \
  --station ST-02=modbus_tcp_v1@modbus-sim-mixer:5020:SIM-MIX-01 \
  --station ST-04=opcua_v1@opcua-sim-calender:4840:SIM-CAL-01 \
  --station ST-03=http_json_v1@gateway-sim-coater:8443:SIM-COAT-01
```
部署窗口里也可以用 `scripts/configure-pilot-adapters.py apply|revert` 批量切换并留审计。完整的手工演练路径
（方法修订 → 矩阵方案 → 排程下发 → 质检关卡 → 多通道 → 故障演练 → 闭环提案）见 [试点操作案例](docs/试点操作案例.md)；`scripts/reset-pilot-case.sh` 可把演示库重置为该案例跑完的结果。

### PostgreSQL 与附件备份恢复演练

正式部署后定期执行可恢复性演练，而不只生成一个从未验证过的备份：

```bash
ssh 10.10.106.51 'sudo bash /opt/ilcs/scripts/backup-and-recovery-drill.sh'
```

脚本先记录 `api`、`executor` 原有运行状态，只暂停当时正在运行的服务；随后生成 PostgreSQL
自定义格式备份、附件归档、逐文件摘要和制品摘要，并立即恢复原先运行的服务。备份会恢复到临时
`ilcs_restore_*` 数据库和独立附件目录，用当前应用版本执行迁移版本与业务一致性核对；结束后
删除演练库和临时附件，但保留 `/opt/ilcs-backup/backup-<时间>/` 下的备份与
`recovery-report.md`。任何一步失败均返回非零状态，退出处理仍会恢复原先运行的服务。

可用 `ILCS_ROOT`、`ILCS_BACKUP_ROOT` 覆盖部署与备份根目录；脚本拒绝 `/`、`/opt` 等过宽
目录，并用文件锁防止两次演练并发。恢复报告和备份制品应纳入运维留存；备份目录应放到独立
磁盘或对象存储，不能只留在数据库同一宿主机。

回滚到静态原型：`docker compose -f /opt/ilcs/deploy/docker-compose.yml down && docker start ilcs-console`。

### 正式环境不要播种演示背书

`seed` 默认会建 15 条资质、9 台带校准记录的资产和 2 份已发布的 SOP。这些在演示库上没问题（备注写明了是种子数据），但它们是**演示用的背书**：校准记录的证书字段是空的，而系统本身要求「合格校准必须附证书」；SOP 是「已发布、已生效、里面什么都没有」，作者与批准人还挂在真人账号上。播到正式库里，会让人以为设备真的校准过、文件真的批过。

正式环境用 `--master-only`：只建 `.env` 指定的组织、默认实验室和一个管理员账号。
它不创建其他角色账号、服务身份、能力、工位、适配器、方法、指标、物料、批号、期初库存、
人员资质、资产校准、SOP、方案或报警。管理员首次改密后，在“用户与权限”和相应业务页面按
真实资料逐项建立；设备/LIMS 密钥由治理页面一次性签发，不能沿用演示固定密钥。

`ILCS_MIGRATION_ORG_ID/CODE/NAME/TIMEZONE` 在首次初始化后即成为正式组织事实，执行前必须
填写真实值。需要迁移旧库时，下一节的补录脚本只搬已有账号和工位事实，不生成任何资质、
校准、库存或审批背书。

### 从老库补录人员与资产档案

老库有工位行和账号，但没有人员档案与资产档案，而「执行人资质」「设备校准」两项开跑检查要靠它们。`scripts/backfill-master-data.py` 把已有的事实搬过去：按账号建人员档案（姓名取 `display_name`，职务取角色），按工位建资产档案（名称/型号/功能岛照抄，一个工位一台，容量 1）。

```bash
ssh 10.10.106.51 'cd /opt/ilcs/deploy && sudo docker compose run --rm migrate python scripts/backfill-master-data.py plan'
ssh 10.10.106.51 'cd /opt/ilcs/deploy && sudo docker compose run --rm migrate python scripts/backfill-master-data.py apply'
```

`apply` 会在 `/data/backfill-<时间戳>.json` 留下清单，`undo <清单>` 按它回退（挂过资质或校准的档案会被保留，不删别人的工作成果）。重复跑是幂等的。

**它不建校准记录，也不建资质记录**，所以跑完批次仍然下发不了：工位上的 `cal_due` 只有到期日，没有校准日期与证书编号，而系统要求合格校准必须附证书（`certificate_required`）；谁有哪项资质更是老库里从来没有的信息。脚本只把「无法校验」变成「缺哪一项」，证书与资质要责任人在界面上补。真正不适用校准的资源（手工工作台一类）在资产上标 `calibration_applicable=false` 并写明理由。

### 重置演示数据

演示环境用久了会堆下点测残留：重复的修订草稿、被点开没锁回去的实验矩阵、历次冒烟留下的半截批次。这些在界面上和真实记录长得一样，看的人分不清。种子只建主数据、不建批次，所以换一个空库再迁移播种就是最干净的基线，比逐表 `DELETE` 少踩外键顺序的坑。

```bash
ssh 10.10.106.51 'sudo bash /opt/ilcs/scripts/reset-demo.sh'
```

它会：停 api/executor → 用 `pg_dump` 备份现库并复制 `files/` → 重建空 PG 演示库 → 建结构 → 播种 → 启动 → 跑一轮首期完整流程。

批次备注由 `ILCS_BATCH_NOTE` 决定，默认「首轮完整流程验证」。只想补跑流程、不动现有数据就加 `--keep-data`。回退：停 api/executor，把 `pg-dump-<时间戳>.sql` 恢复到空库，并把对应 `files-<时间戳>/` 恢复到 `/opt/ilcs/data/files/`，再启动。

## 后续演进

按需求文档 FUT 段继续延后的项：NATS JetStream、Keycloak OIDC、真实工位驱动实现、TimescaleDB 遥测、CP-SAT 直接出时间窗（当前作为优化候选）、AGV 车队系统对接、外部 LIMS/ERP 双向集成。PostgreSQL 的部署、迁移和双数据库回归已纳入当前基线；生产切换仍需在维护窗口执行并留存核对报告。每项的落点见 [ARCHITECTURE.md](ARCHITECTURE.md) 末节。

## 站外通知配置

企业微信 / 钉钉群机器人的官方地址（`qyapi.weixin.qq.com`、`oapi.dingtalk.com`）默认放行，其余 Webhook 主机仍要进
`ILCS_WEBHOOK_ALLOWED_HOSTS`。邮件与消息里的链接在 `deploy/.env` 里配：

```
ILCS_PUBLIC_URL=https://ilcs.lab.internal
ILCS_SMTP_HOST=smtp.lab.internal
ILCS_SMTP_PORT=587
ILCS_SMTP_USER=ilcs-notify
ILCS_SMTP_PASSWORD=...
ILCS_SMTP_FROM=ilcs-notify@lab.internal
ILCS_SMTP_STARTTLS=true
```
