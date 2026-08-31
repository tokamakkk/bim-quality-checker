"""单文件 HTML 报告生成（DESIGN.md §7.3）。

报告结构（与任务规格一致）：
- 页眉：项目名称、生成时间
- 模型摘要：文件名 / IFC 版本 / 元素总数 / 检查时间
- 摘要统计：Pass / Warn / Fail 三色卡片（DESIGN.md §5.2 色板）
- 按规则分组的判定明细表：列 = 状态图标 / 元素名称 / 类型 / 当前值 / 期望值 / 原因，
  行背景色标记状态（绿 / 黄 / 红 tint），点击列头可排序（原生 JS）
- 规则配置快照：阈值与标准依据（如 900 mm、GB50016），附可折叠原始 JSON
- 页脚：生成工具信息

技术要求：
- 单一自包含 .html（所有 CSS/JS 内嵌），无任何外部依赖
- 响应式（表格横向滚动包裹）、中文字体、@media print 打印优化
- 注意：模板含 CSS 变量（:root { --pass: ... }）与 JS 花括号，不能使用
  str.format（{ --pass } 会被当作占位符 → KeyError），一律用 @@TOKEN@@ 替换
"""

import json
from collections import OrderedDict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

from core.verdict import Verdict, VerdictStatus

# ---------------------------------------------------------------------------
# DESIGN.md §5.2 统一色板（与 UI / 3D / GLB 同 token）
# ---------------------------------------------------------------------------
PASS_HEX, WARN_HEX, FAIL_HEX = "#22c55e", "#eab308", "#ef4444"

# 状态 → (中文名, emoji, 主色)
_STATUS_META = {
    VerdictStatus.PASS: ("通过", "✅", PASS_HEX),
    VerdictStatus.WARN: ("警告", "⚠️", WARN_HEX),
    VerdictStatus.FAIL: ("违规", "❌", FAIL_HEX),
}

# 行背景色：主色的浅 tint（深色正文 + 浅色背景，打印与阅读均清晰）
_ROW_TINTS = {
    VerdictStatus.PASS: "#f0fdf4",
    VerdictStatus.WARN: "#fefce8",
    VerdictStatus.FAIL: "#fef2f2",
}

# 行内排序：违规 → 警告 → 通过
_SEV_ORDER = {VerdictStatus.FAIL: 0, VerdictStatus.WARN: 1, VerdictStatus.PASS: 2}

# check_id 前缀 → 中文规则标题（§7.3 表格分组标题）；未知名回退到配置中的规则名
_RULE_TITLES = {
    "R1": "R1 属性完整性检查（名称 / 防火等级）",
    "R2": "R2 疏散门宽度检查（≥ 900 mm）",
}


# ---------------------------------------------------------------------------
# 辅助函数（任务规格要求的两个公共 helper）
# ---------------------------------------------------------------------------

def _summary_stats(verdicts: List[Verdict]) -> Dict[str, int]:
    """统计三档判定数量。返回 {'pass': n, 'warn': n, 'fail': n}。"""
    n = {VerdictStatus.PASS: 0, VerdictStatus.WARN: 0, VerdictStatus.FAIL: 0}
    for v in verdicts:
        n[v.status] += 1
    return {"pass": n[VerdictStatus.PASS], "warn": n[VerdictStatus.WARN], "fail": n[VerdictStatus.FAIL]}


def _format_verdicts_by_rule(verdicts: List[Verdict]) -> "OrderedDict[str, List[Verdict]]":
    """将 verdicts 按 check_id 分组，保持首次出现顺序。返回 {check_id: [verdicts]}。"""
    groups: "OrderedDict[str, List[Verdict]]" = OrderedDict()
    for v in verdicts:
        groups.setdefault(v.check_id, []).append(v)
    return groups


# ---------------------------------------------------------------------------
# 规则配置 → 报告内容
# ---------------------------------------------------------------------------

def _check_id_of(check: Dict[str, Any]) -> str:
    """从检查项配置取 check_id（约定：name 的第一个 token，与引擎一致）。"""
    return check.get("name", "").split()[0]


