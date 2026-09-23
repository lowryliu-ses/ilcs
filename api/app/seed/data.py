"""初始数据集。与 `prototype-final/data.js` 的领域数据保持一致，便于原型与系统对照验收。"""

USERS = [
    ("researcher", "研究员", "researcher", "ilcs1234"),
    ("qa", "QA 负责人", "qa", "ilcs1234"),
    ("operator", "操作员", "operator", "ilcs1234"),
    ("ehs", "EHS 专员", "ehs", "ilcs1234"),
    ("admin", "系统管理员", "admin", "ilcs1234"),
]

CAPABILITIES = [
    ("cap.dose_solid", "粉料计量投加", {"mass": "投加质量 g"}, {
        "maxHoldMin": 30, "pausable": True, "hold": "料斗闸板关闭，天平保持读数", "retryable": False,
        "sideEffect": "重试会重复投加粉料，只允许按天平累计质量补齐差值",
        "verify": ["天平累计投加质量", "料斗余量"]}),
    ("cap.dose_liquid", "液体计量加注", {"volume": "加注体积 mL", "rate": "流速 mL/min"}, {
        "maxHoldMin": 20, "pausable": True, "hold": "泵停止，加注阀关闭", "retryable": False,
        "sideEffect": "重试会重复加注，只允许按泵计量体积续跑",
        "verify": ["泵计量体积", "容器称重变化"]}),
    ("cap.mix", "控温匀浆", {"temp": "浆料温度 ℃", "rpm": "匀浆转速 rpm"}, {
        "maxHoldMin": 60, "pausable": True, "hold": "降至 200 rpm 防沉降搅拌，夹套控温保持", "retryable": False,
        "sideEffect": "重复匀浆会改变浆料剪切历史与粘度，不允许重试",
        "verify": ["浆料温度", "累计匀浆时间"]}),
    ("cap.degas", "真空脱泡", {"vacuum": "真空度 mbar"}, {
        "maxHoldMin": 45, "pausable": True, "hold": "缓慢回充氮气至常压，容器密封", "retryable": True,
        "sideEffect": "重复脱泡无物料损失，仅增加时间与溶剂挥发",
        "verify": ["当前真空度", "浆料液位"]}),
    ("cap.coat", "涂布烘干", {"thickness": "湿膜厚度 μm", "temp": "烘箱温度 ℃"}, {
        "maxHoldMin": 20, "pausable": True, "hold": "走带停止，涂布头抬起，烘箱维持设定温度", "retryable": False,
        "sideEffect": "停机点留下厚度突变，该段极片需剔除；重新涂布会重复消耗浆料",
        "verify": ["已涂布长度", "烘箱实际温度", "涂布头结皮情况"]}),
    ("cap.calender", "辊压冲切", {"gap": "辊缝 μm", "diameter": "冲切直径 mm"}, {
        "maxHoldMin": 90, "pausable": True, "hold": "辊缝打开，走带停止", "retryable": False,
        "sideEffect": "辊压不可逆，重复辊压会过压实并超出压实密度上限",
        "verify": ["已辊压长度", "实测辊缝"]}),
    ("cap.vacuum_dry", "极片真空干燥", {"temp": "箱温 ℃", "vacuum": "真空度 mbar"}, {
        "maxHoldMin": 120, "pausable": True, "hold": "维持真空与箱温，不开箱", "retryable": True,
        "sideEffect": "可延长干燥，但累计高温时间需记入极片履历",
        "verify": ["箱内真空度", "累计干燥时间"]}),
    ("cap.weigh", "精密称重选片", {"mass": "单片质量 g"}, {
        "maxHoldMin": 240, "pausable": True, "hold": "天平待机去皮", "retryable": True,
        "sideEffect": "重复称重无副作用", "verify": []}),
    ("cap.assemble", "扣电组装", {"electrolyte": "注液量 μL"}, {
        "maxHoldMin": 0, "pausable": False, "hold": "", "retryable": False,
        "sideEffect": "注液到封口之间不可中断，异常时进入人工核查",
        "verify": ["已封口电芯数量", "手套箱水含量与氧含量"]}),
    ("cap.test", "电性能测试", {"rate": "倍率 C", "vmax": "充电截止电压 V"}, {
        "maxHoldMin": 120, "pausable": True, "hold": "通道断电，电芯开路静置", "retryable": True,
        "sideEffect": "重测记为新的循环序列，原始数据保留不覆盖",
        "verify": ["已完成循环数", "当前开路电压"]}),
    ("cap.transfer", "托盘转运", {}, {
        "maxHoldMin": 60, "pausable": True, "hold": "AGV 原地停车，托盘锁紧", "retryable": True,
        "sideEffect": "需先确认托盘实际位置与极片盒编号", "verify": ["托盘当前位置"]}),
]

