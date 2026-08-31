"""规则条件实现 —— 配置驱动引擎的判定语义（DESIGN.md §4.4）。

engine.py 只负责编排，本模块实现每个 condition 类型（non_empty / range）
的具体判定逻辑与人类可读文案。新增条件类型 = 在本模块新增函数并注册到
_CONDITION_HANDLERS；阈值、严重级、缺失行为等调参只需改 config/rules.json。

reason 文案按属性语义生成（见 _attr_message），保证终端用户能看懂：
    R1a  Name        → "构件名称为空" / "构件名称完整"
    R1b  FireRating  → "防火等级已标注" / "防火等级缺失，无法判断合规性"
    R2   OverallWidth→ "门宽800mm < 900mm，不符合GB50016要求" 等
"""

from typing import Any, Callable, Dict, Tuple

from core.ifc_utils import (
    get_element_guid,
    get_element_name,
    get_ifc_type,
    get_overall_width_mm,
    get_pset_value,
)
from core.verdict import Verdict, VerdictStatus

# 单位 → 毫米换算系数（R2 阈值在配置中以米为单位，如 min: 0.9）
_UNIT_TO_MM = {"m": 1000.0}


def evaluate_check(element, rule: Dict[str, Any], check: Dict[str, Any]) -> Verdict:
    """对单个元素执行单个 check，返回一条 Verdict。

    :param element: ifcopenshell 元素实例
    :param rule:    所属规则配置（含 name）
    :param check:   check 配置（含 name / attribute / condition）
    """
    condition = check.get("condition", {})
    handler: Callable = _CONDITION_HANDLERS.get(condition.get("type"))
    if handler is None:
        # 不静默失败：未知条件类型直接报错（DESIGN.md §6.2 的精神）
        raise ValueError(
            f"不支持的检查条件类型: {condition.get('type')!r}"
            f"（规则: {rule.get('name')} / check: {check.get('name')}）"
        )
    status, current_value, reason = handler(element, check, condition)
    return Verdict(
        element_guid=get_element_guid(element),
        element_name=get_element_name(element),
        ifc_type=get_ifc_type(element),
        check_id=_check_id(check),
        status=status,
        current_value=current_value,
        expected=_expected_text(check, condition),
        reason=reason,
    )


# ---------------------------------------------------------------------------
# 条件检查实现
# ---------------------------------------------------------------------------

def _check_non_empty(element, check: Dict[str, Any], condition: Dict[str, Any]):
    """non_empty：直接属性或属性集属性非空。

    - 直接属性（如 Name / R1a）：空 → FAIL（默认严重级）
    - 属性集属性（FireRating / R1b，source="pset_any"）：跨所有 pset
      查找（DESIGN.md §2.1），缺失/空 → WARN（condition.severity 覆盖）
      —— warn 表示「数据缺失、无法判定」，提示模型不完整而非不合规（§7.2）
    """
    attr = check["attribute"]
    if condition.get("source") == "pset_any":
        value = get_pset_value(element, attr)
        if value is not None and str(value).strip():
            return VerdictStatus.PASS, str(value), _attr_message(attr, "pass")
        status = _severity(condition, default="warn")
        return status, "缺失", _attr_message(attr, "missing")
    # 直接属性路径（R1a Name）
    value = getattr(element, attr, None)
    text = str(value).strip() if value is not None else ""
    if text:
        return VerdictStatus.PASS, text, _attr_message(attr, "pass")
    return VerdictStatus.FAIL, "空", _attr_message(attr, "empty")


def _check_range(element, check: Dict[str, Any], condition: Dict[str, Any]):
    """range：数值下限检查（R2 门宽）。

    阈值从配置读取（min + unit → 毫米），而非硬编码 —— DESIGN.md §2.2
    「阈值是可配置规则参数」，同一引擎可服务 GB50016（900 mm）或
    IBC（813 mm）等不同管辖标准。属性缺失时按 condition.missing 判定
    （配置为 warn —— 无法判定，而非违规）。
    """
    attr = check["attribute"]
    missing = _severity(condition, default=condition.get("missing", "warn"))
    scale = _UNIT_TO_MM.get(condition.get("unit", ""), 1.0)
    threshold = condition.get("min", 0.0) * scale

    # 门宽走专用工具函数（米 → 毫米）；其他属性走通用读取
    if attr == "OverallWidth":
        value = get_overall_width_mm(element)
    else:
        raw = getattr(element, attr, None)
        raw = getattr(raw, "wrappedValue", raw)
        value = None if raw is None else float(raw) * scale

    if value is None:
        return missing, "缺失", _attr_message(attr, "missing")
    if value >= threshold:
        return (
            VerdictStatus.PASS,
            f"{value:g} mm",
            _attr_message(attr, "pass", value, threshold),
        )
    return (
        VerdictStatus.FAIL,
        f"{value:g} mm",
        _attr_message(attr, "fail", value, threshold),
    )


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------

def _check_id(check: Dict[str, Any]) -> str:
    """子检查 ID：配置含显式 id 时优先；否则取 check 名首词
    （"R1a Name present" → "R1a"，对应 DESIGN.md §7.1）。"""
    return str(check.get("id") or check.get("name", "").split()[0])


def _expected_text(check: Dict[str, Any], condition: Dict[str, Any]) -> str:
    """期望值的人类可读描述，含标准依据（DESIGN.md §7.1 expected 字段）。"""
    ctype = condition.get("type")
    attr = check.get("attribute", "")
    if ctype == "non_empty":
        return f"非空 {attr}"
    if ctype == "range":
        scale = _UNIT_TO_MM.get(condition.get("unit", ""), 1.0)
        threshold = condition.get("min", 0) * scale
        # threshold_basis 如 "GB50016 (疏散门净宽 >= 0.9 m) / IBC 813 mm"，
        # 取首个标准名避免 expected 过长 → "≥ 900 mm (GB50016)"
        basis = condition.get("threshold_basis", "")
        basis_short = basis.split()[0] if basis else ""
        text = f"≥ {threshold:g} mm" if scale == 1000.0 else f"≥ {threshold:g}"
        return f"{text} ({basis_short})" if basis_short else text
    return "（未定义）"


def _severity(condition: Dict[str, Any], default: str) -> VerdictStatus:
    """从配置读取严重级（"fail"/"warn"），未知值回退默认。"""
    try:
        return VerdictStatus(condition.get("severity", default))
    except ValueError:
        return VerdictStatus(default)


def _attr_message(attr: str, key: str, value: float = None, threshold: float = None) -> str:
    """按属性语义返回人类可读文案（终端用户可看懂）。"""
    if attr == "Name":
        return {"empty": "构件名称为空", "pass": "构件名称完整"}.get(key, key)
    if attr == "FireRating":
        return {
            "pass": "防火等级已标注",
            "missing": "防火等级缺失，无法判断合规性",
        }.get(key, key)
    if attr == "OverallWidth":
        if key == "missing":
            return "门宽度数据缺失，无法判断"
        if key == "pass":
            return f"门宽{value:g}mm ≥ {threshold:g}mm，符合要求"
        if key == "fail":
            return f"门宽{value:g}mm < {threshold:g}mm，不符合GB50016要求"
    return key


# 条件类型 → 处理函数注册表（新增条件类型在此登记）
_CONDITION_HANDLERS: Dict[str, Callable] = {
    "non_empty": _check_non_empty,
    "range": _check_range,
}
