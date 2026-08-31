"""BIM 质量检查器 —— Gradio 三栏 UI 主程序（DESIGN.md §5）。

布局（§5.1）：左栏上传区 / 中栏 3D 查看器 / 右栏检查结果，
右下角浮动 AI 助手面板（初始折叠）。全部界面文字为中文。
颜色与 3D / 报告共用 §5.2 统一色板。

单进程设计（§4.1）：本文件即服务器，`python src/app.py` 启动。

运行：python src/app.py  →  http://127.0.0.1:7860
"""

import html
import os
import shutil
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

# src 非包目录：保证从任意位置运行时核心模块可导入
SRC_DIR = Path(__file__).resolve().parent
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

import gradio as gr

import agent  # DESIGN.md §6：LLM Agent 集成点（问答 + 修复工具）
from core.engine import load_rules, run_checks
from core.verdict import Verdict, VerdictStatus
from report.report_html import generate_report
from viz.mesh_exporter import export_colored_glb

PROJECT_ROOT = SRC_DIR.parent
DEFAULT_RULES = PROJECT_ROOT / "config" / "rules.json"
WORK_DIR = PROJECT_ROOT / ".work"  # 上传副本 / GLB / 报告 的输出目录

# DESIGN.md §5.2 色板（UI 文本高亮用）
PASS_HEX, WARN_HEX, FAIL_HEX = "#22c55e", "#eab308", "#ef4444"
_EMOJI = {VerdictStatus.PASS: "✅", VerdictStatus.WARN: "⚠️", VerdictStatus.FAIL: "❌"}
_STATUS_ORDER = {VerdictStatus.FAIL: 0, VerdictStatus.WARN: 1, VerdictStatus.PASS: 2}

# 右栏检查结果独立滚动（内容超高时栏内上下滚动，不与整页联动）
# 浮动聊天面板样式（右下角固定、置顶）
CSS = """
#results-column {
  max-height: calc(100vh - 150px);
  min-height: 340px;
  overflow-y: auto;
  overflow-x: hidden;
  padding-right: 8px;
}
/* Gradio 组件根块内部 overflow 非 visible（min-height:auto→0），
   在固定高度 flex 容器里会被 flex-shrink 压扁、内容被裁剪。
   禁止压缩后内容按自然高度排布，由本列自身滚动（见 §5.1）。 */
#results-column > div.block {
  flex-shrink: 0;
}
#chat-panel {
  position: fixed !important;
  right: 16px; bottom: 16px;
  width: 400px; max-width: 92vw;
  z-index: 999;
  background: var(--block-background-fill, #ffffff);
  border: 1px solid var(--border-color-primary, #d1d5db);
  border-radius: 14px;
  box-shadow: 0 8px 28px rgba(0, 0, 0, .18);
  padding: 10px 12px;
}
"""


# 临时诊断（定位后删除）：把右栏真实 DOM 状态实时显示成文字，便于核对各浏览器差异
# 注意：Gradio 的 js= 参数会被包成 (<js>)(参数) 求值 —— 必须是函数表达式，且不能有结尾分号
DIAG_JS = r"""
() => {
  var box = document.getElementById('diag-box');
  if (!box) return;
  var busy = false;
  function dump() {
    if (busy) return;                       // 忙锁：防止写 diag-box 自身触发观察循环
    busy = true;
    try {
      var col = document.getElementById('results-column');
      if (!col) { box.innerText = 'no-col'; return; }
      var cs = getComputedStyle(col);
      var details = col.querySelectorAll('details');
      var rows = Array.prototype.slice.call(col.querySelectorAll('div[style*="border-left"]'));
      var colR = col.getBoundingClientRect();
      function rowInfo(r) {
        var rr = r.getBoundingClientRect();
        return { top: Math.round(rr.top), h: Math.round(rr.height),
                 visible: rr.top < colR.bottom && rr.bottom > colR.top };
      }
      var text = JSON.stringify({
        col: { scrollH: col.scrollHeight, clientH: col.clientHeight, scrollTop: Math.round(col.scrollTop),
               oy: cs.overflowY, h: cs.height, display: cs.display, flexDir: cs.flexDirection },
        details: details.length, detailsOpen: Array.prototype.map.call(details, function (d) { return d.open; }),
        rowsInDom: rows.length,
        firstRow: rows.length ? rowInfo(rows[0]) : null,
        lastRow: rows.length ? rowInfo(rows[rows.length - 1]) : null,
        colRect: { top: Math.round(colR.top), bottom: Math.round(colR.bottom) }
      });
      box.innerText = text;
      setTimeout(function () { busy = false; }, 0);   // 写入后解锁：观察回调先于解锁触发，见 busy 直接跳过
    } catch (e) { busy = false; }
  }
  dump();
  new MutationObserver(dump).observe(document.getElementById('results-column'),
                                    { subtree: true, childList: true, characterData: true });
}
"""


