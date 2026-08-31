"""彩色 GLB 导出 —— 按 Verdict 状态给 IFC 构件着色（DESIGN.md §5.2）。

借用 BQC 的成熟模式（§4.4）：ifcopenshell.geom 逐构件提取三角网格 →
trimesh 按判定状态着色 → 合并导出单个 GLB 供 gr.Model3D 显示。

着色方案说明：
- 颜色全部取自 DESIGN.md §5.2 的统一色板，与 UI 卡片、HTML 报告同一套
  token（pass #22c55e / warn #eab308 / fail #ef4444 / 未评估 灰）。
- 采用「每颜色一组网格 + 材质 baseColorFactor」而非顶点色：顶点色在
  model-viewer（Gradio Model3D 的渲染内核）中默认不渲染，材质色则被
  所有 GLB 查看器可靠显示 —— 演示视频不能冒颜色丢失的风险。
- 同一构件有多条判定时，取最严重状态着色（FAIL > WARN > PASS）。

性能：测试模型约 10 构件，几何解析 1 秒内完成，无需并行；
大模型可后续在 _get_element_mesh 上套 multiprocessing 加速。
"""

from pathlib import Path
from typing import Dict, List, Optional, Tuple

import ifcopenshell
import ifcopenshell.geom  # 注意：需显式导入（ifcopenshell.__init__ 不自动加载）
import numpy as np
import trimesh
import trimesh.visual.material

from core.verdict import Verdict, VerdictStatus

# ---------------------------------------------------------------------------
# DESIGN.md §5.2 统一色板（hex → RGB 0-1），三面（UI/3D/报告）共用同一 token
# ---------------------------------------------------------------------------
PASS_COLOR: Tuple[float, float, float] = (0x22 / 255, 0xC5 / 255, 0x5E / 255)  # 绿 #22c55e
WARN_COLOR: Tuple[float, float, float] = (0xEA / 255, 0xB3 / 255, 0x08 / 255)  # 黄 #eab308
FAIL_COLOR: Tuple[float, float, float] = (0xEF / 255, 0x44 / 255, 0x44 / 255)  # 红 #ef4444
NEUTRAL_COLOR: Tuple[float, float, float] = (0.7, 0.7, 0.7)  # 灰（未评估元素）

# 状态优先级：同一元素多条判定时取最严重者着色
_STATUS_PRIORITY = {
    VerdictStatus.PASS: 0,
    VerdictStatus.WARN: 1,
    VerdictStatus.FAIL: 2,
}


def get_color_for_verdict(status: Optional[VerdictStatus]) -> Tuple[float, float, float]:
    """判定状态 → RGB 颜色（0-1 浮点，glTF baseColorFactor 格式）。

    无判定（None）→ 灰色，与 DESIGN.md §5.2 的色板一致。
    """
    mapping = {
        VerdictStatus.PASS: PASS_COLOR,
        VerdictStatus.WARN: WARN_COLOR,
        VerdictStatus.FAIL: FAIL_COLOR,
    }
    return mapping.get(status, NEUTRAL_COLOR)


def _element_status_map(verdicts: List[Verdict]) -> Dict[str, VerdictStatus]:
    """把 Verdict 列表折叠为 {element_guid: 最严重状态}（FAIL > WARN > PASS）。"""
    result: Dict[str, VerdictStatus] = {}
    for v in verdicts:
        current = result.get(v.element_guid)
        if current is None or _STATUS_PRIORITY[v.status] > _STATUS_PRIORITY[current]:
            result[v.element_guid] = v.status
    return result


def _get_element_mesh(element, settings) -> Optional[trimesh.Trimesh]:
    """提取单个构件的三角网格；几何解析失败返回 None（调用方跳过）。

    纯属性构件（无几何表示）、表达错误等场景都会走到这里 ——
    按「跳过该元素继续」的策略处理，不让单个坏元素毁掉整个导出。
    """
    try:
        shape = ifcopenshell.geom.create_shape(settings, element)
    except Exception as e:  # 几何解析失败：记录后跳过
        name = getattr(element, "Name", None) or "(未命名)"
        print(f"[mesh_exporter] 跳过几何解析失败的构件 {element.is_a()} {name}: {e}")
        return None
    geometry = shape.geometry
    verts = np.array(geometry.verts, dtype=np.float32)
    faces = np.array(geometry.faces, dtype=np.int64)
    # 解析失败或仅有零维几何（点/线）时不产出网格
    if len(verts) < 3 or len(faces) < 3 or len(faces) % 3 != 0:
        return None
    try:
        return trimesh.Trimesh(
            vertices=verts.reshape(-1, 3),
            faces=faces.reshape(-1, 3),
            process=False,  # 保持原网格，避免合并时坐标被重算
        )
    except Exception as e:
        print(f"[mesh_exporter] 网格组装失败 {element.is_a()}: {e}")
        return None


