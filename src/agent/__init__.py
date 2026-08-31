"""AI 质量助手 —— 与 LLM Agent 的集成点（DESIGN.md §6）。

两个能力（一个聊天窗口）：
- Capability A（§6.1）只读问答：基于当前 verdicts 的确定性回答
  （"哪些疏散门宽度不满足要求？"），保证离线演示永远可用。
- Capability B（§6.2）引导式修复：直接指令立即执行（"把所有小于900mm的
  门改成1000mm"）；咨询"怎么修？"走确认闭环 —— 建议 → 询问 → 用户确认 →
  工具执行（function calling）→ 自动重检汇报。修复写入**工作副本**
  （.work/ 下的新文件），绝不动用户上传的原文件（§6.2：原文件可重新上传
  = 撤销机制）。

LLM 路径：设置了 DEEPSEEK_API_KEY 时所有问答优先调用 DeepSeek（OpenAI 兼容，
模型 deepseek-v4-flash-vision-exp，§6.3），每轮自动注入模型信息 / 规则摘要 /
检查结果上下文（§6.1）；确认修复时走 function calling（prompts/tool_schemas.json），
失败（无 tool_call / 执行异常 / 未配置 key）静默回退到确定性方案执行 ——
演示视频不依赖任何外部 API（§4.1 风险表）。

provider.py / tools.py / context.py 由后续任务补齐；本模块是 app.py 的
薄集成层，保持其接口稳定：agent.chat(...) → dict。
"""

import os
import re
from collections import Counter
from pathlib import Path

from core.engine import run_checks
from core.verdict import Verdict

# DESIGN §6.2 set_door_width 护栏：只允许 [600, 3000] mm
MIN_DOOR_WIDTH, MAX_DOOR_WIDTH = 600, 3000

# 修复意图示例："把所有小于900mm的门改成1000mm" / "把小于 900mm 的门都改成 1000"
_REPAIR_RE = re.compile(
    r"小于\s*(\d+(?:\.\d+)?)\s*mm?\s*的?门\s*(?:都|全部)?\s*(?:改|换)成\s*(\d+(?:\.\d+)?)\s*mm?"
)
# §6.2 set_property 工具（allowlist：Name / FireRating）的确定性意图：
# 名称补全："把空名称的构件都补上名字"；防火等级补全："给缺少防火等级的构件补上防火等级 2h"
_NAME_RE = re.compile(r"(?:空|缺失|没有)(?:名称|名字)")
_FIRE_RE = re.compile(r"(?:缺少|缺失|没有|缺)\s*防火等级|防火等级.{0,10}(?:补|填|加)")
# 修复动作词（与「哪些构件没有名字？」这类提问区分）
_REPAIR_VERB = re.compile(r"补|填|改|加|设置|命名")
# 重检意图："重新运行检查" / "重新检查"
_RERUN_RE = re.compile(r"重新(?:运行)?检查|重检|re-?run")
# Ifc 类型 → 中文名（补名字用，与样例模型 墙-01/门-01 命名风格一致）
_CN_TYPE = {
    "IfcWall": "墙", "IfcDoor": "门", "IfcWindow": "窗", "IfcSlab": "板",
    "IfcColumn": "柱", "IfcBeam": "梁", "IfcRoof": "屋面", "IfcStair": "楼梯",
}

_DEEPSEEK_URL = "https://api.deepseek.com/v1"
_DEFAULT_MODEL = "deepseek-v4-flash-vision-exp"

# ---------------------------------------------------------------------------
# 提示词与工具 schema：prompts/ 为源（DESIGN §6.4），文件缺失回退内联默认
# ---------------------------------------------------------------------------

_PROMPTS_DIR = Path(__file__).resolve().parents[2] / "prompts"

_SYSTEM_PROMPT_FALLBACK = (
    "你是 BIM 质量检查助手（HKU AI Agent Technical Test）。"
    "你的全部回答必须以消息下方的【IFC 模型信息】【生效的检查规则】【检查结果】"
    "为依据：先查阅上下文再回答，只陈述事实，不编造数据；"
    "列举违规项时不要遗漏任何一条；上下文没有的信息要明确说明"
    "「检查结果中未包含」，不得猜测。"
)

_TOOL_SCHEMAS_FALLBACK = [
    {"type": "function", "function": {
        "name": "set_door_width",
        "description": "修改门总宽度（IfcDoor.OverallWidth）。guid 缺省=批量修改所有宽度不足的门。",
        "parameters": {"type": "object", "properties": {
            "guid": {"type": "string", "description": "目标门 GlobalId（可省略）"},
            "threshold_mm": {"type": "number"},
            "new_mm": {"type": "number"}},
            "required": ["threshold_mm", "new_mm"]}}},
    {"type": "function", "function": {
        "name": "set_property",
        "description": "修改构件属性（仅允许 Name / FireRating）。",
        "parameters": {"type": "object", "properties": {
            "guid": {"type": "string"},
            "property_name": {"type": "string", "enum": ["Name", "FireRating"]},
            "value": {"type": "string"}},
            "required": ["guid", "property_name", "value"]}}},
    {"type": "function", "function": {
        "name": "rerun_check",
        "description": "重新运行检查并刷新结果。",
        "parameters": {"type": "object", "properties": {}}}},
]


def _load_prompt_file(name: str, fallback: str) -> str:
    """从 prompts/ 读取文本文件；缺失时回退内联默认值（不破坏离线测试）。"""
    try:
        return (_PROMPTS_DIR / name).read_text(encoding="utf-8").strip()
    except OSError:
        return fallback


