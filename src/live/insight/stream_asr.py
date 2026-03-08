from __future__ import annotations

import importlib
import re
import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

from src.live.insight.models import format_local_ts

DEFAULT_ASR_MODELS: dict[str, str] = {
    "zh": "paraformer-realtime-v2",
    "multi": "gummy-realtime-v1",
}


def resolve_default_asr_model(scene: str) -> str:
    normalized = str(scene or "zh").strip().lower()
    return DEFAULT_ASR_MODELS.get(normalized, DEFAULT_ASR_MODELS["zh"])


@dataclass
class RealtimeAsrEvent:
    global_seq: int
    provider_sentence_id: str
    ts_local: str
    text: str
    event_type: str
    is_final: bool
    start_ms: int | None
    end_ms: int | None
    model: str
    scene: str
    translation_text: str = ""

    def to_json_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "global_seq": int(self.global_seq),
            "provider_sentence_id": self.provider_sentence_id,
            "ts_local": self.ts_local,
            "text": self.text,
            "event_type": self.event_type,
            "is_final": bool(self.is_final),
            "model": self.model,
            "scene": self.scene,
            "translation_text": self.translation_text,
        }
        if self.start_ms is not None:
            payload["start_ms"] = int(self.start_ms)
        if self.end_ms is not None:
            payload["end_ms"] = int(self.end_ms)
        return payload


