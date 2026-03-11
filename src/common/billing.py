from __future__ import annotations

import threading
import time
from dataclasses import dataclass

BILLING_ALERT_COOLDOWN_SEC = 120.0

_ALIYUN_BILLING_URL = "https://billing-cost.console.aliyun.com/"

_SERVICE_DISPLAY_NAMES: dict[str, str] = {
    "openai": "OpenAI",
    "aihubmix": "AIHubMix",
    "dashscope": "DashScope",
    "tingwu": "通义听悟",
    "oss": "阿里云 OSS",
}

_SERVICE_PAYMENT_URLS: dict[str, str] = {
    "openai": "https://platform.openai.com/settings/organization/billing/overview",
    "aihubmix": "https://aihubmix.com/topup",
    "dashscope": _ALIYUN_BILLING_URL,
    "tingwu": _ALIYUN_BILLING_URL,
    "oss": _ALIYUN_BILLING_URL,
}

_SERVICE_SIGNALS: dict[str, tuple[str, ...]] = {
    "openai": (
        "insufficient_quota",
        "exceeded your current quota",
        "billing hard limit",
    ),
    "aihubmix": (
        "insufficient_user_quota",
        "insufficient balance",
    ),
    "dashscope": (
        "arrearage",
        "access denied, please make sure your account is in good standing",
    ),
    "tingwu": (
        "brk.overduetenant",
        "brk.invalidtenant",
        "service status is overdue",
    ),
    "oss": (
        "0003-00000806",
        "the operation is not valid for the user account in the current billing state",
        "accountarrearage",
    ),
}


@dataclass(frozen=True)
class BillingIssue:
    service_key: str
    display_name: str
    matched_signal: str
    payment_url: str

    @property
    def reason_code(self) -> str:
        return f"billing_arrears_{self.service_key}"


class BillingAlertCooldown:
    def __init__(self, cooldown_sec: float = BILLING_ALERT_COOLDOWN_SEC) -> None:
        self.cooldown_sec = max(0.0, float(cooldown_sec))
        self._lock = threading.Lock()
        self._last_sent_mono: dict[str, float] = {}

    def consume(self, *, service_key: str, now_mono: float | None = None) -> tuple[bool, float]:
        key = str(service_key or "").strip().lower()
        if not key:
            return False, 0.0
        now = time.monotonic() if now_mono is None else float(now_mono)
        with self._lock:
            last = float(self._last_sent_mono.get(key, 0.0))
            if self.cooldown_sec > 0 and last > 0:
                elapsed = now - last
                if elapsed < self.cooldown_sec:
                    return False, max(0.0, self.cooldown_sec - elapsed)
            self._last_sent_mono[key] = now
            return True, 0.0

    def clear(self) -> None:
        with self._lock:
            self._last_sent_mono.clear()


_GLOBAL_BILLING_COOLDOWN = BillingAlertCooldown(BILLING_ALERT_COOLDOWN_SEC)


def detect_billing_issue(
    *,
    service_hint: str = "",
    error_text: str = "",
    api_base_url: str = "",
) -> BillingIssue | None:
    normalized_hint = _normalize_service_hint(service_hint, api_base_url=api_base_url)
    compact = " ".join(str(error_text or "").split())
    low = compact.lower()
    if not low:
        return None

    candidate_keys: list[str] = []
    if normalized_hint:
        candidate_keys.append(normalized_hint)
    for key in ("openai", "aihubmix", "dashscope", "tingwu", "oss"):
        if key not in candidate_keys:
            candidate_keys.append(key)

    for key in candidate_keys:
        for signal in _SERVICE_SIGNALS.get(key, ()):
            if signal.lower() in low:
                return BillingIssue(
                    service_key=key,
                    display_name=_SERVICE_DISPLAY_NAMES.get(key, key),
                    matched_signal=signal,
                    payment_url=_SERVICE_PAYMENT_URLS.get(key, _ALIYUN_BILLING_URL),
                )
    return None


def consume_billing_alert_cooldown(service_key: str) -> tuple[bool, float]:
    return _GLOBAL_BILLING_COOLDOWN.consume(service_key=service_key)


def reset_billing_alert_cooldown_for_tests() -> None:
    _GLOBAL_BILLING_COOLDOWN.clear()


def _normalize_service_hint(service_hint: str, *, api_base_url: str = "") -> str:
    text = str(service_hint or "").strip().lower()
    if text in _SERVICE_SIGNALS:
        return text
    if text in {"openai_compatible", "openai-compatible"}:
        text = ""

    base = str(api_base_url or "").strip().lower()
    if "aihubmix.com" in base:
        return "aihubmix"
    if "api.openai.com" in base or "openai.com" in base:
        return "openai"
    return text if text in _SERVICE_SIGNALS else ""
