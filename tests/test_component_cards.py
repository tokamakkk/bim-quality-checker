"""构件卡片数据提取与 HTML 渲染测试。

覆盖（中间列「构件卡片列表」，需求：运行检查后列出全部被检查构件，
卡片只展示构件信息，不含任何检查状态/违规标记/颜色）：
- extract_components：对 sample_data/bad_model.ifc 提取全部被检查构件
  （IfcWall + IfcDoor），属性 = 直接属性 + 全部属性集，按名称排序
- _component_cards_html：卡片 HTML 含名称/类型/GUID/属性键值、
  内联交互（展开/收起、搜索），IFC 字符串全部转义
"""

import sys
from pathlib import Path

import ifcopenshell
import pytest

SRC_DIR = Path(__file__).resolve().parents[1] / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

SAMPLE_DIR = Path(__file__).resolve().parents[1] / "sample_data"

from app import _component_cards_html  # noqa: E402
from core.ifc_utils import extract_components  # noqa: E402


@pytest.fixture(scope="module")
def bad_components():
    """bad_model.ifc 的构件卡片数据（6 元素：IfcWall×2 + IfcDoor×4）。"""
    path = SAMPLE_DIR / "bad_model.ifc"
    if not path.exists() or path.stat().st_size == 0:
        pytest.skip("bad_model.ifc 尚未生成")
    return extract_components(ifcopenshell.open(str(path)))


def test_extract_components_count_and_types(bad_components):
    """全部被检查构件：6 个（2 墙 + 4 门），每项含 guid/name/ifc_type/props。"""
    assert len(bad_components) == 6
    types = [c["ifc_type"] for c in bad_components]
    assert types.count("IfcWall") == 2
    assert types.count("IfcDoor") == 4
    for c in bad_components:
        assert c["guid"]  # GUID 非空
        assert isinstance(c["name"], str)
        assert isinstance(c["props"], list)


def test_extract_components_sorted_by_name(bad_components):
    """卡片按名称升序排列。"""
    names = [c["name"] for c in bad_components]
    assert names == sorted(names)


def test_extract_components_guid_unique(bad_components):
    """GUID 全局唯一。"""
    guids = [c["guid"] for c in bad_components]
    assert len(guids) == len(set(guids))


def test_direct_properties(bad_components):
    """直接属性：门含 Name / OverallWidth，且 GlobalId 不在属性列表。"""
    door = next(c for c in bad_components if c["name"] == "门-01")
    keys = [k for k, _ in door["props"]]
    assert "Name" in keys
    assert "OverallWidth" in keys
    assert "GlobalId" not in keys  # GUID 由独立字段携带，不重复展示


def test_pset_properties(bad_components):
    """属性集属性：键带 pset 名，布尔值转「是 / 否」。"""
    door = next(c for c in bad_components if c["name"] == "门-01")
    props = dict(door["props"])
    assert props.get("FireRating（Pset_DoorCommon）") == "2h"
    assert props.get("FireExit（Pset_DoorCommon）") == "是"


def test_unnamed_element(bad_components):
    """无名称构件：name 回退为「（未命名）」，属性照常提取。"""
    unnamed = next(c for c in bad_components if c["name"] == "（未命名）")
    assert unnamed["ifc_type"] == "IfcWall"
    assert dict(unnamed["props"]).get("FireRating（Pset_WallCommon）") == "2h"


def test_empty_components_placeholder():
    """空列表 → 占位提示，无搜索框与卡片。"""
    html = _component_cards_html([])
    assert "暂无构件卡片" in html
    assert "cc-card" not in html
    assert "cc-search" not in html


def test_cards_html_structure(bad_components):
    """卡片 HTML：搜索框 + 每构件一张卡片（名称/类型/GUID/属性/交互）。"""
    html = _component_cards_html(bad_components)
    assert 'class="cc-search"' in html  # 搜索框
    assert html.count("cc-card") == 7  # 6 张卡片 + 1 处容器 class
    assert "onclick=" in html  # 展开/收起内联交互
    assert "oninput=" in html  # 搜索内联交互
    # 折叠状态不含展开属性（默认收起）；GUID 与属性在卡片内
    for c in bad_components:
        assert c["name"] in html
        assert c["ifc_type"] in html
        assert c["guid"] in html
    assert "OverallWidth" in html


def test_cards_html_escapes_ifc_strings():
    """IFC 字符串转义：名称/属性值含 < > & 时不得破坏页面结构。"""
    components = [{
        "guid": "guid'\"<x>",
        "name": '<script>alert("x")</script>',
        "ifc_type": "IfcWall",
        "props": [("Name & 类型", "<b>1</b>")],
    }]
    html = _component_cards_html(components)
    assert "<script>" not in html  # 原始标签不得出现
    assert "&lt;script&gt;" in html
    assert "&lt;b&gt;" in html
    assert "&amp;" in html