class DashScopeRealtimeAsrClient:
    def __init__(
        self,
        *,
        scene: str,
        model: str,
        api_key: str,
        endpoint: str,
        hotwords: list[str],
        translation_target_languages: list[str],
        on_event: Callable[[RealtimeAsrEvent], None],
        on_error: Callable[[str], None],
        log_fn: Callable[[str], None] | None = None,
    ) -> None:
        self.scene = str(scene or "zh").strip().lower() or "zh"
        self.model = str(model or "").strip() or resolve_default_asr_model(self.scene)
        self.api_key = str(api_key or "").strip()
        self.endpoint = str(endpoint or "").strip() or "wss://dashscope.aliyuncs.com/api-ws/v1/inference"
        self.hotwords = [str(item).strip() for item in list(hotwords or []) if str(item).strip()]
        self.translation_target_languages = [
            str(item).strip() for item in list(translation_target_languages or ["zh"]) if str(item).strip()
        ] or ["zh"]
        self._on_event = on_event
        self._on_error = on_error
        self._log_fn = log_fn or print
        self._lock = threading.Lock()
        self._client: Any = None
        self._seq = 0

    def start(self) -> None:
        with self._lock:
            if self._client is not None:
                return
            if not self.api_key:
                raise ValueError("DASHSCOPE_API_KEY is empty")
            dashscope = importlib.import_module("dashscope")
            dashscope.api_key = self.api_key
            dashscope.base_websocket_api_url = self.endpoint
            asr_module = importlib.import_module("dashscope.audio.asr")
            if self.scene == "multi" or self.model.startswith("gummy-"):
                self._client = self._build_multi_client(asr_module)
            else:
                self._client = self._build_recognition_client(asr_module)
            self._client.start()
            self._log_fn(
                "[rt-stream-asr] started "
                f"scene={self.scene} model={self.model} endpoint={self.endpoint} hotwords={len(self.hotwords)}"
            )

    def send_audio_frame(self, data: bytes) -> bool:
        if not data:
            return True
        with self._lock:
            client = self._client
        if client is None:
            return False
        sender = getattr(client, "send_audio_frame", None)
        if callable(sender):
            result = sender(data)
            if isinstance(result, bool):
                return result
            return True
        sender = getattr(client, "sendAudioFrame", None)
        if callable(sender):
            result = sender(data)
            if isinstance(result, bool):
                return result
            return True
        raise RuntimeError("DashScope realtime client missing send_audio_frame")

    def stop(self) -> None:
        with self._lock:
            client = self._client
            self._client = None
        if client is None:
            return
        try:
            stopper = getattr(client, "stop", None)
            if callable(stopper):
                stopper()
        except Exception:
            pass
        try:
            duplex = getattr(client, "get_duplex_api", None)
            if callable(duplex):
                api = duplex()
                closer = getattr(api, "close", None)
                if callable(closer):
                    closer(1000, "bye")
        except Exception:
            pass

    def _build_recognition_client(self, asr_module: Any) -> Any:
        recognition_cls = getattr(asr_module, "Recognition")
        callback_base = getattr(asr_module, "RecognitionCallback")
        outer = self

        class _Callback(callback_base):  # type: ignore[misc,valid-type]
            def on_open(self) -> None:
                return

            def on_close(self) -> None:
                return

            def on_complete(self) -> None:
                return

            def on_error(self, result: Any) -> None:
                msg = str(getattr(result, "message", "") or getattr(result, "error", "") or result)
                outer._on_error(msg or "dashscope recognition callback error")

            def on_event(self, result: Any) -> None:
                try:
                    outer._handle_recognition_event(result, asr_module=asr_module)
                except Exception as exc:
                    outer._on_error(f"recognition on_event failed: {exc}")

        kwargs: dict[str, Any] = {
            "model": self.model,
            "format": "pcm",
            "sample_rate": 16000,
            "callback": _Callback(),
        }
        if self.hotwords:
            kwargs["hotwords"] = self.hotwords
        return _build_instance_with_optional_kwargs(recognition_cls, kwargs)

    def _build_multi_client(self, asr_module: Any) -> Any:
        realtime_cls = getattr(asr_module, "TranslationRecognizerRealtime")
        callback_base = getattr(asr_module, "TranslationRecognizerCallback")
        outer = self

        class _Callback(callback_base):  # type: ignore[misc,valid-type]
            def on_open(self) -> None:
                return

            def on_close(self) -> None:
                return

            def on_error(self, error: Any) -> None:
                msg = str(getattr(error, "message", "") or getattr(error, "error", "") or error)
                outer._on_error(msg or "dashscope translation callback error")

            def on_event(
                self,
                request_id: Any,
                transcription_result: Any,
                translation_result: Any,
                usage: Any,
            ) -> None:
                del request_id, usage
                try:
                    outer._handle_translation_event(
                        transcription_result=transcription_result,
                        translation_result=translation_result,
                    )
                except Exception as exc:
                    outer._on_error(f"translation on_event failed: {exc}")

        kwargs: dict[str, Any] = {
            "model": self.model,
            "format": "pcm",
            "sample_rate": 16000,
            "transcription_enabled": True,
            "translation_enabled": True,
            "translation_target_languages": self.translation_target_languages,
            "callback": _Callback(),
        }
        if self.hotwords:
            kwargs["hotwords"] = self.hotwords
        return _build_instance_with_optional_kwargs(realtime_cls, kwargs)

    def _handle_recognition_event(self, result: Any, *, asr_module: Any) -> None:
        sentence = None
        getter = getattr(result, "get_sentence", None)
        if callable(getter):
            sentence = getter()
        if sentence is None:
            sentence = getattr(result, "sentence", None)
        text = _extract_sentence_text(sentence)
        if not text:
            return
        sentence_id = _extract_sentence_id(sentence)
        start_ms, end_ms = _extract_sentence_range(sentence)
        is_final = _detect_is_final_recognition(result=result, sentence=sentence, asr_module=asr_module)
        self._emit_event(
            provider_sentence_id=sentence_id,
            text=text,
            is_final=is_final,
            start_ms=start_ms,
            end_ms=end_ms,
            translation_text="",
        )

    def _handle_translation_event(self, *, transcription_result: Any, translation_result: Any) -> None:
        text = _extract_attr_text(transcription_result)
        if not text:
            return
        sentence_id = str(getattr(transcription_result, "sentence_id", "") or "").strip()
        start_ms = _to_int_or_none(
            getattr(transcription_result, "pre_end_start_time", None)
            or getattr(transcription_result, "start_time", None)
        )
        end_ms = _to_int_or_none(
            getattr(transcription_result, "pre_end_end_time", None)
            or getattr(transcription_result, "end_time", None)
        )
        is_final = bool(
            getattr(transcription_result, "is_sentence_end", False)
            or getattr(transcription_result, "sentence_end", False)
            or getattr(transcription_result, "vad_pre_end", False)
        )
        if not is_final and self.scene == "multi":
            # Gummy Python callback may not expose explicit final flag on every SDK version.
            is_final = True
        translated = _extract_translation_text(translation_result=translation_result, targets=self.translation_target_languages)
        self._emit_event(
            provider_sentence_id=sentence_id,
            text=text,
            is_final=is_final,
            start_ms=start_ms,
            end_ms=end_ms,
            translation_text=translated,
        )

    def _emit_event(
        self,
        *,
        provider_sentence_id: str,
        text: str,
        is_final: bool,
        start_ms: int | None,
        end_ms: int | None,
        translation_text: str,
    ) -> None:
        with self._lock:
            self._seq += 1
            global_seq = int(self._seq)
        ts_local = format_local_ts(datetime.now().astimezone())
        event = RealtimeAsrEvent(
            global_seq=global_seq,
            provider_sentence_id=str(provider_sentence_id or "").strip(),
            ts_local=ts_local,
            text=str(text or "").strip(),
            event_type="final" if is_final else "partial",
            is_final=bool(is_final),
            start_ms=start_ms,
            end_ms=end_ms,
            model=self.model,
            scene=self.scene,
            translation_text=str(translation_text or "").strip(),
        )
        self._on_event(event)