ISLANDS = [
    (1, "岛 #1 高通量浆料制备"), (2, "岛 #2 中试匀浆"), (3, "岛 #3 涂布烘干"),
    (4, "岛 #4 极片后处理"), (5, "岛 #5 扣电组装"), (6, "岛 #6 电性能测试"),
]

SLURRY_LIMITS = {
    "cap.dose_solid": {"mass": [0.001, 50]},
    "cap.dose_liquid": {"volume": [0.1, 200], "rate": [0.1, 20]},
    "cap.mix": {"temp": [15, 80], "rpm": [0, 3000]},
    "cap.degas": {"vacuum": [5, 900]},
}

STATIONS = [
    dict(id="ST-01-A", island=1, name="高通量匀浆站 A", model="EXP-SLURRY-24", status="idle",
         cal_due="2026-12-02", positions=24, clean=True, limits=SLURRY_LIMITS),
    dict(id="ST-01-B", island=1, name="高通量匀浆站 B", model="EXP-SLURRY-24", status="idle",
         cal_due="2026-12-02", positions=24, clean=True, limits=SLURRY_LIMITS),
    dict(id="ST-02", island=2, name="中试匀浆罐", model="PILOT-MIX-8", status="idle",
         cal_due="2026-10-30", positions=8, clean=True, limits={
             "cap.dose_solid": {"mass": [1, 2000]},
             "cap.dose_liquid": {"volume": [10, 5000], "rate": [1, 100]},
             "cap.mix": {"temp": [15, 60], "rpm": [0, 1200]},
             "cap.degas": {"vacuum": [50, 900]}}),
    dict(id="ST-03", island=3, name="涂布烘干线", model="COATER-150", status="idle",
         cal_due="2027-01-15", positions=1, clean=True,
         limits={"cap.coat": {"thickness": [20, 400], "temp": [40, 150]}}),
    dict(id="ST-04", island=4, name="辊压冲切机", model="CALENDER-P2", status="idle",
         cal_due="2026-11-20", positions=1, clean=True,
         limits={"cap.calender": {"gap": [10, 200], "diameter": [8, 20]}}),
    dict(id="ST-05", island=4, name="真空干燥与称重站", model="VAC-WEIGH-12", status="idle",
         cal_due="2026-12-29", positions=12, clean=True, limits={
             "cap.vacuum_dry": {"temp": [40, 160], "vacuum": [0.1, 100]},
             "cap.weigh": {"mass": [0.00001, 200]}}),
    dict(id="ST-06", island=5, name="手套箱组装线", model="GB-ASSY-8", status="idle",
         cal_due="2027-03-17", positions=8, clean=True,
         limits={"cap.assemble": {"electrolyte": [10, 200]}}),
    dict(id="ST-07", island=6, name="充放电测试柜", model="CYCLER-32", status="idle", channels=8,
         cal_due="2027-02-10", positions=32, clean=True,
         limits={"cap.test": {"rate": [0.01, 10], "vmax": [2.0, 5.0]}}),
    dict(id="AGV-01", island=0, name="AGV-01", model="MiR-250", status="idle",
         cal_due="-", positions=1, clean=True, limits={"cap.transfer": {}}),
    dict(id="AGV-02", island=0, name="AGV-02", model="MiR-250", status="idle",
         cal_due="-", positions=1, clean=True, limits={"cap.transfer": {}}),
]

