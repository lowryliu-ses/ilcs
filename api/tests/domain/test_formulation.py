"""配液模板：表头解析、列识别、加料 / 搅拌步骤的生成规则、阶段依赖与方案（重复瓶、样本顺序）。"""
import copy
from decimal import Decimal

from app.domain import formulation as rules
from app.domain import steps as steps_rules
from app.domain.dosing import covers

REFERENCE_HEADER = ["序列号", "EC (g)", "EMC(g)", "DEC(g)", "DMC(g)", "EP(g)", "LiPF6(g)", "LiFSI(g)", "LiTFSI(g)",
                    "LiBF4", "VC(g)", "FEC(g)", "LiDFP(g)"]
REFERENCE_ROW = ["ELY-0001", 10.6632, 14.9328, 17.0640, 7.2000, 7.2000, 4.6080, 5.7120, 1.2000, 0.3600, 1.3680,
                 1.3680, 0.3240]
CATEGORIES = {
    "EC": "预热溶剂", "EMC": "溶剂", "DEC": "溶剂", "DMC": "溶剂", "EP": "溶剂",
    "LiPF6": "锂盐", "LiFSI": "锂盐", "LiTFSI": "锂盐", "LiBF4": "锂盐", "LiDFP": "锂盐",
    "VC": "添加剂", "FEC": "添加剂",
}
CATALOG = {name: {"category": category, "base_unit": "g"} for name, category in CATEGORIES.items()}
METRIC = "METRIC-discharge_capacity-v1"


def capability_specs(prefix: str = "cap.t.") -> dict[str, dict]:
    def cap(name, params):
        return {
            "name": name, "retired": False,
            "params": {key: key for key in params},
            "param_specs": {key: {"type": kind, "unit": unit} for key, (unit, kind) in params.items()},
        }

    return {
        f"{prefix}pick": cap("机械手取放", {}),
        f"{prefix}chamber": cap("过渡舱转移", {}),
        f"{prefix}dose_liquid": cap("液体称量加注", {"mass": ("g", "number")}),
        f"{prefix}dose_solid": cap("固体称量加料", {"mass": ("g", "number")}),
        f"{prefix}stir": cap("控温搅拌", {"temp": ("℃", "number"), "time": ("s", "number"), "rpm": ("rpm", "number")}),
        f"{prefix}capper": cap("开关盖", {"torque": ("N", "number")}),
        f"{prefix}aliquot": cap("电解液分装", {"bottles": ("瓶", "integer"), "volume": ("mL", "number")}),
        f"{prefix}conductivity": cap("电导率检测", {"temp": ("℃", "number"), "repeats": ("次", "integer")}),
    }


