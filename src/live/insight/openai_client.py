from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from src.live.insight.models import KeywordConfig
from src.live.insight.prompting import build_system_prompt, build_user_prompt


class OpenAIClientUnavailableError(RuntimeError):
    pass


def _load_openai_cls():
    try:
        from openai import OpenAI  # type: ignore

        return OpenAI
    except Exception as exc:  # pragma: no cover - import error path
        raise OpenAIClientUnavailableError(
            "openai sdk is unavailable; install dependencies from requirements.txt"
        ) from exc


@dataclass
class InsightModelResult:
    important: bool
    summary: str
    context_summary: str
    matched_terms: list[str]
    reason: str
    event_type: str = ""
    headline: str = ""
    immediate_action: str = ""
    key_details: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.event_type = str(self.event_type or "").strip()
        self.headline = str(self.headline or "").strip()
        self.immediate_action = str(self.immediate_action or "").strip()
        self.key_details = _normalize_key_details(self.key_details)


class OpenAIInsightClient:
    def __init__(self, *, api_key: str, timeout_sec: float, base_url: str = "") -> None:
        if not api_key:
            raise ValueError("OPENAI_API_KEY is empty")
        openai_cls = _load_openai_cls()
        kwargs: dict[str, Any] = {
            "api_key": api_key,
            "timeout": max(2.0, float(timeout_sec)),
        }
        normalized_base_url = (base_url or "").strip()
        if normalized_base_url:
            kwargs["base_url"] = normalized_base_url
        self.client = openai_cls(**kwargs)

    def transcribe_chunk(
        self,
        *,
        chunk_path: Path,
        stt_model: str,
        timeout_sec: float,
    ) -> str:
        if not stt_model:
            raise ValueError("stt_model is empty")
        with chunk_path.open("rb") as handle:
            response = self.client.audio.transcriptions.create(
                model=stt_model,
                file=handle,
                timeout=max(1.0, float(timeout_sec)),
            )
        return _extract_transcript_text(response)

    def analyze_text(
        self,
        *,
        analysis_model: str,
        keywords: KeywordConfig,
        current_text: str,
        context_text: str,
        chunk_seconds: float,
        timeout_sec: float,
        debug_hook: Callable[[dict[str, Any]], None] | None = None,
    ) -> InsightModelResult:
        if not analysis_model:
            raise ValueError("analysis_model is empty")
        system_prompt = build_system_prompt(chunk_seconds)
        user_prompt = build_user_prompt(
            keywords=keywords,
            current_text=current_text,
            context_text=context_text,
            chunk_seconds=chunk_seconds,
        )
        request_payload = _build_analysis_request_payload(
            analysis_model=analysis_model,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            timeout_sec=timeout_sec,
        )
        parsed_payload = self._run_analysis_attempt(
            request_payload=request_payload,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            current_text=current_text,
            context_text=context_text,
            chunk_seconds=chunk_seconds,
            debug_hook=debug_hook,
        )
        return InsightModelResult(
            important=_to_bool(parsed_payload.get("important")),
            summary=str(parsed_payload.get("summary", "")).strip(),
            context_summary=str(parsed_payload.get("context_summary", "")).strip(),
            matched_terms=_to_str_list(parsed_payload.get("matched_terms")),
            reason=str(parsed_payload.get("reason", "")).strip(),
            event_type=str(parsed_payload.get("event_type", "")).strip(),
            headline=str(parsed_payload.get("headline", "")).strip(),
            immediate_action=str(parsed_payload.get("immediate_action", "")).strip(),
            key_details=_normalize_key_details(parsed_payload.get("key_details")),
        )

    def _run_analysis_attempt(
        self,
        *,
        request_payload: dict[str, Any],
        system_prompt: str,
        user_prompt: str,
        current_text: str,
        context_text: str,
        chunk_seconds: float,
        debug_hook: Callable[[dict[str, Any]], None] | None,
    ) -> dict:
        response = None
        effective_request: dict[str, Any] = dict(request_payload)
        raw_response_text = ""
        duration_sec = 0.0
        parse_error: Exception | None = None
        parsed_payload: dict | None = None
        started = time.monotonic()
        try:
            response, effective_request = self._create_analysis_response(request_payload)
            raw_response_text = _safe_extract_output_text(response)
            parsed_payload = _extract_analysis_payload(response)
        except Exception as exc:
            parse_error = exc
        duration_sec = time.monotonic() - started

        if parsed_payload is not None:
            _emit_analysis_debug(
                hook=debug_hook,
                payload={
                    "chunk_seconds": float(chunk_seconds),
                    "current_text": current_text,
                    "context_text": context_text,
                    "system_prompt": system_prompt,
                    "user_prompt": user_prompt,
                    "request_payload_snapshot": effective_request,
                    "raw_response_text": raw_response_text,
                    "parsed_ok": True,
                    "parsed_payload": parsed_payload,
                    "error": "",
                    "duration_sec": duration_sec,
                },
            )
            return parsed_payload

        if parse_error is None:
            parse_error = ValueError("analysis attempt failed with unknown error")

        _emit_analysis_debug(
            hook=debug_hook,
            payload={
                "chunk_seconds": float(chunk_seconds),
                "current_text": current_text,
                "context_text": context_text,
                "system_prompt": system_prompt,
                "user_prompt": user_prompt,
                "request_payload_snapshot": effective_request,
                "raw_response_text": raw_response_text,
                "parsed_ok": False,
                "parsed_payload": {},
                "error": str(parse_error),
                "duration_sec": duration_sec,
            },
        )

        if response is not None and _should_retry_analysis_response(response=response, error=parse_error):
            retry_payload = dict(request_payload)
            retry_payload["max_output_tokens"] = max(1600, int(request_payload.get("max_output_tokens", 1200)) * 2)
            return self._run_analysis_attempt(
                request_payload=retry_payload,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                current_text=current_text,
                context_text=context_text,
                chunk_seconds=chunk_seconds,
                debug_hook=debug_hook,
            )
        raise parse_error

    def _create_analysis_response(self, payload: dict[str, Any]) -> tuple[Any, dict[str, Any]]:
        request = dict(payload)
        request["temperature"] = 0
        removed: set[str] = set()
        value_adjusted: set[str] = set()
        for _ in range(8):
            try:
                return self.client.responses.create(**request), dict(request)
            except Exception as exc:
                unsupported = _extract_unsupported_parameter(exc)
                if unsupported:
                    key = unsupported.split(".", 1)[0]
                    if key and key in request and key not in removed:
                        removed.add(key)
                        request = dict(request)
                        request.pop(key, None)
                        continue
                adjusted_request, adjusted_key = _apply_unsupported_value_fallback(
                    request=request,
                    exc=exc,
                )
                if adjusted_request is None:
                    raise
                if adjusted_key in value_adjusted:
                    raise
                value_adjusted.add(adjusted_key)
                request = adjusted_request
        return self.client.responses.create(**request), dict(request)


