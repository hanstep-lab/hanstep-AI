"""챗봇 금융 정책 검색 도구 ``search_finance_policy``(이슈 #88).

해외송금·통장 관련 규정(외국환거래규정·외국환거래법·시행령·전자금융거래법·외국인고용법)과
금융감독원 가이드북·보도자료를 검색해 현행 조문만 돌려준다. 프롬프트 v4.2에서만 붙는다.

- 검색어는 LLM이 한국어로 쓴다(도구 설명에 명시). 한글이 없으면 검색은 dense만 쓴다.
- 결과에는 문서 제목·번호·조문 번호(가이드북·보도자료는 쪽)·시행일(또는 발행일)·원문 인용만 준다.
- sources는 비운다(결정 2026-11-17: 새 계약 0, 답변 본문에 조문·시행일 인용).
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.chat.tools import UNAVAILABLE, ToolOutcome
from app.finance_policy.search import PolicyHit

SEARCH_FINANCE_POLICY = "search_finance_policy"
# 도구가 돌려주는 청크 수: 2단계 핵심 사실 포함@3·@5·@8에서 더 오르지 않는 가장 작은 값
# (설계 문서 15절).
RESULT_K = 8

TOOL_SCHEMA: dict[str, Any] = {
    "name": SEARCH_FINANCE_POLICY,
    "description": (
        "Search the official Korean rules on sending money abroad and on bank accounts: the "
        "Foreign Exchange Transaction Regulation, the Foreign Exchange Transactions Act and its "
        "Enforcement Decree, the Electronic Financial Transactions Act, the Act on the Employment "
        "of Foreign Workers, and FSS guides. Use it for remittance limits, required documents, "
        "designated foreign exchange banks, resident or non-resident status, small-sum remittance "
        "apps, departure guarantee insurance payouts, and lending, selling or renting out bank "
        "accounts or cards. Write queryKo in Korean, as a short search phrase with the legal "
        "terms if you know them (for example 외국인거주자 지급 한도, 접근매체 대여). Returns only "
        "the rules in force now: document, article number (or guide page), effective date and "
        "the original text. Not for visa rules, fees, exchange rates or tax."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "queryKo": {
                "type": "string",
                "description": "Korean search phrase, e.g. 외국인거주자 증빙서류 없이 송금 한도.",
            }
        },
        "required": ["queryKo"],
        "additionalProperties": False,
    },
}


class SearchInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    queryKo: str = Field(min_length=1, max_length=200)  # noqa: N815 - 도구 인자 이름 그대로


class PolicySearcher(Protocol):
    def search(self, query: str, k: int) -> tuple[list[PolicyHit], str]: ...


class FinancePolicyTool:
    """요청 하나에 묶인다. 검색기가 없으면(문서 폴더·모델 미설정) FAILED SEARCH_NOT_CONFIGURED."""

    def __init__(
        self, searcher: PolicySearcher | None, *, timeout: float, k: int = RESULT_K
    ) -> None:
        self._searcher = searcher
        self._timeout = timeout
        self._k = k

    @property
    def schemas(self) -> list[dict[str, Any]]:
        return [TOOL_SCHEMA]

    async def run(self, name: str, arguments: dict[str, Any]) -> ToolOutcome:
        started = time.perf_counter()

        def outcome(status: str, content: Any, **extra: Any) -> ToolOutcome:
            text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
            latency = round((time.perf_counter() - started) * 1000)
            return ToolOutcome(name, arguments, status, text, latency, **extra)

        if name != SEARCH_FINANCE_POLICY:
            return outcome("REJECTED", "unknown tool", error_code="UNKNOWN_TOOL")
        try:
            parsed = SearchInput.model_validate(arguments)
        except ValidationError:
            return outcome("REJECTED", "invalid arguments: queryKo", error_code="INVALID_ARGUMENTS")
        if self._searcher is None:
            return outcome("FAILED", UNAVAILABLE, error_code="SEARCH_NOT_CONFIGURED")
        query = parsed.queryKo.strip()
        try:
            hits, mode = await asyncio.wait_for(
                asyncio.to_thread(self._searcher.search, query, self._k), self._timeout
            )
        except TimeoutError:
            return outcome("TIMEOUT", UNAVAILABLE, error_code="SEARCH_TIMEOUT")
        except Exception:  # 검색 실패는 답 없이 알린다
            return outcome("FAILED", UNAVAILABLE, error_code="SEARCH_ERROR")
        content: dict[str, Any] = {"results": [h.as_result() for h in hits]}
        if not hits:
            content["note"] = "No matching rule in the documents."
        # 측정 전용: 검색어·방식·청크와 LLM에 준 결과(측정 스크립트가 근거 숫자로 쓴다).
        measurement = {
            "query": query,
            "mode": mode,
            "chunkIds": [h.chunk_id for h in hits],
            "content": content,
        }
        return outcome("SUCCEEDED", content, measurement=measurement)