def template_config(prefix: str = "cap.t.", methods: dict[str, str] | None = None) -> dict:
    """参考产线（6 固 6 液）的配液模板。`methods` 给了就按能力短名挂设备方法引用。"""
    methods = methods or {}

    def device(key, name, cap, params=None, after=None, dur=2):
        step = {"kind": "device", "name": name, "cap": f"{prefix}{cap}", "params": params or {}, "dur": dur}
        if key:
            step["key"] = key
        if after is not None:
            step["after"] = after
        if cap in methods:
            step["method"] = {"id": methods[cap]}
        return step

    def manual(key, name, form, after=None, **extra):
        step = {"kind": "manual", "key": key, "name": name, "dur": 5, "form": form, **extra}
        if after is not None:
            step["after"] = after
        return step

    return {
        "plate": 96, "risk": "RA-ELY-01", "design": "C 公司电解液配制", "unit": "g", "sample_type": "电解液",
        "serial_headers": ["序列号", "编号", "瓶号", "样品编号", "serial", "id"],
        "required_metrics": [METRIC],
        "prefix": [
            manual("load", "过渡舱载入空瓶", [{"key": "count", "label": "载入空瓶数", "type": "number"}], after=[],
                   requires_signature=True),
            manual("dispense", "手动分装物料", [{"key": "lots", "label": "已核对原料批号", "type": "bool"}],
                   after=["load"]),
            manual("scan", "手动扫码核对瓶身序列号", [{"key": "scanned", "label": "扫码数量", "type": "number"}],
                   after=["dispense"]),
            device("pick", "机械手取空瓶放到周转位", "pick", after=["load"]),
            device("xf_in", "过渡舱转物料进配液段", "chamber", after=["scan", "pick"]),
            device("xf_solid", "过渡舱转固体物料到加料位", "chamber", after=["xf_in"]),
        ],
        "stages": [
            {"key": "liquid", "label": "液体", "after": ["xf_in"], "stir_after_last": True, "then": [
                device("l_cap", "配液段拧盖", "capper", {"torque": 10}),
                device("l_cold", "冷藏搅拌", "stir", {"temp": 5, "time": 600, "rpm": 300}),
                device("l_open", "配液段开盖", "capper", {"torque": 10}),
            ]},
            {"key": "salt", "label": "锂盐与添加剂", "after": ["xf_solid"], "stir_after_last": False, "then": [
                device("s_cap", "配液段关盖", "capper", {"torque": 10}),
                device("s_mix", "终混", "stir", {"temp": 25, "time": 3600, "rpm": 500}),
                device("xf_out", "过渡舱转测试段", "chamber"),
            ]},
        ],
        "routes": {
            "预热溶剂": {"stage": "liquid", "param": "mass", "stir_after": False,
                     "step": device(None, "{material} 预热移液加注", "dose_liquid", {"mass": 0}, dur=5)},
            "溶剂": {"stage": "liquid", "param": "mass",
                   "step": device(None, "{material} 称量加注", "dose_liquid", {"mass": 0}, dur=3)},
            "锂盐": {"stage": "salt", "param": "mass",
                   "step": device(None, "{material} 称量加料", "dose_solid", {"mass": 0}, dur=5)},
            "添加剂": {"stage": "salt", "param": "mass",
                    "step": device(None, "{material} 移液加注", "dose_liquid", {"mass": 0}, dur=3)},
        },
        "stir": device(None, "{material} 加料后制冷搅拌", "stir", {"temp": -10, "time": 60, "rpm": 400}),
        "suffix": [
            device("t_open", "测试段开盖", "capper", {"torque": 10}),
            device("aliquot", "电解液分装", "aliquot", {"bottles": 2, "volume": 30}),
            device("keep_cap", "母瓶与备用瓶关盖", "capper", {"torque": 10}, after=["aliquot"]),
            device("keep_store", "母瓶与备用瓶入库", "pick"),
            device("cnd", "第 1 瓶电导率", "conductivity", {"temp": 25, "repeats": 3}, after=["aliquot"]),
            device("dv", "第 1 瓶密度黏度", "conductivity", {"temp": 25, "repeats": 3}),
            device("raman", "第 1 瓶拉曼", "conductivity", {"temp": 25, "repeats": 3}),
            device("test_cap", "第 1 瓶关盖", "capper", {"torque": 10}),
            device("test_store", "第 1 瓶复测存储", "pick"),
        ],
        "experiment_params": [
            {"key": "bottles", "label": "分装瓶数", "step": "aliquot", "param": "bottles", "unit": "瓶", "default": 2},
            {"key": "volume", "label": "每瓶分装量", "step": "aliquot", "param": "volume", "unit": "mL", "default": 30},
        ],
    }


def generate(table, config=None, params=None, catalog=CATALOG):
    return rules.generate(config or template_config(), catalog, table, params or {},
                          capabilities=capability_specs(), filename="formula.csv", template_name="配液")


def test_header_units_accept_half_and_full_width_parentheses_or_none():
    assert rules.split_header("EC (g)", "g") == ("EC", "g")
    assert rules.split_header("EMC(g)", "g") == ("EMC", "g")
    assert rules.split_header("EC（g）", "g") == ("EC", "g")
    assert rules.split_header(" LiBF4 ", "g") == ("LiBF4", "g")
    assert rules.split_header("DMC (ml)", "g") == ("DMC", "mL")
    assert rules.split_header("(g)", "g") == ("(g)", "g"), "只有括号没有名称时不当单位后缀"


