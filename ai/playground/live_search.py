"""채팅 탭의 '운영 경로' 검색: ai-server의 POST /internal/v1/visa/policies/search를 HTTP로 부른다.

검색과 승인 확인(MongoDB 벡터 검색 + Spring 승인 대조)은 서버가 한다. 여기서는 응답을 서버 응답
모델로 검증하고 테스트 인덱스 결과(search_payload)와 같은 모양으로 바꾼다. 실패는 SearchToolError로
바꿔 도구 오류(is_error)로 Claude에게 돌려준다. 테스트 화면용이라 재시도하지 않는다(한 번만 호출).
"""

from __future__ import annotations

import httpx
from pydantic import ValidationError

from app.api.routes.visa_policies import PolicySearchResponse
from playground.e74_chat import SearchToolError
from playground.rag_test_index import TEXT_PREVIEW_CHARS

SEARCH_PATH = "/internal/v1/visa/policies/search"
# 서버의 검색 제한(SEARCH_TIMEOUT_S 20초)보다 길게 둔다.
TIMEOUT_SECONDS = 30.0


def live_search_client(base_url: str, token: str, token_header: str) -> httpx.Client:
    """ai-server 내부 API 클라이언트. 토큰은 헤더로만 보내고 메시지·로그에 넣지 않는다."""
    return httpx.Client(base_url=base_url, headers={token_header: token}, timeout=TIMEOUT_SECONDS)


def live_search_payload(client: httpx.Client, query: str, top_k: int) -> list[dict[str, object]]:
    """운영 검색 결과를 search_payload와 같은 모양으로 돌려준다. 승인된 문서만 나온다."""
    try:
        response = client.post(SEARCH_PATH, json={"query": query, "topK": top_k})
    except httpx.TimeoutException as exc:
        raise SearchToolError("운영 검색 시간 초과(ai-server가 응답하지 않음)") from exc
    except httpx.HTTPError as exc:
        raise SearchToolError(f"운영 검색 서버에 연결하지 못함({type(exc).__name__})") from exc
    if response.status_code != httpx.codes.OK:
        raise SearchToolError(_failure_message(response))
    try:
        body = PolicySearchResponse.model_validate(response.json())
    except (ValueError, ValidationError) as exc:
        raise SearchToolError("운영 검색 응답 형식 오류") from exc
    return [
        {
            "rank": rank,
            "score": round(result.score, 4),
            "title": result.title,
            "sourceOrg": result.source_org,
            "page": result.page_number,
            "section": result.section_title,
            "url": result.source_url,
            "policyDocumentId": result.policy_document_id,
            "text": result.text[:TEXT_PREVIEW_CHARS],
        }
        for rank, result in enumerate(body.results, start=1)
    ]


def _failure_message(response: httpx.Response) -> str:
    code = ""
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if isinstance(payload, dict) and payload.get("code"):
        code = f" {payload['code']}"
    message = f"운영 검색 실패: HTTP {response.status_code}{code}"
    if response.status_code == httpx.codes.SERVICE_UNAVAILABLE:
        message += " (승인 확인 실패: Spring이 떠 있는지, 승인된 문서가 있는지 확인)"
    elif response.status_code == httpx.codes.UNAUTHORIZED:
        message += " (AI_INTERNAL_API_TOKEN 확인)"
    return message
