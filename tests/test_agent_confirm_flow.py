"""Agent 修复确认闭环测试（DESIGN.md §6.2 建议 → 询问 → 确认 → 工具执行 → 重检汇报）。

覆盖：建议轮（无 key 确定性 / 有 key LLM 并兜底询问句）、确认轮（无 key 确定性执行 /
function calling 经 monkeypatch 的 _llm_chat_message）、单构件修复、拒绝清除 pending、
无 pending 确认引导、_run_tool 护栏、全通过模型无方案、直接指令无 pending 键回归。
全部离线，绝不触达真实 API。
"""

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

# src 不是包目录（无 __init__.py），将 src/ 加入导入路径（与 test_agent_context.py 同款）
SRC_DIR = Path(__file__).resolve().parents[1] / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

ROOT = SRC_DIR.parent
SAMPLE_DIR = ROOT / "sample_data"
CONFIG_RULES = ROOT / "config" / "rules.json"

import agent  # noqa: E402
from core.engine import load_rules, run_checks  # noqa: E402


@pytest.fixture(scope="module")
def bad_model_path():
    """bad_model.ifc（2 墙 + 4 门，5 处缺陷；§8.1 验收样本）。"""
    path = SAMPLE_DIR / "bad_model.ifc"
    if not path.exists() or path.stat().st_size == 0:
        pytest.skip("bad_model.ifc 尚未生成")
    return path


@pytest.fixture(scope="module")
def good_model_path():
    """good_model.ifc（全通过基线）。"""
    path = SAMPLE_DIR / "good_model.ifc"
    if not path.exists() or path.stat().st_size == 0:
        pytest.skip("good_model.ifc 尚未生成")
    return path


@pytest.fixture()
def rules():
    return load_rules(CONFIG_RULES)


@pytest.fixture()
def bad_verdicts(bad_model_path, rules):
    return run_checks(bad_model_path, rules)


def _door_plan(verdicts, rules, message="门太窄了，怎么修？"):
    """构造待确认方案（与 chat 建议轮同源）。"""
    return agent._build_repair_plan(message, verdicts, rules)


# ---------------------------------------------------------------------------
# 建议轮
# ---------------------------------------------------------------------------

def test_advice_no_key_produces_pending(bad_model_path, bad_verdicts, rules):
    """(1) 无 key 问"怎么修" → 确定性建议 + 询问句 + pending 方案（不动模型）。"""
    result = agent.chat("门太窄了，怎么修？", [], bad_verdicts,
                        str(bad_model_path), rules, None)
    assert "3 扇门宽度不足" in result["reply"]
    assert "门-01（800 mm）" in result["reply"]
    assert "需要我帮您修复吗" in result["reply"]
    assert result["model_path"] is None and result["verdicts"] is None
    plan = result["pending"]
    assert plan["action"] == "fix_doors" and plan["kind"] == "batch"
    assert len(plan["items"]) == 3 and plan["threshold"] == 900


def test_advice_with_key_appends_ask_suffix(bad_model_path, bad_verdicts, rules, monkeypatch):
    """(2) 有 key：LLM 收到含【拟定修复方案】的消息；文案缺询问句时确定性补上。"""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    captured = {}

    def fake_llm(messages):
        captured["messages"] = messages
        return "建议将这三扇门统一加宽。"

    monkeypatch.setattr(agent, "_llm_chat", fake_llm)
    result = agent.chat("门太窄了，怎么修？", [], bad_verdicts,
                        str(bad_model_path), rules, None)
    assert result["reply"] == "建议将这三扇门统一加宽。" + agent._ASK_SUFFIX
    system = captured["messages"][0]["content"]
    assert "【拟定修复方案】" in system and "set_door_width" in system
    assert "3 扇门" in system and "new_mm=1000" in system
    assert result["pending"]["action"] == "fix_doors"


# ---------------------------------------------------------------------------
# 确认轮
# ---------------------------------------------------------------------------

