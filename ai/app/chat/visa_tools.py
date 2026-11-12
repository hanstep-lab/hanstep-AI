"""챗봇 비자 도구: 정책 검색(search_visa_policies)과 E-7-4 점수(calculate_e74_score).

입력·출력 형식은 app.visa.tools(#22)를 따른다.
- 사용자 id와 요청 id는 요청마다 고정된다. LLM 인자로는 다른 사용자를 지정할 수 없다.
- 점수: Spring 내부 API가 MySQL 프로필로 계산·저장하고 행 id를 준다. 결과 참조·근거·화면 카드는
  e74_records로 만든다(카드 숫자는 도구 결과에서만).
- 검색: 운영 검색(SearchService: MongoDB 벡터 검색 + Spring 승인 확인). 근거는 실제 검색 결과의
  문서·청크 id만 쓴다.
- 채팅 기록(agent.tool_call의 arguments)에는 recorded_arguments를 거친 값만 남긴다(감점 6문항 답
  값 제외). 도구 실행에는 원래 인자를 쓴다.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable
from datetime import date, datetime
from typing import Any, Protocol
from zoneinfo import ZoneInfo

import httpx
from pydantic import ValidationError

from app.chat.tools import UNAVAILABLE, ToolOutcome
from app.integrations.spring_client import SpringApiError, SpringInternalClient
from app.rag.errors import ValidationFailedError
from app.rag.retrieval import SearchHit, SearchService, SearchUnavailableError
from app.visa.tools import (
    CALCULATE_E74_SCORE,
    SEARCH_VISA_POLICIES,
    TOOL_INPUTS,
    CalculateE74ScoreInput,
    E74AssessmentToolRequest,
    E74AssessmentToolResponse,
    SearchVisaPoliciesInput,
    VisaToolRecords,
    e74_records,
    policy_search_records,
    recorded_arguments,
    tool_definitions,
)

logger = logging.getLogger(__name__)

SEOUL = ZoneInfo("Asia/Seoul")
VISA_TOOL_NAMES = (SEARCH_VISA_POLICIES, CALCULATE_E74_SCORE)
# 상태·LLM에 줄 내용(+ 결과 참조·근거·카드)으로 ToolOutcome을 만든다. 지연 시간은 이 함수가 잰다.
OutcomeFn = Callable[..., ToolOutcome]

# Spring 실패 이유별로 LLM에 알려 줄 문장. 그 밖의 실패는 UNAVAILABLE.
E74_FAILURES = {
    "NO_VISA_PROFILE": (
        "no saved visa profile for this user: they need to register their visa information "
        "(alien registration card) in the app first"
    ),
    "RULE_SET_NOT_APPROVED": "E-7-4 scoring rules are not approved yet, so no score is available",
    "NO_APPLICABLE_RULE_SET": "no E-7-4 scoring rules apply to this date",
}


class E74AssessmentSource(Protocol):
    """E-7-4 평가를 만들어(또는 저장된 것을 재사용해) 돌려주는 곳."""

    async def assess(
        self, user_id: int, request: E74AssessmentToolRequest, request_id: str | None
    ) -> E74AssessmentToolResponse: ...


class SpringE74Source:
    """Spring POST /internal/v1/ai-context/users/{userId}/e74-assessments."""

    def __init__(self, client: SpringInternalClient, timeout: float) -> None:
        self._client = client
        self._timeout = timeout

    async def assess(
        self, user_id: int, request: E74AssessmentToolRequest, request_id: str | None
    ) -> E74AssessmentToolResponse:
        data = await asyncio.to_thread(
            self._client.e74_assessment,
            user_id,
            request.model_dump(mode="json", by_alias=True),
            timeout=self._timeout,
            request_id=request_id,
        )
        return E74AssessmentToolResponse.model_validate(data)


class PolicySearchSource(Protocol):
    """승인된 정책 문서 청크를 검색하는 곳."""

    async def search(
        self, query: str, top_k: int, visa_types: list[str] | None
    ) -> list[SearchHit]: ...


class ServicePolicySearch:
    """SearchService(동기: 임베딩·MongoDB·Spring 승인 확인)를 스레드에서 돌린다."""

    def __init__(self, service: SearchService) -> None:
        self._service = service

    async def search(self, query: str, top_k: int, visa_types: list[str] | None) -> list[SearchHit]:
        return await asyncio.to_thread(self._service.search, query, top_k, visa_types)


def kst_today() -> date:
    return datetime.now(SEOUL).date()


class VisaTools:
    """요청 하나에 묶인 비자 도구. 설정이 없는 쪽(source가 None)은 실행 시 FAILED로 답한다."""

    def __init__(
        self,
        *,
        e74_source: E74AssessmentSource | None,
        search_source: PolicySearchSource | None,
        user_id: int,
        request_id: str | None,
        timeout: float,
    ) -> None:
        self._e74 = e74_source
        self._search = search_source
        self._user_id = user_id
        self._request_id = request_id
        self._timeout = timeout

    @property
    def schemas(self) -> list[dict[str, Any]]:
        return [d for d in tool_definitions() if d["name"] in VISA_TOOL_NAMES]

    async def run(self, name: str, arguments: dict[str, Any]) -> ToolOutcome:
        started = time.perf_counter()
        recorded = recorded_arguments(arguments)

        def outcome(status: str, content: Any, **extra: Any) -> ToolOutcome:
            text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
            latency = round((time.perf_counter() - started) * 1000)
            return ToolOutcome(name, recorded, status, text, latency, **extra)

        model = TOOL_INPUTS.get(name) if name in VISA_TOOL_NAMES else None
        if model is None:
            return outcome("REJECTED", "unknown tool", error_code="UNKNOWN_TOOL")
        try:
            parsed = model.model_validate(arguments)
        except ValidationError as exc:
            # 어느 인자가 틀렸는지만 알려 준다(값은 넣지 않음). LLM이 고쳐서 다시 부를 수 있다.
            fields = [".".join(str(p) for p in e["loc"]) + f" ({e['type']})" for e in exc.errors()]
            return outcome(
                "REJECTED",
                f"invalid arguments: {', '.join(fields)}",
                error_code="INVALID_ARGUMENTS",
            )
        if isinstance(parsed, CalculateE74ScoreInput):
            return await self._calculate(parsed, outcome)
        assert isinstance(parsed, SearchVisaPoliciesInput)
        return await self._search_policies(parsed, outcome)

    async def _calculate(self, parsed: CalculateE74ScoreInput, outcome: OutcomeFn) -> ToolOutcome:
        if self._e74 is None:
            return outcome("FAILED", UNAVAILABLE, error_code="SPRING_NOT_CONFIGURED")
        request = E74AssessmentToolRequest(
            as_of_date=parsed.as_of_date or kst_today(),
            scenario=parsed.scenario,
            legal_declaration=parsed.legal_declaration,
        )
        try:
            response = await asyncio.wait_for(
                self._e74.assess(self._user_id, request, self._request_id), self._timeout
            )
        except (TimeoutError, httpx.TimeoutException):
            return outcome("TIMEOUT", UNAVAILABLE, error_code="SPRING_TIMEOUT")
        except SpringApiError as exc:
            message = E74_FAILURES.get(exc.reason or "", UNAVAILABLE)
            return outcome("FAILED", message, error_code=f"SPRING_HTTP_{exc.status}")
        except ValidationError:
            return outcome("FAILED", UNAVAILABLE, error_code="INVALID_RESPONSE")
        except Exception as exc:  # 연결 실패 등: 점수 없이 답하게 한다
            logger.warning("e74 assessment failed: %s", type(exc).__name__)
            return outcome("FAILED", UNAVAILABLE, error_code="SPRING_ERROR")
        return _succeeded(outcome, e74_records(response))

    async def _search_policies(
        self, parsed: SearchVisaPoliciesInput, outcome: OutcomeFn
    ) -> ToolOutcome:
        if self._search is None:
            return outcome("FAILED", UNAVAILABLE, error_code="SEARCH_NOT_CONFIGURED")
        try:
            hits = await asyncio.wait_for(
                self._search.search(parsed.query, parsed.top_k, parsed.visa_types),
                self._timeout,
            )
        except TimeoutError:
            return outcome("TIMEOUT", UNAVAILABLE, error_code="SEARCH_TIMEOUT")
        except ValidationFailedError as exc:
            return outcome("REJECTED", f"invalid arguments: {exc}", error_code="INVALID_ARGUMENTS")
        except SearchUnavailableError:
            # fail-closed: 승인 상태를 확인하지 못하면 결과를 쓰지 않는다.
            return outcome("FAILED", UNAVAILABLE, error_code="SEARCH_UNAVAILABLE")
        except Exception as exc:
            logger.warning("policy search failed: %s", type(exc).__name__)
            return outcome("FAILED", UNAVAILABLE, error_code="SEARCH_ERROR")
        return _succeeded(outcome, policy_search_records(hits))


def _succeeded(outcome: OutcomeFn, records: VisaToolRecords) -> ToolOutcome:
    data = records.model_dump(mode="json", by_alias=True)
    return outcome(
        "SUCCEEDED",
        data["llmOutput"],
        result_ref=data["resultRef"],
        sources=data["sources"],
        blocks=data["blocks"],
    )