def test_reference_formula_generates_the_43_step_line_in_order():
    result = generate([REFERENCE_HEADER, REFERENCE_ROW])
    assert result["issues"] == [], result["issues"]
    steps = result["steps"]
    assert len(steps) == 43
    assert [step["step_id"] for step in steps] == [f"s{index:02d}" for index in range(1, 44)]
    names = [step["name"] for step in steps]
    assert names[:6] == ["过渡舱载入空瓶", "手动分装物料", "手动扫码核对瓶身序列号", "机械手取空瓶放到周转位",
                         "过渡舱转物料进配液段", "过渡舱转固体物料到加料位"]
    # 液体阶段：EC 预热移液后不搅拌；其余溶剂每个后面搅拌；阶段最后一个（EP）照 stir_after_last 搅拌
    assert names[6:15] == [
        "EC 预热移液加注", "EMC 称量加注", "EMC 加料后制冷搅拌", "DEC 称量加注", "DEC 加料后制冷搅拌",
        "DMC 称量加注", "DMC 加料后制冷搅拌", "EP 称量加注", "EP 加料后制冷搅拌",
    ]
    assert names[15:18] == ["配液段拧盖", "冷藏搅拌", "配液段开盖"]
    # 锂盐与添加剂按表格列顺序；阶段最后一个（LiDFP）不搅拌
    assert names[18:31] == [
        "LiPF6 称量加料", "LiPF6 加料后制冷搅拌", "LiFSI 称量加料", "LiFSI 加料后制冷搅拌",
        "LiTFSI 称量加料", "LiTFSI 加料后制冷搅拌", "LiBF4 称量加料", "LiBF4 加料后制冷搅拌",
        "VC 移液加注", "VC 加料后制冷搅拌", "FEC 移液加注", "FEC 加料后制冷搅拌", "LiDFP 称量加料",
    ]
    assert names[31:] == ["配液段关盖", "终混", "过渡舱转测试段", "测试段开盖", "电解液分装", "母瓶与备用瓶关盖",
                          "母瓶与备用瓶入库", "第 1 瓶电导率", "第 1 瓶密度黏度", "第 1 瓶拉曼", "第 1 瓶关盖",
                          "第 1 瓶复测存储"]
    after = {step["step_id"]: step["after"] for step in steps}
    assert after["s01"] == [] and after["s02"] == ["s01"] and after["s04"] == ["s01"]
    assert after["s05"] == ["s03", "s04"] and after["s06"] == ["s05"]
    # 第一个阶段写了 after：第一个加料步骤只等「物料进配液段」，从 prefix 分叉；
    # 「固体物料到加料位」另走一路，到锂盐阶段第一个加料步骤处汇合（后面的阶段 = 上一步 + 阶段 after）
    assert after["s07"] == ["s05"] and after["s08"] == ["s07"]
    assert after["s19"] == ["s18", "s06"]
    assert not any("s06" in after[step_id] for step_id in after if step_id not in ("s19",)), "固体物料那一路只在锂盐处汇合"
    assert after["s37"] == ["s36"] and after["s39"] == ["s36"] and after["s40"] == ["s39"]
    assert all("key" not in step for step in steps)

    dose = steps[6]
    assert dose["consumes_materials"] is True and dose["material"] == "EC" and dose["material_param"] == "mass"
    assert dose["params"] == {"mass": 0}
    assert "material" not in steps[7 + 1], "搅拌步骤不投料"
    assert result["bom"] == []
    assert [row["name"] for row in result["reagents"]] == list(CATEGORIES)[:5] + ["LiPF6", "LiFSI", "LiTFSI", "LiBF4",
                                                                                   "VC", "FEC", "LiDFP"]


def test_plan_factors_follow_dose_steps_and_experiment_params():
    result = generate([REFERENCE_HEADER, REFERENCE_ROW], params={"bottles": 3})
    plan = result["plan"]
    assert plan["plan_type"] == "matrix" and plan["repeats"] == 1 and plan["sample_ids"] == ["ELY-0001"]
    factors = plan["factors"]
    assert len(factors) == 12 + 2
    assert factors[0] == {"name": "EC", "unit": "g", "levels": [10.6632], "target": {"step_id": "s07", "param": "mass"},
                          "material": {"name": "EC", "unit": "g", "per": 1}}
    assert factors[-2] == {"name": "分装瓶数", "unit": "瓶", "levels": [3], "target": {"step_id": "s36", "param": "bottles"}}
    assert factors[-1]["levels"] == [30] and factors[-1]["target"] == {"step_id": "s36", "param": "volume"}
    assert plan["design_points"] == [[*REFERENCE_ROW[1:], 3, 30]]
    assert plan["required_metrics"] == [METRIC]
    assert "1 个配方、1 瓶" in plan["goal"]
    assert result["recipe"]["risk"] == "RA-ELY-01" and result["recipe"]["plate"] == 96
    assert result["recipe"]["name"].startswith("配液 · EC、EMC、DEC")
    assert "液体：EC → EMC → DEC → DMC → EP" in result["recipe"]["design"]


def test_generation_is_deterministic():
    table = [REFERENCE_HEADER, REFERENCE_ROW, ["ELY-0002", *REFERENCE_ROW[1:]]]
    assert generate(copy.deepcopy(table)) == generate(copy.deepcopy(table))


def test_serial_column_is_recognised_case_and_space_insensitively():
    result = generate([[" Serial ", "EC (g)"], ["B-1", 1.5]])
    assert result["issues"] == [] and result["rows"] == [{"row": 2, "serial": "B-1", "amounts": {"EC": 1.5}}]
    assert result["columns"][0]["kind"] == "serial"
    # xlsx 里的整数序列号读出来是 float
    assert generate([["瓶号", "EC (g)"], [1001.0, 1]])["rows"][0]["serial"] == "1001"
    missing = generate([["瓶", "EC (g)"], ["B-1", 1]])
    assert any("没有识别到序列号列" in item for item in missing["issues"])
    two = generate([["序列号", "编号", "EC (g)"], ["B-1", "X", 1]])
    assert any("都像序列号列" in item for item in two["issues"])


