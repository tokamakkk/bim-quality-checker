"""规则引擎测试 —— mock 模型（精确控制缺陷）+ 真实文件冒烟 + 错误处理。

覆盖 DESIGN.md §8.1 的判定语义：
- R1a：Name 空 → FAIL；有名字 → PASS
- R1b：FireRating 缺失（跨所有 pset 查找不到）→ WARN；任一 pset（含
      厂商 Pset_Revit_...）存在非空值 → PASS（§2.1 核心语义）
- R2 ：OverallWidth ≥ 900 mm → PASS；< 900 mm → FAIL；缺失 → WARN（§2.2）
- 错误处理：模型/规则文件不存在 → 友好 FileNotFoundError

mock 模型由 ifcopenshell.api 构造（与 DESIGN.md §3.2 的合成模型生成方式
一致），写入 pytest 临时目录后经 run_checks(model_path, rules) 全链路运行。
"""

import sys
from pathlib import Path

import ifcopenshell
import pytest
from ifcopenshell import api

# src 不是包目录（无 __init__.py），将 src/ 加入导入路径
SRC_DIR = Path(__file__).resolve().parents[1] / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

SAMPLE_DIR = Path(__file__).resolve().parents[1] / "sample_data"
CONFIG_RULES = SRC_DIR.parent / "config" / "rules.json"

from core.engine import load_rules, run_checks  # noqa: E402
from core.verdict import Verdict, VerdictStatus  # noqa: E402


# ---------------------------------------------------------------------------
# mock 模型构造（临时目录内）
# ---------------------------------------------------------------------------

def _make_mock_ifc():
    """用 ifcopenshell.api 构造 5 元素 mock 模型（内存）。

    场景设计（覆盖每条规则的全部分支）：
        wall_01    有名字的墙            → R1a PASS · R1b WARN（无 pset）
        wall_02    空名字的墙            → R1a FAIL · R1b WARN
        door_01    0.9 m 门 + 标准 pset FireRating → 全 PASS（R2 边界 = 900）
        door_02    0.8 m 门、无 pset      → R1a PASS · R1b WARN · R2 FAIL
        door_03    无宽度 + 厂商 pset FireRating → R1b PASS（§2.1 厂商 pset）
                                              · R2 WARN（宽度缺失）
    """
    ifc = ifcopenshell.file(schema="IFC4")
    api.run("root.create_entity", ifc, ifc_class="IfcProject", name="MockProject")
    api.run("context.add_context", ifc, context_type="Model")

    # ① 有名字的墙（R1a 通过）
    api.run("root.create_entity", ifc, ifc_class="IfcWall", name="墙-01")
    # ② 空名字的墙（R1a 失败 —— DESIGN.md §3.2 缺陷①）
    api.run("root.create_entity", ifc, ifc_class="IfcWall")

    # ③ 合规门：0.9 m 宽 + 标准 Pset_DoorCommon 含 FireRating
    door_good = api.run("root.create_entity", ifc, ifc_class="IfcDoor", name="门-01")
    door_good.OverallWidth = 0.9
    pset_std = api.run("pset.add_pset", ifc, product=door_good, name="Pset_DoorCommon")
    api.run("pset.edit_pset", ifc, pset=pset_std, properties={"FireRating": "2HR"})

    # ④ 窄门：0.8 m 宽、无任何 pset（R2 失败 + R1b 警告）
    door_narrow = api.run("root.create_entity", ifc, ifc_class="IfcDoor", name="门-02")
    door_narrow.OverallWidth = 0.8

    # ⑤ 无宽度门：FireRating 放在厂商 pset（Revit 导出习惯，§2.1）
    door_no_width = api.run("root.create_entity", ifc, ifc_class="IfcDoor", name="门-03")
    pset_vendor = api.run("pset.add_pset", ifc, product=door_no_width, name="Pset_Revit_门")
    api.run("pset.edit_pset", ifc, pset=pset_vendor, properties={"FireRating": "1HR"})

    return ifc


@pytest.fixture
def mock_verdicts(tmp_path):
    """mock 模型写入临时目录，经 run_checks 全链路运行后返回 Verdict 列表。"""
    ifc = _make_mock_ifc()
    path = tmp_path / "mock.ifc"
    ifc.write(str(path))
    rules = load_rules(CONFIG_RULES)  # 使用真实 config/rules.json
    return run_checks(path, rules)


def _by_check(verdicts, check_id):
    """按 check_id 过滤 Verdict 列表。"""
    return [v for v in verdicts if v.check_id == check_id]