def _rule_map(rules_config: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """{check_id: rule_config} —— 依据配置中的 checks 顺序建立。"""
    mapping = {}
    for rule in rules_config.get("validation_rules", []):
        for check in rule.get("checks", []):
            mapping[_check_id_of(check)] = rule
    return mapping


def _threshold_text(condition: Dict[str, Any]) -> str:
    """条件 → 阈值文本（单位 m 统一换算为 mm 展示，与引擎口径一致）。"""
    if condition.get("type") != "range":
        return "—"
    unit = condition.get("unit", "")
    scale = 1000 if unit == "m" else 1
    unit_disp = "mm" if unit == "m" else unit
    parts = []
    if "min" in condition:
        parts.append(f"≥ {condition['min'] * scale:g} {unit_disp}")
    if "max" in condition:
        parts.append(f"≤ {condition['max'] * scale:g} {unit_disp}")
    return " 且 ".join(parts)


def _condition_text(condition: Dict[str, Any]) -> str:
    """条件类型 + 来源 → 中文说明。"""
    cond_type = condition.get("type", "")
    if cond_type == "non_empty":
        if condition.get("source") == "pset_any":
            return "非空（任意属性集，含厂商 Pset）"
        return "非空（直接属性）"
    if cond_type == "range":
        missing = condition.get("missing", "warn")
        missing_text = "缺失时警告" if missing == "warn" else f"缺失时 {missing}"
        return f"{_threshold_text(condition)} · {missing_text}"
    return cond_type or "—"


def _snapshot_rows(rules_config: Dict[str, Any]) -> str:
    """规则配置快照：逐规则渲染阈值 / 标准依据 / 缺失行为。"""
    rows = []
    for rule in rules_config.get("validation_rules", []):
        entities = "、".join(rule.get("entity", [])) or "—"
        rows.append(
            f'<tr><td><b>{rule.get("name", "—")}</b>'
            f'<div class="mono" style="font-size:12px">{rule.get("description", "")}</div></td>'
            f'<td>{entities}</td>'
            f'<td>{"<br>".join(_snapshot_check_rows(c) for c in rule.get("checks", []))}</td>'
            f'<td>{"<br>".join(c.get("condition", {}).get("threshold_basis", "—") for c in rule.get("checks", []))}</td>'
            f'</tr>'
        )
    return "\n".join(rows)


def _snapshot_check_rows(check: Dict[str, Any]) -> str:
    """单条检查 → 快照单元格（检查名 + 条件说明）。"""
    cid = _check_id_of(check)
    cond = _condition_text(check.get("condition", {}))
    return f'<b>{cid}</b> {check.get("attribute", "")} · {cond}'


# ---------------------------------------------------------------------------
# 判定明细行 / 表格
# ---------------------------------------------------------------------------

def _row_html(v: Verdict) -> str:
    """一条判定 → 表格行（背景色 tint + data-* 供排序）。"""
    label, emoji, color = _STATUS_META[v.status]
    tint = _ROW_TINTS[v.status]
    return (
        f'<tr data-status="{_SEV_ORDER[v.status]}" '
        f'data-element_name="{v.element_name}" data-ifc_type="{v.ifc_type}" '
        f'data-current_value="{v.current_value}" data-expected="{v.expected}" '
        f'data-reason="{v.reason}" style="background:{tint}">'
        f'<td><span class="tag" style="background:{color}">{emoji} {label}</span></td>'
        f'<td>{v.element_name}</td>'
        f'<td>{v.ifc_type}</td>'
        f'<td>{v.current_value}</td>'
        f'<td>{v.expected}</td>'
        f'<td>{v.reason}</td>'
        f'</tr>'
    )


def _rule_section(title: str, description: str, verdicts: List[Verdict]) -> str:
    """单个规则分组：h2 标题 + 可排序明细表。"""
    sorted_v = sorted(verdicts, key=lambda v: (_SEV_ORDER[v.status], v.element_name))
    rows = "\n".join(_row_html(v) for v in sorted_v)
    desc = f'<p class="rule-desc">{description}</p>' if description else ""
    return (
        f'<section>'
        f'<h2>{title}</h2>{desc}'
        f'<div class="table-wrap"><table class="sortable">'
        f'<thead><tr>'
        f'<th data-key="status">状态</th>'
        f'<th data-key="element_name">元素名称</th>'
        f'<th data-key="ifc_type">类型</th>'
        f'<th data-key="current_value">当前值</th>'
        f'<th data-key="expected">期望值</th>'
        f'<th data-key="reason">原因</th>'
        f'</tr></thead>'
        f'<tbody>{rows}</tbody>'
        f'</table></div>'
        f'</section>'
    )


def _rule_sections_html(verdicts: List[Verdict], rules_config: Dict[str, Any]) -> str:
    """按规则分组渲染全部判定明细表。

    分组顺序 = 规则配置顺序；同一规则下合并其全部检查项（如 R1a + R1b）。
    配置外的 check_id（或配置缺失）落入「其他检查」分组，避免丢数据。
    """
    by_check = _format_verdicts_by_rule(verdicts)
    if not by_check:
        return '<section><p class="rule-desc">没有产生任何判定。</p></section>'

    check_to_rule = _rule_map(rules_config)
    sections: List[str] = []

    # ① 配置中的规则（保持配置顺序）
    for rule in rules_config.get("validation_rules", []):
        rule_checks = [_check_id_of(c) for c in rule.get("checks", [])]
        rule_verdicts = [v for cid in rule_checks for v in by_check.get(cid, [])]
        if not rule_verdicts:
            continue
        # 规则标题键：check_id 去掉尾部字母（"R1a" → "R1"），查中文标题表
        prefix = next((cid.rstrip("abcdefghijklmnopqrstuvwxyz") for cid in rule_checks), "")
        key = _RULE_TITLES.get(prefix, rule.get("name", "检查规则"))
        sections.append(_rule_section(key, rule.get("description", ""), rule_verdicts))

    # ② 配置之外的 check_id（防御：不丢任何判定）
    known = set(check_to_rule)
    extras = [v for cid, vs in by_check.items() if cid not in known for v in vs]
    if extras:
        sections.append(_rule_section("其他检查", "", extras))

    return "\n".join(sections)


# ---------------------------------------------------------------------------
# 报告模板（@@TOKEN@@ 占位；CSS/JS 内嵌，无外部依赖）
# ---------------------------------------------------------------------------
_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>BIM 质量检查报告</title>
<style>
  :root { --pass: #22c55e; --warn: #eab308; --fail: #ef4444; --ink: #1f2937; --muted: #6b7280; --line: #e5e7eb; }
  * { box-sizing: border-box; }
  body { font-family: "Microsoft YaHei", "PingFang SC", "Noto Sans SC", system-ui, sans-serif;
         color: var(--ink); margin: 0; background: #f8fafc; -webkit-print-color-adjust: exact; print-color-adjust: exact; }
  header { background: #111827; color: #fff; padding: 26px 40px; }
  header h1 { margin: 0 0 6px; font-size: 22px; }
  header p { margin: 0; color: #9ca3af; font-size: 13px; }
  main { max-width: 1000px; margin: 24px auto; padding: 0 24px 60px; }
  section { background: #fff; border: 1px solid var(--line); border-radius: 10px;
            padding: 18px 22px; margin-bottom: 20px; }
  h2 { font-size: 16px; margin: 0 0 6px; border-left: 4px solid #111827; padding-left: 10px; }
  .rule-desc { color: var(--muted); font-size: 13px; margin: 0 0 12px; }
  .summary-bar { display: flex; gap: 12px; flex-wrap: wrap; }
  .stat { flex: 1 1 140px; border-radius: 8px; padding: 14px 16px; color: #fff; }
  .stat b { display: block; font-size: 26px; }
  .stat span { font-size: 13px; opacity: .92; }
  .table-wrap { overflow-x: auto; }
  table { width: 100%; border-collapse: collapse; font-size: 13px; min-width: 620px; }
  th, td { text-align: left; padding: 8px 10px; border-bottom: 1px solid var(--line); vertical-align: top; }
  th { background: #f3f4f6; cursor: pointer; user-select: none; white-space: nowrap; }
  th::after { content: " ↕"; font-size: 11px; color: var(--muted); }
  tr:hover td { background: #f9fafb; }
  .tag { display: inline-block; padding: 1px 8px; border-radius: 10px; color: #fff; font-size: 12px; white-space: nowrap; }
  .mono { font-family: Consolas, "Courier New", monospace; }
  pre { background: #f3f4f6; border-radius: 8px; padding: 14px; overflow-x: auto;
        font-size: 12px; line-height: 1.5; }
  details summary { cursor: pointer; font-size: 13px; color: var(--muted); margin: 10px 0 4px; }
  footer { text-align: center; color: var(--muted); font-size: 12px; padding: 20px; }
  @media print {
    header { background: #111827 !important; }
    main { max-width: none; margin: 0; padding: 0 8px; }
    section { break-inside: avoid; }
    tr { break-inside: avoid; }
    footer { position: fixed; bottom: 0; width: 100%; }
  }
</style>
</head>
<body>
<header>
  <h1>@@PROJECT_NAME@@</h1>
  <p>BIM 质量检查报告 · 生成时间：@@GENERATED_AT@@</p>
</header>
<main>
  <section>
    <h2>模型摘要</h2>
    <table>
      <tr><th style="width:180px">文件名</th><td>@@FILE_NAME@@</td></tr>
      <tr><th>IFC 版本</th><td>@@SCHEMA@@</td></tr>
      <tr><th>元素总数</th><td>@@ELEMENT_TOTAL@@</td></tr>
      <tr><th>检查时间</th><td>@@CHECKED_AT@@</td></tr>
    </table>
  </section>

  <section>
    <h2>摘要统计</h2>
    <div class="summary-bar">
      <div class="stat" style="background: var(--fail)"><b>@@N_FAIL@@</b><span>违规（需修复）</span></div>
      <div class="stat" style="background: var(--warn)"><b>@@N_WARN@@</b><span>警告（数据缺失）</span></div>
      <div class="stat" style="background: var(--pass)"><b>@@N_PASS@@</b><span>通过</span></div>
    </div>
  </section>

  @@RULE_SECTIONS@@

  <section>
    <h2>规则配置快照</h2>
    <div class="table-wrap"><table>
      <thead><tr><th style="width:36%">规则</th><th style="width:14%">目标类型</th>
      <th>检查项 / 阈值</th><th>标准依据</th></tr></thead>
      <tbody>@@SNAPSHOT_ROWS@@</tbody>
    </table></div>
    <details>
      <summary>查看完整配置 JSON</summary>
      <pre>@@RULES_SNAPSHOT@@</pre>
    </details>
  </section>
</main>
<footer>由 BIM Quality Checker（AI-Agent Web Prototype）生成 · 判定逻辑见 DESIGN.md §7</footer>
<script>
// 点击列头排序（原生 JS，无依赖；多表格共用）
document.querySelectorAll('table.sortable').forEach(t => {
  const tbody = t.querySelector('tbody');
  const rows = Array.from(tbody.querySelectorAll('tr'));
  t.querySelectorAll('th').forEach(th => {
    th.addEventListener('click', () => {
      const key = th.dataset.key;
      const asc = t.dataset.dir !== key;
      t.dataset.dir = key;
      const dir = asc ? 1 : -1;
      rows.sort((a, b) => {
        const av = (a.dataset[key] || ''), bv = (b.dataset[key] || '');
        const an = parseFloat(av), bn = parseFloat(bv);
        const cmp = (!isNaN(an) && !isNaN(bn)) ? an - bn : av.localeCompare(bv, 'zh');
        return cmp * dir;
      });
      rows.forEach(r => tbody.appendChild(r));
    });
  });
});
</script>
</body>
</html>
"""


def generate_report(
    verdicts: List[Verdict],           # Verdict 对象列表
    model_info: Dict[str, Any],        # {'filename', 'schema', 'element_count', 'project_name', 'checked_at'}
    rules_config: Dict[str, Any],      # 规则配置 JSON（含 validation_rules）
    output_path: str,                  # 输出 HTML 文件路径
) -> str:
    """生成单文件 HTML 报告，返回输出文件路径。

    :param verdicts:     判定列表（来自 run_checks）
    :param model_info:   模型摘要 dict，字段均可选：
                         filename / schema / element_count / project_name / checked_at
    :param rules_config: 规则配置 dict（validation_rules 列表）
    :param output_path:  输出 .html 路径（父目录不存在时自动创建）
    """
    stats = _summary_stats(verdicts)

    tokens = {
        "@@PROJECT_NAME@@": model_info.get("project_name") or "BIM 质量检查报告",
        "@@GENERATED_AT@@": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "@@FILE_NAME@@": model_info.get("filename", "—"),
        "@@SCHEMA@@": model_info.get("schema", "—"),
        "@@ELEMENT_TOTAL@@": str(model_info.get("element_count", "—")),
        "@@CHECKED_AT@@": model_info.get("checked_at") or "—",
        "@@N_FAIL@@": str(stats["fail"]),
        "@@N_WARN@@": str(stats["warn"]),
        "@@N_PASS@@": str(stats["pass"]),
        "@@RULE_SECTIONS@@": _rule_sections_html(verdicts, rules_config),
        "@@SNAPSHOT_ROWS@@": _snapshot_rows(rules_config),
        "@@RULES_SNAPSHOT@@": json.dumps(rules_config, ensure_ascii=False, indent=2),
    }
    html = _TEMPLATE
    for token, value in tokens.items():
        html = html.replace(token, value)
    if "@@" in html:  # 防御：模板与 token 不一致时明确报错，而不是产出坏报告
        cut = html.index("@@")
        raise ValueError(f"报告模板存在未替换的占位符：…{html[cut - 20:cut + 20]}…")

    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    return str(out)
