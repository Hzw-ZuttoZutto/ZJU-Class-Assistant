from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from src.live.insight.models import KeywordConfig

HISTORY_CONTEXT_HEADER = "========== 历史上下文区（仅供参考，禁止仅凭本区判定 important=true） =========="
HISTORY_CONTEXT_FOOTER = "========== 历史上下文区结束 =========="
CURRENT_CHUNK_FOOTER = "========== 当前待判定区结束 =========="
NO_HISTORY_CONTEXT_LINE = "[no_history] 无历史文本块"
SYSTEM_PROMPT_PLACEHOLDER = "{{CURRENT_SEGMENT_REF}}"
SYSTEM_PROMPT_LEGACY_PLACEHOLDER = "{current_segment_ref}"

_DEFAULT_SYSTEM_PROMPT_FALLBACK = (
    "你是课堂实时紧急事项提炼助手。"
    f"任务是基于“{SYSTEM_PROMPT_PLACEHOLDER}”和“最近历史上下文”，判断当前是否出现需要学生立即关注的课堂事项，"
    "并把结果整理成适合告警阅读的动作化 JSON。"
    "输出必须是严格 JSON 对象，不得输出任何额外文本。"
)
_DEFAULT_SYSTEM_PROMPT_PATH = Path(__file__).resolve().parents[3] / "config" / "realtime_system_prompt.txt"


@lru_cache(maxsize=8)
def _load_system_prompt_template_cached(path_text: str) -> str:
    path = Path(path_text)
    try:
        template = path.read_text(encoding="utf-8").strip()
    except OSError:
        return _DEFAULT_SYSTEM_PROMPT_FALLBACK
    return template or _DEFAULT_SYSTEM_PROMPT_FALLBACK


def load_system_prompt_template(path: Path | str | None = None) -> str:
    if path is None:
        resolved = _DEFAULT_SYSTEM_PROMPT_PATH
    else:
        resolved = Path(path).expanduser().resolve()
    return _load_system_prompt_template_cached(str(resolved))


def format_chunk_seconds(chunk_seconds: float | None) -> str:
    if chunk_seconds is None:
        return ""
    value = float(chunk_seconds)
    if value <= 0:
        return ""
    value = max(0.1, value)
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


def build_current_chunk_block(*, current_text: str, chunk_seconds: float | None) -> str:
    chunk_seconds_text = format_chunk_seconds(chunk_seconds)
    if chunk_seconds_text:
        header = (
            "========== 当前待判定区"
            f"（唯一主判定对象，本块时长约 {chunk_seconds_text} 秒；最终 important 只能由本区决定） =========="
        )
    else:
        header = "========== 当前待判定区（唯一主判定对象；最终 important 只能由本区决定） =========="
    body = str(current_text or "").strip() or "[empty_current_chunk] （当前块为空）"
    return "\n".join([header, body, CURRENT_CHUNK_FOOTER])


def build_system_prompt(chunk_seconds: float | None, *, template: str | None = None) -> str:
    chunk_seconds_text = format_chunk_seconds(chunk_seconds)
    current_segment_ref = f"当前{chunk_seconds_text}秒文本" if chunk_seconds_text else "当前文本段"
    prompt_template = str(template or load_system_prompt_template()).strip() or _DEFAULT_SYSTEM_PROMPT_FALLBACK
    prompt = (
        prompt_template.replace(SYSTEM_PROMPT_PLACEHOLDER, current_segment_ref)
        .replace(SYSTEM_PROMPT_LEGACY_PLACEHOLDER, current_segment_ref)
        .strip()
    )
    if current_segment_ref not in prompt:
        return f"主判定对象：{current_segment_ref}。\n{prompt}"
    return prompt


def build_user_prompt(
    *,
    keywords: KeywordConfig,
    current_text: str,
    context_text: str,
    chunk_seconds: float | None,
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