def _make_colored_mesh(meshes: List[trimesh.Trimesh], color: Tuple[float, float, float]) -> trimesh.Trimesh:
    """把同色的一组网格合并，并赋予 PBR 材质色（baseColorFactor）。

    使用材质色而非顶点色：model-viewer（Gradio Model3D 渲染内核）默认
    不渲染顶点色，材质色在一切 GLB 查看器中可靠显示。
    注意 trimesh 5.x 的 baseColorFactor 约定为 0-255，内部转换回 glTF
    标准的 0-1 写入 GLB。
    """
    merged = trimesh.util.concatenate(meshes)
    merged.visual = trimesh.visual.TextureVisuals(
        material=trimesh.visual.material.PBRMaterial(
            baseColorFactor=[round(c * 255) for c in color] + [255]
        )
    )
    return merged


def export_colored_glb(
    model_path: str,
    verdicts: List[Verdict],
    output_path: str,
    mode: str = "all",
) -> str:
    """按 Verdict 状态给 IFC 模型着色并导出单个 GLB 文件。

    :param model_path:  IFC 文件路径
    :param verdicts:    Verdict 列表（同一元素多条判定时取最严重状态）
    :param output_path: 输出 .glb 文件路径（目录不存在时自动创建）
    :param mode:        "all" 导出全部构件；"violations_only" 只导出
                        WARN/FAIL 构件（过滤掉 pass 与未评估，即
                        DESIGN.md §5.3 的「只显示违规构件」手势）
    :return:            输出文件路径
    :raises FileNotFoundError: 模型文件不存在
    :raises ValueError: 模型解析失败 / 导出模式未知 / 无可导出网格
    """
    if mode not in ("all", "violations_only"):
        raise ValueError(f"未知导出模式: {mode!r}（支持 'all' / 'violations_only'）")
    model_path = Path(model_path)
    if not model_path.exists():
        raise FileNotFoundError(f"IFC 模型文件不存在: {model_path}")
    try:
        ifc = ifcopenshell.open(str(model_path))
    except Exception as e:
        raise ValueError(f"IFC 文件解析失败: {model_path}（{e}）") from e

    # 折叠 verdicts 为 guid → 最严重状态
    status_map = _element_status_map(verdicts)

    # 世界坐标：所有构件按真实位置对齐（否则每个构件都从原点开始、互相重叠）
    settings = ifcopenshell.geom.settings()
    settings.set(settings.USE_WORLD_COORDS, True)

    # 按颜色分组收集网格：{color: [mesh, ...]}
    groups: Dict[Tuple[float, float, float], List[trimesh.Trimesh]] = {}
    skipped = 0
    for element in ifc.by_type("IfcElement"):
        status = status_map.get(element.GlobalId)
        if mode == "violations_only" and status not in (VerdictStatus.WARN, VerdictStatus.FAIL):
            continue  # 过滤掉 pass 与未评估构件
        mesh = _get_element_mesh(element, settings)
        if mesh is None:
            skipped += 1
            continue
        groups.setdefault(get_color_for_verdict(status), []).append(mesh)

    if not groups:
        detail = ""
        if mode == "violations_only":
            detail = "（violations_only 模式下模型可能没有 warn/fail 构件）"
        raise ValueError(f"没有任何可导出的网格: {model_path} {detail}")

    # 每颜色一组 → 一个带材质色的网格 → 组装为场景导出 GLB
    scene = trimesh.Scene()
    for color, meshes in groups.items():
        scene.add_geometry(_make_colored_mesh(meshes, color))

    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    scene.export(str(output), file_type="glb")

    if skipped:
        print(f"[mesh_exporter] 跳过 {skipped} 个无法解析几何的构件")
    return str(output)