def _load_tool_schemas() -> list:
    """从 prompts/tool_schemas.json 读取工具定义；缺失/非法时回退内置最小集。"""
    import json
    try:
        schemas = json.loads((_PROMPTS_DIR / "tool_schemas.json").read_text(encoding="utf-8"))
        return schemas if isinstance(schemas, list) and schemas else _TOOL_SCHEMAS_FALLBACK
    except (OSError, ValueError):
        return _TOOL_SCHEMAS_FALLBACK


_SYSTEM_PROMPT = _load_prompt_file("system_prompt.txt", _SYSTEM_PROMPT_FALLBACK)
_TOOL_SCHEMAS = _load_tool_schemas()


# ---------------------------------------------------------------------------
# Capability B —— 修复工具（DESIGN §6.2）
# ---------------------------------------------------------------------------

def set_door_width(model_path: str, threshold_mm: float, new_mm: float,
                   output_path: str, guid: str = None):
    """把模型中小于 threshold_mm 的 IfcDoor.OverallWidth 改为 new_mm（§6.2）。

    护栏：new_mm 钳制到 [MIN_DOOR_WIDTH, MAX_DOOR_WIDTH]（600–3000 mm）；
    只改 OverallWidth 这一个属性，不创建/删除任何实体。
    guid 给定时只修改该扇门（宽度不足才改，否则跳过）。
    写入 output_path（工作副本），原文件不被修改。

    :return: (output_path, 修改的门数量)
    """
    import ifcopenshell
    model = ifcopenshell.open(str(model_path))
    new_mm = min(max(new_mm, MIN_DOOR_WIDTH), MAX_DOOR_WIDTH)
    changed = 0
    doors = [_by_guid_or_raise(model, guid)] if guid else model.by_type("IfcDoor")
    for door in doors:
        if not door.is_a("IfcDoor"):
            continue
        width = getattr(door, "OverallWidth", None)
        if width is None:
            continue
        if width * 1000.0 < threshold_mm:
            door.OverallWidth = new_mm / 1000.0
            changed += 1
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    model.write(str(out))
    return str(out), changed


def fill_missing_names(model_path: str, output_path: str,
                       guid: str = None, value: str = None):
    """给名称为空的构件补上名字（§6.2 set_property 的 allowlist：Name）。

    命名风格与样例模型一致（墙-01 / 门-01），写入工作副本，原文件不动。
    guid 给定时只处理该构件；value 缺省时按类型自动生成名字。

    :return: (output_path, 修改的构件数量)
    """
    import ifcopenshell
    model = ifcopenshell.open(str(model_path))
    changed = 0
    if guid:
        elem = _by_guid_or_raise(model, guid)
        if not getattr(elem, "Name", None):
            elem.Name = value or f"{_CN_TYPE.get(elem.is_a(), elem.is_a())}-{guid[:4]}"
            changed += 1
    else:
        for i, elem in enumerate(model.by_type("IfcElement"), start=1):
            if not getattr(elem, "Name", None):
                elem.Name = value or f"{_CN_TYPE.get(elem.is_a(), elem.is_a())}-{i:02d}"
                changed += 1
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    model.write(str(out))
    return str(out), changed


def _ensure_fire_rating(model, elem, pset_name: dict, value: str) -> bool:
    """给单个构件补 FireRating；已有（任意属性集）→ 跳过。返回是否实际修改。"""
    import ifcopenshell.api
    rel = next(
        (r for r in (elem.IsDefinedBy or []) if r.is_a("IfcRelDefinesByProperties")),
        None,
    )
    if rel and rel.RelatingPropertyDefinition.is_a("IfcPropertySet"):
        pset = rel.RelatingPropertyDefinition
        if pset.HasProperties and any(
            p.is_a("IfcPropertySingleValue") and p.Name == "FireRating"
            for p in pset.HasProperties
        ):
            return False
    pset = (
        rel.RelatingPropertyDefinition if rel and rel.RelatingPropertyDefinition.is_a("IfcPropertySet")
        else ifcopenshell.api.run(
            "pset.add_pset", model, product=elem,
            name=pset_name.get(elem.is_a(), "Pset_ElementCommon"),
        )
    )
    ifcopenshell.api.run("pset.edit_pset", model, pset=pset, properties={"FireRating": value})
    return True


def fill_missing_fire_ratings(model_path: str, output_path: str, value: str = "2h",
                              guid: str = None):
    """给缺少 FireRating 的构件补上防火等级（§6.2 allowlist：FireRating）。

    有既有属性集的写入该属性集；没有的按类型建标准 pset（Pset_WallCommon /
    Pset_DoorCommon / Pset_ElementCommon）。值默认 "2h"，写入工作副本。
    guid 给定时只处理该构件。

    :return: (output_path, 修改的构件数量)
    """
    import ifcopenshell
    model = ifcopenshell.open(str(model_path))
    pset_name = {
        "IfcWall": "Pset_WallCommon",
        "IfcDoor": "Pset_DoorCommon",
    }
    changed = 0
    elems = [_by_guid_or_raise(model, guid)] if guid else model.by_type("IfcElement")
    for elem in elems:
        if _ensure_fire_rating(model, elem, pset_name, value):
            changed += 1
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    model.write(str(out))
    return str(out), changed


_ALLOWED_PROPERTIES = ("Name", "FireRating")
_TOOL_OUTPUT_NAME = "model_fixed.ifc"