def test_unknown_columns_block_only_when_they_carry_numbers():
    result = generate([["序列号", "EC (g)", "备注", "XYZ (g)"], ["B-1", 1, "", 2], ["B-2", 2, "手工", None]])
    assert any("列 XYZ (g) 不是已登记的试剂" in item for item in result["issues"])
    assert any("列 备注 不是试剂，已忽略" in item for item in result["warnings"])
    kinds = {col["header"]: col["kind"] for col in result["columns"]}
    assert kinds == {"序列号": "serial", "EC (g)": "reagent", "备注": "ignored", "XYZ (g)": "ignored"}
    # 物料主数据里有同名物料但没有类别，同样不认
    catalog = {**CATALOG, "XYZ": {"category": "", "base_unit": "g"}}
    assert any("不是已登记的试剂" in item for item in generate([["序列号", "XYZ"], ["B-1", 1]], catalog=catalog)["issues"])


def test_all_zero_reagent_generates_no_dose_step():
    header = ["序列号", "EC (g)", "EMC (g)", "LiBF4"]
    result = generate([header, ["B-1", 1, 2, 0], ["B-2", 1, 2, 0]])
    assert result["issues"] == []
    assert "LiBF4 全为 0，不生成加料步骤" in result["warnings"]
    # 整列空白 = 这次不用这种料，与整列 0 一样；只有同一列有的填了、有的空着才是问题
    unused = generate([header, ["B-1", 1, 2, None], ["B-2", 1, 2, ""]])
    assert unused["issues"] == [] and "LiBF4 全为 0，不生成加料步骤" in unused["warnings"]
    assert not any(step.get("material") == "LiBF4" for step in result["steps"])
    assert {row["name"]: row["zero_rows"] for row in result["reagents"]} == {"EC": 0, "EMC": 0, "LiBF4": 2}
    # 锂盐阶段没有加料：阶段 after（固体物料到加料位）落到本阶段第一个 then 步骤上
    close = next(step for step in result["steps"] if step["name"] == "配液段关盖")
    xf_solid = next(step for step in result["steps"] if step["name"] == "过渡舱转固体物料到加料位")
    assert xf_solid["step_id"] in close["after"]


def test_stir_rules_route_default_and_stage_last():
    config = template_config()
    config["routes"]["溶剂"]["stir_after"] = False
    result = generate([["序列号", "EMC (g)", "DEC (g)", "LiPF6", "VC"], ["B-1", 1, 2, 0.5, 0.1]], config=config)
    names = [step["name"] for step in result["steps"]]
    liquid = names[6:names.index("配液段拧盖")]
    # 溶剂不紧跟搅拌；阶段最后一个（DEC）照 stir_after_last 搅拌
    assert liquid == ["EMC 称量加注", "DEC 称量加注", "DEC 加料后制冷搅拌"]
    salt = names[names.index("配液段开盖") + 1:names.index("配液段关盖")]
    assert salt == ["LiPF6 称量加料", "LiPF6 加料后制冷搅拌", "VC 移液加注"]


def test_repeated_formula_rows_become_repeats_in_condition_order():
    header = ["序列号", "EC (g)", "EMC (g)"]
    rows = [["B-1", 1, 2], ["B-2", 3, 2], ["B-3", 1, 2], ["B-4", 3, 2]]
    result = generate([header, *rows])
    plan = result["plan"]
    assert result["issues"] == []
    assert plan["repeats"] == 2
    assert [point[:2] for point in plan["design_points"]] == [[1, 2], [3, 2]]
    # 条件 0 的两瓶在前，条件 1 的两瓶在后：第 i 个 = 条件序号 × 重复数 + (重复号 − 1)
    assert plan["sample_ids"] == ["B-1", "B-3", "B-2", "B-4"]
    assert plan["factors"][0]["levels"] == [1, 3] and plan["factors"][1]["levels"] == [2]

    uneven = generate([header, *rows[:3]])
    assert any("配方重复数不一致" in item for item in uneven["issues"])


def test_plate_and_repeat_limits():
    config = template_config()
    config["plate"] = 2
    result = generate([["序列号", "EC (g)"], ["B-1", 1], ["B-2", 2], ["B-3", 3]], config=config)
    assert any("超过流程每批 2 个样品位" in item for item in result["issues"])
    many = generate([["序列号", "EC (g)"], *[[f"B-{i}", 1] for i in range(13)]])
    assert any("超过 12 次重复上限" in item for item in many["issues"])


