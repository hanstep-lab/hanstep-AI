"""E-7-4 계산용 날짜 도우미: 만 나이, 개월 더하기, 기간 개월 수."""

from __future__ import annotations

import calendar
from datetime import date


def age_on(birth: date, as_of: date) -> int:
    """기준일의 만 나이. 생일 전이면 한 살 적다.

    2월 29일생은 평년에 (2, 28) < (2, 29)라 2월 28일까지 생일 전, 3월 1일부터 생일 후로 본다.
    """
    before_birthday = (as_of.month, as_of.day) < (birth.month, birth.day)
    return as_of.year - birth.year - (1 if before_birthday else 0)


def add_months(day: date, months: int) -> date:
    """개월 수를 더한다. 그 달에 같은 날이 없으면 말일로 맞춘다(1월 31일 + 1개월 = 2월 말일)."""
    years, month_index = divmod(day.month - 1 + months, 12)
    year = day.year + years
    month = month_index + 1
    return date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


def full_months_between(start: date, end: date) -> int:
    """start부터 end까지 꽉 찬 개월 수. end가 start 이전이면 0."""
    if end < start:
        return 0
    months = (end.year - start.year) * 12 + (end.month - start.month)
    if add_months(start, months) > end:
        months -= 1
    return months