def _by_guid_or_raise(model, guid: str):
    """按 GUID 取构件；不存在时抛中文 ValueError（ifcopenshell 原生抛 RuntimeError）。

    by_guid 对不存在的 GUID 抛 RuntimeError 而非返回 None，统一转成
    _run_tool 护栏契约的 ValueError，LLM 汇报时也能拿到可读消息。
    """
    try:
        return model.by_guid(guid)
    except RuntimeError:
        raise ValueError(f"未找到 GUID 对应的构件：{guid}") from None


def set_property_by_guid(model_path: str, guid: str, property_name: str,
                         value: str, output_path: str):
    """按 GUID 设置单个构件属性（§6.2 set_property；allowlist：Name / FireRating）。

    护栏：property_name 不在 allowlist → ValueError；GUID 不存在 → ValueError；
    只改目标属性，不创建/删除实体。写入工作副本。

    :return: (output_path, 修改的构件数量)
    """
    if property_name not in _ALLOWED_PROPERTIES:
        raise ValueError(f"不允许修改属性 {property_name}，仅支持 Name / FireRating。")
    import ifcopenshell
    model = ifcopenshell.open(str(model_path))
    elem = _by_guid_or_raise(model, guid)
    if property_name == "Name":
        elem.Name = str(value)
    else:
        _ensure_fire_rating(
            model, elem,
            {"IfcWall": "Pset_WallCommon", "IfcDoor": "Pset_DoorCommon"},
            str(value))
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    model.write(str(out))
    return str(out), 1


def _run_tool(name: str, args: dict, model_path: str, work_dir) -> tuple:
    """统一工具执行器：校验工具名/参数，在工作副本上执行，返回结果摘要。

    :return: (output_path, changed, desc)  desc 为中文描述（供 LLM 汇报）
    :raises ValueError: 未知工具 / 非法参数 / GUID 不存在
    """
    out = str(Path(work_dir) / _TOOL_OUTPUT_NAME)
    if name == "set_door_width":
        threshold_mm = float(args["threshold_mm"])
        new_mm = float(args["new_mm"])
        fixed, changed = set_door_width(model_path, threshold_mm, new_mm, out,
                                        guid=args.get("guid"))
        desc = (f"门宽修复：{changed} 扇门宽度改为 {new_mm:g}mm"
                f"（阈值 {threshold_mm:g}mm）")
    elif name == "set_property":
        guid = str(args["guid"])
        prop = str(args["property_name"])
        value = str(args["value"])
        fixed, changed = set_property_by_guid(model_path, guid, prop, value, out)
        desc = f"构件 {guid[:12]} 的 {prop} 已设为「{value}」"
    elif name == "rerun_check":
        return model_path, 0, "重新检查完成"
    else:
        raise ValueError(f"未知工具：{name}")
    return fixed, changed, desc


# ---------------------------------------------------------------------------
# Capability A —— 确定性问答（离线兜底）
# ---------------------------------------------------------------------------

def _ask_doors_too_narrow(verdicts):
    """"哪些疏散门宽度不满足要求？" → 列出门名 + 宽度。"""
    fails = [v for v in verdicts if v.check_id == "R2" and v.is_fail]
    if not fails:
        return "当前模型所有门宽度均满足 ≥ 900mm 要求。"
    names = "、".join(f"{v.element_name}（{v.current_value}）" for v in sorted(
        fails, key=lambda v: v.element_name))
    return f"当前模型有 {len(fails)} 扇门宽度不足：{names}，均小于 900mm（GB50016）。"


def _ask_missing_names(verdicts):
    """"哪些构件没有名字？" → 列出类型 + 当前值。"""
    fails = [v for v in verdicts if v.check_id == "R1a" and v.is_fail]
    if not fails:
        return "当前模型所有构件名称均完整。"
    names = "、".join(f"{v.ifc_type}" for v in sorted(fails, key=lambda v: v.element_name))
    return f"当前模型有 {len(fails)} 个构件名称为空：{names}。"


def _ask_missing_fire_ratings(verdicts):
    """"哪些构件缺少防火等级？" → 列出元素名 + 状态。"""
    warns = [v for v in verdicts if v.check_id == "R1b" and v.is_warn]
    if not warns:
        return "当前模型所有构件均已标注 FireRating。"
    names = "、".join(f"{v.element_name or v.ifc_type}" for v in sorted(
        warns, key=lambda v: v.element_name))
    return f"当前模型有 {len(warns)} 个构件缺少 FireRating：{names}（数据缺失，无法判定）。"


def _check_summary_line(verdicts):
    """检查汇总行（供上下文块与 _summary_line 共用）。"""
    n_pass = sum(1 for v in verdicts if v.is_pass)
    n_warn = sum(1 for v in verdicts if v.is_warn)
    n_fail = sum(1 for v in verdicts if v.is_fail)
    return f"✅ {n_pass} 通过 · ⚠️ {n_warn} 警告 · ❌ {n_fail} 违规（共 {len(verdicts)} 条判定）"


def _summary_line(verdicts):
    """"重新检查" 后的结果摘要行。"""
    return "重新检查完成：" + _check_summary_line(verdicts)


def _repair_reply(action: str, changed: int, verdicts: list) -> str:
    """修复后的统一回复：动作 + 数量 + 新判定摘要 + 剩余问题提示。"""
    fails = [v for v in verdicts if v.is_fail]
    warns = [v for v in verdicts if v.is_warn]
    if fails:
        hint = f"仍剩 {len(fails)} 条违规，可继续修复。"
    elif warns:
        hint = f"无违规，仍剩 {len(warns)} 条警告（数据缺失）。"
    else:
        hint = "全部通过。"
    return f"已{action} {changed} 个构件（写入工作副本，原文件未动）。{_summary_line(verdicts)}{hint}"