# ---------------------------------------------------------------------------
# 渲染辅助
# ---------------------------------------------------------------------------

def _summary_html(verdicts: list, model_name: str = "") -> str:
    """右栏摘要条：✅ N 通过 | ⚠ N 警告 | ❌ N 违规。

    附带运行完成时间戳与模型名——用于确认「运行检查」确实完成
    （页面刷新后状态会清空，需重新上传并运行）。
    """
    n = {s: sum(1 for v in verdicts if v.status is s) for s in VerdictStatus}
    stamp = ""
    if verdicts:
        stamp = (
            f'<div style="font-size:12px;color:#6b7280;margin-top:2px">'
            f'✅ 检查完成 {datetime.now():%m-%d %H:%M:%S} · {html.escape(model_name or "未知模型")}</div>'
        )
    return (
        f'<div style="font-size:15px;line-height:1.9">'
        f'<span style="color:{PASS_HEX}">✅ {n[VerdictStatus.PASS]} 通过</span>'
        f'<span style="margin:0 10px;color:{WARN_HEX}">⚠️ {n[VerdictStatus.WARN]} 警告</span>'
        f'<span style="color:{FAIL_HEX}">❌ {n[VerdictStatus.FAIL]} 违规</span>'
        f'<span style="color:#6b7280;font-size:13px">（共 {len(verdicts)} 条判定）</span>'
        f'</div>{stamp}'
    )


def _results_html(verdicts: list) -> str:
    """R1/R2 规则结果（按违规→警告→通过排序，每行含状态色标记）。

    分组用原生 <details>（可折叠），不用 gr.Accordion：Gradio 折叠组件在
    父容器限制高度时会把内容压缩进内部滚动（面板只剩标题），原生元素不受影响。
    """
    color = {VerdictStatus.PASS: PASS_HEX, VerdictStatus.WARN: WARN_HEX, VerdictStatus.FAIL: FAIL_HEX}

    def section(title: str, ids: set) -> str:
        sel = [v for v in verdicts if v.check_id in ids]
        if not sel:
            body = '<div style="color:#9ca3af">暂无结果，请先运行检查</div>'
        else:
            sel.sort(key=lambda v: (_STATUS_ORDER[v.status], v.element_name))
            rows = []
            for v in sel:
                rows.append(
                    f'<div style="padding:5px 8px;border-left:3px solid {color[v.status]};'
                    f'background:#f9fafb;border-radius:4px;margin:3px 0;font-size:13px">'
                    f'{_EMOJI[v.status]} <b>{html.escape(v.element_name or "（未命名）")}</b>'
                    f'<span style="color:#6b7280"> · {html.escape(v.ifc_type)} · {html.escape(v.check_id)}</span><br>'
                    f'<span style="color:#4b5563">{html.escape(v.reason or "")}</span>'
                    f'<span style="color:#9ca3af;font-size:12px">（当前 {html.escape(v.current_value or "")}，'
                    f'期望 {html.escape(v.expected or "")}）</span></div>'
                )
            body = "".join(rows)
        return (
            f'<details open style="margin:8px 0 2px">'
            f'<summary style="cursor:pointer;font-weight:600;color:#111827;padding:4px 0">{html.escape(title)}</summary>'
            f'<div style="margin-top:4px">{body}</div>'
            f'</details>'
        )

    return section("R1 属性完整性（名称 / 防火等级）", {"R1a", "R1b"}) + section("R2 门宽度（≥ 900mm）", {"R2"})


