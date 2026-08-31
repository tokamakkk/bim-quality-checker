"""IFC 工具函数 —— 元素属性提取与属性集遍历。

对应 DESIGN.md §4.4 借用的 BQC 模式：把元素直接属性 + 属性集展平为
查找表。FireRating 等属性不在 IFC 直接属性上，而是藏在属性集
（IfcPropertySet）中；Revit / ArchiCAD 导出时可能放在厂商自定义
Pset（如 Pset_Revit_...）而非标准 Pset（Pset_WallCommon）里，
因此必须遍历元素挂接的所有属性集（DESIGN.md §2.1）。

说明：若只想快速拿某个属性的值，ifcopenshell.util.element.get_pset()
可以一步返回 {pset_name: {属性名: 值}}，本模块的实现与该工具行为
一致，但显式展开了完整遍历链路，便于测试与理解。
"""

from typing import Any, Dict, List, Optional, Tuple

import ifcopenshell.ifcopenshell_wrapper
from ifcopenshell.entity_instance import entity_instance


def get_element_name(element) -> str:
    """获取元素 Name（直接属性）。

    R1a 判定依赖：Name 为空串/None 时返回空字符串。
    """
    if element is None:
        return ""
    return getattr(element, "Name", None) or ""


def get_ifc_type(element) -> str:
    """返回最具体的 IFC 类型字符串，如 "IfcWall" / "IfcDoor"。"""
    if element is None:
        return ""
    try:
        # is_a() 无参调用返回最具体的实体类型名
        return element.is_a()
    except Exception:  # 兜底：非 ifcopenshell 实例（如测试桩）
        return type(element).__name__


def get_element_guid(element) -> str:
    """返回元素 GlobalId（IfcGloballyUniqueId 解包后为 str）。

    在 ifcopenshell 中 GlobalId 是 STRING 派生类型，getattr 通常已解包
    为 str；这里对 express 类型实例做 wrappedValue 兜底。
    """
    if element is None:
        return ""
    guid = getattr(element, "GlobalId", None)
    if guid is None:
        return ""
    if isinstance(guid, entity_instance):
        wrapped = getattr(guid, "wrappedValue", None)
        return str(wrapped) if wrapped is not None else ""
    return str(guid)


def _is_non_empty(value) -> bool:
    """判定属性值是否"非空"：None / 空白字符串均视为空。"""
    if value is None:
        return False
    if isinstance(value, str):
        return value.strip() != ""
    return True


def get_pset_value(element, property_name: str):
    """跨所有属性集查找属性值，返回第一个**非空**匹配值（DESIGN.md §2.1）。

    兼容 Revit / ArchiCAD 导出差异：FireRating 可能位于标准 Pset
    （Pset_WallCommon / Pset_DoorCommon）或厂商 Pset（Pset_Revit_...），
    因此遍历 element.IsDefinedBy 挂接的每一个属性集，任一属性集中
    存在非空值即判定为"存在"。

    完整链路（IFC 2×3 与 IFC4 通用）：
        element.IsDefinedBy → IfcRelDefinesByProperties.RelatingPropertyDefinition
          → IfcPropertySet.HasProperties → IfcPropertySingleValue.NominalValue

    同时兼容 IfcPropertyEnumeratedValue（枚举值取第一个）。
    全部属性集均无匹配/值为空时返回 None。
    """
    if element is None:
        return None
    defined_by = getattr(element, "IsDefinedBy", None) or []
    for rel in defined_by:
        # 只关心属性定义关系；IsDefinedBy 中还有 IfcRelDefinesByType 等
        if not rel.is_a("IfcRelDefinesByProperties"):
            continue
        pset = getattr(rel, "RelatingPropertyDefinition", None)
        # 跳过 IfcElementQuantity 等非属性集定义（它们是量集，不是属性）
        if pset is None or not pset.is_a("IfcPropertySet"):
            continue
        for prop in getattr(pset, "HasProperties", None) or []:
            value = _property_value(prop, property_name)
            if _is_non_empty(value):
                return value
    return None


def _property_value(prop, property_name: Optional[str] = None):
    """从单个 IfcProperty 提取属性值。

    property_name 给定时仅匹配同名属性（get_pset_value 用）；
    为 None 时返回任意属性的值（构件卡片全量展示用）。
    仅处理 IfcPropertySingleValue（标量值）与 IfcPropertyEnumeratedValue
    （枚举值，取第一个）；其余属性类型（如复合属性）不予考虑。
    """
    if property_name is not None and getattr(prop, "Name", None) != property_name:
        return None
    if prop.is_a("IfcPropertySingleValue"):
        nominal = getattr(prop, "NominalValue", None)
        if nominal is None:
            return None
        # 解包 express 类型实例（IfcLabel / IfcBoolean / IfcLengthMeasure...）
        return getattr(nominal, "wrappedValue", nominal)
    if prop.is_a("IfcPropertyEnumeratedValue"):
        values = getattr(prop, "EnumerationValues", None) or []
        if values:
            first = values[0]
            return getattr(first, "wrappedValue", first)
    return None


# ---------------------------------------------------------------------------
# 构件卡片数据（UI 中间列「构件卡片列表」，DESIGN 需求）
# ---------------------------------------------------------------------------