# ---------------------------------------------------------------------------
# 修复确认闭环（DESIGN §6.2：建议 → 询问 → 确认 → 工具执行 → 重检汇报）
# ---------------------------------------------------------------------------

_ASK_SUFFIX = "\n\n需要我帮您修复吗？"
# 拒绝识别："不用/不修复/先不管/算了/不用了/暂时不"（§6.2 交互协议）
_REJECT_RE = re.compile(r"不用|不需要|不修复|先不管|算了|不用了|暂时不|不要|别")
# 咨询识别："怎么修？" / "有什么问题？" → 进入建议闭环
_ASK_RE = re.compile(
    r"怎么修|如何修|怎么解决|如何解决|怎么办|怎么处理|有什么问题|什么问题|能修|修一下|帮我看看")
# 确认识别：≤12 字短句 ∈ 肯定词表，或含"修复吧/请修复…"动作短语，且不含否定词
_CONFIRM_PHRASES = ("好的", "好呀", "好", "行吧", "行", "可以", "是的", "是", "要",
                    "确定", "嗯", "对", "同意", "ok", "OK")
_CONFIRM_ACTION_RE = re.compile(r"修复吧|修吧|请修复|帮我修|开始修|执行修复")
# 汇报轮工具调用文本误输出识别（实验模型偶发）：XML <tool_calls> / JSON / 代码块
_TOOL_CALL_TEXT_RE = re.compile(r"<tool_calls|tool_calls|invoke name=|```tool|\"function\"")

_DOOR_ACTION = "fix_doors"
_NAME_ACTION = "fix_names"
_FIRE_ACTION = "fix_fire"


def _is_decline(message: str) -> bool:
    return bool(_REJECT_RE.search(message))


def _is_confirmation(message: str) -> bool:
    """确认词判定：先排除否定，再匹配动作短语或短句肯定词。"""
    if _is_decline(message):
        return False
    if _CONFIRM_ACTION_RE.search(message):
        return True
    return len(message) <= 12 and message in _CONFIRM_PHRASES


def _door_threshold_mm(rules: dict) -> float:
    """从规则配置提取门宽阈值（米 → 毫米）；缺省 900。"""
    for rule in (rules or {}).get("validation_rules", []):
        for check in rule.get("checks", []):
            cond = check.get("condition", {}) or {}
            if cond.get("type") == "range" and cond.get("min"):
                return float(cond["min"]) * 1000.0
    return 900.0


def _build_repair_plan(message: str, verdicts: list, rules: dict) -> dict:
    """从判定结果确定性生成修复方案（§6.2 锚点：文案与执行必须一致）。

    action：消息关键词定向（门→fix_doors；名称→fix_names；防火→fix_fire），
    无关键词时按严重度取有问题的类别（doors > names > fire）；
    kind：消息提到具体元素名或 GUID 前缀 → single，否则 batch。
    :return: 方案 dict；无可修项返回 None
    """
    door_fails = [v for v in verdicts if v.check_id == "R2" and v.is_fail]
    name_fails = [v for v in verdicts if v.check_id == "R1a" and v.is_fail]
    fire_warns = [v for v in verdicts if v.check_id == "R1b" and v.is_warn]

    if any(k in message for k in ("门", "宽", "窄")):
        action = _DOOR_ACTION
    elif any(k in message for k in ("名称", "名字")):
        action = _NAME_ACTION
    elif "防火" in message:
        action = _FIRE_ACTION
    elif door_fails:
        action = _DOOR_ACTION
    elif name_fails:
        action = _NAME_ACTION
    elif fire_warns:
        action = _FIRE_ACTION
    else:
        return None

    pool = {_DOOR_ACTION: door_fails, _NAME_ACTION: name_fails,
            _FIRE_ACTION: fire_warns}[action]
    if not pool:
        return None

    # 单个：消息中提到某个具体构件名 / GUID 前缀
    target = next(
        (v for v in pool
         if (v.element_name and v.element_name in message)
         or (v.element_guid and v.element_guid[:12] in message)),
        None,
    )
    kind = "single" if target else "batch"
    items = [
        {"name": v.element_name or v.ifc_type, "guid": v.element_guid,
         "current": v.current_value}
        for v in sorted(pool, key=lambda v: v.element_name)
    ]
    return {
        "action": action, "kind": kind,
        "guid": target.element_guid if target else None,
        "name": target.element_name if target else None,
        "items": items,
        "threshold": _door_threshold_mm(rules),
        "target": 1000, "value": "2h", "target_name": None,
    }


def _plan_text(plan: dict) -> str:
    """【拟定修复方案】中文块：动作 + 对象列表 + 参数（注入 LLM 消息）。"""
    items = "、".join(f"{it['name']}（{it['current']}）" for it in plan["items"])
    if plan["action"] == _DOOR_ACTION:
        obj = f"构件 {plan['name']}" if plan["kind"] == "single" \
            else f"{len(plan['items'])} 扇门：{items}"
        return (f"【拟定修复方案】\n"
                f"- 动作：set_door_width（修改门总宽度）\n"
                f"- 对象：{obj}\n"
                f"- 参数：threshold_mm={plan['threshold']:g}，new_mm={plan['target']:g}"
                f"（宽度钳制 600–3000 mm）")
    if plan["action"] == _NAME_ACTION:
        obj = f"构件 {plan['name']}" if plan["kind"] == "single" \
            else f"{len(plan['items'])} 个名称为空的构件"
        return (f"【拟定修复方案】\n"
                f"- 动作：set_property（Name）\n"
                f"- 对象：{obj}\n"
                f"- 参数：补全 Name（命名风格 墙-01 / 门-01）")
    obj = f"构件 {plan['name']}" if plan["kind"] == "single" \
        else f"{len(plan['items'])} 个缺少 FireRating 的构件"
    return (f"【拟定修复方案】\n"
            f"- 动作：set_property（FireRating）\n"
            f"- 对象：{obj}\n"
            f"- 参数：FireRating=\"{plan['value']}\"")


