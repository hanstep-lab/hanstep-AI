"""점수 계산 운영 경로(playground/live_e74.py). HTTP는 httpx.MockTransport로 바꾼다."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.visa.e74.schemas import E74CalculateRequest
from app.visa.e74.service import calculate
from app.visa.tools import E74AssessmentToolResponse
from playground.e74_chat import ToolContext, execute_tool
from playground.live_e74 import (
    ASSESSMENT_PATH,
    CALCULATE_PATH,
    LiveCalculateError,
    assess_via_spring,
    calculate_via_ai,
)
from tests.unit._e74_builders import profile
from tests.unit._e74_cases import profile_json

pytestmark = pytest.mark.unit

TOKEN = "test-internal-token-0123456789abcdef"
AS_OF = date(2026, 11, 12)
APP = Path(__file__).resolve().parents[2] / "playground" / "e74_app.py"


def _engine_response(profile_id: str = "P05") -> dict[str, Any]:
    """AI 계산 API 응답(실제 엔진 결과, 합성 프로필)."""
    request = E74CalculateRequest.model_validate(
        {"asOfDate": AS_OF.isoformat(), "profile": profile_json(profile_id)}
    )
    data: dict[str, Any] = calculate(request, include_draft=True).model_dump(
        mode="json", by_alias=True
    )
    return data


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.Client:
    return httpx.Client(
        base_url="http://server.test",
        headers={"X-Internal-Token": TOKEN},
        transport=httpx.MockTransport(handler),
    )


def test_ai_api_sends_editor_profile_and_parses_engine_result() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(
            path=request.url.path,
            token=request.headers["X-Internal-Token"],
            body=json.loads(request.content),
        )
        return httpx.Response(200, json=_engine_response())

    result = calculate_via_ai(_client(handler), profile(profile_json("P05")), AS_OF)

    assert seen["path"] == CALCULATE_PATH and seen["token"] == TOKEN
    assert seen["body"]["asOfDate"] == "2026-11-12" and seen["body"]["scenario"] is None
    assert seen["body"]["profile"]["visaProfile"]["visaTypeCode"] == "E-9"
    # 필리핀 테스트 사용자(P05): 고용기업 추천서 미제출
    assert result.status.value == "NEEDS_EVIDENCE"
    assert (result.score.total, result.potential_score.total) == (235, 285)


def test_spring_path_sends_user_id_in_path_and_legal_answers_in_body() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(path=request.url.path, body=json.loads(request.content))
        return httpx.Response(
            201, json={"assessmentId": 74, "reused": False, "result": _engine_response()}
        )

    declaration = profile(profile_json("P05")).legal_declaration
    saved = assess_via_spring(_client(handler), 185, AS_OF, declaration)

    assert seen["path"] == ASSESSMENT_PATH.format(user_id=185)
    assert seen["body"]["asOfDate"] == "2026-11-12"
    assert seen["body"]["scenario"] is None
    assert seen["body"]["legalDeclaration"]["finesUnder1mCount"] == 0
    assert (saved.assessment_id, saved.reused) == (74, False)
    assert saved.result.score.total == 235


@pytest.mark.parametrize(
    ("status", "payload", "expected"),
    [
        (401, {"code": "UNAUTHORIZED"}, "토큰 확인"),
        (404, {"code": "RESOURCE_NOT_FOUND"}, "사용자가 없음"),
        (
            409,
            {"code": "INVALID_STATE", "details": {"reason": "RULE_SET_NOT_APPROVED"}},
            "승인 전(DRAFT) 규칙",
        ),
        # 같은 422라도 Spring이 준 이유에 맞는 안내를 붙인다.
        (
            422,
            {"code": "INSUFFICIENT_DATA", "details": {"reason": "NO_VISA_PROFILE"}},
            "비자 프로필이 없음(사용자 id 확인)",
        ),
        (
            422,
            {"code": "INSUFFICIENT_DATA", "details": {"reason": "PROFILE_REJECTED"}},
            "입력 검증",
        ),
        (422, {"code": "INSUFFICIENT_DATA"}, "데이터가 부족"),
        # Spring 502는 대부분 Spring→ai-server 호출 실패다.
        (502, {"code": "AI_UPSTREAM_ERROR"}, "ai-server 계산 결과를 받지 못함"),
        (500, {"code": "INTERNAL_ERROR"}, "HTTP 500"),
    ],
)
def test_error_status_becomes_live_calculate_error(
    status: int, payload: dict[str, Any], expected: str
) -> None:
    client = _client(lambda _: httpx.Response(status, json=payload))

    with pytest.raises(LiveCalculateError) as info:
        assess_via_spring(client, 185, AS_OF, None)

    assert expected in str(info.value)
    assert TOKEN not in str(info.value)


def test_connection_failure_and_bad_body_are_errors() -> None:
    def refuse(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    with pytest.raises(LiveCalculateError, match="연결하지 못함"):
        calculate_via_ai(_client(refuse), profile(profile_json("P05")), AS_OF)
    with pytest.raises(LiveCalculateError, match="JSON이 아님"):
        calculate_via_ai(
            _client(lambda _: httpx.Response(200, text="<html>")),
            profile(profile_json("P05")),
            AS_OF,
        )
    with pytest.raises(LiveCalculateError, match="형식 오류"):
        assess_via_spring(_client(lambda _: httpx.Response(200, json={"x": 1})), 1, AS_OF, None)


def test_chat_tool_uses_calculate_path_and_reports_errors() -> None:
    calls: list[date] = []

    def calculate_fn(as_of: date) -> dict[str, Any]:
        calls.append(as_of)
        return {"assessmentId": 74, "status": "NEEDS_EVIDENCE"}

    # 운영 경로면 편집기 프로필이 없어도(Spring 경유) 계산한다.
    ctx = ToolContext(profile=None, include_draft=True, search=None, calculate=calculate_fn)
    output, is_error = execute_tool("calculate_e74_score", {"as_of_date": "2026-11-12"}, ctx)
    assert not is_error and json.loads(output)["assessmentId"] == 74
    assert calls == [AS_OF]

    def failing(_: date) -> dict[str, Any]:
        raise LiveCalculateError("계산 실패: HTTP 409 INVALID_STATE RULE_SET_NOT_APPROVED")

    failing_ctx = ToolContext(profile=None, include_draft=True, search=None, calculate=failing)
    output, is_error = execute_tool("calculate_e74_score", {}, failing_ctx)
    assert is_error and "RULE_SET_NOT_APPROVED" in output


def test_chat_tool_without_calculate_path_still_uses_engine() -> None:
    ctx = ToolContext(profile=profile(profile_json("P05")), include_draft=True, search=None)
    output, is_error = execute_tool("calculate_e74_score", {"as_of_date": "2026-11-12"}, ctx)

    assert not is_error
    assert json.loads(output)["score"]["total"] == 235


def test_app_spring_result_hides_on_input_change_and_skips_broken_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Streamlit이 있을 때만 실행: uv run --project ai --with streamlit pytest ..."""
    testing = pytest.importorskip("streamlit.testing.v1")
    from playground import live_e74

    users: list[int] = []

    def fake_assess(
        _: httpx.Client, user_id: int, as_of: date, declaration: object
    ) -> E74AssessmentToolResponse:
        users.append(user_id)
        return E74AssessmentToolResponse.model_validate(
            {"assessmentId": 74, "reused": False, "result": _engine_response()}
        )

    # e74_app은 실행할 때마다 live_e74에서 함수를 가져오므로 모듈 속성을 바꾸면 된다.
    monkeypatch.setattr(live_e74, "assess_via_spring", fake_assess)
    at = testing.AppTest.from_file(str(APP), default_timeout=30).run()
    at.radio(key="calc_mode").set_value("Spring 경유").run()
    at.number_input(key="spring_user_id").set_value(185).run()
    at.button(key="live_calculate").click().run()
    assert not at.exception
    assert users == [185]
    assert any("assessmentId 74" in element.value for element in at.success)

    # 사용자 id를 바꾸면 185의 결과를 보여 주지 않는다.
    at.number_input(key="spring_user_id").set_value(186).run()
    assert not any("assessmentId" in element.value for element in at.success)
    assert any("입력" in element.value and "바뀜" in element.value for element in at.info)

    # 편집기 프로필이 깨지면 감점 문항 없이 보내지 않는다(계산 요청 버튼도 없음).
    at.text_area(key="profile_text").input("{ broken").run()
    assert not at.exception
    assert any("프로필 JSON 오류" in element.value for element in at.error)
    assert not [button for button in at.button if button.key == "live_calculate"]
    assert users == [185]