def test_cell_problems_name_the_row_and_serial():
    result = generate([["序列号", "EC (g)", "EMC (g)"], ["B-1", -1, 2], ["B-2", "abc", 2], ["", 1, 2], ["B-1", 1, 2]])
    issues = result["issues"]
    assert any("第 2 行（B-1）EC 是负数 -1" in item for item in issues)
    assert any("第 3 行（B-2）EC 的值 'abc' 不是数字" in item for item in issues)
    assert "第 4 行没有序列号" in issues
    assert "第 5 行的序列号 B-1 与第 2 行重复" in issues


def test_column_unit_must_match_the_route_parameter_unit():
    result = generate([["序列号", "EC (mL)"], ["B-1", 1]])
    assert any("列 EC (mL) 的单位 mL 与「预热溶剂」加法的用量参数 mass 的单位 g 不同" in item for item in result["issues"])


def test_experiment_param_values_must_be_numbers():
    result = generate([REFERENCE_HEADER, REFERENCE_ROW], params={"bottles": "两", "unknown": 1})
    assert any("实验参数「分装瓶数」的值 '两' 不是数字" in item for item in result["issues"])
    assert "实验参数 unknown 模板里没有，已忽略" in result["warnings"]


def test_template_issues_accept_the_reference_template():
    methods = {"M-DOSE": {"capability_id": "cap.t.dose_liquid", "code": "DM-001", "name": "溶剂"}}
    config = template_config(methods={"dose_liquid": "M-DOSE"})
    assert rules.template_issues(config, capability_specs(), methods, {METRIC}) == []


def test_template_issues_list_every_problem():
    config = template_config(methods={"dose_solid": "M-DRAFT", "dose_liquid": "M-OTHER"})
    config["plate"] = 0
    config["risk"] = ""
    config["prefix"][1]["after"] = ["scan"]
    config["prefix"].append(copy.deepcopy(config["prefix"][0]))
    config["routes"]["锂盐"]["stage"] = "powder"
    config["routes"]["溶剂"]["param"] = "volume"
    config["experiment_params"][0]["step"] = "missing"
    config["required_metrics"] = ["METRIC-nope"]
    caps = capability_specs()
    caps["cap.t.stir"]["param_specs"]["rpm"]["unit"] = ""
    config["routes"]["添加剂"] = {"stage": "salt", "param": "rpm",
                               "step": {"name": "x", "cap": "cap.t.stir", "params": {"rpm": 0}}}
    methods = {"M-OTHER": {"capability_id": "cap.t.stir", "code": "DM-009", "name": "搅拌"}}
    issues = rules.template_issues(config, caps, methods, {METRIC})
    expected = [
        "每批样品位 plate 必须是 1–96 的整数",
        "要填生成流程的风险评估编号 risk（开跑检查要求非空）",
        "必测指标 METRIC-nope 没有登记或已停用",
        "固定步骤「dispense」的 after 引用了 scan：不存在或排在它之后",
        "固定步骤 key「load」重复",
        "加法「锂盐」的阶段 powder 不存在",
        "加法「锂盐」的步骤模板引用的设备方法 M-DRAFT 不存在或未发布",
        "加法「溶剂」的用量参数 volume 不是能力 cap.t.dose_liquid 的参数",
        "加法「溶剂」的步骤模板引用的设备方法 DM-009 的能力是 cap.t.stir，与步骤能力 cap.t.dose_liquid 不一致",
        "加法「添加剂」的用量参数 rpm 没有登记单位，无法与表格单位对账",
        "实验参数「分装瓶数」指向的 missing 不是设备固定步骤",
    ]
    for item in expected:
        assert item in issues, (item, issues)
    assert rules.template_issues([], caps, {}, set()) == ["模板配置必须是 JSON 对象"]


def test_first_stage_without_doses_forks_at_its_first_then_step():
    # 液体阶段一种料都没有：阶段 after 落到它第一个 then 步骤上，同样只等「物料进配液段」
    result = generate([["序列号", "LiPF6"], ["B-1", 1]])
    steps = {step["name"]: step for step in result["steps"]}
    ids = {name: step["step_id"] for name, step in steps.items()}
    assert steps["配液段拧盖"]["after"] == [ids["过渡舱转物料进配液段"]]
    assert steps["LiPF6 称量加料"]["after"] == [ids["配液段开盖"], ids["过渡舱转固体物料到加料位"]]
    # 第一个阶段没写 after：照旧接 prefix 的最后一步
    config = template_config()
    config["stages"][0]["after"] = []
    chained = {step["name"]: step for step in generate([REFERENCE_HEADER, REFERENCE_ROW], config=config)["steps"]}
    assert chained["EC 预热移液加注"]["after"] == [chained["过渡舱转固体物料到加料位"]["step_id"]]