def _deterministic_advice(plan: dict) -> str:
    """无 key 时的建议文案（镜像 LLM 建议的格式，末尾带询问句）。"""
    if plan["action"] == _DOOR_ACTION:
        if plan["kind"] == "single":
            it = plan["items"][0]
            return (f"{plan['name']} 当前宽度 {it['current']}，小于规范要求的 "
                    f"{plan['threshold']:g}mm。建议将其宽度调整为 "
                    f"{plan['target']:g}mm（留有充足余量）。{_ASK_SUFFIX}")
        items = "、".join(f"{it['name']}（{it['current']}）" for it in plan["items"])
        return (f"发现 {len(plan['items'])} 扇门宽度不足：{items}，均小于 "
                f"{plan['threshold']:g}mm（GB50016）。建议统一改为 "
                f"{plan['target']:g}mm。{_ASK_SUFFIX}")
    if plan["action"] == _NAME_ACTION:
        items = "、".join(it["name"] for it in plan["items"])
        if plan["kind"] == "single":
            return (f"{plan['name']} 的 Name 属性为空，无法识别该构件。"
                    f"建议将其命名为「墙-01」风格的名字。{_ASK_SUFFIX}")
        return (f"发现 {len(plan['items'])} 个构件名称为空：{items}。"
                f"建议按类型补全名字（如 墙-01 / 门-01）。{_ASK_SUFFIX}")
    items = "、".join(it["name"] for it in plan["items"])
    if plan["kind"] == "single":
        return (f"{plan['name']} 缺少 FireRating 防火等级。"
                f"建议补上「{plan['value']}」。{_ASK_SUFFIX}")
    return (f"发现 {len(plan['items'])} 个构件缺少 FireRating：{items}。"
            f"建议统一补上「{plan['value']}」。{_ASK_SUFFIX}")


def _deterministic_execute(plan: dict, model_path: str, rules: dict,
                           work_dir) -> dict:
    """无 key / LLM 失效时按方案确定性执行修复 + 重检（§4.1 离线兜底）。"""
    out = str(Path(work_dir) / _TOOL_OUTPUT_NAME)
    try:
        if plan["action"] == _DOOR_ACTION:
            fixed, changed = set_door_width(
                model_path, plan["threshold"], plan["target"], out,
                guid=plan.get("guid"))
            new_verdicts = run_checks(fixed, rules)
            if plan["kind"] == "single":
                reply = (f"已修复 {plan['name']}（门宽改为 {plan['target']:g}mm，"
                         f"工作副本已保存，原文件未动）。{_summary_line(new_verdicts)}")
            else:
                reply = (f"已修复 {changed} 扇门（宽度 < {plan['threshold']:g}mm 的改为 "
                         f"{plan['target']:g}mm，工作副本已保存，原文件未动）。"
                         f"{_summary_line(new_verdicts)}")
        elif plan["action"] == _NAME_ACTION:
            fixed, changed = fill_missing_names(model_path, out, guid=plan.get("guid"))
            new_verdicts = run_checks(fixed, rules)
            reply = _repair_reply("补上名称", changed, new_verdicts)
        else:  # _FIRE_ACTION
            fixed, changed = fill_missing_fire_ratings(
                model_path, out, value=plan["value"], guid=plan.get("guid"))
            new_verdicts = run_checks(fixed, rules)
            reply = _repair_reply("补上防火等级", changed, new_verdicts)
        return {"reply": reply, "model_path": fixed, "verdicts": new_verdicts}
    except Exception as e:
        return {"reply": f"修复失败：{e}", "model_path": None, "verdicts": None}


# ---------------------------------------------------------------------------
# LLM 上下文构建（DESIGN §6.1：模型信息 + 规则摘要 + 检查结果）
# ---------------------------------------------------------------------------

_MODEL_INFO_CACHE: dict = {}   # (路径字符串, mtime) -> 模型摘要；避免每轮问答重读 IFC
_MODEL_INFO_CACHE_MAX = 64     # 缓存上限（演示规模足够；溢出即整体清空）
_FINDINGS_MAX_ROWS = 30        # 结果明细截断上限：上下文保持紧凑（§6.1）
_HISTORY_LIMIT = 6             # 注入 LLM 的最近对话轮数


def _model_summary_md(model_path: str) -> str:
    """模型信息摘要（上下文块之一）：文件名 / IFC 版本 / 构件总数 / 主要类型。

    与 app.py 的 _model_info_md 同源但独立实现（agent 层不依赖 app 层）。
    每轮问答都会调用 → 用 (路径, mtime) 做小缓存：文件未变不重读 IFC；
    修复工具写的是新工作副本路径（覆盖同路径 → mtime 变化），缓存自然失效。
    打开失败返回占位说明而非抛异常 —— 问答仍可基于 verdicts 继续。
    """
    if not model_path:
        return "- 模型：未提供"
    path = Path(model_path)
    try:
        key = (str(path), path.stat().st_mtime)
    except OSError:
        return f"- 模型：无法读取（{path.name}）"
    if key not in _MODEL_INFO_CACHE:
        try:
            import ifcopenshell
            ifc = ifcopenshell.open(str(path))
            elems = ifc.by_type("IfcElement")
            counts = Counter(e.is_a() for e in elems)
            top = "、".join(f"{k} × {v}" for k, v in counts.most_common(5)) or "—"
            _MODEL_INFO_CACHE[key] = (
                f"- 文件名：{path.name}\n"
                f"- IFC 版本：{getattr(ifc, 'schema', '未知')}\n"
                f"- 构件总数：{len(elems)}\n"
                f"- 主要类型：{top}"
            )
        except Exception as e:
            _MODEL_INFO_CACHE[key] = f"- 模型：解析失败（{e}）"
        if len(_MODEL_INFO_CACHE) > _MODEL_INFO_CACHE_MAX:
            _MODEL_INFO_CACHE.clear()  # 防无界增长：演示场景下极少触发
    return _MODEL_INFO_CACHE[key]