def _model_info_md(path: str) -> str:
    """左栏模型信息：文件名 / IFC 版本 / 构件数量与构成。"""
    import ifcopenshell
    try:
        ifc = ifcopenshell.open(str(path))
    except Exception as e:
        return f"⚠️ 无法解析模型：{e}"
    elems = ifc.by_type("IfcElement")
    counts = Counter(e.is_a() for e in elems)
    top = "、".join(f"{k} × {v}" for k, v in counts.most_common(5)) or "—"
    return (
        f"**模型信息**\n"
        f"- 文件名：`{Path(path).name}`\n"
        f"- IFC 版本：`{getattr(ifc, 'schema', '未知')}`\n"
        f"- 构件总数：**{len(elems)}**\n"
        f"- 主要类型：{top}"
    )


# ---------------------------------------------------------------------------
# 事件处理
# ---------------------------------------------------------------------------

def _to_path(file_obj) -> Path:
    """归一化 gr.File 的返回值（Gradio 5 传 FileData，也有传 str 的情况）。"""
    if file_obj is None:
        return None
    src = file_obj.path if hasattr(file_obj, "path") else file_obj
    return Path(src)


def on_upload_ifc(file_obj):
    """上传 IFC：保存工作副本，返回（路径, 模型信息）。"""
    src = _to_path(file_obj)
    if src is None or not src.exists():
        return None, "请上传 .ifc 模型文件（也可使用 sample_data/ 下的示例）"
    WORK_DIR.mkdir(exist_ok=True)
    dst = WORK_DIR / f"model_{time.strftime('%H%M%S')}_{src.name}"
    shutil.copy2(src, dst)
    return str(dst), _model_info_md(str(dst))


def run_check(model_path, rules_file, progress=gr.Progress()):
    """点击「运行检查」：规则加载 → 引擎检查 → 着色 GLB → 更新各面板。

    返回：(verdicts, 摘要, R1/R2 结果, GLB 路径)
    """
    if not model_path:
        gr.Warning("请先上传 IFC 模型文件")
        return [], "", "", None

    # 错误直接显示在摘要区（gr.Error 的红色提示易被忽略/一闪而过）
    def _err_summary(msg: str) -> str:
        return f'<div style="color:#ef4444;font-size:14px">❌ 检查失败：{html.escape(msg)}</div>'

    # 规则配置：优先用上传的，否则默认 config/rules.json
    rules_path = _to_path(rules_file)
    try:
        rules = load_rules(rules_path) if rules_path else load_rules(DEFAULT_RULES)
    except (FileNotFoundError, ValueError) as e:
        return [], _err_summary(f"规则配置加载失败：{e}"), "（暂无结果）", None

    progress(0.2, desc="解析 IFC 模型…")
    try:
        verdicts = run_checks(model_path, rules)
    except (FileNotFoundError, ValueError) as e:
        return [], _err_summary(str(e)), "（暂无结果）", None

    progress(0.6, desc="生成着色 3D 模型…")
    try:
        glb_path = export_colored_glb(model_path, verdicts, WORK_DIR / "model_all.glb", mode="all")
    except ValueError as e:
        return [], _err_summary(f"3D 模型生成失败：{e}"), "（暂无结果）", None

    progress(1.0, desc="完成")
    results_html = _results_html(verdicts)
    # 临时诊断（定位后删除）：记录结果负载大小，确认服务端确实下发
    sys.stderr.write(f"[diag] run_check 结果负载 {len(results_html)} 字符\n")
    return (
        verdicts,
        _summary_html(verdicts, Path(model_path).name),
        results_html,
        glb_path,
    )


def on_mode_change(mode, model_path, verdicts):
    """切换显示模式：全部显示 / 仅显示违规（重新导出 GLB）。"""
    if not model_path or not verdicts:
        return None
    export_mode = "all" if mode == "全部显示" else "violations_only"
    try:
        return export_colored_glb(
            model_path, verdicts, WORK_DIR / f"model_{export_mode}.glb", mode=export_mode
        )
    except ValueError as e:
        raise gr.Error(f"切换视图失败：{e}") from e