# ---------------------------------------------------------------------------
# R1a —— Name 非空
# ---------------------------------------------------------------------------

def test_r1a_mock_name_empty_fails(mock_verdicts):
    """R1a：空 Name 的墙 → FAIL，reason 明确。"""
    fails = [v for v in _by_check(mock_verdicts, "R1a") if v.is_fail]
    assert len(fails) == 1
    assert fails[0].ifc_type == "IfcWall"
    assert fails[0].current_value == "空"
    assert fails[0].expected == "非空 Name"
    assert "名称为空" in fails[0].reason


def test_r1a_mock_named_elements_pass(mock_verdicts):
    """R1a：其余 4 个有名字的元素 → PASS。"""
    passes = [v for v in _by_check(mock_verdicts, "R1a") if v.is_pass]
    assert len(passes) == 4
    assert all("完整" in v.reason for v in passes)


# ---------------------------------------------------------------------------
# R1b —— FireRating 跨所有 pset 查找
# ---------------------------------------------------------------------------

def test_r1b_mock_missing_firerating_warns(mock_verdicts):
    """R1b：无任何 pset 的元素（2 墙 + 窄门）→ WARN（数据缺失，非违规）。"""
    warns = [v for v in _by_check(mock_verdicts, "R1b") if v.is_warn]
    assert len(warns) == 3
    assert all(v.current_value == "缺失" for v in warns)
    assert all("无法判断" in v.reason for v in warns)


def test_r1b_mock_vendor_pset_counts_as_present(mock_verdicts):
    """R1b：FireRating 在厂商 pset（Pset_Revit_门）也算存在（DESIGN.md §2.1）。"""
    passes = [v for v in _by_check(mock_verdicts, "R1b") if v.is_pass]
    assert len(passes) == 2  # 门-01（标准 pset）+ 门-03（厂商 pset）
    assert all("已标注" in v.reason for v in passes)


# ---------------------------------------------------------------------------
# R2 —— 门宽 ≥ 900 mm
# ---------------------------------------------------------------------------

def test_r2_mock_widths(mock_verdicts):
    """R2 三档：0.9 m → PASS（边界）· 0.8 m → FAIL · 缺失 → WARN。"""
    r2 = _by_check(mock_verdicts, "R2")

    passed = [v for v in r2 if v.is_pass]
    assert len(passed) == 1
    assert passed[0].current_value == "900 mm"          # 值带单位
    assert passed[0].expected == "≥ 900 mm (GB50016)"   # 期望带标准依据
    assert "900mm ≥ 900mm" in passed[0].reason

    failed = [v for v in r2 if v.is_fail]
    assert len(failed) == 1
    assert failed[0].element_name == "门-02"
    assert failed[0].current_value == "800 mm"
    assert "800mm < 900mm" in failed[0].reason and "GB50016" in failed[0].reason

    warned = [v for v in r2 if v.is_warn]
    assert len(warned) == 1
    assert warned[0].element_name == "门-03"
    assert "缺失" in warned[0].reason


# ---------------------------------------------------------------------------
# 全链路 & 错误处理
# ---------------------------------------------------------------------------

def test_mock_total_verdicts(mock_verdicts):
    """2 墙 × 2 checks（R1）+ 3 门 × 3 checks（R1+R2）= 13 条 Verdict，字段完整可序列化。"""
    assert len(mock_verdicts) == 13
    for v in mock_verdicts:
        assert v.element_guid and v.ifc_type  # element_name 允许为空（空名墙正是 R1a 失败场景）
        assert v.check_id in {"R1a", "R1b", "R2"}
        assert v.status in set(VerdictStatus)
        assert v.current_value and v.expected and v.reason
        d = v.to_dict()
        assert d["status"] in {"pass", "warn", "fail"}


def test_run_checks_real_file():
    """真实 IFC 文件端到端冒烟（Duplex 公寓，57 墙 / 14 门）。"""
    path = SAMPLE_DIR / "Duplex_A_20110907.ifc"
    if not path.exists():
        pytest.skip("Duplex 样本文件缺失")
    verdicts = run_checks(path, load_rules(CONFIG_RULES))
    assert verdicts
    # R2 必须覆盖全部 IfcDoor
    assert len(_by_check(verdicts, "R2")) == 14
    # R1 覆盖全部墙与门（57 + 14）
    assert len(_by_check(verdicts, "R1a")) == 71
    assert len(_by_check(verdicts, "R1b")) == 71