def _rules_summary_md(rules: dict) -> str:
    """规则配置摘要（上下文块之二）：查了什么实体、按什么标准判定。"""
    lines = []
    for rule in (rules or {}).get("validation_rules", []):
        entities = "、".join(rule.get("entity", [])) or "—"
        lines.append(f"- {rule.get('name', '规则')}（实体：{entities}）")
        for check in rule.get("checks", []):
            cond = check.get("condition", {}) or {}
            if cond.get("type") == "range":
                basis = cond.get("threshold_basis", "")
                detail = (f"，期望 ≥ {cond.get('min')} {cond.get('unit', '')}"
                          f"（{basis.split()[0] if basis else '配置阈值'}）")
            elif cond.get("type") == "non_empty":
                detail = f"，要求 {check.get('attribute', '')} 非空"
            else:
                detail = ""
            lines.append(f"  · {check.get('name', '')}：{check.get('description', '')}{detail}")
    return "\n".join(lines) or "- 无规则配置"


def _findings_report_md(verdicts: list) -> str:
    """检查结果明细（上下文块之三）：违规/警告逐条列出，按严重级排序。

    pass 行不逐条列（只进汇总数）——LLM 需要的是「哪里有问题」；
    fail/warn 超过 _FINDINGS_MAX_ROWS 时截断并注明（大型模型上下文仍紧凑）。
    """
    verdicts = verdicts or []
    if not verdicts:
        return "- 尚未运行检查（无检查结果）"
    rank = {"fail": 0, "warn": 1, "pass": 2}
    rows = sorted(
        (v for v in verdicts if not v.is_pass),
        key=lambda v: (rank[v.status.value], v.check_id, v.element_name),
    )
    lines = [_check_summary_line(verdicts)]
    for v in rows[:_FINDINGS_MAX_ROWS]:
        mark = "FAIL" if v.is_fail else "WARN"
        guid = (v.element_guid or "")[:12]
        lines.append(
            f"[{mark}] {v.check_id} {v.element_name or '（未命名）'}"
            f"（{v.ifc_type}，GUID {guid}）当前 {v.current_value}，"
            f"期望 {v.expected} —— {v.reason}"
        )
    if len(rows) > _FINDINGS_MAX_ROWS:
        lines.append(
            f"……（结果过多，仅列出最严重的 {_FINDINGS_MAX_ROWS} 条，"
            f"其余 {len(rows) - _FINDINGS_MAX_ROWS} 条从略）"
        )
    return "\n".join(lines)


def _build_context(model_path: str, rules: dict, verdicts: list) -> str:
    """组装注入 LLM 的完整上下文块（DESIGN §6.1）。

    顺序即呈现顺序：模型是什么 → 按什么规则查 → 查出什么结果。
    """
    return "\n\n".join(
        (
            "【IFC 模型信息】\n" + _model_summary_md(model_path),
            "【生效的检查规则】\n" + _rules_summary_md(rules),
            "【检查结果】\n" + _findings_report_md(verdicts),
        )
    )


def _recent_history(history: list) -> list:
    """取最近 _HISTORY_LIMIT 条对话（防御性过滤：role 限 user/assistant、内容非空）。"""
    turns = [
        {"role": h.get("role"), "content": str(h.get("content", ""))}
        for h in (history or [])
        if h.get("role") in ("user", "assistant") and str(h.get("content", "")).strip()
    ]
    return turns[-_HISTORY_LIMIT:]


# ---------------------------------------------------------------------------
# LLM 路径（§6.3 DeepSeek，OpenAI 兼容；失败静默回退确定性）
# ---------------------------------------------------------------------------

def _llm_chat_message(messages: list, tools: list = None):
    """DeepSeek chat completions（可带 tools）；返回原始 message 对象。

    任何失败（无 key/网络/格式）→ 抛异常由调用方回退。
    """
    import openai
    client = openai.OpenAI(
        api_key=os.environ["DEEPSEEK_API_KEY"], base_url=_DEEPSEEK_URL
    )
    kwargs = dict(
        model=os.environ.get("DEEPSEEK_MODEL", _DEFAULT_MODEL),
        messages=messages,
        temperature=0.2,
        max_tokens=600,
    )
    if tools:
        kwargs["tools"] = tools
    resp = client.chat.completions.create(**kwargs)
    return resp.choices[0].message


def _llm_chat(messages: list) -> str:
    """文本对话（无工具）；返回 content 字符串。"""
    return _llm_chat_message(messages).content or ""