ADAPTERS = [
    ("ST-01-A", "SiLA 2", "1.1", ""), ("ST-01-B", "SiLA 2", "1.1", ""),
    ("ST-02", "自定义 · Modbus/TCP", "0.9", ""), ("ST-03", "SiLA 2", "1.0", ""),
    ("ST-04", "自定义 · OPC UA", "0.7", "心跳间隔偏长，超过 5 s 阈值时标记为降级，仍接受查询"),
    ("ST-05", "SiLA 2", "1.1", ""), ("ST-06", "SiLA 2", "1.1", ""),
    ("ST-07", "自定义 · 厂商 SDK", "2.3", ""),
    ("AGV-01", "Open-RMF", "23.12", ""), ("AGV-02", "Open-RMF", "23.12", ""),
]

RECIPES = [
    dict(
        id="R-201", name="NCM811 正极高通量配方筛选", version="1.2.0", state="released", owner="研究员",
        updated="2026-09-10", plate=24, risk="RA-201 v3",
        design="8 组导电剂与粘结剂配比 × 3 次重复，随机化种子 20260910",
        bom=[{"material": "NMP 溶剂", "qty": 0.6, "unit": "L"}, {"material": "NCM811 正极粉", "qty": 120, "unit": "g"}],
        steps=[
            {"name": "活性物质与助剂投加", "cap": "cap.dose_solid", "consumes_materials": True, "params": {"mass": 18}, "dur": 20},
            {"name": "NMP 溶剂加注", "cap": "cap.dose_liquid", "consumes_materials": True, "params": {"volume": 45, "rate": 4}, "dur": 10},
            {"name": "控温匀浆", "cap": "cap.mix", "params": {"temp": 25, "rpm": 2000}, "dur": 60},
            {"name": "真空脱泡", "cap": "cap.degas", "params": {"vacuum": 20}, "dur": 15},
            {"name": "涂布烘干", "cap": "cap.coat", "params": {"thickness": 180, "temp": 110}, "dur": 40,
             "hard": {"from": "脱泡结束", "maxGapMin": 30}},
            {"name": "辊压冲切", "cap": "cap.calender", "params": {"gap": 65, "diameter": 14}, "dur": 25},
        ],
        history=[
            {"v": "1.0.0", "state": "retired", "note": "基线配方", "by": "研究员", "at": "2026-06-01"},
            {"v": "1.1.0", "state": "retired", "note": "匀浆时间延长至 60 min", "by": "研究员", "at": "2026-07-20"},
            {"v": "1.2.0", "state": "released", "note": "增加真空脱泡与涂布硬时限", "by": "QA 负责人", "at": "2026-09-10"},
        ],
    ),
    dict(
        id="R-205", name="扣电组装与首次充放电", version="2.0.1", state="released", owner="研究员",
        updated="2026-08-28", plate=8, risk="RA-205 v2", design="8 通道独立电解液配方，各 1 次",
        bom=[{"material": "电解液 LP57", "qty": 1, "unit": "mL"}],
        steps=[
            {"name": "极片真空干燥", "cap": "cap.vacuum_dry", "params": {"temp": 120, "vacuum": 1}, "dur": 60},
            {"name": "称重选片", "cap": "cap.weigh", "params": {"mass": 0.0152}, "dur": 15,
             "hard": {"from": "真空干燥结束", "maxGapMin": 15}},
            {"name": "注液封口组装", "cap": "cap.assemble", "consumes_materials": True, "params": {"electrolyte": 60}, "dur": 30,
             "hard": {"from": "称重结束", "maxGapMin": 20}},
            {"name": "首次充放电测试", "cap": "cap.test", "params": {"rate": 0.1, "vmax": 4.3}, "dur": 45},
        ],
        history=[{"v": "2.0.1", "state": "released", "note": "注液量由 80 μL 调整为 60 μL",
                  "by": "QA 负责人", "at": "2026-08-28"}],
    ),
    dict(
        id="R-203", name="硅碳负极配方 C 系列", version="0.3.0", state="review", owner="研究员",
        updated="2026-09-16", plate=8, risk="RA-203 v1（草案）", design="4 组硅碳比例 × 2 次重复",
        bom=[{"material": "PVDF 粘结剂", "qty": 6, "unit": "g"}, {"material": "NMP 溶剂", "qty": 0.3, "unit": "L"}],
        steps=[
            {"name": "硅碳粉与粘结剂投加", "cap": "cap.dose_solid", "consumes_materials": True, "params": {"mass": 8}, "dur": 10},
            {"name": "NMP 溶剂加注", "cap": "cap.dose_liquid", "consumes_materials": True, "params": {"volume": 40, "rate": 3}, "dur": 10},
            {"name": "高剪切匀浆", "cap": "cap.mix", "params": {"temp": 70, "rpm": 2500}, "dur": 90},
            {"name": "真空脱泡", "cap": "cap.degas", "params": {"vacuum": 30}, "dur": 15},
        ],
        history=[{"v": "0.3.0", "state": "review", "note": "提高匀浆温度与转速改善分散",
                  "by": "研究员", "at": "2026-09-16"}],
        diff=[["-", '  "temp": 60,'], ["+", '  "temp": 70,    // 改善硅碳分散'],
              ["-", '  "rpm": 2000'], ["+", '  "rpm": 2500']],
    ),
    dict(
        id="R-207", name="低温电解液配样", version="0.1.0", state="draft", owner="研究员",
        updated="2026-09-17", plate=12, risk="", design="待定义",
        bom=[{"material": "电解液 LP57", "qty": 3, "unit": "mL"}],
        steps=[
            {"name": "电解液微量注入", "cap": "cap.dose_liquid", "consumes_materials": True, "params": {"volume": 0.5, "rate": 0.05}, "dur": 8},
            {"name": "低温调配", "cap": "cap.mix", "params": {"temp": 20, "rpm": 300}, "dur": 30},
            {"name": "真空脱泡", "cap": "cap.degas", "params": {"vacuum": 10}, "dur": 12},
        ],
        history=[{"v": "0.1.0", "state": "draft", "note": "新建", "by": "研究员", "at": "2026-09-17"}],
    ),
]

