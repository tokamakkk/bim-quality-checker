"""生成测试用微型 IFC 模型（DESIGN.md §3.2）—— 小型公寓（2 墙 + 4 门）。

    good_model.ifc —— 健康基线模型：所有构件有名称、有 FireRating（标准 Pset）、
                      门宽全部 ≥ 900 mm → 引擎输出 0 fail / 0 warn
    bad_model.ifc  —— 演示主角模型：故意制造缺陷，验收数字对齐 DESIGN.md §8.1：
                      「R1 恰好 1 fail + 1 warn · R2 恰好 3 fails（800/700/800 mm，
                      含 1 个 FireExit 标记门）· 总计 5 findings（4 fail / 1 warn）」：

        | 构件   | 缺陷                                | 触发的判定              |
        |--------|-------------------------------------|-------------------------|
        | 墙-01  | Name 为空字符串（§3.2 缺陷①）        | R1a fail（红）          |
        | 墙-02  | 缺少 FireRating 属性（缺陷②的变体）   | R1b warn（黄，无法判定） |
        | 门-01  | 800 mm（§3.2 缺陷③）+ FireExit 标记   | R2 fail（红）           |
        | 门-02  | 700 mm（§3.2 缺陷④）                 | R2 fail（红）           |
        | 门-03  | 800 mm（§8.1 的第三个 R2 fail）      | R2 fail（红）           |
        | 门-04  | 1000 mm（合规）                      | R2 pass（绿，保留三色）  |

    注：「FireExit 标记但 800 mm」由门-01 承担（Pset_DoorCommon.FireExit = true，
    供 §2.2 的 EXIT 分组机制使用）。缺 FireRating 只放在墙-02 一处，
    保证 R1b 恰好 1 条 warn（§8.1）。

    运行：python generate_models.py   （输出到 sample_data/，覆盖占位文件）
"""

from pathlib import Path

import ifcopenshell
import ifcopenshell.api

ROOT = Path(__file__).resolve().parent
SAMPLE_DIR = ROOT / "sample_data"

# 门高（几何用，与宽度无关）
_DOOR_HEIGHT = 2.1
# 墙体尺寸：长 × 厚 × 高（米）
_WALL_LENGTH, _WALL_THICKNESS, _WALL_HEIGHT = 3.0, 0.2, 3.0


# ---------------------------------------------------------------------------
# 基础构件
# ---------------------------------------------------------------------------

def _create_shell():
    """创建项目骨架：IfcProject + IfcSite + IfcBuilding + IfcBuildingStorey。

    同时创建几何上下文（Body）与 SI 单位，保证后续可被 mesh 提取/查看。
    """
    file = ifcopenshell.api.run("project.create_file")
    project = ifcopenshell.api.run(
        "root.create_entity", file, ifc_class="IfcProject", name="微型公寓测试项目"
    )
    # SI 单位必须显式声明为米：assign_unit 无参默认毫米（MILLIMETERS），
    # 而本文件几何常量全部按米书写 —— 不声明会导致 3.0m 的墙被写成 3mm，
    # 3D 查看器中整个模型缩成毫米级、只剩几个色点（§5.1 的 gr.Model3D）。
    ifcopenshell.api.run(
        "unit.assign_unit",
        file,
        length={"is_metric": True, "raw": "METERS"},
    )
    context = ifcopenshell.api.run(
        "context.add_context", file, context_type="Model", context_identifier="Body"
    )
    site = ifcopenshell.api.run("root.create_entity", file, ifc_class="IfcSite", name="测试场地")
    building = ifcopenshell.api.run(
        "root.create_entity", file, ifc_class="IfcBuilding", name="测试建筑"
    )
    storey = ifcopenshell.api.run(
        "root.create_entity", file, ifc_class="IfcBuildingStorey", name="首层"
    )
    # 空间层级：项目 → 场地 → 建筑 → 楼层
    ifcopenshell.api.run("aggregate.assign_object", file, relating_object=project, products=[site])
    ifcopenshell.api.run("aggregate.assign_object", file, relating_object=building, products=[site])
    ifcopenshell.api.run("aggregate.assign_object", file, relating_object=building, products=[storey])
    return file, storey, context


def _add_box_geometry(file, product, context, size: tuple):
    """给构件附加简单盒体几何（拉伸矩形截面）。

    尺寸 size = (x 宽, y 深, z 高)，单位为米；构件放置后即可被
    ifcopenshell.geom 提取网格（供后续 trimesh → GLB 查看器使用）。
    """
    # 矩形截面轮廓（居中于原点，XDim/YDim 为全尺寸）
    profile = file.create_entity(
        "IfcRectangleProfileDef",
        "AREA",
        None,
        file.create_entity("IfcAxis2Placement2D", file.create_entity("IfcCartesianPoint", (0.0, 0.0))),
        size[0],
        size[1],
    )
    # 沿 Z 轴拉伸
    solid = file.create_entity(
        "IfcExtrudedAreaSolid",
        profile,
        file.create_entity("IfcAxis2Placement3D", file.create_entity("IfcCartesianPoint", (0.0, 0.0, 0.0))),
        file.create_entity("IfcDirection", (0.0, 0.0, 1.0)),
        size[2],
    )
    representation = file.create_entity(
        "IfcShapeRepresentation", context, "Body", "SweptSolid", [solid]
    )
    product.Representation = file.create_entity("IfcProductDefinitionShape", None, None, [representation])


def _place(file, product, position: tuple):
    """给构件设置局部放置坐标（简化：直接世界坐标，未关联父级放置）。

    模型为微型演示模型，构件沿 x 轴错开摆放，便于 3D 查看器中逐个查看。
    """
    product.ObjectPlacement = file.create_entity(
        "IfcLocalPlacement",
        None,  # PlacementRelTo：无父级放置，坐标即世界坐标
        file.create_entity("IfcAxis2Placement3D", file.create_entity("IfcCartesianPoint", position)),
    )