def _llm_chat_with_context(message: str, history: list, model_path: str,
                           rules: dict, verdicts: list) -> str:
    """组装消息列表并调用 DeepSeek（§6.1 上下文注入）。

    消息顺序：system（身份 + 回答纪律 + 上下文块）→ 最近历史 → 当前问题。
    上下文放 system 而非单独的 user 消息：OpenAI 兼容模型对 system 的遵循
    优先级最高，且不会把上下文误当成用户提问、不会被后续历史稀释。
    """
    context = _build_context(model_path, rules, verdicts)
    messages = [{"role": "system", "content": f"{_SYSTEM_PROMPT}\n\n{context}"}]
    messages += _recent_history(history)
    messages.append({"role": "user", "content": message})
    return _llm_chat(messages)


def _llm_advice(message: str, history: list, plan: dict, model_path: str,
                rules: dict, verdicts: list) -> str:
    """建议轮：LLM 按【拟定修复方案】叙述建议并询问用户（不传工具，只动嘴）。"""
    context = _build_context(model_path, rules, verdicts)
    system = f"{_SYSTEM_PROMPT}\n\n{context}\n\n{_plan_text(plan)}"
    messages = [{"role": "system", "content": system}]
    messages += _recent_history(history)
    messages.append({"role": "user", "content": message})
    return _llm_chat(messages)


def _llm_repair_turn(message: str, history: list, plan: dict, model_path: str,
                     rules: dict, verdicts: list, work_dir) -> dict:
    """确认轮（function calling）：LLM 产出 tool_calls → 校验执行 → 重检 → LLM 汇报。

    每个 tool_call 校验后在工作副本执行；失败（无 tool_calls / 执行异常 /
    汇报为空）→ 抛异常由调用方回退确定性执行。
    """
    import json
    context = _build_context(model_path, rules, verdicts)
    system = (f"{_SYSTEM_PROMPT}\n\n{context}\n\n{_plan_text(plan)}\n\n"
              "用户已明确确认执行上述修复。请立即调用工具完成修复；"
              "修复完成后调用 rerun_check。")
    messages = [{"role": "system", "content": system}]
    messages += _recent_history(history)
    messages.append({"role": "user", "content": message})

    msg = _llm_chat_message(messages, tools=_TOOL_SCHEMAS)
    tool_calls = getattr(msg, "tool_calls", None)
    if not tool_calls:
        raise ValueError("LLM 未产出修复工具调用")

    current_path, current_verdicts = model_path, verdicts
    for tc in tool_calls:
        fn = tc.function
        name = fn.name
        try:
            args = json.loads(fn.arguments or "{}")
        except ValueError:
            args = {}
        if name == "rerun_check":
            result_desc = "检查已重新运行"
        else:
            current_path, changed, desc = _run_tool(name, args, current_path, work_dir)
            current_verdicts = run_checks(current_path, rules)
            result_desc = f"{desc}（改动 {changed} 处）"
        messages.append({
            "role": "assistant", "content": None,
            "tool_calls": [{"id": tc.id, "type": "function",
                            "function": {"name": name, "arguments": fn.arguments}}],
        })
        messages.append({"role": "tool", "tool_call_id": tc.id, "content": result_desc})

    # 汇报轮（无工具）：实验模型偶发把工具调用输出成 XML/JSON 文本而非正常汇报
    # → 视为失败，回退确定性执行（幂等：重跑工具改动 0 处，但汇报文案可靠）
    final = (_llm_chat(messages) or "").strip()
    if not final or _TOOL_CALL_TEXT_RE.search(final):
        raise ValueError("LLM 未生成正常汇报（空或仍为工具调用文本）")
    return {"reply": final, "model_path": current_path, "verdicts": current_verdicts}


def _execute_pending(message: str, history: list, pending: dict, model_path: str,
                     rules: dict, verdicts: list, work_dir) -> dict:
    """确认后的执行入口：有 key 先试 function calling，任何失败回退确定性执行。"""
    if os.environ.get("DEEPSEEK_API_KEY"):
        try:
            return _llm_repair_turn(message, history, pending, model_path,
                                    rules, verdicts, work_dir)
        except Exception:
            pass  # 静默回退确定性执行（§4.1：离线演示不依赖任何外部 API）
    return _deterministic_execute(pending, model_path, rules, work_dir)


# ---------------------------------------------------------------------------
# 主入口（app.py 调用）
# ---------------------------------------------------------------------------

