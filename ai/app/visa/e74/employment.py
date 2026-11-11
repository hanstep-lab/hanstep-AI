"""근무 이력 계산: 사업장명 정규화, 기간 합산(겹침 제거), "N개월 이상" 판정.

기간은 [시작일, 끝) 반열린 구간이다. 끝난 근무는 종료일 다음 날, 현재 근무는 기준일이 끝이다.
"N개월 이상"은 기준일에서 N개월 전부터 기준일까지의 일수 이상으로 본다. 하나로 이어진 근무면
달력 개월 수와 정확히 같고, 여러 구간을 합칠 때도 같은 기준을 쓴다.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, timedelta

from app.visa.e74.dates import add_months
from app.visa.e74.schemas import E74Profile, EmploymentHistoryIn

Interval = tuple[date, date]

_COMPANY_MARKERS = re.compile(r"\(주\)|㈜|주식회사|\s+")


def normalize_employer_name(name: str) -> str:
    """같은 사업장 판정용: 공백, (주), ㈜, 주식회사를 지운다."""
    return _COMPANY_MARKERS.sub("", name)


def employment_interval(row: EmploymentHistoryIn, as_of: date) -> Interval | None:
    """기준일까지 실제로 근무한 구간. 기준일 이후에 시작했으면 None."""
    end = as_of if row.ended_on is None else min(row.ended_on + timedelta(days=1), as_of)
    return (row.started_on, end) if row.started_on < end else None


def clip_interval(interval: Interval, window_start: date) -> Interval | None:
    start = max(interval[0], window_start)
    return (start, interval[1]) if start < interval[1] else None


def covered_days(intervals: Iterable[Interval]) -> int:
    """구간들이 덮는 일수. 겹치는 날은 한 번만 센다."""
    total = 0
    current_start: date | None = None
    current_end: date | None = None
    for start, end in sorted(intervals):
        if current_end is None or current_start is None or start > current_end:
            if current_end is not None and current_start is not None:
                total += (current_end - current_start).days
            current_start, current_end = start, end
        else:
            current_end = max(current_end, end)
    if current_end is not None and current_start is not None:
        total += (current_end - current_start).days
    return total


def required_days(as_of: date, months: int) -> int:
    return (as_of - add_months(as_of, -months)).days


@dataclass(frozen=True)
class CurrentStint:
    """현재 근무처 근속 계산 재료.

    same_employer: 현재 근무처와 사업장명이 같은 모든 근무(같은 업체 재고용 합산).
    previous: 현재 근무를 시작하기 전에 끝난 다른 사업장 중 가장 최근 것(직전 근무처).
    """

    current: EmploymentHistoryIn
    same_employer: tuple[EmploymentHistoryIn, ...]
    previous: EmploymentHistoryIn | None


def current_stint(profile: E74Profile) -> CurrentStint | None:
    current = profile.current_employment
    if current is None:
        return None
    key = normalize_employer_name(current.employer_name)
    same = tuple(
        row
        for row in profile.employment_histories
        if normalize_employer_name(row.employer_name) == key
    )
    earlier_others = [
        row
        for row in profile.employment_histories
        if normalize_employer_name(row.employer_name) != key
        and row.ended_on is not None
        and row.ended_on < current.started_on
    ]
    previous = max(earlier_others, key=lambda row: row.ended_on or row.started_on, default=None)
    return CurrentStint(current=current, same_employer=same, previous=previous)


def intervals_of(rows: Iterable[EmploymentHistoryIn], as_of: date) -> list[Interval]:
    return [i for row in rows if (i := employment_interval(row, as_of)) is not None]
