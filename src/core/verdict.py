"""Verdict 数据模型 —— 检查结果的单一数据源。

对应 DESIGN.md §7.1：UI 卡片、3D 着色、聊天上下文、HTML 报告全部
从 Verdict 列表派生，一种格式，各表面之间零漂移。
"""

from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any, Dict


class VerdictStatus(Enum):
    """三档判定结果（DESIGN.md §1.2：非二元的 pass/warn/fail，比布尔判定更有信息量）。

    - PASS: 有证据的通过（不是默认放行，见 §7.2）
    - WARN: 数据缺失，无法判定（黄色）——提示模型不完整，而非不合规
    - FAIL: 明确违规（红色）——可直接执行修复
    """

    PASS = "pass"
    WARN = "warn"
    FAIL = "fail"


@dataclass
class Verdict:
    """单条检查结论，面向人（UI/报告）和机器（JSON/聊天上下文）的通用表示。"""

    element_guid: str      # 元素 GlobalId，可追踪到 3D 模型
    element_name: str      # 元素名称（人类可读）
    ifc_type: str          # IFC 类型，如 "IfcWall"
    check_id: str          # 规则子检查 ID："R1a" / "R1b" / "R2"
    status: VerdictStatus  # 三档判定
    current_value: str     # 当前值的人类可读描述，如 "800 mm"
    expected: str          # 期望值描述，如 "≥ 900 mm"
    reason: str            # 人类可读的原因说明

    def to_dict(self) -> Dict[str, Any]:
        """序列化为 dict（JSON 报告、聊天上下文、GLB 着色均依赖）。

        status 枚举需转为字符串值（"pass"/"warn"/"fail"）。
        """
        data = asdict(self)
        data["status"] = self.status.value
        return data

    @property
    def is_pass(self) -> bool:
        """是否通过（供过滤器使用）。"""
        return self.status is VerdictStatus.PASS

    @property
    def is_warn(self) -> bool:
        """是否警告（数据缺失，无法判定）。"""
        return self.status is VerdictStatus.WARN

    @property
    def is_fail(self) -> bool:
        """是否违规（可执行修复）。"""
        return self.status is VerdictStatus.FAIL