def _build_model_info(model_path: str) -> dict:
    """从 IFC 提取报告所需的模型摘要 dict（generate_report 的 model_info 参数）。"""
    import ifcopenshell
    ifc = ifcopenshell.open(str(model_path))
    project = next(iter(ifc.by_type("IfcProject")), None)
    return {
        "filename": Path(model_path).name,
        "schema": getattr(ifc, "schema", "未知"),
        "element_count": len(ifc.by_type("IfcElement")),
        "project_name": getattr(project, "Name", None) or Path(model_path).stem,
        "checked_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }


def on_export_report(model_path, verdicts, rules_file):
    """导出 HTML 报告（DESIGN.md §7.3）。"""
    if not model_path or not verdicts:
        gr.Warning("请先上传模型并运行检查")
        return None
    try:
        rules_path = _to_path(rules_file)
        rules = load_rules(rules_path) if rules_path else load_rules(DEFAULT_RULES)
        return generate_report(verdicts, _build_model_info(model_path), rules, WORK_DIR / "report.html")
    except Exception as e:
        raise gr.Error(f"报告生成失败：{e}") from e


def chat_respond(message, history, verdicts, model_path, rules_file):
    """聊天入口 —— 调用 LLM Agent（DESIGN.md §6）。

    Agent 修复/重检模型后会返回新模型路径与新判定，这里同步刷新
    右侧结果面板与 3D 着色模型（Capability B 的 rerun_check 联动，
    §6.2 工作流）；普通问答保持各面板不变（gr.update()）。
    返回完整消息列表（messages 格式），UI 与 API 两种路径均兼容。
    """
    # 规则配置：与「运行检查」共用同一加载逻辑（上传优先，否则默认）
    try:
        rules_path = _to_path(rules_file)
        rules = load_rules(rules_path) if rules_path else load_rules(DEFAULT_RULES)
    except (FileNotFoundError, ValueError):
        rules = load_rules(DEFAULT_RULES)

    result = agent.chat(message, history, verdicts, model_path, rules, WORK_DIR)
    new_history = list(history or []) + [
        {"role": "user", "content": message},
        {"role": "assistant", "content": result["reply"]},
    ]

    # 修复 / 重检发生了 → 刷新 3D 与右侧面板
    if result.get("verdicts") and result.get("model_path"):
        new_model = result["model_path"]
        new_verdicts = result["verdicts"]
        try:
            glb = export_colored_glb(
                new_model, new_verdicts, WORK_DIR / "model_fixed_all.glb", mode="all"
            )
        except ValueError as e:
            raise gr.Error(f"修复后 3D 模型生成失败：{e}") from e
        return (
            new_history, new_model, new_verdicts,
            _summary_html(new_verdicts),
            _results_html(new_verdicts),
            glb,
        )
    # 普通问答：面板保持不变
    return (new_history, model_path, verdicts,
            gr.update(), gr.update(), gr.update())


# ---------------------------------------------------------------------------
# UI 构建
# ---------------------------------------------------------------------------

with gr.Blocks(title="BIM 质量检查器", theme=gr.themes.Soft(), css=CSS, js=DIAG_JS) as demo:
    # 全局状态：判定结果 / 模型路径
    verdicts_state = gr.State([])
    model_path_state = gr.State(None)

    gr.Markdown(
        "# 🏗️ BIM 质量检查器\n"
        "上传 IFC 模型与规则配置，运行合规性检查（R1 属性完整性 · R2 门宽度 ≥ 900mm）。"
    )

    with gr.Row(equal_height=False):
        # ------------------------------------------------ 左栏：上传区
        # 三栏宽度比 1 : 3 : 1.5（DESIGN.md §5.1），Gradio 要求整数 scale，
        # 同比例 ×2 → 2 : 6 : 3
        with gr.Column(scale=2, min_width=260):
            ifc_file = gr.File(
                label="① 上传 IFC 模型", file_types=[".ifc"],
                height=90,
            )
            rules_file = gr.File(
                label="② 上传规则配置（可选）", file_types=[".json"],
                height=90,
            )
            run_btn = gr.Button("运行检查", variant="primary")
            model_info = gr.Markdown("请上传 IFC 模型文件，将在此显示模型信息。")
            gr.Markdown("**使用步骤**：上传模型 → 点击运行 → 查看结果 → 导出报告")

        # ------------------------------------------------ 中栏：3D 查看器
        with gr.Column(scale=6, min_width=420):
            model_3d = gr.Model3D(
                label="3D 模型（按检查结果着色）", height=520,
                value=None,
            )
            with gr.Row():
                view_mode = gr.Radio(
                    choices=["全部显示", "仅显示违规"],
                    value="全部显示", label="显示模式",
                    info="「仅显示违规」只保留 警告/违规 构件",
                )
            gr.Markdown(
                f"**颜色图例**："
                f"<span style='color:{PASS_HEX}'>■ 通过</span> &nbsp; "
                f"<span style='color:{WARN_HEX}'>■ 警告（数据缺失）</span> &nbsp; "
                f"<span style='color:{FAIL_HEX}'>■ 违规</span>",
            )

        # ------------------------------------------------ 右栏：检查结果
        with gr.Column(scale=3, min_width=320, elem_id="results-column"):
            summary = gr.HTML("检查结果将在此显示。")
            # 结果分组用原生 <details>（在 _results_html 里渲染），不用 gr.Accordion：
            # Gradio 折叠组件会把内容压缩进内部滚动，导致具体判定不显示（见 _results_html 注释）
            r_results = gr.HTML("（暂无结果）")
            with gr.Row():
                export_btn = gr.Button("导出 HTML 报告", variant="secondary")
                report_file = gr.File(label="报告下载", interactive=False)
            # 临时诊断盒（定位后删除）：实时显示右栏 DOM 状态
            gr.HTML('<div id="diag-box" style="font-size:11px;color:#6b7280;white-space:pre-wrap;font-family:monospace">（诊断等待中…）</div>')

    # ------------------------------------------------ 右下角浮动 AI 助手
    with gr.Column(elem_id="chat-panel"):
        with gr.Row():
            chat_title = gr.Markdown("🤖 **AI 质量助手**", elem_id="chat-title")
            chat_toggle = gr.Button("收起 / 展开", size="sm")
        chat_body = gr.Column(visible=False)
        with chat_body:
            chatbot = gr.Chatbot(type="messages", height=280, label="对话")
            chat_input = gr.Textbox(
                placeholder="问我：「哪些门太窄了？」",
                show_label=False, container=False,
            )
            chat_send = gr.Button("发送", variant="primary", size="sm")

    # ------------------------------------------------ 事件绑定
    ifc_file.change(on_upload_ifc, inputs=[ifc_file], outputs=[model_path_state, model_info])
    run_btn.click(
        run_check,
        inputs=[model_path_state, rules_file],
        outputs=[verdicts_state, summary, r_results, model_3d],
    )
    view_mode.change(
        on_mode_change,
        inputs=[view_mode, model_path_state, verdicts_state],
        outputs=[model_3d],
    )
    export_btn.click(
        on_export_report,
        inputs=[model_path_state, verdicts_state, rules_file],
        outputs=[report_file],
    )
    # 聊天：接入 LLM Agent（DESIGN.md §6），修复/重检后刷新各面板
    chat_toggle.click(lambda visible: gr.update(visible=not visible), inputs=[chat_body], outputs=[chat_body])
    chat_send.click(
        chat_respond,
        inputs=[chat_input, chatbot, verdicts_state, model_path_state, rules_file],
        outputs=[chatbot, model_path_state, verdicts_state, summary, r_results, model_3d],
    )
    chat_input.submit(
        chat_respond,
        inputs=[chat_input, chatbot, verdicts_state, model_path_state, rules_file],
        outputs=[chatbot, model_path_state, verdicts_state, summary, r_results, model_3d],
    )

if __name__ == "__main__":
    demo.queue()
    # 默认本地模式：share=True 会让前端 JS 从境外 S3 CDN 加载，
    # 网络不通时页面白屏；需要对外分享链接时设环境变量 GRADIO_SHARE=1。
    # 固定端口 7860（见文件头 docstring），避免重复启动时端口漂移。
    demo.launch(
        share=os.getenv("GRADIO_SHARE") == "1",
        server_port=7860,
        # 工作目录（.work/ 下的 GLB / 报告）可能不在 cwd（如从 src/ 启动），
        # Gradio 5 只允许返回 cwd / temp / allowed_paths 内的文件
        allowed_paths=[str(WORK_DIR)],
    )