def test_run_checks_no_target_elements(tmp_path):
    """模型中没有目标类型元素 → 返回空列表（不报错）。"""
    ifc = _make_mock_ifc()
    path = tmp_path / "only_walls.ifc"
    ifc.write(str(path))
    rules = {"validation_rules": [{"name": "空检查", "entity": ["IfcSpace"], "checks": []}]}
    assert run_checks(path, rules) == []


def test_run_checks_missing_model():
    """模型文件不存在 → 友好 FileNotFoundError。"""
    with pytest.raises(FileNotFoundError, match="IFC 模型文件不存在"):
        run_checks(SAMPLE_DIR / "no_such_model.ifc", load_rules(CONFIG_RULES))


def test_load_rules_missing_file():
    """规则配置文件不存在 → 友好 FileNotFoundError。"""
    with pytest.raises(FileNotFoundError, match="规则配置文件不存在"):
        load_rules("no_such_rules.json")


def test_load_rules_invalid_json(tmp_path):
    """规则配置损坏（非 JSON）→ 友好 ValueError。"""
    bad = tmp_path / "bad_rules.json"
    bad.write_text("{ 这不是 JSON", encoding="utf-8")
    with pytest.raises(ValueError, match="不是合法 JSON"):
        load_rules(bad)


# ---------------------------------------------------------------------------
# 合成样本模型验收（DESIGN.md §8.1，对应 §3.2 缺陷集）
# ---------------------------------------------------------------------------

def _load_sample_verdicts(name: str):
    """加载样本模型并对真实规则配置运行检查。"""
    path = SAMPLE_DIR / name
    if not path.exists() or path.stat().st_size == 0:
        pytest.skip(f"样本模型 {name} 尚未生成")
    return run_checks(path, load_rules(CONFIG_RULES))


@pytest.fixture
def good_model():
    """健康模型检查结果（good_model.ifc，无缺陷）。"""
    return _load_sample_verdicts("good_model.ifc")


@pytest.fixture
def bad_model():
    """缺陷模型检查结果（bad_model.ifc，5 处缺陷，对齐 §8.1 验收表）。"""
    return _load_sample_verdicts("bad_model.ifc")


def test_good_model_acceptance(good_model):
    """§8.1 验收：good_model → R1 0 fail · R2 0 fail / 0 warn（全绿）。"""
    assert len(good_model) == 13  # 2 墙 × 2 checks + 3 门 × 3 checks
    assert all(v.is_pass for v in good_model)


def test_bad_model_acceptance(bad_model):
    """§8.1 验收：bad_model → R1a 1 fail + R1b 1 warn + R2 3 fail + 1 pass，总计 5 findings。"""
    # R1a：恰好 1 fail —— 空名墙
    r1a_fail = [v for v in bad_model if v.check_id == "R1a" and v.is_fail]
    assert len(r1a_fail) == 1
    assert r1a_fail[0].ifc_type == "IfcWall" and r1a_fail[0].element_name == ""

    # R1b：恰好 1 warn —— 墙-02 缺 FireRating（数据缺失，无法判定）
    r1b_warn = [v for v in bad_model if v.check_id == "R1b" and v.is_warn]
    assert len(r1b_warn) == 1
    assert r1b_warn[0].element_name == "墙-02"

    # R2：恰好 3 fail（800 / 700 / 800 mm，含 FireExit 门）+ 1 pass（1000 mm）
    r2_fail = [v for v in bad_model if v.check_id == "R2" and v.is_fail]
    assert sorted(v.current_value for v in r2_fail) == ["700 mm", "800 mm", "800 mm"]
    r2_pass = [v for v in bad_model if v.check_id == "R2" and v.is_pass]
    assert len(r2_pass) == 1 and r2_pass[0].current_value == "1000 mm"

    # 总计 5 findings（4 fail / 1 warn）
    findings = [v for v in bad_model if not v.is_pass]
    assert len(findings) == 5
    assert sum(v.is_fail for v in findings) == 4
    assert sum(v.is_warn for v in findings) == 1


def test_bad_model_exit_door_marked():
    """门-01（800 mm）带 FireExit 标记 —— §2.2 EXIT 分组机制的数据基础。"""
    ifc = ifcopenshell.open(str(SAMPLE_DIR / "bad_model.ifc"))
    door = next(d for d in ifc.by_type("IfcDoor") if d.Name == "门-01")
    from core.ifc_utils import get_pset_value
    assert get_pset_value(door, "FireExit") is True
