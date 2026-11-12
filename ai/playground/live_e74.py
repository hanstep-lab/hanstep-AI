"""점수 계산의 운영 경로: ai-server 계산 API(AI API) 또는 Spring 평가 API(Spring 경유)를
HTTP로 부른다.

- AI API: POST {PLAYGROUND_AI_SERVER_URL}/internal/v1/visa/e74/calculate. 편집기 프로필로 계산하고
  저장하지 않는다. DRAFT 규칙 허용 여부는 ai-server 설정(E74_INCLUDE_DRAFT_RULES)을 따른다.
- Spring 경유: POST {SPRING_INTERNAL_API_URL}/internal/v1/ai-context/users/{userId}/e74-assessments.
  MySQL에 저장된 프로필로 계산·저장한다(같은 입력이면 저장된 결과를 다시 돌려준다, reused=true).
  감점·제외 6문항은 저장하지 않으므로 요청 때 함께 보낸다.

실패는 LiveCalculateError로 바꾼다. 화면은 오류로 보여 주고, 채팅 도구는 is_error로 Claude에게
돌려준다.
테스트 화면용이라 재시도하지 않는다(한 번만 호출).
"""

from __future__ import annotations

from datetime import date
from typing import Any

import httpx
from pydantic import ValidationError

from app.visa.e74.schemas import E74Profile, LegalDeclarationIn
from app.visa.e74.service import E74CalculateResponse
from app.visa.tools import E74AssessmentToolRequest, E74AssessmentToolResponse
from playground.e74_chat import CalculateToolError

CALCULATE_PATH = "/internal/v1/visa/e74/calculate"
ASSESSMENT_PATH = "/internal/v1/ai-context/users/{user_id}/e74-assessments"
# Spring은 AI 계산 API를 불러 결과를 저장하므로 넉넉하게 둔다.
TIMEOUT_SECONDS = 30.0

# 확인할 것(화면·Claude에 그대로 보인다). Spring이 details.reason을 주면 그것을 먼저 쓴다.
_REASON_HINTS = {
    "NO_VISA_PROFILE": "이 사용자에게 저장된 비자 프로필이 없음(사용자 id 확인)",
    "PROFILE_REJECTED": "저장된 프로필이 계산기 입력 검증에 걸림",
    "RULE_SET_NOT_APPROVED": (
        "승인 전(DRAFT) 규칙이라 저장하지 않음. 로컬이면 Spring의 E74_INCLUDE_DRAFT_RULES 확인"
    ),
    "NO_APPLICABLE_RULE_SET": "기준일에 적용할 규칙 버전이 없음",
    "RULE_SET_NOT_REGISTERED": "Spring e74_rule_sets에 규칙 버전이 등록되지 않음",
}
_STATUS_HINTS = {
    401: "토큰 확인(AI API는 AI_INTERNAL_API_TOKEN, Spring은 SPRING_INTERNAL_API_KEY)",
    404: "사용자가 없음(사용자 id 확인)",
    422: "계산에 필요한 데이터가 부족함",
    502: "Spring이 AI 응답을 저장하지 못함",
    503: "계산 서버를 쓸 수 없음(ai-server·Spring이 떠 있는지 확인)",
}


class LiveCalculateError(CalculateToolError):
    """운영 경로 계산 실패(연결 실패, 서버 오류, 응답 형식 오류).

    메시지에 토큰·프로필 값을 넣지 않는다.
    """


def live_client(base_url: str, token: str, token_header: str) -> httpx.Client:
    """내부 API 클라이언트. 토큰은 헤더로만 보내고 메시지·로그에 넣지 않는다."""
    return httpx.Client(base_url=base_url, headers={token_header: token}, timeout=TIMEOUT_SECONDS)


def calculate_via_ai(
    client: httpx.Client, profile: E74Profile, as_of: date
) -> E74CalculateResponse:
    """ai-server 계산 API로 편집기 프로필을 계산한다(저장 없음)."""
    body = {
        "asOfDate": as_of.isoformat(),
        "profile": profile.model_dump(mode="json", by_alias=True),
        "scenario": None,
    }
    data = _post(client, CALCULATE_PATH, body)
    try:
        return E74CalculateResponse.model_validate(data)
    except ValidationError as exc:
        raise LiveCalculateError("AI 계산 응답 형식 오류") from exc


def assess_via_spring(
    client: httpx.Client,
    user_id: int,
    as_of: date,
    legal_declaration: LegalDeclarationIn | None,
) -> E74AssessmentToolResponse:
    """Spring 평가 API로 MySQL 프로필을 계산·저장한다. 채팅 도구와 같은 내부 API다."""
    request = E74AssessmentToolRequest(as_of_date=as_of, legal_declaration=legal_declaration)
    data = _post(
        client,
        ASSESSMENT_PATH.format(user_id=user_id),
        request.model_dump(mode="json", by_alias=True),
    )
    try:
        return E74AssessmentToolResponse.model_validate(data)
    except ValidationError as exc:
        raise LiveCalculateError("Spring 평가 응답 형식 오류") from exc


def _post(client: httpx.Client, path: str, body: dict[str, Any]) -> Any:
    try:
        response = client.post(path, json=body)
    except httpx.TimeoutException as exc:
        raise LiveCalculateError("계산 요청 시간 초과(서버가 응답하지 않음)") from exc
    except httpx.HTTPError as exc:
        raise LiveCalculateError(f"계산 서버에 연결하지 못함({type(exc).__name__})") from exc
    if response.status_code >= 400:
        raise LiveCalculateError(_failure_message(response))
    try:
        return response.json()
    except ValueError as exc:
        raise LiveCalculateError("계산 응답이 JSON이 아님") from exc


def _failure_message(response: httpx.Response) -> str:
    """상태 코드, 팀 공통 오류 코드, details.reason만 담는다(요청 값은 넣지 않음)."""
    try:
        payload = response.json()
    except ValueError:
        payload = None
    parts = [f"계산 실패: HTTP {response.status_code}"]
    reason = None
    if isinstance(payload, dict):
        if payload.get("code"):
            parts.append(str(payload["code"]))
        details = payload.get("details")
        if isinstance(details, dict) and details.get("reason"):
            reason = str(details["reason"])
            parts.append(reason)
    message = " ".join(parts)
    hint = _REASON_HINTS.get(reason or "") or _STATUS_HINTS.get(response.status_code)
    return f"{message} ({hint})" if hint else message