def _add_fire_rating(file, product, pset_name: str, properties: dict):
    """添加属性集并写入属性（如 {"FireRating": "2h"}）。"""
    pset = ifcopenshell.api.run("pset.add_pset", file, product=product, name=pset_name)
    ifcopenshell.api.run("pset.edit_pset", file, pset=pset, properties=properties)
    return pset


def _add_wall(file, storey, context, name, position) -> "entity":
    """创建一面墙：名字 + 盒体几何 + 挂入楼层。"""
    wall = ifcopenshell.api.run(
        "root.create_entity", file, ifc_class="IfcWall", name=name
    )
    _add_box_geometry(file, wall, context, (_WALL_LENGTH, _WALL_THICKNESS, _WALL_HEIGHT))
    _place(file, wall, position)
    ifcopenshell.api.run("spatial.assign_container", file, products=[wall], relating_structure=storey)
    return wall


def _add_door(file, storey, context, name, width_mm: int, position) -> "entity":
    """创建一扇门：名字 + OverallWidth（直接属性，米）+ 盒体几何（宽=门宽）+ 挂入楼层。"""
    door = ifcopenshell.api.run(
        "root.create_entity", file, ifc_class="IfcDoor", name=name
    )
    door.OverallWidth = width_mm / 1000.0  # 直接属性，单位：米
    _add_box_geometry(file, door, context, (width_mm / 1000.0, 0.1, _DOOR_HEIGHT))
    _place(file, door, position)
    ifcopenshell.api.run("spatial.assign_container", file, products=[door], relating_structure=storey)
    return door


# ---------------------------------------------------------------------------
# 两个模型
# ---------------------------------------------------------------------------

def create_good_model():
    """健康基线模型：无任何缺陷（DESIGN.md §3.2）。"""
    file, storey, context = _create_shell()

    # 两面板墙：都有名字 + FireRating（标准 Pset_WallCommon）
    wall1 = _add_wall(file, storey, context, "墙-01", (0.0, 0.0, 0.0))
    _add_fire_rating(file, wall1, "Pset_WallCommon", {"FireRating": "2h"})
    wall2 = _add_wall(file, storey, context, "墙-02", (4.0, 0.0, 0.0))
    _add_fire_rating(file, wall2, "Pset_WallCommon", {"FireRating": "1h"})

    # 三扇门：1000 / 900 / 1200 mm，均有 FireRating（标准 Pset_DoorCommon）
    for idx, (width_mm, x) in enumerate([(1000, 8.0), (900, 12.0), (1200, 16.0)], start=1):
        door = _add_door(file, storey, context, f"门-0{idx}", width_mm, (x, 0.0, 0.0))
        _add_fire_rating(file, door, "Pset_DoorCommon", {"FireRating": "2h"})

    return file


def create_bad_model():
    """演示主角模型：5 处故意缺陷（见模块 docstring 对照表，对齐 §8.1 验收）。"""
    file, storey, context = _create_shell()

    # 墙-01：Name 为空字符串（缺陷① → R1a fail），FireRating 正常
    wall1 = _add_wall(file, storey, context, "", (0.0, 0.0, 0.0))
    _add_fire_rating(file, wall1, "Pset_WallCommon", {"FireRating": "2h"})

    # 墙-02：Name 完整，但缺 FireRating（→ R1b warn，数据缺失无法判定）
    _add_wall(file, storey, context, "墙-02", (4.0, 0.0, 0.0))

    # 门-01：800 mm（缺陷③ → R2 fail），并标记 FireExit（§3.2 缺陷⑤的语义）
    door1 = _add_door(file, storey, context, "门-01", 800, (8.0, 0.0, 0.0))
    _add_fire_rating(file, door1, "Pset_DoorCommon", {"FireRating": "2h", "FireExit": True})

    # 门-02：700 mm（缺陷④ → R2 fail）
    door2 = _add_door(file, storey, context, "门-02", 700, (12.0, 0.0, 0.0))
    _add_fire_rating(file, door2, "Pset_DoorCommon", {"FireRating": "2h"})

    # 门-03：800 mm（§8.1 的第三个 R2 fail）
    door3 = _add_door(file, storey, context, "门-03", 800, (16.0, 0.0, 0.0))
    _add_fire_rating(file, door3, "Pset_DoorCommon", {"FireRating": "2h"})

    # 门-04：1000 mm（合规 → R2 pass，保留红黄绿三色演示）
    door4 = _add_door(file, storey, context, "门-04", 1000, (20.0, 0.0, 0.0))
    _add_fire_rating(file, door4, "Pset_DoorCommon", {"FireRating": "2h"})

    return file


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def _summarize(path: Path):
    """读取并打印模型元素统计（验证文件可被 IFCOpenShell 正确读取）。"""
    ifc = ifcopenshell.open(str(path))
    from collections import Counter
    counts = Counter(e.is_a() for e in ifc.by_type("IfcProduct") if e.is_a("IfcElement"))
    print(f"{path.name}: {sum(counts.values())} 个构件 -> {dict(counts)}")


def main():
    SAMPLE_DIR.mkdir(exist_ok=True)
    good_path = SAMPLE_DIR / "good_model.ifc"
    bad_path = SAMPLE_DIR / "bad_model.ifc"

    create_good_model().write(str(good_path))
    create_bad_model().write(str(bad_path))

    print("已生成:")
    _summarize(good_path)
    _summarize(bad_path)


if __name__ == "__main__":
    main()
