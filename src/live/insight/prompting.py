from __future__ import annotations

from src.live.insight.models import KeywordConfig


def build_system_prompt() -> str:
    return (
        "你是课堂实时关键信息提取助手。"
        "请基于当前10秒转写文本和历史文本块上下文，判断是否有重要信息。"
        "输出必须是严格 JSON 对象，不得输出任何额外文本。"
        "JSON 字段必须包含："
        "important(boolean), summary(string), context_summary(string), "
        "matched_terms(string array), reason(string)。"
        "判定时以“当前10秒文本”为主，上下文只用于消歧和判断是否是同一事项的延续。"
        "只有以下情况可判定 important=true："
        "1) 当前文本直接出现测验/作业/签到等重要信号；"
        "2) 当前文本虽未直接出现关键词，但紧接着前文的重要事项，且当前文本提供了可执行细节"
        "（如页码、题号、提交方式、截止日期、签到动作或签到码）。"
        "特别地：若前1-2个文本块已经在布置作业，当前块继续说明题号/小题/解法要求（如“第一大题”“第一小题”“泰勒展开”），应判定为 important=true。"
        "如果当前文本只是寒暄、口头禅、过渡句、无实质信息，不得仅因历史出现过重要信息就判 true。"
        "matched_terms 优先填写命中的关键词/短语，不要凭空扩展；"
        "若属于“延续细节”场景且当前块未显式出现关键词，可回填最相关主关键词（如“作业”“签到”“小测”）。"
        "如果没有重要信息：important=false, summary='当前没有什么重要内容', "
        "context_summary='无重要内容'。"
        "不要输出逐字稿，不要复述过长原文，只输出概括性结论。"
    )


def build_user_prompt(
    *,
    keywords: KeywordConfig,
    current_text: str,
    context_text: str,
) -> str:
    return (
        "请结合以下规则分析：\n"
        f"{keywords.prompt_text()}\n"
        f"当前10秒转写文本：\n{current_text}\n"
        "历史文本块上下文（按时间顺序）：\n"
        f"{context_text}\n"
        "请返回严格 JSON。"
    )
