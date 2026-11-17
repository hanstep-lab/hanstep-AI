"""일시 오류 재시도 규칙(``ai/app/worker/retry.py``와 같다).

Worker는 재시도할 메시지를 retry 큐로 다시 발행하고 원본을 ack한다. 재발행 메시지에는
``x-retry-count``(몇 번째 재시도인지)와 ``expiration``(대기 ms 문자열)을 붙인다.
대기 시간 표는 설정값 하나(RETRY_DELAYS_MS)이고, 표의 길이가 최대 재시도 횟수다.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

RETRY_COUNT_HEADER = "x-retry-count"
# 브로커가 dead-letter 때 붙이는 헤더. 재발행 메시지는 새 메시지라 옮기지 않는다.
_BROKER_HEADER_PREFIXES = ("x-death", "x-first-death-", "x-last-death-")


def retry_count(headers: Mapping[str, Any] | None) -> int:
    """지금까지 재시도한 횟수. 헤더가 없거나 숫자가 아니면 0(BE가 처음 발행한 메시지)."""
    if not headers:
        return 0
    try:
        return max(int(headers.get(RETRY_COUNT_HEADER, 0)), 0)
    except (TypeError, ValueError):
        return 0


def retry_expiration(count: int, delays_ms: Sequence[int]) -> str | None:
    """``count``번 재시도한 메시지가 또 일시 오류로 실패했을 때 다음 재발행의 expiration.

    1회차 "5000", 2회차 "15000", 3회차 "45000"(기본값). 표를 다 썼으면 None(더 재시도하지 않음).
    """
    if count >= len(delays_ms):
        return None
    return str(delays_ms[count])


def retry_headers(headers: Mapping[str, Any] | None, next_count: int) -> dict[str, Any]:
    """재발행 메시지 헤더. 원본 헤더를 옮기고 재시도 횟수만 바꾼다."""
    copied = {
        key: value
        for key, value in (headers or {}).items()
        if not key.startswith(_BROKER_HEADER_PREFIXES)
    }
    copied[RETRY_COUNT_HEADER] = next_count
    return copied
