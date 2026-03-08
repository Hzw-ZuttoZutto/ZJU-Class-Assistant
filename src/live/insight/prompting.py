from __future__ import annotations

from src.live.insight.models import KeywordConfig

HISTORY_CONTEXT_HEADER = "========== 历史上下文区（仅供参考，禁止仅凭本区判定 important=true） =========="
HISTORY_CONTEXT_FOOTER = "========== 历史上下文区结束 =========="
CURRENT_CHUNK_FOOTER = "========== 当前待判定区结束 =========="
NO_HISTORY_CONTEXT_LINE = "[no_history] 无历史文本块"


def format_chunk_seconds(chunk_seconds: float) -> str:
    value = max(0.1, float(chunk_seconds))
    rounded = f"{value:.3f}".rstrip("0").rstrip(".")
    return rounded or "0.1"


def build_history_context_block(context_text: str) -> str:
    body = str(context_text or "").strip() or NO_HISTORY_CONTEXT_LINE
    if body.startswith(HISTORY_CONTEXT_HEADER):
        return body
    return "\n".join(
        [
            HISTORY_CONTEXT_HEADER,
            body,
            HISTORY_CONTEXT_FOOTER,
        ]
    )


def build_current_chunk_block(*, current_text: str, chunk_seconds: float) -> str:
    chunk_seconds_text = format_chunk_seconds(chunk_seconds)
    header = (
        "========== 当前待判定区"
        f"（唯一主判定对象，本块时长约 {chunk_seconds_text} 秒；最终 important 只能由本区决定） =========="
    )
    body = str(current_text or "").strip() or "[empty_current_chunk] （当前块为空）"
    return "\n".join([header, body, CURRENT_CHUNK_FOOTER])


def build_system_prompt(chunk_seconds: float) -> str:
    chunk_seconds_text = format_chunk_seconds(chunk_seconds)
    return (
        "你是课堂实时紧急事项提炼助手。"
        f"任务是基于“当前{chunk_seconds_text}秒文本”和“最近历史上下文”，判断当前是否出现需要学生立即关注的课堂事项，"
        "并把结果整理成适合告警阅读的动作化 JSON。"
        "输出必须是严格 JSON 对象，不得输出任何额外文本。"
        "JSON 必须包含字段："
        "important(boolean), summary(string), context_summary(string), matched_terms(string array), "
        "reason(string), event_type(string), headline(string), immediate_action(string), key_details(string array)。"
        f"判定时必须以“当前{chunk_seconds_text}秒文本”为唯一主判定对象；"
        "历史上下文只允许用于消歧、识别是否为同一事项的延续，以及补足紧邻上下文中的细节。"
        "关键词规则分为事件分组、别名、典型短语、延续细节线索和负向词。"
        "这些规则只是提示，不是硬触发器；不能因为命中了某个词就机械判定 important=true。"
        "历史上下文区绝对不能单独触发 important=true；如果当前块本身没有重要信号或明确可执行细节，就必须返回 important=false。"
        "detail_cues 只能在“前文已经明确是同一重要事项，当前块在补充可执行细节”时增强判断，"
        "不能单独把普通讲课、寒暄、口头禅、举例、闲聊判成紧急。"
        "只有以下情况可判定 important=true："
        "1) 当前块直接出现签到、作业、测验、重要通知等明确重要信号；"
        "2) 当前块虽然未直接重复主关键词，但紧接着前文同一重要事项，并补充了明确的可执行细节，"
        "例如题号、小题、截止时间、提交方式、签到码、签到链接、二维码、打开手机等。"
        "若前1-2个文本块已经在布置作业，当前块继续说明题号/小题/要求，也应判定为 important=true。"
        "如果当前块只是泛泛续讲、背景说明、口头禅、过渡句，或只有模糊提及而没有形成明确可执行事项，即使历史块出现过重要事项，也必须判定为 important=false。"
        "headline 必须是一句短动作标题，优先直接命令式，例如“立即签到”“记录作业要求”“准备随堂测验”。"
        "immediate_action 必须是一句现在就该做的动作说明。"
        "key_details 只保留 0-3 条最关键细节，必须来自当前块或紧邻上下文，不得编造，不要写成长段。"
        "matched_terms 优先填写真实命中的关键词/短语；若属于延续细节场景且当前块未显式出现主关键词，"
        "可回填最相关的主关键词。"
        "summary 要概括“当前最紧急的事”；context_summary 要用人话解释判断依据或关键背景，不要写技术标签。"
        "reason 保持简短机器可读，例如 keyword_hit / continuation_detail / none。"
        "如果没有重要信息，返回：important=false, summary='当前没有什么重要内容', context_summary='无重要内容', "
        "matched_terms=[], reason='none', event_type='none', headline='', immediate_action='', key_details=[]."
        "不要输出逐字稿，不要复述过长原文，只输出概括性结论。"
    )


def build_user_prompt(
    *,
    keywords: KeywordConfig,
    current_text: str,
    context_text: str,
    chunk_seconds: float,
) -> str:
    history_block = build_history_context_block(context_text)
    current_block = build_current_chunk_block(current_text=current_text, chunk_seconds=chunk_seconds)
    return (
        "请结合以下规则分析：\n"
        f"{keywords.prompt_text()}\n"
        "请严格遵守以下输入分区规则：\n"
        "1. 只能对“当前待判定区”做最终 important 判断。\n"
        "2. “历史上下文区”只能用于消歧，或确认当前区是否出现明确可执行细节延续。\n"
        "3. 如果当前区没有直接重要信号，也没有明确细节延续，就必须返回 important=false。\n\n"
        f"{history_block}\n\n"
        f"{current_block}\n\n"
        "请先判断是否存在当前最紧急事项，再输出动作化 JSON。"
    )