def invoke_analyze_text(
    client: Any,
    *,
    analysis_model: str,
    keywords: KeywordConfig,
    current_text: str,
    context_text: str,
    chunk_seconds: float,
    timeout_sec: float,
    debug_hook: Callable[[dict[str, Any]], None] | None = None,
) -> InsightModelResult:
    request_kwargs: dict[str, Any] = {
        "analysis_model": analysis_model,
        "keywords": keywords,
        "current_text": current_text,
        "context_text": context_text,
        "chunk_seconds": chunk_seconds,
        "timeout_sec": timeout_sec,
    }
    if debug_hook is not None:
        request_kwargs["debug_hook"] = debug_hook

    current_kwargs = dict(request_kwargs)
    removed: set[str] = set()
    for _ in range(3):
        try:
            return client.analyze_text(**current_kwargs)
        except TypeError as exc:
            unsupported = _extract_unexpected_keyword(exc)
            if not unsupported or unsupported in removed or unsupported not in current_kwargs:
                raise
            removed.add(unsupported)
            current_kwargs = dict(current_kwargs)
            current_kwargs.pop(unsupported, None)
    return client.analyze_text(**current_kwargs)


def _extract_transcript_text(response: Any) -> str:
    text = ""
    if isinstance(response, dict):
        text = str(response.get("text", "")).strip()
    else:
        text = str(getattr(response, "text", "")).strip()
        if not text and hasattr(response, "model_dump"):
            dumped = response.model_dump()
            text = str(dumped.get("text", "")).strip()
    if not text:
        raise ValueError("transcription response has no text")
    return text


