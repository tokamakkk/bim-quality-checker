"""彩色 GLB 导出测试 —— 验证按 Verdict 状态着色与两种导出模式。

使用 sample_data/bad_model.ifc（5 构件：墙-01 红 / 墙-02 黄 / 门-01 红 /
门-02 红 / 门-03 绿）与 good_model.ifc（全绿）。

GLB 读回后按材质 baseColorFactor 断言颜色（导出使用材质色而非顶点色，
见 mesh_exporter.py 模块 docstring 的说明）。
"""

import sys
from pathlib import Path

import numpy as np
import pytest
import trimesh

SRC_DIR = Path(__file__).resolve().parents[1] / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

SAMPLE_DIR = Path(__file__).resolve().parents[1] / "sample_data"

from core.engine import load_rules, run_checks  # noqa: E402
from core.verdict import VerdictStatus  # noqa: E402
from viz.mesh_exporter import (  # noqa: E402
    FAIL_COLOR,
    NEUTRAL_COLOR,
    PASS_COLOR,
    WARN_COLOR,
    export_colored_glb,
    get_color_for_verdict,
)


def _bad_verdicts():
    """对 bad_model 运行真实规则配置，得到 4 条问题判定。"""
    path = SAMPLE_DIR / "bad_model.ifc"
    if not path.exists() or path.stat().st_size == 0:
        pytest.skip("bad_model.ifc 尚未生成")
    return run_checks(path, load_rules(SRC_DIR.parent / "config" / "rules.json"))


def _material_colors(glb_path):
    """读回 GLB 场景，返回所有网格的材质 baseColorFactor 列表（RGB 0-1）。"""
    loaded = trimesh.load(str(glb_path))
    scene = loaded if isinstance(loaded, trimesh.Scene) else trimesh.Scene(loaded)
    colors = []
    for geom in scene.geometry.values():
        factor = np.asarray(geom.visual.material.baseColorFactor, dtype=float)
        if factor.max() > 1.0:  # trimesh 5.x 以 0-255 呈现，归一化回 0-1
            factor = factor / 255.0
        colors.append(tuple(round(c, 3) for c in factor[:3]))
    return colors


def test_color_mapping():
    """状态 → 颜色映射（DESIGN.md §5.2 色板）。"""
    assert get_color_for_verdict(VerdictStatus.PASS) == PASS_COLOR
    assert get_color_for_verdict(VerdictStatus.WARN) == WARN_COLOR
    assert get_color_for_verdict(VerdictStatus.FAIL) == FAIL_COLOR
    assert get_color_for_verdict(None) == NEUTRAL_COLOR  # 未评估 → 灰


def test_export_all_mode(tmp_path):
    """all 模式：bad 模型导出全部 5 构件，材质色 = {红, 黄, 绿}。"""
    out = export_colored_glb(SAMPLE_DIR / "bad_model.ifc", _bad_verdicts(), tmp_path / "all.glb")
    assert Path(out).exists() and Path(out).stat().st_size > 0

    colors = _material_colors(out)
    assert len(colors) == 3  # 红（墙-01/门-01/门-02）+ 黄（墙-02）+ 绿（门-03）
    for c in colors:
        assert any(np.allclose(c, ref, atol=0.02) for ref in (FAIL_COLOR, WARN_COLOR, PASS_COLOR))


def test_export_violations_only(tmp_path):
    """violations_only 模式：只含 WARN/FAIL 构件（过滤 pass 与未评估）。"""
    out = export_colored_glb(
        SAMPLE_DIR / "bad_model.ifc", _bad_verdicts(), tmp_path / "v.glb", mode="violations_only"
    )
    colors = _material_colors(out)
    assert len(colors) == 2  # 红（3 构件）+ 黄（墙-02），门-03（绿）被过滤
    for c in colors:
        assert any(np.allclose(c, ref, atol=0.02) for ref in (FAIL_COLOR, WARN_COLOR))


def test_export_all_green_good_model(tmp_path):
    """good 模型 all 模式：全部构件为绿色。"""
    path = SAMPLE_DIR / "good_model.ifc"
    if not path.exists() or path.stat().st_size == 0:
        pytest.skip("good_model.ifc 尚未生成")
    verdicts = run_checks(path, load_rules(SRC_DIR.parent / "config" / "rules.json"))
    out = export_colored_glb(path, verdicts, tmp_path / "good.glb")
    colors = _material_colors(out)
    assert len(colors) == 1  # 全部 pass → 单色
    assert np.allclose(colors[0], PASS_COLOR, atol=0.02)


def test_export_invalid_mode(tmp_path):
    """未知导出模式 → 友好 ValueError。"""
    with pytest.raises(ValueError, match="未知导出模式"):
        export_colored_glb(SAMPLE_DIR / "bad_model.ifc", _bad_verdicts(), tmp_path / "x.glb", mode="nope")


def test_export_missing_model(tmp_path):
    """模型不存在 → FileNotFoundError。"""
    with pytest.raises(FileNotFoundError, match="IFC 模型文件不存在"):
        export_colored_glb(SAMPLE_DIR / "no_such.ifc", [], tmp_path / "x.glb")
