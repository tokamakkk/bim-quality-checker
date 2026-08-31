"""规则引擎 —— 配置驱动的检查编排（DESIGN.md §4.3 数据流）。

设计遵循 DESIGN.md 原则 #2「规则是数据，不是代码」：engine.py 只做
编排（加载模型 → 按规则的 entity/check 配置遍历元素 → 分发到条件
检查 → 汇总 Verdict），判定语义全部在 rules_impl.py；阈值、严重级、
缺失行为等调参只改 config/rules.json，不触碰 Python 代码。

数据流（DESIGN.md §4.3）：
    upload → ifcopenshell.open → 遍历 rules → 按实体类型取元素 →
    逐 check 判定 → 产出 Verdict 列表
    → 消费者：UI 卡片 / 着色 GLB / HTML 报告 / 聊天上下文
"""

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

import ifcopenshell

from core.rules_impl import evaluate_check
from core.verdict import Verdict

# 项目根目录（src/core/engine.py 向上两级）
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_RULES_PATH = PROJECT_ROOT / "config" / "rules.json"


def load_rules(rules_path: Union[str, Path]) -> Dict[str, Any]:
    """加载规则配置文件（config/rules.json），返回配置 dict。

    - 文件不存在 → FileNotFoundError（带路径提示）
    - JSON 损坏 → ValueError（带路径与解析错误提示）
    - entity 字段结构无效 → ValueError（中文提示，避免 run_checks 逐字符
      迭代字符串后抛 "Entity with name 'S' not found" 的难懂错误）
    """
    path = Path(rules_path)
    if not path.exists():
        raise FileNotFoundError(f"规则配置文件不存在: {path}")
    try:
        with open(path, encoding="utf-8") as f:
            config = json.load(f)
    except json.JSONDecodeError as e:
        raise ValueError(f"规则配置文件不是合法 JSON: {path}（{e}）") from e
    _validate_entities(config)
    return config


def _validate_entities(rules_config: Dict[str, Any]) -> None:
    """轻量结构校验：每条规则的 entity 必须是 IFC 类型名数组。

    若写成字符串（"IfcWall" 或任意文本），run_checks 会对字符串逐字符迭代，
    抛 "Entity with name 'X' not found in schema"。这里提前拦截并指出
    是哪条规则、当前值是什么，用户一眼能改。
    """
    for rule in rules_config.get("validation_rules", []):
        for et in rule.get("entity", []):
            if not isinstance(et, str) or not et.startswith("Ifc"):
                name = rule.get("name", "?")
                raise ValueError(
                    f"规则「{name}」的 entity 字段格式无效：应为 IFC 类型名数组"
                    f"（如 [\"IfcWall\", \"IfcDoor\"]），当前为 {et!r}"
                )


def _open_model(model_path: Union[str, Path]):
    """打开 IFC 文件；失败时抛出带路径的友好异常。"""
    path = Path(model_path)
    if not path.exists():
        raise FileNotFoundError(f"IFC 模型文件不存在: {path}")
    try:
        return ifcopenshell.open(str(path))
    except Exception as e:
        raise ValueError(f"IFC 文件解析失败（文件损坏或格式不受支持）: {path}（{e}）") from e


def run_checks(
    model_path: Union[str, Path],
    rules_config: Optional[Union[Dict, str, Path]] = None,
) -> List[Verdict]:
    """对 IFC 模型执行规则配置中的所有检查，返回 Verdict 列表。

    :param model_path:   IFC 文件路径（.ifc，IFC 2×3 或 IFC4）
    :param rules_config: 规则配置。可传配置 dict、JSON 文件路径；
                         不传则缺省加载 config/rules.json
    :return:             Verdict 列表；模型中没有规则目标类型元素时
                         返回空列表（不报错）
    """
    # 1. 解析规则配置（dict / 文件路径 / 缺省配置）
    if rules_config is None:
        rules_config = load_rules(DEFAULT_RULES_PATH)
    elif isinstance(rules_config, (str, Path)):
        rules_config = load_rules(rules_config)

    # 2. 加载模型
    ifc = _open_model(model_path)

    # 3. 遍历 规则 → 实体类型 → 元素 → check，逐条产出 Verdict
    verdicts: List[Verdict] = []
    for rule in rules_config.get("validation_rules", []):
        for entity_type in rule.get("entity", []):
            # 模型中不存在该类型元素时 by_type 返回空列表 → 自然跳过，不报错
            for element in ifc.by_type(entity_type):
                for check in rule.get("checks", []):
                    verdicts.append(evaluate_check(element, rule, check))
    return verdicts