def _extract_attr_text(obj: Any) -> str:
    if obj is None:
        return ""
    value = getattr(obj, "text", None)
    if isinstance(value, str):
        return value.strip()
    return str(value or "").strip()


def _extract_translation_text(*, translation_result: Any, targets: list[str]) -> str:
    if translation_result is None:
        return ""
    getter = getattr(translation_result, "get_translation", None)
    if callable(getter):
        for language in list(targets or ["zh"]):
            try:
                item = getter(language)
            except Exception:
                continue
            text = _extract_attr_text(item)
            if text:
                return text
    text = _extract_attr_text(translation_result)
    return text


def _extract_sentence_text(sentence: Any) -> str:
    if isinstance(sentence, dict):
        return str(sentence.get("text", "")).strip()
    if sentence is None:
        return ""
    text = getattr(sentence, "text", "")
    if isinstance(text, str):
        return text.strip()
    return str(text or "").strip()


def _extract_sentence_id(sentence: Any) -> str:
    if isinstance(sentence, dict):
        return str(sentence.get("sentence_id") or sentence.get("id") or "").strip()
    if sentence is None:
        return ""
    return str(getattr(sentence, "sentence_id", "") or getattr(sentence, "id", "") or "").strip()


def _extract_sentence_range(sentence: Any) -> tuple[int | None, int | None]:
    if isinstance(sentence, dict):
        start = (
            sentence.get("begin_time")
            or sentence.get("start_time")
            or sentence.get("start")
            or sentence.get("start_ms")
        )
        end = (
            sentence.get("end_time")
            or sentence.get("end")
            or sentence.get("end_ms")
        )
        return _to_int_or_none(start), _to_int_or_none(end)
    if sentence is None:
        return None, None
    start = (
        getattr(sentence, "begin_time", None)
        or getattr(sentence, "start_time", None)
        or getattr(sentence, "start", None)
    )
    end = getattr(sentence, "end_time", None) or getattr(sentence, "end", None)
    return _to_int_or_none(start), _to_int_or_none(end)


def _detect_is_final_recognition(*, result: Any, sentence: Any, asr_module: Any) -> bool:
    checker_cls = getattr(asr_module, "RecognitionResult", None)
    if checker_cls is not None:
        checker = getattr(checker_cls, "is_sentence_end", None)
        if callable(checker):
            try:
                return bool(checker(sentence))
            except Exception:
                pass
    for name in ("is_sentence_end", "isSentenceEnd"):
        method = getattr(result, name, None)
        if callable(method):
            try:
                return bool(method())
            except Exception:
                continue
    if isinstance(sentence, dict):
        value = sentence.get("sentence_end")
        if isinstance(value, bool):
            return value
    return False


def _to_int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    text = str(value).strip()
    if not text:
        return None
    if re.fullmatch(r"-?\d+", text):
        try:
            return int(text)
        except ValueError:
            return None
    return None


def _build_instance_with_optional_kwargs(cls: Any, kwargs: dict[str, Any]) -> Any:
    request = dict(kwargs)
    removed: set[str] = set()
    while True:
        try:
            return cls(**request)
        except TypeError as exc:
            key = _extract_unexpected_kwarg(exc)
            if not key or key in removed or key not in request:
                raise
            removed.add(key)
            request.pop(key, None)


def _extract_unexpected_kwarg(exc: Exception) -> str:
    message = str(exc)
    match = re.search(r"unexpected keyword argument '([^']+)'", message)
    if not match:
        return ""
    return str(match.group(1) or "").strip()
