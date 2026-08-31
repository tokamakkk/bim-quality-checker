"""Agent 问答上下文注入测试（DESIGN.md §6.1）。

覆盖：上下文块内容（模型信息 / 规则摘要 / 检查结果，不发 API）、
LLM 收到完整消息列表（上下文 + 最近历史 + 当前问题，monkeypatch _llm_chat）、
未运行检查的护栏、无 key 时的确定性回退、LLM 失败时的回退链。
全部离线，绝不触达真实 API。
"""

import sys
from pathlib import Path

import pytest

# src 不是包目录（无 __init__.py），将 src/ 加入导入路径（与 test_e2e.py 同款）
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
    """与 test_e2e.py 同款 fixture：bad_model.ifc（2 墙 + 4 门，5 处缺陷）。"""
    path = SAMPLE_DIR / "bad_model.ifc"
    if not path.exists() or path.stat().st_size == 0:
        pytest.skip("bad_model.ifc 尚未生成")
    return path


def test_build_context_contains_model_rules_findings(bad_model_path):
    """(a) 上下文块含模型信息、规则摘要、检查结果明细；第二次调用命中缓存。"""
    agent._MODEL_INFO_CACHE.clear()
    rules = load_rules(CONFIG_RULES)
    verdicts = run_checks(bad_model_path, rules)
    ctx = agent._build_context(str(bad_model_path), rules, verdicts)

    assert "bad_model.ifc" in ctx and "IFC4" in ctx
    assert "构件总数" in ctx and "IfcDoor" in ctx
    assert "R1 Attribute Completeness" in ctx and "R2 Exit Door Width" in ctx
    assert "GB50016" in ctx
    assert "4 违规" in ctx and "1 警告" in ctx          # §8.1：4 fail / 1 warn
    assert "FAIL] R2" in ctx and "700 mm" in ctx and "800 mm" in ctx
    assert "FAIL] R1a" in ctx and "WARN] R1b" in ctx
    assert "从略" not in ctx                            # 5 条 < 30 条上限，无截断注记
    # 小缓存：(路径, mtime) 不变时不再重开 IFC
    assert agent._build_context(str(bad_model_path), rules, verdicts) == ctx
    assert len(agent._MODEL_INFO_CACHE) == 1


def test_llm_receives_context_history_and_question(bad_model_path, monkeypatch):
    """(b) key 已配置：LLM 收到 system(含上下文) → 最近 6 条历史 → 当前问题。"""
    rules = load_rules(CONFIG_RULES)
    verdicts = run_checks(bad_model_path, rules)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    captured = {}

    def fake_llm(messages):
        captured["messages"] = messages
        return "LLM 回答"

    monkeypatch.setattr(agent, "_llm_chat", fake_llm)
    history = [{"role": "user", "content": f"问题 {i}"} for i in range(1, 9)]

    result = agent.chat("哪些门太窄了？", history, verdicts,
                        str(bad_model_path), rules, None)
    assert result["reply"] == "LLM 回答"
    msgs = captured["messages"]
    assert msgs[0]["role"] == "system"
    for expected in ("bad_model.ifc", "构件总数", "R2 Exit Door Width",
                     "FAIL] R2", "GB50016", "4 违规"):
        assert expected in msgs[0]["content"]
    assert msgs[1:7] == history[2:]                     # 历史截断到 6 条
    assert msgs[-1] == {"role": "user", "content": "哪些门太窄了？"}


def test_qa_guard_before_checks(bad_model_path, monkeypatch):
    """(c) 未运行检查（verdicts 为空）→ 引导先运行检查；即使有 key 也不调 LLM。"""
    rules = load_rules(CONFIG_RULES)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")

    def boom(messages):
        raise AssertionError("verdicts 为空时不应调用 LLM")

    monkeypatch.setattr(agent, "_llm_chat", boom)
    result = agent.chat("哪些门太窄了？", [], [], str(bad_model_path), rules, None)
    assert "运行检查" in result["reply"]
    assert "均满足" not in result["reply"]              # 不输出误导性结论


def test_no_key_falls_back_to_deterministic(bad_model_path, monkeypatch):
    """(d) 未配置 key：三个已知模式走确定性答案，其余走帮助菜单（行为与今天一致）。"""
    rules = load_rules(CONFIG_RULES)
    verdicts = run_checks(bad_model_path, rules)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

    r1 = agent.chat("哪些门太窄了？", [], verdicts, str(bad_model_path), rules, None)
    assert "3 扇门宽度不足" in r1["reply"]
    assert "700 mm" in r1["reply"] and "800 mm" in r1["reply"]

    r2 = agent.chat("哪些构件没有名字？", [], verdicts, str(bad_model_path), rules, None)
    assert "1 个构件名称为空" in r2["reply"]

    r3 = agent.chat("哪些构件缺少防火等级？", [], verdicts, str(bad_model_path), rules, None)
    assert "缺少 FireRating" in r3["reply"]

    r4 = agent.chat("今天天气怎么样？", [], verdicts, str(bad_model_path), rules, None)
    assert "我是质量检查助手" in r4["reply"]


def test_llm_failure_falls_back_to_deterministic(bad_model_path, monkeypatch):
    """(e) key 已配置但 LLM 抛异常 → 静默回退确定性答案 / 帮助菜单。"""
    rules = load_rules(CONFIG_RULES)
    verdicts = run_checks(bad_model_path, rules)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")

    def boom(messages):
        raise RuntimeError("API 不可用")

    monkeypatch.setattr(agent, "_llm_chat", boom)
    r1 = agent.chat("哪些门太窄了？", [], verdicts, str(bad_model_path), rules, None)
    assert "3 扇门宽度不足" in r1["reply"]
    r2 = agent.chat("今天天气怎么样？", [], verdicts, str(bad_model_path), rules, None)
    assert "我是质量检查助手" in r2["reply"]


def test_llm_empty_reply_falls_back_to_deterministic(bad_model_path, monkeypatch):
    """(f) key 已配置但 LLM 返回空内容（实验模型偶发）→ 不显示空回复，走确定性回退。"""
    rules = load_rules(CONFIG_RULES)
    verdicts = run_checks(bad_model_path, rules)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")

    def empty_reply(messages):
        return "   "

    monkeypatch.setattr(agent, "_llm_chat", empty_reply)
    r1 = agent.chat("哪些门太窄了？", [], verdicts, str(bad_model_path), rules, None)
    assert r1["reply"].strip()
    assert "3 扇门宽度不足" in r1["reply"]
    r2 = agent.chat("今天天气怎么样？", [], verdicts, str(bad_model_path), rules, None)
    assert "我是质量检查助手" in r2["reply"]
