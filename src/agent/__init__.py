"""AI 质量助手 —— 与 LLM Agent 的集成点（DESIGN.md §6）。

两个能力（一个聊天窗口）：
- Capability A（§6.1）只读问答：基于当前 verdicts 的确定性回答
  （"哪些门太窄了？"），保证离线演示永远可用。
- Capability B（§6.2）引导式修复：set_door_width 工具 + 自动重检。
  修复写入**工作副本**（.work/ 下的新文件），绝不动用户上传的原文件
  （§6.2：原文件可重新上传 = 撤销机制）。

LLM 路径：设置了 DEEPSEEK_API_KEY 时优先调用 DeepSeek（OpenAI 兼容，
模型 deepseek-v4-flash-vision-exp，§6.3）；未设置或调用失败时回退到
确定性意图处理 —— 演示视频不依赖任何外部 API（§4.1 风险表）。

provider.py / tools.py / context.py 由后续任务补齐；本模块是 app.py 的
薄集成层，保持其接口稳定：agent.chat(...) → dict。
"""

import os
import re
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
_SYSTEM_PROMPT = (
    "你是 BIM 质量检查助手（HKU AI Agent Technical Test）。"
    "基于给定的 IFC 模型检查结果回答中文问题，只陈述事实，不编造数据。"
)


# ---------------------------------------------------------------------------
# Capability B —— 修复工具（DESIGN §6.2）
# ---------------------------------------------------------------------------

def set_door_width(model_path: str, threshold_mm: float, new_mm: float, output_path: str):
    """把模型中小于 threshold_mm 的 IfcDoor.OverallWidth 改为 new_mm。

    护栏（§6.2）：new_mm 钳制到 [MIN_DOOR_WIDTH, MAX_DOOR_WIDTH]（600–3000 mm）；
    只改 OverallWidth 这一个属性，不创建/删除任何实体。
    写入 output_path（工作副本），原文件不被修改。

    :return: (output_path, 修改的门数量)
    """
    import ifcopenshell
    model = ifcopenshell.open(str(model_path))
    new_mm = min(max(new_mm, MIN_DOOR_WIDTH), MAX_DOOR_WIDTH)
    changed = 0
    for door in model.by_type("IfcDoor"):
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


def fill_missing_names(model_path: str, output_path: str):
    """给名称为空的构件补上名字（§6.2 set_property 的 allowlist：Name）。

    命名风格与样例模型一致（墙-01 / 门-01），写入工作副本，原文件不动。

    :return: (output_path, 修改的构件数量)
    """
    import ifcopenshell
    model = ifcopenshell.open(str(model_path))
    changed = 0
    for i, elem in enumerate(model.by_type("IfcElement"), start=1):
        if not getattr(elem, "Name", None):
            elem.Name = f"{_CN_TYPE.get(elem.is_a(), elem.is_a())}-{i:02d}"
            changed += 1
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    model.write(str(out))
    return str(out), changed


def fill_missing_fire_ratings(model_path: str, output_path: str, value: str = "2h"):
    """给缺少 FireRating 的构件补上防火等级（§6.2 set_property 的 allowlist：FireRating）。

    有既有属性集的写入该属性集；没有的按类型建标准 pset（Pset_WallCommon /
    Pset_DoorCommon / Pset_ElementCommon）。值默认 "2h"，写入工作副本。

    :return: (output_path, 修改的构件数量)
    """
    import ifcopenshell
    import ifcopenshell.api
    model = ifcopenshell.open(str(model_path))
    pset_name = {
        "IfcWall": "Pset_WallCommon",
        "IfcDoor": "Pset_DoorCommon",
    }
    changed = 0
    for elem in model.by_type("IfcElement"):
        # 已有 FireRating（任意属性集，含厂商 Pset）→ 跳过
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
                continue
        pset = (
            rel.RelatingPropertyDefinition if rel and rel.RelatingPropertyDefinition.is_a("IfcPropertySet")
            else ifcopenshell.api.run(
                "pset.add_pset", model, product=elem,
                name=pset_name.get(elem.is_a(), "Pset_ElementCommon"),
            )
        )
        ifcopenshell.api.run("pset.edit_pset", model, pset=pset, properties={"FireRating": value})
        changed += 1
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    model.write(str(out))
    return str(out), changed


# ---------------------------------------------------------------------------
# Capability A —— 确定性问答（离线兜底）
# ---------------------------------------------------------------------------

def _ask_doors_too_narrow(verdicts):
    """"哪些门太窄了？" → 列出门名 + 宽度。"""
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


def _summary_line(verdicts):
    """"重新检查" 后的结果摘要行。"""
    n_pass = sum(1 for v in verdicts if v.is_pass)
    n_warn = sum(1 for v in verdicts if v.is_warn)
    n_fail = sum(1 for v in verdicts if v.is_fail)
    return f"重新检查完成：✅ {n_pass} 通过 · ⚠️ {n_warn} 警告 · ❌ {n_fail} 违规。"


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
# LLM 路径（§6.3 DeepSeek，OpenAI 兼容；失败静默回退确定性）
# ---------------------------------------------------------------------------

def _llm_chat(messages: list) -> str:
    """DeepSeek chat completions；任何失败（无 key/网络/格式）→ 抛异常由调用方回退。"""
    import openai
    client = openai.OpenAI(
        api_key=os.environ["DEEPSEEK_API_KEY"], base_url=_DEEPSEEK_URL
    )
    resp = client.chat.completions.create(
        model=os.environ.get("DEEPSEEK_MODEL", _DEFAULT_MODEL),
        messages=messages,
        temperature=0.2,
        max_tokens=400,
    )
    return resp.choices[0].message.content or ""


# ---------------------------------------------------------------------------
# 主入口（app.py 调用）
# ---------------------------------------------------------------------------

def chat(message: str, history: list, verdicts: list, model_path: str,
         rules: dict, work_dir) -> dict:
    """一次聊天回复。

    :return: dict：{"reply": str,
                     "model_path": 修复后模型路径（无变更时为 None）,
                     "verdicts":    修复/重检后的新判定（无变更时为 None）}
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

    # ---- Capability A：问答（LLM 优先，失败/未配置回退确定性）----
    deterministic = None
    if "防火" in message:
        deterministic = _ask_missing_fire_ratings(verdicts)
    elif any(k in message for k in ("名称", "名字")) and any(
        k in message for k in ("空", "缺", "没有", "缺失")):
        deterministic = _ask_missing_names(verdicts)
    elif any(k in message for k in ("门", "宽", "窄", "哪些")):
        deterministic = _ask_doors_too_narrow(verdicts)
    if deterministic and os.environ.get("DEEPSEEK_API_KEY"):
        try:
            return {"reply": _llm_chat([
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": message},
            ]), "model_path": None, "verdicts": None}
        except Exception:
            pass  # 回退确定性回答
    if deterministic:
        return {"reply": deterministic, "model_path": None, "verdicts": None}

    return {"reply": "我是质量检查助手。可以问我：\n"
                     "· 「哪些门太窄了？」\n"
                     "· 「把所有小于 900mm 的门改成 1000mm」（修复后自动重新检查）\n"
                     "· 「重新运行检查」",
            "model_path": None, "verdicts": None}