PLANS = [
    dict(
        id="EP-201-03", name="NCM811 导电剂 / 粘结剂配比筛选 · 第 3 轮", recipe_id="R-201", owner="研究员",
        state="locked", created="2026-09-09", repeats=3, layout="randomized", seed=20260910,
        goal="在 8 组配比中找出面密度 CV < 2% 且首次放电比容量 ≥ 205 mAh/g 的配比窗口",
        factors=[
            {"name": "导电剂比例", "unit": "%", "levels": [1, 2, 3, 4],
             "material": {"name": "Super P 导电剂", "unit": "g", "per": 0.9}},
            {"name": "粘结剂比例", "unit": "%", "levels": [2, 3],
             "material": {"name": "PVDF 粘结剂", "unit": "g", "per": 0.9}},
        ],
        control={"label": "对照：基线配比 2% / 2%", "cond": [2, 2]},
    ),
    dict(
        id="EP-205-01", name="电解液 FEC 添加剂含量与注液量", recipe_id="R-205", owner="研究员",
        state="locked", created="2026-09-12", repeats=1, layout="sequential", seed=1,
        goal="8 通道各 1 次，比较 FEC 含量与注液量对首次放电比容量的影响",
        factors=[
            {"name": "FEC 含量", "unit": "%", "levels": [0, 2, 5, 10]},
            {"name": "注液量", "unit": " μL", "levels": [50, 60],
             "material": {"name": "电解液 LP57", "unit": "mL", "per": 0.001}},
        ],
        control={"label": "对照：无添加剂 60 μL", "cond": [0, 60]},
    ),
    dict(
        id="EP-203-01", name="硅碳负极硅含量梯度", recipe_id="R-203", owner="研究员",
        state="draft", created="2026-09-16", repeats=2, layout="randomized", seed=20260916,
        goal="4 个硅含量水平 × 2 次重复，评估分散工艺对面密度均一性的影响",
        factors=[{"name": "硅含量", "unit": "%", "levels": [5, 10, 15, 20]}], control=None,
    ),
]