def _extract_output_text(response: Any) -> str:
    direct = getattr(response, "output_text", None)
    if isinstance(direct, str) and direct.strip():
        return direct.strip()

    output = getattr(response, "output", None)
    text = _extract_text_from_output(output)
    if text:
        return text

    if hasattr(response, "model_dump"):
        dumped = response.model_dump()
        text = _extract_text_from_output(dumped.get("output"))
        if text:
            return text
    raise ValueError("model response has no output_text")


def _extract_analysis_payload(response: Any) -> dict:
    output_text = _extract_output_text(response)
    return _parse_json_payload(output_text)


def _safe_extract_output_text(response: Any) -> str:
    try:
        return _extract_output_text(response)
    except Exception:
        return ""


def _extract_text_from_output(output: Any) -> str:
    if not isinstance(output, list):
        return ""
    parts: list[str] = []
    for item in output:
        content = None
        if isinstance(item, dict):
            content = item.get("content")
        else:
            content = getattr(item, "content", None)
            if content is None and hasattr(item, "model_dump"):
                content = item.model_dump().get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict):
                block_type = block.get("type")
                if block_type in {"output_text", "text"}:
                    text = str(block.get("text", "")).strip()
                    if text:
                        parts.append(text)
            else:
                block_type = getattr(block, "type", "")
                if block_type in {"output_text", "text"}:
                    text = str(getattr(block, "text", "")).strip()
                    if text:
                        parts.append(text)
    return "\n".join(parts).strip()


def _parse_json_payload(text: str) -> dict:
    raw = text.strip()
    start = raw.find("{")
    end = raw.rfind("}")
    if start != -1 and end != -1 and end > start:
        raw = raw[start : end + 1]
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"model output is not valid JSON: {text}") from exc
    if not isinstance(payload, dict):
        raise ValueError("model output JSON must be object")
    return payload


def _should_retry_analysis_response(*, response: Any, error: Exception) -> bool:
    if not _is_output_parsing_error(error):
        return False
    dumped = _safe_model_dump(response)
    if not isinstance(dumped, dict):
        return False
    status = str(dumped.get("status", "")).strip().lower()
    if status != "incomplete":
        return False
    details = dumped.get("incomplete_details")
    if not isinstance(details, dict):
        return False
    reason = str(details.get("reason", "")).strip().lower()
    return reason == "max_output_tokens"


def _is_output_parsing_error(error: Exception) -> bool:
    message = str(error).lower()
    return ("output_text" in message) or ("valid json" in message)


def _safe_model_dump(response: Any) -> dict | None:
    if isinstance(response, dict):
        return response
    if hasattr(response, "model_dump"):
        try:
            dumped = response.model_dump()
        except Exception:
            return None
        if isinstance(dumped, dict):
            return dumped
    return None


def _emit_analysis_debug(
    *,
    hook: Callable[[dict[str, Any]], None] | None,
    payload: dict[str, Any],
) -> None:
    if hook is None:
        return
    try:
        hook(payload)
    except Exception:
        return


def _to_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"true", "1", "yes", "y"}:
            return True
        if text in {"false", "0", "no", "n"}:
            return False
    if isinstance(value, int):
        return value != 0
    return False


def _to_str_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    items: list[str] = []
    for item in value:
        text = str(item).strip()
        if text:
            items.append(text)
    return items


def _normalize_key_details(value: Any) -> list[str]:
    details = _to_str_list(value)
    if len(details) <= 3:
        return details
    return details[:3]


def _extract_unexpected_keyword(exc: TypeError) -> str:
    match = re.search(r"unexpected keyword argument '([^']+)'", str(exc))
    if match is None:
        return ""
    return str(match.group(1)).strip()