def test_row_with_a_serial_but_no_amounts_is_an_issue():
    result = generate([["序列号", "EC (g)", "EMC (g)"], ["B-1", 1, 2], ["B-2", 3, 2], ["B-3", None, ""]])
    # 整行都空只报这一行，不再按列逐格报空白
    assert result["issues"] == ["第 4 行（序列号 B-3）没有填任何试剂用量"]
    zeros = generate([["序列号", "EC (g)", "EMC (g)"], ["B-1", 1, 2], ["B-2", 0, 0]])
    assert "第 3 行（序列号 B-2）所有试剂都是 0" in zeros["issues"]
    # 值本身有问题的行只报那个问题，不再叠一条「都是 0」
    bad = generate([["序列号", "EC (g)"], ["B-1", 1], ["B-2", "abc"]])
    assert not any("所有试剂都是 0" in item for item in bad["issues"])


def test_huge_and_too_precise_numbers_become_issues_or_warnings_not_crashes():
    header = ["序列号", "EC (g)", "EMC (g)", "LiBF4"]
    for value in ("1e30", "1e22", "-1e30", 1e30):
        result = generate([header, ["B-1", value, 1, 0]])
        assert any(item.startswith("第 2 行（B-1）EC 的值") and item.endswith("过大") for item in result["issues"]), \
            (value, result["issues"])
    params = generate([REFERENCE_HEADER, REFERENCE_ROW], params={"bottles": 1e30})
    assert any("实验参数「分装瓶数」的值" in item and "过大" in item for item in params["issues"])
    # 量化到 6 位后是 0：合计、全 0 判断与因子水平一致，不生成水平全是 0 的加料步骤
    tiny = generate([header, ["B-1", 1, 2, "1e-9"], ["B-2", 1, 2, 0]])
    assert tiny["issues"] == [], tiny["issues"]
    assert "LiBF4 全为 0，不生成加料步骤" in tiny["warnings"]
    assert "有用量超出 6 位小数精度，已按 6 位小数计：第 2 行（B-1）LiBF4 1e-9 → 0" in tiny["warnings"]
    assert not any(step.get("material") == "LiBF4" for step in tiny["steps"])
    assert {row["name"]: row["zero_rows"] for row in tiny["reagents"]}["LiBF4"] == 2
    assert rules._plain(rules.Decimal("1e30")) == 10 ** 30, "兜底：超大数也不抛 InvalidOperation"


def test_row_numbers_follow_the_file_when_blank_rows_are_kept():
    from app.core.spreadsheet import read_table

    table = read_table("f.csv", "序列号,EC (g)\nB-1,1\n\n\n,2\nB-1,3\n".encode())
    result = generate(table)
    assert "第 5 行没有序列号" in result["issues"]
    assert "第 6 行的序列号 B-1 与第 2 行重复" in result["issues"]
    assert [row["row"] for row in result["rows"]] == [2, 5, 6]
    # 表头上面有空行（标题区）：表头是第一个非空行，行号照样是文件里的
    padded = generate([[None, None], [None, None], ["序列号", "EC (g)"], ["", 1]])
    assert "第 4 行没有序列号" in padded["issues"]


def test_numeric_serial_cells_are_flagged():
    result = generate([["瓶号", "EC (g)"], [1.0, 1], ["0002", 2], [3.0, 3]])
    assert [row["serial"] for row in result["rows"]] == ["1", "0002", "3"]
    assert any(item.startswith("第 2、4 行的序列号是数字单元格") for item in result["warnings"]), result["warnings"]


def test_float_noise_from_formulas_is_not_a_precision_warning_and_warnings_are_merged():
    # xlsx 公式结果带二进制尾差（0.1 + 0.2）：按 15 位有效数字取，与 Excel 显示一致，不提醒
    noisy = generate([["序列号", "EC (g)", "EMC (g)"], ["B-1", 0.1 + 0.2, 2.0]])
    assert not any("精度" in item for item in noisy["warnings"]), noisy["warnings"]
    assert noisy["rows"][0]["amounts"]["EC"] == 0.3
    # 真超出 6 位的多处合成一条提醒，不是一格一条
    many = generate([["序列号", "EC (g)", "EMC (g)"]] + [[f"B-{n}", "1.0000001", "2.0000001"] for n in range(1, 30)])
    precision = [item for item in many["warnings"] if "精度" in item]
    assert len(precision) == 1 and "等 58 处" in precision[0], precision