LOTS = [
    dict(id="LOT-NMP-2601", material="NMP 溶剂", cas="872-50-4", type="溶剂", qty=8.4, unit="L",
         release="已放行", sds="SDS-NMP v2", compat="非水溶剂组", expiry="2027-03-01", opened="2026-09-02",
         storage="常温密闭避光", ghs=["生殖毒性", "刺激"]),
    dict(id="LOT-NMP-2602", material="NMP 溶剂", cas="872-50-4", type="溶剂", qty=4.0, unit="L",
         release="待复验", sds="SDS-NMP v2", compat="非水溶剂组", expiry="2027-04-10", opened="",
         storage="常温密闭避光", ghs=["生殖毒性", "刺激"]),
    dict(id="LOT-NCM-0907", material="NCM811 正极粉", cas="182442-95-1", type="活性物质", qty=1900, unit="g",
         release="已放行", sds="SDS-NCM v1", compat="兼容矩阵 CM-201", expiry="2027-10-05", opened="2026-09-07",
         storage="干燥柜 露点 −40℃", ghs=["刺激"]),
    dict(id="LOT-PVDF-0812", material="PVDF 粘结剂", cas="24937-79-9", type="粘结剂", qty=120, unit="g",
         release="已放行", sds="SDS-PVDF v1", compat="遇潮结块，单独存放", expiry="2026-09-25", opened="2026-08-12",
         storage="干燥密封", ghs=["粉尘刺激"]),
    dict(id="LOT-ELY-0611", material="电解液 LP57", cas="21324-40-3", type="电解液", qty=4200, unit="mL",
         release="已放行", sds="SDS-LP57 v3", compat="遇水产生 HF，单独存放", expiry="2028-01-01", opened="2026-06-11",
         storage="手套箱 −20℃", ghs=["腐蚀", "遇水放热"]),
    dict(id="LOT-SP-0903", material="Super P 导电剂", cas="1333-86-4", type="导电剂", qty=400, unit="g",
         release="已放行", sds="SDS-SP v1", compat="粉尘易扬，单独称量", expiry="2027-06-01", opened="2026-09-03",
         storage="干燥密封", ghs=["粉尘刺激"]),
]

WASTE = [
    ("Tank A", "NMP 回收液", 12.0, 20.0),
    ("Tank B", "含锂清洗废液", 45.0, 20.0),
    ("Tank C", "含镍钴锰废料浆", 78.0, 20.0),
]

ALARMS = [
    dict(id="A-1039", severity=3, state="active", condition_active=True, owner="EHS 专员",
         source_type="material", source_id="Tank C",
         message="含镍钴锰废料桶液位 78%，超过 75% 预警线",
         response="下次排放前确认容积与兼容性，安排换桶。"),
    dict(id="A-1037", severity=3, state="active", condition_active=True, owner="EHS 专员",
         source_type="material", source_id="LOT-PVDF-0812",
         message="PVDF 粘结剂将在 7 天内过期",
         response="复验或报废，更新批号放行状态。"),
    dict(id="A-1036", severity=4, state="shelved", condition_active=True, owner="设备工程师",
         source_type="station", source_id="ST-02",
         message="中试匀浆罐校准将在 40 天内到期", response="安排校准。", shelved_until="2026-10-01"),
]


# ---------- 组织与访问范围 ----------

ORGANIZATION = dict(
    id="ORG-001", code="MAIN", name="本部电池实验室", timezone="Asia/Shanghai",
    note="首期单组织部署；跨组织隔离已生效，第二个组织只需登记成员关系",
)
# 第二个组织只登记、不给成员：隔离用例靠它验证「看不到 = 不存在」
ISOLATION_ORGANIZATION = dict(
    id="ORG-002", code="PILOT", name="试点二号实验室", timezone="Asia/Shanghai",
    note="隔离验收用；本部账号不是它的成员",
)
LABS = [("ORG-001-LAB-1", "ORG-001", "一号实验室"), ("ORG-002-LAB-1", "ORG-002", "试点实验室")]