def test_confirm_no_key_deterministic_execute(bad_model_path, bad_verdicts, rules,
                                              tmp_path):
    """(3) 确认后无 key → 确定性批量执行：3 扇门修好、原文件字节不变、pending 清除。"""
    plan = _door_plan(bad_verdicts, rules)
    original = bad_model_path.read_bytes()

    result = agent.chat("好的", [], bad_verdicts, str(bad_model_path), rules,
                        str(tmp_path), pending=plan)
    assert "已修复 3 扇门" in result["reply"]
    assert "原文件未动" in result["reply"]
    assert result["pending"] is None
    assert bad_model_path.read_bytes() == original            # 原文件字节不变
    fixed = result["model_path"]
    assert fixed.endswith("model_fixed.ifc")
    r2_fails = [v for v in result["verdicts"] if v.check_id == "R2" and v.is_fail]
    assert r2_fails == []                                     # R2 全 pass


def test_confirm_function_calling(bad_model_path, bad_verdicts, rules, tmp_path,
                                  monkeypatch):
    """(4) 有 key：LLM 产出 tool_call → 校验执行 → 重检 → LLM 汇报（真实 function calling 路径）。"""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    calls = []

    def fake_message(messages, tools=None):
        # 存副本：_llm_repair_turn 后续会原地 append tool 消息到同一列表
        calls.append((list(messages), tools))
        if len(calls) == 1:
            return SimpleNamespace(
                content=None,
                tool_calls=[SimpleNamespace(
                    id="call_1",
                    function=SimpleNamespace(
                        name="set_door_width",
                        arguments='{"threshold_mm":900,"new_mm":1000}'))])
        return SimpleNamespace(content="✅ 已按方案修复完成！", tool_calls=None)

    monkeypatch.setattr(agent, "_llm_chat_message", fake_message)
    plan = _door_plan(bad_verdicts, rules)

    result = agent.chat("好的，修复吧", [], bad_verdicts, str(bad_model_path),
                        rules, str(tmp_path), pending=plan)
    assert result["reply"] == "✅ 已按方案修复完成！"
    assert result["pending"] is None
    assert result["model_path"].endswith("model_fixed.ifc")
    r2_fails = [v for v in result["verdicts"] if v.check_id == "R2" and v.is_fail]
    assert r2_fails == []

    # 第一轮：带 tools、system 含方案与确认指令、末位为确认消息
    first_msgs, first_tools = calls[0]
    assert first_tools == agent._TOOL_SCHEMAS
    assert "用户已明确确认" in first_msgs[0]["content"]
    assert first_msgs[-1]["content"] == "好的，修复吧"
    # 汇报轮：含 assistant tool_call echo + tool 结果消息
    second_msgs, second_tools = calls[1]
    assert second_tools is None
    assert any(
        m.get("role") == "assistant" and m.get("tool_calls")
        for m in second_msgs
    )
    assert any(
        m.get("role") == "tool" and "门宽修复" in m.get("content", "")
        for m in second_msgs
    )


def test_confirm_llm_report_emits_tool_text_falls_back(
        bad_model_path, bad_verdicts, rules, tmp_path, monkeypatch):
    """(4b) 工具执行成功但汇报轮输出工具调用文本（实验模型偶发）→ 视为失败回退确定性汇报。"""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")

    def fake_message(messages, tools=None):
        if tools:
            return SimpleNamespace(
                content=None,
                tool_calls=[SimpleNamespace(
                    id="call_1",
                    function=SimpleNamespace(
                        name="set_door_width",
                        arguments='{"threshold_mm":900,"new_mm":1000}'))])
        return SimpleNamespace(content="<tool_calls>\n<invoke name=\"rerun_check\">\n</invoke>\n</tool_calls>",
                               tool_calls=None)

    monkeypatch.setattr(agent, "_llm_chat_message", fake_message)
    plan = _door_plan(bad_verdicts, rules)

    result = agent.chat("好的，修复吧", [], bad_verdicts, str(bad_model_path),
                        rules, str(tmp_path), pending=plan)
    # 回退确定性执行：正常中文汇报、模型已修复、pending 清除
    assert "已修复 3 扇门" in result["reply"]
    assert "<tool_calls>" not in result["reply"]
    assert result["pending"] is None
    r2_fails = [v for v in result["verdicts"] if v.check_id == "R2" and v.is_fail]
    assert r2_fails == []