def test_sop_reference_is_validated_and_passes_through_to_generated_steps():
    config = template_config()
    config["stir"]["sop_step"] = "加料后制冷搅拌"
    # 写了 sop_step 却没有 sop：生成的流程不知道对哪份 SOP，模板就不合格
    assert any("没有用 sop 指定 SOP" in item for item in rules.sop_issues(config))
    config["sop"] = {"code": "SOP-T-01"}
    assert rules.sop_issues(config) == []
    config["prefix"][0]["sop_step"] = ""
    assert any("sop_step 要写 SOP 步骤标题" in item for item in rules.sop_issues(config))
    config["prefix"][0]["sop_step"] = "过渡舱载入空瓶"
    config["sop"] = {"code": ""}
    assert any("sop 要写成" in item for item in rules.sop_issues(config))
    # 标题原样带到生成的步骤上，由服务层按当时生效的 SOP 版本换成步骤标识
    config["sop"] = {"code": "SOP-T-01"}
    result = generate([REFERENCE_HEADER, REFERENCE_ROW], config=config)
    stirs = [step for step in result["steps"] if step["name"].endswith("加料后制冷搅拌")]
    assert stirs and all(step["sop_step"] == "加料后制冷搅拌" for step in stirs)


def _stirred(result: dict, serial: str) -> list[str]:
    """这一瓶实际会被搅的那些「加料后搅拌」（按步骤的 applies_to 与这瓶的用量判）。"""
    amounts = next(row["amounts"] for row in result["rows"] if row["serial"] == serial)
    material = {step["step_id"]: step["material"] for step in result["steps"] if step.get("material")}
    return [step["name"] for step in result["steps"] if (rule := steps_rules.applies_to(step))
            and covers(rule, lambda step_id: Decimal(str(amounts[material[step_id]])))]


def test_stirs_apply_only_to_bottles_dosed_at_their_step():
    steps = generate([REFERENCE_HEADER, REFERENCE_ROW])["steps"]
    ids = {step["name"]: step["step_id"] for step in steps}
    rule = {step["name"]: step.get("applies_to") for step in steps if step["name"].endswith("加料后制冷搅拌")}
    # 液体阶段：只看这瓶在前一步加没加料；最后一种（EP）同样搅
    assert rule["EMC 加料后制冷搅拌"] == {"dosed": ids["EMC 称量加注"]}
    assert rule["EP 加料后制冷搅拌"] == {"dosed": ids["EP 称量加注"]}
    # 锂盐与添加剂阶段最后一种不搅：这瓶之后在本阶段还要再加一种才搅
    later = [ids[name] for name in ("LiFSI 称量加料", "LiTFSI 称量加料", "LiBF4 称量加料", "VC 移液加注",
                                    "FEC 移液加注", "LiDFP 称量加料")]
    assert rule["LiPF6 加料后制冷搅拌"] == {"dosed": ids["LiPF6 称量加料"], "then_any": later}
    assert rule["FEC 加料后制冷搅拌"] == {"dosed": ids["FEC 移液加注"], "then_any": [ids["LiDFP 称量加料"]]}
    assert not any("applies_to" in step for step in steps if not step["name"].endswith("加料后制冷搅拌"))
    assert all(steps_rules.applies_to_issues(step, steps, index) == [] for index, step in enumerate(steps))


def test_a_bottle_without_a_material_is_stirred_as_if_it_were_alone():
    header = ["序列号", "EC (g)", "EMC(g)", "LiPF6(g)", "FEC(g)"]
    together = generate([header, ["A-1", 10, 15, 4.6, 0], ["B-1", 10, 15, 4.6, 1.4]])
    alone = generate([header, ["A-1", 10, 15, 4.6, 0]])
    assert together["issues"] == [] and alone["issues"] == []
    # A 不含 FEC：LiPF6 是它最后一种料，不搅、直接关盖终混——和它单独配时一样
    assert _stirred(together, "A-1") == _stirred(alone, "A-1") == ["EMC 加料后制冷搅拌"]
    assert _stirred(together, "B-1") == ["EMC 加料后制冷搅拌", "LiPF6 加料后制冷搅拌"]