# 服务身份。凭据原文只在种子里出现，用于开发与联调；正式环境必须签发并轮换。
SERVICE_IDENTITIES = [
    dict(
        source="executor-sim", name="模拟执行器",
        secret="ilcs-executor-dev-secret",
        scopes={
            "stations": [
                "ST-01-A", "ST-01-B", "ST-02", "ST-03", "ST-04", "ST-05", "ST-06", "ST-07",
                "AGV-01", "AGV-02",
            ],
            "analysis_tasks": "all",
        },
    ),
    dict(
        source="lims-ec", name="电性能 LIMS", secret="ilcs-lims-dev-secret",
        scopes={"stations": ["ST-07"], "analysis_tasks": "all",
                "instrument_serials": ["EC-TESTER-0001"]},
    ),
]

# ---------- 人员与资质 ----------

PEOPLE = [
    dict(code="P-001", name="研究员", title="课题负责人", username="researcher", contact="ext.101"),
    dict(code="P-002", name="QA 负责人", title="质量负责人", username="qa", contact="ext.102"),
    dict(code="P-003", name="操作员", title="实验操作员", username="operator", contact="ext.103"),
    dict(code="P-004", name="EHS 专员", title="安全环保", username="ehs", contact="ext.104"),
    dict(code="P-005", name="系统管理员", title="系统管理", username="admin", contact="ext.105"),
]

# 操作员拿全部设备能力资质；研究员只有称重与测试——分配到涂布步骤时会被资质挡住
QUALIFICATIONS = [
    ("P-003", "capability", "cap.dose_solid", 365),
    ("P-003", "capability", "cap.dose_liquid", 365),
    ("P-003", "capability", "cap.mix", 365),
    ("P-003", "capability", "cap.degas", 365),
    ("P-003", "capability", "cap.coat", 365),
    ("P-003", "capability", "cap.calender", 365),
    ("P-003", "capability", "cap.vacuum_dry", 365),
    ("P-003", "capability", "cap.weigh", 365),
    ("P-003", "capability", "cap.assemble", 365),
    ("P-003", "capability", "cap.test", 365),
    ("P-003", "capability", "cap.transfer", 365),
    ("P-003", "safety", "危化品操作", 365),
    ("P-001", "capability", "cap.weigh", 365),
    ("P-001", "capability", "cap.test", 365),
    ("P-005", "capability", "cap.mix", 365),
]

# ---------- 资产与校准 ----------

ASSETS = [
    dict(asset_no="AS-0001", name="高通量匀浆站 A", model="EXP-SLURRY-24", serial="SLR-24-A01",
         location="岛 #1", stations=["ST-01-A"], capacity=1, cal_days=365),
    dict(asset_no="AS-0002", name="高通量匀浆站 B", model="EXP-SLURRY-24", serial="SLR-24-B01",
         location="岛 #1", stations=["ST-01-B"], capacity=1, cal_days=365),
    dict(asset_no="AS-0003", name="中试匀浆罐", model="PILOT-MIX-8", serial="PM-8-001",
         location="岛 #2", stations=["ST-02"], capacity=1, cal_days=40),
    dict(asset_no="AS-0004", name="涂布烘干线", model="COATER-150", serial="CT-150-001",
         location="岛 #3", stations=["ST-03"], capacity=1, cal_days=365),
    dict(asset_no="AS-0005", name="辊压冲切机", model="CAL-200", serial="CL-200-001",
         location="岛 #4", stations=["ST-04"], capacity=1, cal_days=365),
    dict(asset_no="AS-0006", name="真空干燥箱", model="VAC-80", serial="VC-80-001",
         location="岛 #4", stations=["ST-05"], capacity=1, cal_days=365),
    dict(asset_no="AS-0007", name="手套箱组装台", model="GB-ASM", serial="GB-001",
         location="岛 #5", stations=["ST-06"], capacity=1, cal_days=365),
    dict(asset_no="AS-0008", name="电性能测试柜", model="EC-TESTER", serial="EC-TESTER-0001",
         location="岛 #6", stations=["ST-07"], capacity=1, cal_days=365),
    # 手工工作台：没有适配器，也明确不适用校准
    dict(asset_no="AS-0009", name="称量工作台", model="BENCH-W", serial="", location="备料间",
         stations=[], capacity=1, cal_days=0, calibration_applicable=False,
         calibration_exempt_reason="手工工作台，无计量输出，按实验室规定不做校准"),
]