def test_confirm_single_element(bad_model_path, bad_verdicts, rules, tmp_path):
    """(5) 提到具体构件名 → 单构件方案；确认后只修该门，其余仍 fail。"""
    door01 = next(v for v in bad_verdicts if v.element_name == "门-01")

    result = agent.chat("门-01 怎么修？", [], bad_verdicts,
                        str(bad_model_path), rules, None)
    plan = result["pending"]
    assert plan["kind"] == "single" and plan["guid"] == door01.element_guid
    assert "门-01" in result["reply"]

    result2 = agent.chat("可以", [], bad_verdicts, str(bad_model_path), rules,
                         str(tmp_path), pending=plan)
    assert "已修复 门-01" in result2["reply"]
    fixed_fails = [v for v in result2["verdicts"]
                   if v.check_id == "R2" and v.is_fail]
    assert [v.element_name for v in fixed_fails] == ["门-02", "门-03"]


def test_decline_clears_pending(bad_model_path, bad_verdicts, rules):
    """(6) 拒绝词 → 礼貌回复 + pending 清除（不再等待确认）。"""
    plan = _door_plan(bad_verdicts, rules)
    result = agent.chat("不用了，我先看看", [], bad_verdicts,
                        str(bad_model_path), rules, None, pending=plan)
    assert "暂时不执行修复" in result["reply"]
    assert result["pending"] is None


def test_confirmation_without_pending_guides(bad_model_path, bad_verdicts, rules):
    """(7) 无待确认方案时回"好的" → 引导用户先提出修复需求。"""
    result = agent.chat("好的", [], bad_verdicts, str(bad_model_path), rules, None)
    assert "当前没有待执行的修复建议" in result["reply"]
    assert result["pending"] is None


# ---------------------------------------------------------------------------
# 工具护栏
# ---------------------------------------------------------------------------

def test_run_tool_guardrails(bad_model_path, bad_verdicts, rules, tmp_path):
    """(8) _run_tool 护栏：未知工具 / 非 allowlist 属性 / 不存在 GUID → ValueError。"""
    door01 = next(v for v in bad_verdicts if v.element_name == "门-01")
    with pytest.raises(ValueError, match="未知工具"):
        agent._run_tool("delete_model", {}, str(bad_model_path), str(tmp_path))
    with pytest.raises(ValueError, match="不允许修改属性"):
        agent._run_tool(
            "set_property",
            {"guid": door01.element_guid, "property_name": "Length", "value": "1"},
            str(bad_model_path), str(tmp_path))
    with pytest.raises(ValueError, match="未找到 GUID"):
        agent._run_tool(
            "set_property",
            {"guid": "0" * 22, "property_name": "Name", "value": "x"},
            str(bad_model_path), str(tmp_path))
    # rerun_check 不写文件，原样返回
    out, changed, desc = agent._run_tool(
        "rerun_check", {}, str(bad_model_path), str(tmp_path))
    assert out == str(bad_model_path) and changed == 0 and desc == "重新检查完成"


# ---------------------------------------------------------------------------
# 边界与回归
# ---------------------------------------------------------------------------

def test_all_pass_model_no_plan(good_model_path, rules):
    """(9) 全通过模型问"怎么修" → 无方案可给，走现有问答（帮助菜单）。"""
    good_verdicts = run_checks(good_model_path, rules)
    result = agent.chat("怎么修？", [], good_verdicts,
                        str(good_model_path), rules, None)
    assert "我是质量检查助手" in result["reply"]
    assert "pending" not in result                      # 无方案：不产生 pending 键


def test_direct_command_no_pending_key(bad_model_path, bad_verdicts, rules, tmp_path):
    """(10) 参数完整的直接指令立即执行（§8.2 验收路径），结果不携带 pending 键。"""
    result = agent.chat("把所有小于900mm的门改成1000mm", [], bad_verdicts,
                        str(bad_model_path), rules, str(tmp_path))
    assert "已修复 3 扇门" in result["reply"]
    assert "pending" not in result                      # 无关路径不触碰 pending 契约

    # 即使存在待确认方案，直接指令也立即执行且不覆盖 pending（保持现值）
    plan = _door_plan(bad_verdicts, rules)
    result2 = agent.chat("把所有小于900mm的门改成1000mm", [], bad_verdicts,
                         str(bad_model_path), rules, str(tmp_path), pending=plan)
    assert "已修复 3 扇门" in result2["reply"]
    assert "pending" not in result2
