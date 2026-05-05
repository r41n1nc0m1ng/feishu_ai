"""
决策主题分类表。

DECISION_TOPICS 是唯一的真相源，修改此处即可影响分段器和卡片生成器的行为。
格式：{大方向: [小方向, ...]}
"""

DECISION_TOPICS: dict[str, list[str]] = {
    "产品规划": [
        "项目范围与MVP边界",
        "功能优先级与排期",
        "用户权限与角色",
    ],
    "用户体验": [
        "用户界面与交互设计",
        "用户流程设计",
    ],
    "技术实现": [
        "技术选型",
        "系统架构设计",
        "数据模型与存储方案",
        "接口与协议设计",
        "部署与运维方案",
        "算法与模型策略",
        "",
        "错误处理与容错策略",
        "性能与可扩展性",
    ],
    "数据与安全": [
        "数据格式与输入输出",
        "安全与隐私保护",
    ],
    "质量保障": [
        "测试与评测方案",
        "验收标准",
    ],
    "项目管理": [
        "分工与排期",
        "里程碑与交付物",
        "外部依赖与集成",
        "命名与术语规范",
        "风险管理",
    ],
    "其他": [
        "无关消息"
    ]
}


def flat_list() -> list[str]:
    """返回所有 '大方向-小方向' 组合，供 prompt 注入和 key 校验使用。"""
    return [
        f"{major}-{minor}"
        for major, minors in DECISION_TOPICS.items()
        for minor in minors
    ]


def format_for_prompt() -> str:
    """返回适合嵌入 LLM prompt 的多行文本，每行一个分类。"""
    lines = []
    for major, minors in DECISION_TOPICS.items():
        items = "、".join(minors)
        lines.append(f"  {major}：{items}")
    return "\n".join(lines)
