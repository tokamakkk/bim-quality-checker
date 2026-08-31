"""HTML 报告生成测试（DESIGN.md §7.3）—— 覆盖任务规格的每一项。

- generate_report(verdicts, model_info, rules_config, output_path) 签名
- 页眉（项目名称/生成时间）、模型摘要（文件名/IFC版本/元素总数/检查时间）
- 摘要统计三色卡片、按规则分组的明细表（行背景色标记状态）
- 规则配置快照（阈值 900mm / GB50016 依据）
- 响应式 + 可排序（原生 JS）+ @media print
- 回归保护：模板内嵌 CSS/JS（:root { --pass: ... }），曾因 str.format 把
  `{ --pass` 误读为占位符而 KeyError（' --pass'）
"""

import sys
from pathlib import Path

import pytest

SRC_DIR = Path(__file__).resolve().parents[1] / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

SAMPLE_DIR = Path(__file__).resolve().parents[1] / "sample_data"
CONFIG_RULES = SRC_DIR.parent / "config" / "rules.json"

from core.engine import load_rules, run_checks  # noqa: E402
from report.report_html import (  # noqa: E402
    _format_verdicts_by_rule,
    _summary_stats,
    generate_report,
)

MODEL_INFO = {
    "filename": "bad_model.ifc",
    "schema": "IFC4",
    "element_count": 6,
    "project_name": "测试项目",
    "checked_at": "2026-08-30 10:00",
}


@pytest.fixture(scope="module")
def bad_verdicts():
    """对 bad_model 运行真实规则配置（16 判定：11 通过 / 1 警告 / 4 违规）。"""
    path = SAMPLE_DIR / "bad_model.ifc"
    if not path.exists() or path.stat().st_size == 0:
        pytest.skip("bad_model.ifc 尚未生成")
    return run_checks(path, load_rules(CONFIG_RULES))


@pytest.fixture(scope="module")
def report_html(bad_verdicts, tmp_path_factory):
    """生成 bad_model 报告并返回 HTML 文本。"""
    out = generate_report(
        bad_verdicts, MODEL_INFO, load_rules(CONFIG_RULES),
        tmp_path_factory.mktemp("report") / "report.html",
    )
    return Path(out).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------

def test_summary_stats(bad_verdicts):
    """_summary_stats 返回 {'pass', 'warn', 'fail'} 计数。"""
    s = _summary_stats(bad_verdicts)
    assert s == {"pass": 11, "warn": 1, "fail": 4}


def test_format_verdicts_by_rule_groups_by_check_id(bad_verdicts):
    """_format_verdicts_by_rule 按 check_id 分组，保持首次出现顺序。"""
    groups = _format_verdicts_by_rule(bad_verdicts)
    assert list(groups.keys()) == ["R1a", "R1b", "R2"]
    assert len(groups["R1a"]) == 6 and len(groups["R1b"]) == 6 and len(groups["R2"]) == 4


# ---------------------------------------------------------------------------
# 报告结构（任务规格逐项）
# ---------------------------------------------------------------------------

def test_report_header_and_summary(report_html):
    """页眉含项目名称与生成时间；模型摘要含文件名/IFC版本/元素总数/检查时间。"""
    assert "测试项目" in report_html
    assert "生成时间" in report_html
    assert "bad_model.ifc" in report_html          # 文件名
    assert "IFC4" in report_html                   # IFC 版本
    assert "<td>6</td>" in report_html             # 元素总数
    assert "2026-08-30 10:00" in report_html       # 检查时间


def test_report_stats_cards(report_html):
    """摘要统计三色卡片：4 违规 / 1 警告 / 11 通过。"""
    assert report_html.count('class="stat"') == 3
    for token in ("<b>4</b>", "<b>1</b>", "<b>11</b>"):
        assert token in report_html


def test_report_rules_grouped_tables(report_html):
    """按规则分组：R1 / R2 各一个 h2 标题与 sortable 表格。"""
    assert "R1 属性完整性检查" in report_html
    assert "R2 疏散门宽度检查" in report_html
    assert report_html.count('class="sortable"') == 2
    # 列：状态 / 元素名称 / 类型 / 当前值 / 期望值 / 原因
    for col in ("状态", "元素名称", "类型", "当前值", "期望值", "原因"):
        assert f'>{col}<' in report_html
    # R1 表格行含全部 12 条判定（6 墙门 × R1a/R1b），R2 表格含 4 条
    assert report_html.count('data-status="') == 16
    assert 'data-status="0"' in report_html  # 违规行


def test_report_row_tint_colors(report_html):
    """行背景色标记状态：绿/黄/红 tint（§5.2 色板的浅色变体）。"""
    for tint in ("#f0fdf4", "#fefce8", "#fef2f2"):
        assert f"background:{tint}" in report_html


def test_report_thresholds_in_snapshot(report_html):
    """规则配置快照：阈值 900 mm 与标准依据（GB50016 / IBC 813 mm）。"""
    assert "规则配置快照" in report_html
    assert "≥ 900 mm" in report_html
    assert "GB50016" in report_html and "IBC 813 mm" in report_html
    assert "非空（任意属性集，含厂商 Pset）" in report_html


def test_report_sortable_and_print(report_html):
    """表格可排序（原生 JS）且含 @media print 打印优化。"""
    assert "addEventListener('click'" in report_html
    assert "@media print" in report_html
    assert "break-inside: avoid" in report_html


def test_report_css_vars_no_keyerror(report_html):
    """CSS 变量（--pass/--warn/--fail）原样保留，不再触发 format KeyError（回归）。"""
    assert ":root { --pass: #22c55e" in report_html


def test_report_no_unreplaced_tokens(report_html):
    """占位符全部替换（@@ 不应残留）。"""
    assert "@@" not in report_html


def test_report_empty_verdicts(tmp_path):
    """空判定列表不报错：全零统计卡片 + 「没有产生任何判定」。"""
    out = generate_report([], MODEL_INFO, load_rules(CONFIG_RULES), tmp_path / "empty.html")
    txt = Path(out).read_text(encoding="utf-8")
    assert "<b>0</b>" in txt and "没有产生任何判定" in txt
    assert "@@" not in txt
