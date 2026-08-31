"""端到端集成测试 —— 全模块协同（DESIGN.md §8.1 验收口径）。

test_full_pipeline()：bad_model.ifc 一条链路走完
    加载 → 引擎检查 → 着色 GLB 导出 → HTML 报告生成，
    并断言各模块产出互相咬合（同一模型、同一判定集合）。

test_repair_flow()：Agent 修复（Capability B，§6.2）
    找出全部 < 900mm 的门 → set_door_width 改为 1000mm → 重检全绿，
    且**原文件未被修改**（工作副本机制 = 撤销能力）。

均只依赖 src 核心模块（不启动服务器），离线可运行。
"""

import sys
from pathlib import Path

import pytest

# src 不是包目录（无 __init__.py），将 src/ 加入导入路径
SRC_DIR = Path(__file__).resolve().parents[1] / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

ROOT = SRC_DIR.parent
SAMPLE_DIR = ROOT / "sample_data"
CONFIG_RULES = ROOT / "config" / "rules.json"

from core.engine import load_rules, run_checks  # noqa: E402
from report.report_html import generate_report  # noqa: E402
from viz.mesh_exporter import export_colored_glb  # noqa: E402


@pytest.fixture(scope="module")
def bad_model_path():
    """§8.1 验收主角：bad_model.ifc（2 墙 + 4 门，5 处缺陷）。"""
    path = SAMPLE_DIR / "bad_model.ifc"
    if not path.exists() or path.stat().st_size == 0:
        pytest.skip("bad_model.ifc 尚未生成")
    return path


def test_full_pipeline(bad_model_path, tmp_path_factory):
    """上传文件 → 运行检查 → 着色 3D → 导出报告 的完整链路。"""
    rules = load_rules(CONFIG_RULES)
    tmp = tmp_path_factory.mktemp("e2e")

    # ① 引擎检查：§8.1 验收 —— R1 至少 1 fail，R2 至少 2 fail
    verdicts = run_checks(bad_model_path, rules)
    r1_fail = [v for v in verdicts if v.check_id == "R1" and v.is_fail] or \
              [v for v in verdicts if v.check_id in ("R1a", "R1b") and v.is_fail]
    r2_fail = [v for v in verdicts if v.check_id == "R2" and v.is_fail]
    assert len(r1_fail) >= 1, "R1 应至少 1 条违规（空名称）"
    assert len(r2_fail) >= 2, "R2 应至少 2 条违规（700/800 mm）"
    assert sum(1 for v in verdicts if not v.is_pass) == 5  # §8.1：4 fail / 1 warn

    # ② 导出着色 GLB：文件存在且非空，网格可被 trimesh 解析
    glb_path = export_colored_glb(
        bad_model_path, verdicts, tmp / "model_all.glb", mode="all"
    )
    assert Path(glb_path).exists() and Path(glb_path).stat().st_size > 0
    import trimesh
    scene = trimesh.load(glb_path)
    assert scene is not None

    # ③ 导出 HTML 报告：包含模型信息、按规则分组、违规标记与阈值快照
    model_info = {
        "filename": Path(bad_model_path).name,
        "schema": "IFC4",
        "element_count": 6,
        "project_name": "测试项目",
        "checked_at": "2026-08-30 10:00",
    }
    report_path = generate_report(
        verdicts, model_info, rules, tmp / "report.html"
    )
    assert Path(report_path).exists() and Path(report_path).stat().st_size > 0
    html = Path(report_path).read_text(encoding="utf-8")
    assert "违规" in html
    assert "R1 属性完整性检查" in html and "R2 疏散门宽度检查" in html
    assert html.count('class="sortable"') == 2
    assert "≥ 900 mm" in html and "GB50016" in html


def test_repair_flow(bad_model_path, tmp_path_factory):
    """Agent 修复全流程（DESIGN §6.2 三工具）：门宽 → 名称 → 防火等级 → 全绿。

    每一步只动目标属性（门宽修复后 R1 问题仍在 = 修复是精准的），
    工作副本机制保证原文件始终不被修改（§6.2 撤销能力）。
    """
    from agent import chat

    rules = load_rules(CONFIG_RULES)
    tmp = tmp_path_factory.mktemp("repair")
    original = bad_model_path.read_bytes()

    # 修复前：3 扇窄门（700/800/800 mm）
    verdicts = run_checks(bad_model_path, rules)
    assert sum(1 for v in verdicts if v.check_id == "R2" and v.is_fail) == 3

    # ① 门宽修复（set_door_width）：R2 全绿；R1 问题精准保留
    r1 = chat("把所有小于900mm的门改成1000mm", [], verdicts,
              str(bad_model_path), rules, tmp)
    assert "已修复 3 扇门" in r1["reply"]
    assert not any(v.check_id == "R2" and v.is_fail for v in r1["verdicts"])
    remaining = [v for v in r1["verdicts"] if not v.is_pass]
    assert len(remaining) == 2  # R1a 空名 fail + R1b 缺防火等级 warn

    # ② 名称补全（set_property：Name）：R1a 全绿
    r2 = chat("把空名称的构件都补上名字", [], r1["verdicts"],
              r1["model_path"], rules, tmp)
    assert "已补上名称 1 个构件" in r2["reply"]
    assert not any(v.check_id == "R1a" and v.is_fail for v in r2["verdicts"])

    # ③ 防火等级补全（set_property：FireRating）：R1b 全绿 → 整个模型 0 fail / 0 warn
    r3 = chat("给缺少防火等级的构件补上防火等级", [], r2["verdicts"],
              r2["model_path"], rules, tmp)
    assert "已补上防火等级 1 个构件" in r3["reply"]
    assert all(v.is_pass for v in r3["verdicts"]), "修复后应全绿"

    # ④ 原文件未被修改（工作副本 = 撤销机制）
    assert r1["model_path"] != str(bad_model_path)
    assert bad_model_path.read_bytes() == original