def _build_analysis_request_payload(
    *,
    analysis_model: str,
    system_prompt: str,
    user_prompt: str,
    timeout_sec: float,
) -> dict[str, Any]:
    normalized_model = _normalize_model_name(analysis_model)
    request_payload: dict[str, Any] = {
        "model": analysis_model,
        "input": [
            {
                "role": "system",
                "content": [{"type": "input_text", "text": system_prompt}],
            },
            {
                "role": "user",
                "content": [{"type": "input_text", "text": user_prompt}],
            },
        ],
        "max_output_tokens": 1200,
        "timeout": max(1.0, float(timeout_sec)),
    }
    # Different model families enforce different request contracts.
    # Keep one branch for gpt-4.1 to avoid unsupported-value errors and
    # another for gpt-5 where low-verbosity + minimal reasoning is preferred.
    if _is_gpt41_family(normalized_model):
        request_payload["text"] = {"verbosity": "medium"}
        return request_payload
    request_payload["text"] = {"verbosity": "low"}
    if _is_gpt5_family(normalized_model):
        request_payload["reasoning"] = {"effort": "minimal"}
    return request_payload


def _normalize_model_name(model: str) -> str:
    return str(model or "").strip().lower()


def _is_gpt5_family(model: str) -> bool:
    return model.startswith("gpt-5")


def _is_gpt41_family(model: str) -> bool:
    return model.startswith("gpt-4.1")


def _apply_unsupported_value_fallback(*, request: dict[str, Any], exc: Exception) -> tuple[dict[str, Any] | None, str]:
    unsupported_value, supported_values = _extract_unsupported_value_info(exc)
    if not unsupported_value or not supported_values:
        return None, ""

    adjusted = _replace_nested_value_if_matches(
        request=request,
        nested_path=("text", "verbosity"),
        unsupported_value=unsupported_value,
        supported_values=supported_values,
    )
    if adjusted is not None:
        return adjusted, "text.verbosity"

    adjusted = _replace_nested_value_if_matches(
        request=request,
        nested_path=("reasoning", "effort"),
        unsupported_value=unsupported_value,
        supported_values=supported_values,
    )
    if adjusted is not None:
        return adjusted, "reasoning.effort"
    return None, ""


def _replace_nested_value_if_matches(
    *,
    request: dict[str, Any],
    nested_path: tuple[str, str],
    unsupported_value: str,
    supported_values: list[str],
) -> dict[str, Any] | None:
    top_key, leaf_key = nested_path
    node = request.get(top_key)
    if not isinstance(node, dict):
        return None
    current = str(node.get(leaf_key, "")).strip().lower()
    if current != unsupported_value:
        return None
    new_value = supported_values[0]
    updated_node = dict(node)
    updated_node[leaf_key] = new_value
    updated_request = dict(request)
    updated_request[top_key] = updated_node
    return updated_request


def _extract_unsupported_value_info(exc: Exception) -> tuple[str, list[str]]:
    message = str(exc)
    unsupported_match = re.search(
        r"unsupported value:\s*['\"]?([a-zA-Z0-9_.-]+)['\"]?",
        message,
        flags=re.IGNORECASE,
    )
    if not unsupported_match:
        return "", []
    unsupported_value = str(unsupported_match.group(1)).strip().lower()
    supported_match = re.search(
        r"supported values are:\s*([^\n]+)",
        message,
        flags=re.IGNORECASE,
    )
    if not supported_match:
        return unsupported_value, []
    segment = str(supported_match.group(1)).strip().split("(", 1)[0].strip()
    quoted = re.findall(r"['\"]([^'\"]+)['\"]", segment)
    raw_values = quoted if quoted else re.split(r"[,/ ]+", segment)
    values: list[str] = []
    for item in raw_values:
        normalized = str(item).strip().strip(".").lower()
        if normalized and normalized not in values:
            values.append(normalized)
    return unsupported_value, values


def _is_temperature_unsupported_error(exc: Exception) -> bool:
    return _extract_unsupported_parameter(exc) == "temperature"


def _extract_unsupported_parameter(exc: Exception) -> str:
    message = str(exc)
    patterns = [
        r"unsupported parameter:\s*['\"]?([a-zA-Z0-9_.-]+)['\"]?",
        r"unsupported parameter\s+['\"]?([a-zA-Z0-9_.-]+)['\"]?",
    ]
    for pattern in patterns:
        match = re.search(pattern, message, flags=re.IGNORECASE)
        if match:
            return str(match.group(1)).strip().lower()
    return ""