def test_ec_is_never_chilled_alone_and_cannot_be_the_last_liquid():
    config = template_config()
    config["routes"]["预热溶剂"]["not_last"] = "EC 常温是固体，加完要紧接着加下一种溶剂"
    header = ["序列号", "EC (g)", "EMC(g)", "DEC(g)", "LiPF6(g)"]
    # X 瓶 EMC 为 0：EMC 那一次搅拌不搅它，EC 之后直接接 DEC
    result = generate([header, ["X-1", 10, 0, 17, 4.6], ["Y-1", 10, 15, 17, 4.6]], config=config)
    assert result["issues"] == []
    assert _stirred(result, "X-1") == ["DEC 加料后制冷搅拌"]
    assert _stirred(result, "Y-1") == ["EMC 加料后制冷搅拌", "DEC 加料后制冷搅拌"]
    # EC 成了这瓶最后一种液体：后面的溶剂都是 0，或 EC 排在最后一列
    last = generate([header, ["X-1", 10, 0, 0, 4.6], ["Y-1", 10, 15, 17, 4.6]], config=config)
    assert last["issues"] == ["第 2 行（X-1）EC 之后在「液体」阶段没有再加别的料：EC 常温是固体，加完要紧接着加下一种溶剂"]
    ordered = generate([["序列号", "EMC(g)", "EC (g)", "LiPF6(g)"], ["Z-1", 15, 10, 4.6]], config=config)
    assert any(item.startswith("第 2 行（Z-1）EC 之后") for item in ordered["issues"]), ordered["issues"]


def test_blank_cells_next_to_filled_ones_are_issues_not_zeros():
    header = ["序列号", "EC (g)", "EMC(g)", "DEC(g)", "LiPF6(g)"]
    result = generate([header, ["D-1", 10, "", 17, 4.6], ["E-1", 10, 15, 17, 4.6], ["F-1", 10, None, 16, 4.6]])
    assert result["issues"] == ["EMC 列第 2、4 行是空白：同一列有的填了、有的空着，不加这种料请填 0"]
    assert generate([header, ["D-1", 10, 0, 17, 4.6], ["E-1", 10, 15, 17, 4.6]])["issues"] == []


def test_volume_check_compares_aliquots_with_the_estimated_mother_liquor():
    config = template_config()
    config["volume_check"] = {"bottles": "bottles", "volume": "volume", "density": 1.35, "reserve": 5}
    # 参考配方 72 g 按 1.35 g/mL 约 53.3 mL：2 × 30 mL + 5 mL 放不下，2 × 20 mL + 5 mL 可以
    result = generate([REFERENCE_HEADER, REFERENCE_ROW], config=config)
    assert result["issues"] == [
        "分装 2 瓶 × 30 mL、母瓶至少留 5 mL，共要 65 mL，超过母液体积（总质量按 1.35 g/mL 估算）："
        "第 2 行（ELY-0001）72 g 约 53.3 mL；请减少分装瓶数或每瓶分装量，或加大配制量"
    ]
    assert generate([REFERENCE_HEADER, REFERENCE_ROW], config=config, params={"volume": 20})["issues"] == []
    # 试剂列不是按 g 填的：估算不了体积，只提醒
    grams = generate([["序列号", "EC (mg)"], ["B-1", 1000]], config=config)
    assert "有试剂列不是按质量（g）填的，估算不了母液体积，没有核对分装量" in grams["warnings"]


def test_template_issues_check_not_last_and_volume_check():
    config = template_config()
    config["routes"]["预热溶剂"]["not_last"] = " "
    config["volume_check"] = {"bottles": "bottles", "volume": "size", "density": 0, "reserve": -1}
    issues = rules.template_issues(config, capability_specs(), {}, {METRIC})
    for item in ("加法「预热溶剂」的 not_last 只能是是或否，或写明原因的文字",
                 "volume_check 的 volume 要写一个实验参数的 key",
                 "volume_check 的 density（估算母液体积用的密度，g/mL）必须是正数",
                 "volume_check 的 reserve（母瓶至少留多少 mL）必须是不小于 0 的数"):
        assert item in issues, (item, issues)
    assert not any("volume_check 的 bottles" in item for item in issues)


def test_applies_to_must_point_at_dose_steps():
    steps = generate([REFERENCE_HEADER, REFERENCE_ROW])["steps"]
    index = next(position for position, step in enumerate(steps) if step["name"] == "LiPF6 加料后制冷搅拌")
    stir, dosed = steps[index], steps[index]["applies_to"]["dosed"]

    def issues(rule):
        return steps_rules.applies_to_issues({**stir, "applies_to": rule}, steps, index)

    assert issues({"dosed": "s99"}) == ["applies_to 引用的投料步骤 s99 不存在或不在本步之前"]
    assert issues({"dosed": "s05"}) == ["applies_to 引用的 s05 不是指定了投料物料与用量参数的设备步骤"]
    assert issues({"dosed": dosed, "then_any": ["s07"]}) == [f"applies_to 的 then_any 引用的 s07 要排在 {dosed} 之后"]
    assert issues(dosed) == ['applies_to 要写成 {"dosed": 投料步骤标识, "then_any": [投料步骤标识…]}']
    manual = {**steps[0], "applies_to": {"dosed": "s07"}}
    assert steps_rules.applies_to_issues(manual, steps, 0) == ["只有设备步骤能按瓶限定处理对象（applies_to）"]