def _format_value(value: Any) -> str:
    """属性值 → 展示字符串：布尔转「是 / 否」，其余原样转 str。"""
    if isinstance(value, bool):
        return "是" if value else "否"
    return str(value)


def _direct_properties(element) -> List[Tuple[str, str]]:
    """元素直接属性中所有标量可读值（排除实体引用与 GlobalId）。

    直接属性 = IFC schema 声明的非逆向属性（如 IfcDoor 的
    OverallWidth / OverallHeight / PredefinedType / OperationType...）。
    值类型为实体引用（OwnerHistory / ObjectPlacement / Representation）
    或集合（LIST/SET OF ...）的无法友好展示，跳过；枚举值（Ifc*Enum）
    与基础类型（IfcLabel / IfcText / IfcBoolean / IfcLengthMeasure...）
    解包 wrappedValue 后展示。GlobalId 由 extract_components 单独携带，
    不在属性列表里重复出现。
    """
    try:
        schema_name = element.file.wrapped_data.schema
        decl = ifcopenshell.ifcopenshell_wrapper.schema_by_name(schema_name)
        # all_attributes 含继承链（IfcRoot.Name 等），attributes 只含类型自身声明
        attrs = decl.declaration_by_name(element.is_a()).all_attributes()
    except Exception:
        return []
    props = []
    for attr in attrs:
        name = attr.name()
        if name == "GlobalId":
            continue
        try:
            value = getattr(element, name)
        except Exception:
            continue
        if value is None:
            continue
        if isinstance(value, entity_instance):
            wrapped = getattr(value, "wrappedValue", None)
            if wrapped is None:
                continue  # 实体引用（IfcOwnerHistory 等对象）
            value = wrapped
        if isinstance(value, (str, float, int, bool)):
            props.append((name, _format_value(value)))
    return props


def _pset_properties(element) -> List[Tuple[str, str]]:
    """全部属性集的全部属性 → [(属性名（pset名）, 值)]。

    与 get_pset_value 同一遍历链路（IsDefinedBy → IfcRelDefinesByProperties
    → IfcPropertySet.HasProperties），但返回**所有**属性的键值对，
    而非按名查找单个属性。键附带属性集名，避免不同 pset 同名冲突
    （如标准 Pset_WallCommon 与厂商 Pset_Revit_* 都可能有 FireRating）。
    属性存在但值为空 → 显示「（空）」；非标量/枚举之外的属性类型跳过。
    """
    props = []
    for rel in getattr(element, "IsDefinedBy", None) or []:
        # IsDefinedBy 中还有 IfcRelDefinesByType 等关系，只关心属性定义
        if not rel.is_a("IfcRelDefinesByProperties"):
            continue
        pset = getattr(rel, "RelatingPropertyDefinition", None)
        # 跳过 IfcElementQuantity 等非属性集定义（量集不是属性）
        if pset is None or not pset.is_a("IfcPropertySet"):
            continue
        pset_name = getattr(pset, "Name", None) or "属性集"
        for prop in getattr(pset, "HasProperties", None) or []:
            prop_name = getattr(prop, "Name", None) or "?"
            key = f"{prop_name}（{pset_name}）"
            value = _property_value(prop)
            if value is None:
                # 值确实为空（属性存在但无值）→ 展示占位；
                # 非 SingleValue/EnumeratedValue 的属性类型也归入此处跳过语义
                if prop.is_a("IfcPropertySingleValue") or prop.is_a("IfcPropertyEnumeratedValue"):
                    props.append((key, "（空）"))
                continue
            props.append((key, _format_value(value)))
    return props


def extract_components(ifc, entity_types: Tuple[str, ...] = ("IfcWall", "IfcDoor")) -> List[Dict[str, Any]]:
    """提取被检查构件列表（构件卡片数据源）。

    对指定 IFC 类型逐元素收集：GlobalId / 名称 / 具体类型 / 全部可读
    属性（直接属性 + 属性集，见 _direct_properties / _pset_properties）。
    按名称排序（无名称按「（未命名）」参与排序），保证卡片顺序稳定。

    :param ifc:          已打开的 ifcopenshell 模型
    :param entity_types: 被检查的构件类型（默认与 config/rules.json 一致）
    :return: [{"guid", "name", "ifc_type", "props": [(键, 值), ...]}, ...]
    """
    components: List[Dict[str, Any]] = []
    for entity_type in entity_types:
        for element in ifc.by_type(entity_type):
            components.append({
                "guid": get_element_guid(element),
                "name": get_element_name(element) or "（未命名）",
                "ifc_type": get_ifc_type(element),
                "props": _direct_properties(element) + _pset_properties(element),
            })
    components.sort(key=lambda c: c["name"])
    return components


def get_overall_width_mm(door) -> Optional[float]:
    """读取 IfcDoor.OverallWidth（直接属性，单位：米）并换算为毫米。

    DESIGN.md §2.2：R2 检查的是名义门宽 OverallWidth（作为净宽的
    文档化近似），阈值 900 mm 以毫米比较。属性缺失返回 None
    （规则层据此给出 warn —— 无法判定）。
    """
    if door is None:
        return None
    width = getattr(door, "OverallWidth", None)
    if width is None:
        return None
    # 兼容 ifcopenshell 返回 express 类型实例（需解包 wrappedValue）的情况
    width = getattr(width, "wrappedValue", width)
    if width is None:
        return None
    return float(width) * 1000.0