# ---------- 指标定义 ----------

METRICS = [
    dict(code="areal_density", name="面密度", version="v1", value_type="number", unit="mg/cm2",
         method_version="EC-02 v2", sample_types=["极片"], rules={"min": 0, "max": 100}),
    dict(code="discharge_capacity", name="首次放电比容量", version="v1", value_type="number",
         unit="mAh/g", method_version="EC-02 v2", sample_types=["极片"],
         rules={"min": 0, "max": 400}),
    dict(code="retention", name="容量保持率", version="v1", value_type="number", unit="%",
         method_version="EC-02 v2", sample_types=["极片"], rules={"min": 0, "max": 100}),
    dict(code="appearance", name="外观判定", version="v1", value_type="enum", unit="",
         method_version="EC-02 v2", sample_types=["极片"],
         rules={"options": ["合格", "轻微缺陷", "不合格"]}),
]

# ---------- SOP ----------

SOPS = [
    dict(code="SOP-EC-02", title="扣电组装与首次充放电作业指导书", version="v2",
         capability_scope=["cap.assemble", "cap.test"], sample_types=["极片"],
         requires_training_ack=False),
    dict(code="SOP-SLR-01", title="浆料制备与称量作业指导书", version="v3",
         capability_scope=["cap.dose_solid", "cap.dose_liquid", "cap.mix"], sample_types=["极片"],
         requires_training_ack=False),
]

# ---------- 单条件样本实验：四类节点的样例方法与方案 ----------

SINGLE_CONDITION_RECIPE = dict(
    id="R-210", name="单条件样本实验 · 人工备料 + 设备执行 + 等待 + 审核", version="1.0.0",
    state="released", owner="研究员", updated="2026-09-21", plate=8, risk="RA-210 v1",
    design="四类节点顺序流程：人工称量备料 → 设备匀浆 → 定时静置 → QA 审核",
    bom=[{"material": "NCM811 正极粉", "qty": 20, "unit": "g"},
         {"material": "NMP 溶剂", "qty": 0.2, "unit": "L"}],
    steps=[
        {
            "step_id": "s01", "kind": "manual", "name": "人工称量备料", "dur": 20,
            "consumes_materials": True,
            "requires_signature": True, "requires_sample_check": True,
            "form": [
                {"key": "weighed_g", "label": "实际称量质量 g", "type": "number", "required": True},
                {"key": "balance_id", "label": "天平编号", "type": "text", "required": True},
                {"key": "double_check", "label": "已复核称量读数", "type": "bool", "required": True},
            ],
            "qualification": {"safety": "危化品操作"},
        },
        {
            "step_id": "s02", "kind": "device", "name": "控温匀浆", "cap": "cap.mix",
            "params": {"temp": 25, "rpm": 2000}, "dur": 45,
        },
        {
            # 这是联调专用方法，不是正式工艺参数。等待 3 秒足以验证“由后台推进器到期
            # 唤醒、而不是靠浏览器刷新”，同时让全链路验收可以在一次部署检查中完成。
            "step_id": "s03", "kind": "wait", "name": "联调等待（3 秒）", "dur": 0.05,
            "wait_for": {"mode": "duration"},
            "hard": {"from": "匀浆结束", "maxGapMin": 90},
        },
        {
            "step_id": "s04", "kind": "review", "name": "QA 复核备料与匀浆记录",
            "review_role": "qa",
        },
    ],
    history=[{"v": "1.0.0", "state": "released", "note": "单条件样本实验样例流程",
              "by": "QA 负责人", "at": "2026-09-21"}],
)

SINGLE_CONDITION_PLAN = dict(
    id="EP-210-01", name="单条件样本实验 · 首期联调样例", recipe_id="R-210", owner="研究员",
    state="draft", plan_type="single_condition", created="2026-09-21", repeats=1,
    layout="sequential", seed=1, factors=[], control=None, sample_count=4,
    goal="按 1.3 节建议样例贯通人工 / 设备 / 等待 / 审核四类节点；不代表已选定正式实验方法",
    metric_codes=["areal_density", "discharge_capacity"],
)