def chat(message: str, history: list, verdicts: list, model_path: str,
         rules: dict, work_dir, pending: dict = None) -> dict:
    """一次聊天回复。

    :param pending: 上一轮建议的待确认修复方案（dict；app.py 状态传入）
    :return: dict：{"reply", "model_path", "verdicts",
                    "pending"(可选：含键=新值，缺省=保持现值)}
    """
    message = (message or "").strip()
    if not message:
        return {"reply": "", "model_path": None, "verdicts": None}
    if not model_path:
        return {"reply": "请先上传模型并运行检查，我才能基于结果回答。",
                "model_path": None, "verdicts": None}

    # ---- Capability B：修复意图（先于 LLM，保证离线可用）----
    m = _REPAIR_RE.search(message)
    if m:
        threshold_mm, new_mm = float(m.group(1)), float(m.group(2))
        try:
            fixed_path, changed = set_door_width(
                model_path, threshold_mm, new_mm, Path(work_dir) / "model_fixed.ifc"
            )
            new_verdicts = run_checks(fixed_path, rules)
            reply = (
                f"已修复 {changed} 扇门（宽度 < {threshold_mm:g}mm 的改为 {new_mm:g}mm，"
                f"工作副本已保存，原文件未动）。{_summary_line(new_verdicts)}"
            )
            fails = [v for v in new_verdicts if v.is_fail]
            if fails:
                reply += f"仍剩 {len(fails)} 条违规（属性问题），可继续修复。"
            else:
                reply += "全部通过。"
            return {"reply": reply, "model_path": fixed_path, "verdicts": new_verdicts}
        except Exception as e:
            return {"reply": f"修复失败：{e}", "model_path": None, "verdicts": None}

    # ---- Capability B：防火等级补全（set_property：FireRating，§6.2）----
    if _FIRE_RE.search(message) and _REPAIR_VERB.search(message):
        vm = re.search(r"防火等级\D{0,8}?(\d+(?:\.\d+)?)\s*h", message)
        value = f"{vm.group(1)}h" if vm else "2h"
        try:
            fixed_path, changed = fill_missing_fire_ratings(
                model_path, Path(work_dir) / "model_fixed_fire.ifc", value
            )
            new_verdicts = run_checks(fixed_path, rules)
            return {"reply": _repair_reply("补上防火等级", changed, new_verdicts),
                    "model_path": fixed_path, "verdicts": new_verdicts}
        except Exception as e:
            return {"reply": f"修复失败：{e}", "model_path": None, "verdicts": None}

    # ---- Capability B：名称补全（set_property：Name，§6.2）----
    if _NAME_RE.search(message) and _REPAIR_VERB.search(message):
        try:
            fixed_path, changed = fill_missing_names(
                model_path, Path(work_dir) / "model_fixed_names.ifc"
            )
            new_verdicts = run_checks(fixed_path, rules)
            return {"reply": _repair_reply("补上名称", changed, new_verdicts),
                    "model_path": fixed_path, "verdicts": new_verdicts}
        except Exception as e:
            return {"reply": f"修复失败：{e}", "model_path": None, "verdicts": None}

    # ---- Capability B：重新检查 ----
    if _RERUN_RE.search(message):
        try:
            new_verdicts = run_checks(model_path, rules)
            return {"reply": _summary_line(new_verdicts),
                    "model_path": model_path, "verdicts": new_verdicts}
        except Exception as e:
            return {"reply": f"重新检查失败：{e}", "model_path": None, "verdicts": None}

    # ---- Capability B：修复确认闭环（§6.2 建议→确认→执行；在直接指令之后）----
    # 有待确认方案：先判拒绝，再判确认；无方案时确认词给出引导。
    if pending:
        if _is_decline(message):
            return {"reply": "好的，暂时不执行修复。您可以随时再次提出修复请求。",
                    "model_path": None, "verdicts": None, "pending": None}
        if _is_confirmation(message):
            return {**_execute_pending(message, history, pending, model_path,
                                       rules, verdicts, work_dir), "pending": None}
    elif _is_confirmation(message):
        return {"reply": "当前没有待执行的修复建议。请先告诉我您想修复什么，"
                         "例如：「疏散门宽度不足，如何解决？」",
                "model_path": None, "verdicts": None, "pending": None}

    # 咨询"怎么修"：确定性生成方案（锚点）→ LLM 建议（有 key）或确定性建议
    if _ASK_RE.search(message) and verdicts:
        plan = _build_repair_plan(message, verdicts, rules)
        if plan:
            if os.environ.get("DEEPSEEK_API_KEY"):
                try:
                    reply = _llm_advice(message, history, plan, model_path,
                                        rules, verdicts)
                except Exception:
                    reply = _deterministic_advice(plan)
            else:
                reply = _deterministic_advice(plan)
            if "需要我帮您修复吗" not in reply:
                reply += _ASK_SUFFIX
            return {"reply": reply, "model_path": None, "verdicts": None,
                    "pending": plan}

    # ---- Capability A：问答（LLM 优先，失败/未配置回退确定性）----
    # 护栏：尚未运行检查（verdicts 为空）→ 引导先运行，绝不回答。
    # 确定性助手在空结果上会误报「所有门宽度均满足」，LLM 也无数据可依。
    # 注意：修复意图在上一段已先行处理（修复合法地发生在运行检查之前）。
    if not verdicts:
        return {"reply": "请先点击「运行检查」，我才能基于检查结果回答模型问题。",
                "model_path": None, "verdicts": None}

    # 三个已知问题的确定性答案（离线兜底，也是 LLM 失败时的回退答案）
    deterministic = None
    if "防火" in message:
        deterministic = _ask_missing_fire_ratings(verdicts)
    elif any(k in message for k in ("名称", "名字")) and any(
        k in message for k in ("空", "缺", "没有", "缺失")):
        deterministic = _ask_missing_names(verdicts)
    elif any(k in message for k in ("门", "宽", "窄", "哪些")):
        deterministic = _ask_doors_too_narrow(verdicts)

    # LLM 优先：配置了 DEEPSEEK_API_KEY 时所有问题都走 LLM（注入模型信息 +
    # 规则摘要 + 检查结果 + 最近历史），失败（网络/格式/额度/**返回空内容**）
    # 静默回退 deterministic → 帮助菜单（§4.1：离线演示不依赖任何外部 API）。
    if os.environ.get("DEEPSEEK_API_KEY"):
        try:
            reply = _llm_chat_with_context(
                message, history, model_path, rules, verdicts)
            if (reply or "").strip():
                return {"reply": reply, "model_path": None, "verdicts": None}
        except Exception:
            pass  # 回退确定性回答 / 帮助菜单
    if deterministic:
        return {"reply": deterministic, "model_path": None, "verdicts": None}

    return {"reply": "我是质量检查助手。可以问我：\n"
                     "· 「哪些疏散门宽度不满足要求？」\n"
                     "· 「将所有小于 900mm 的门改成 1000mm」（修复后自动重新检查）\n"
                     "· 「重新运行检查」",
            "model_path": None, "verdicts": None}
