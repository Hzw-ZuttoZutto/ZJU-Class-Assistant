from __future__ import annotations

import re


def redact_dingtalk_error(error: Exception) -> str:
    return re.sub(
        r"(?i)(access_token|sign|timestamp)=([^&\s\\'\"<>]*)",
        r"\1=[REDACTED]",
        str(error),
    )
