"""E-7-4 로드맵(단계 고르기·예상 점수·문장·API) 단위 테스트. DB·네트워크·LLM 없이 실행한다."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.integrations.policy_gateway import UnconfiguredPolicyGateway
from app.main import AppState, create_app
from app.rag.embeddings import BgeM3Embedder
from app.visa.roadmap.schemas import RoadmapRequest, RoadmapResponse
from app.visa.roadmap.service import StepWriter, build_roadmap
from app.visa.roadmap.text import (
    FORBIDDEN_PHRASE_CODES,
    FORBIDDEN_PHRASES,
    PROMPT_VERSION,
    SYSTEM_PROMPT,
    LlmStepWriter,
    numbers_in,
)
from tests.unit._e74_cases import profile_json

pytestmark = pytest.mark.unit

AS_OF = "2026-11-27"
EXPIRES = "2027-10-04"
TOKEN = "r" * 40
ROADMAP = "/internal/v1/visa/roadmap"


def _request(as_of: str = AS_OF, expires: str = EXPIRES, **profile_changes: Any) -> dict[str, Any]:
    profile = profile_json("P04")
    profile["visaProfile"].update(profile_changes)
    return {"asOfDate": as_of, "stayExpiresOn": expires, "profile": profile}


def _roadmap(body: dict[str, Any], writer: StepWriter | None = None) -> RoadmapResponse:
    return build_roadmap(RoadmapRequest.model_validate(body), include_draft=True, writer=writer)


def _dump(r: RoadmapResponse) -> dict[str, Any]:
    return r.model_dump(mode="json", by_alias=True)


# --- P04 시연 데이터 ------------------------------------------------------------------------


def test_p04_matches_the_screen_numbers() -> None:
    d = _dump(_roadmap(_request()))
    assert (d["goalVisaCode"], d["ruleVersion"], d["ruleStatus"]) == (
        "E-7-4",
        "E74-2026-v1",
        "DRAFT",
    )
    assert d["stay"] == {
        "visaTypeCode": "E-9",
        "stayStartedOn": "2022-12-05",
        "stayExpiresOn": "2027-10-04",
        "daysToExpiry": 311,
        "applicableFrom": "2026-12-05",
    }
    assert (d["score"]["confirmed"], d["score"]["potential"], d["score"]["required"]) == (
        160,
        210,
        None,
    )
    assert [s["ruleCode"] for s in d["steps"]] == [
        "REC_EMPLOYER",
        "INCOME_25_30",
        "KOREAN_L4_PLUS",
        None,
    ]
    assert [s["kind"] for s in d["steps"]] == [
        "OBTAIN_BONUS",
        "SUBMIT_EVIDENCE",
        "RAISE_LEVEL",
        "APPLY",
    ]
    assert [s["required"] for s in d["steps"]] == [True, False, False, True]
    assert [s["estimatedAdditionalScore"] for s in d["steps"]] == [50, 50, 40, None]
    apply = d["steps"][-1]
    assert (apply["availableFrom"], apply["targetDate"]) == ("2026-12-05", "2027-10-04")
    assert d["projectedScore"] == {"afterRequiredSteps": 260, "afterAllSteps": 300}
    assert d["warnings"] == ["DRAFT_RULES", "RULES_EXPIRE"]
    assert d["text"]["source"] == "TEMPLATE" and d["isMock"] is True


def test_p04_score_items_are_base_scores_and_relevant_bonuses() -> None:
    items = {i["ruleCode"]: i for i in _dump(_roadmap(_request()))["score"]["items"]}
    assert list(items) == ["INCOME_25_30", "KOREAN_L3", "AGE_27_33", "TENURE_3Y", "REC_EMPLOYER"]
    income = items["INCOME_25_30"]
    assert (income["status"], income["earnedPoints"], income["potentialPoints"]) == (
        "NEEDS_EVIDENCE",
        0,
        50,
    )
    assert income["basis"] == "TRANSACTION_ESTIMATE"
    assert income["value"] == {"averageIncome": 28_200_000}
    assert (items["KOREAN_L3"]["basis"], items["KOREAN_L3"]["value"]) == ("TOPIK", {"level": 3})
    assert (items["AGE_27_33"]["basis"], items["AGE_27_33"]["value"]) == ("ARC", {"age": 31})
    assert items["TENURE_3Y"]["value"] == {"tenureMonths": 47}
    rec = items["REC_EMPLOYER"]
    assert (rec["status"], rec["earnedPoints"], rec["potentialPoints"]) == ("NOT_MET", 0, 50)
    assert rec["requiredForApplication"] is True


def test_threshold_is_not_exposed() -> None:
    d = _dump(_roadmap(_request()))
    assert d["score"]["required"] is None
    texts = " ".join(f"{s['title']} {s['description']}" for s in d["steps"])
    assert 200 not in numbers_in(texts)


def test_on_the_stay_date_the_application_opens_without_stay_warning() -> None:
    d = _dump(_roadmap(_request(as_of="2026-12-05")))
    assert d["stay"]["applicableFrom"] == "2026-12-05"
    assert d["stay"]["daysToExpiry"] == 303
    assert "STAY_EXPIRES_FIRST" not in d["warnings"]
    assert all(s["ruleCode"] != "REQ_STAY_PERIOD" for s in d["steps"])


def test_unknown_prior_stay_means_no_date_and_a_question() -> None:
    d = _dump(_roadmap(_request(priorStayMonths=None)))
    assert d["stay"]["applicableFrom"] is None
    assert d["steps"][-1]["availableFrom"] is None
    fields = {m["field"]: m["ruleCodes"] for m in d["missingInputs"]}
    assert fields["visaProfile.priorStayMonths"] == ["REQ_STAY_PERIOD"]


def test_stay_ending_before_the_application_date_is_warned() -> None:
    d = _dump(_roadmap(_request(expires="2026-12-01")))
    assert d["stay"]["applicableFrom"] == "2026-12-05"
    assert d["warnings"] == ["DRAFT_RULES", "STAY_EXPIRES_FIRST"]


def test_once_required_steps_are_done_optional_ones_follow_by_points() -> None:
    body = _request()
    profile = body["profile"]
    profile["visaEvidences"].append(
        {"visaEvidenceId": 2, "evidenceType": "RECOMMENDATION_EMPLOYER", "status": "VERIFIED"}
    )
    for record in profile["incomeRecords"]:
        record.update(source="DOCUMENT", verificationStatus="VERIFIED")
    d = _dump(_roadmap(body))
    assert d["score"]["confirmed"] == 260
    assert [s["ruleCode"] for s in d["steps"]] == [
        "KOREAN_L4_PLUS",
        "REC_CENTRAL",
        "QUALIFICATION",
        None,
    ]
    assert not any(s["required"] for s in d["steps"][:-1])
    # 필수·증빙 단계가 없으면 지금 확정 점수 그대로, 전부 하면 엔진 재계산 결과(+40+30+20).
    assert d["projectedScore"] == {"afterRequiredSteps": 260, "afterAllSteps": 350}
    income = next(i for i in d["score"]["items"] if i["category"] == "BASE_INCOME")
    assert income["basis"] == "DOCUMENT"


def test_only_one_government_recommendation_is_chosen() -> None:
    body = _request()
    profile = body["profile"]
    # 필수 단계와 증빙 제출, 한국어를 채워 정부 추천이 후보에 오르게 한다.
    profile["visaEvidences"] = [
        {
            "visaEvidenceId": 1,
            "evidenceType": "TOPIK",
            "levelValue": "4",
            "status": "VERIFIED",
            "issuedOn": "2025-10-19",
            "expiresOn": "2027-10-19",
        },
        {"visaEvidenceId": 2, "evidenceType": "RECOMMENDATION_EMPLOYER", "status": "VERIFIED"},
    ]
    d = _dump(_roadmap(body))
    codes = [s["ruleCode"] for s in d["steps"]]
    assert sum(code in ("REC_CENTRAL", "REC_REGIONAL") for code in codes) == 1


# --- LLM 문장 ------------------------------------------------------------------------------


@dataclass
class _Block:
    text: str
    type: str = "text"


@dataclass
class _Response:
    content: list[_Block]
    stop_reason: str = "end_turn"


class _FakeMessages:
    def __init__(self, reply: Any) -> None:
        self.reply = reply
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if isinstance(self.reply, Exception):
            raise self.reply
        steps = json.loads(kwargs["messages"][0]["content"])["steps"]
        return _Response([_Block(json.dumps({"steps": self.reply(steps)}, ensure_ascii=False))])


class _FakeClient:
    def __init__(self, reply: Any) -> None:
        self.messages = _FakeMessages(reply)


def _writer(reply: Any) -> tuple[LlmStepWriter, _FakeMessages]:
    client = _FakeClient(reply)
    return LlmStepWriter(client, "claude-test"), client.messages


def _rewrite(description: str) -> Any:
    return lambda steps: [
        {"order": s["order"], "title": f"{s['order']}단계", "description": description}
        for s in steps
    ]


def test_llm_text_is_used_when_numbers_come_from_step_data() -> None:
    writer, messages = _writer(_rewrite("2026-12-05부터 준비해요. 추천서를 받으면 +50점이에요."))
    d = _dump(_roadmap(_request(), writer))
    assert d["text"] == {
        "source": "LLM",
        "modelId": "claude-test",
        "promptVersion": PROMPT_VERSION,
        "fallbackReason": None,
    }
    assert d["steps"][0]["title"] == "1단계"
    # 숫자·날짜는 바뀌지 않는다.
    assert d["projectedScore"] == {"afterRequiredSteps": 260, "afterAllSteps": 300}
    sent = json.loads(messages.calls[0]["messages"][0]["content"])["steps"]
    assert [s["order"] for s in sent] == [1, 2, 3, 4]
    assert "profile" not in messages.calls[0]["messages"][0]["content"]


@pytest.mark.parametrize(
    ("description", "reason"),
    [
        ("추천서를 받으면 +70점이에요.", "UNVERIFIED_NUMBER"),
        ("총 200점을 넘길 수 있어요.", "UNVERIFIED_NUMBER"),
        ("이 단계를 마치면 합격할 수 있어요.", "FORBIDDEN_PHRASE"),
        ("상위권이 될 거예요.", "FORBIDDEN_PHRASE"),
    ],
)
def test_llm_text_falls_back_to_template(description: str, reason: str) -> None:
    writer, _ = _writer(_rewrite(description))
    template = _dump(_roadmap(_request()))
    d = _dump(_roadmap(_request(), writer))
    assert d["text"] == {
        "source": "TEMPLATE",
        "modelId": None,
        "promptVersion": None,
        "fallbackReason": reason,
    }
    assert d["steps"] == template["steps"]


def test_llm_missing_a_step_or_failing_falls_back() -> None:
    writer, _ = _writer(lambda steps: _rewrite("준비해요.")(steps)[:-1])
    assert _dump(_roadmap(_request(), writer))["text"]["fallbackReason"] == "INVALID_OUTPUT"
    writer, _ = _writer(RuntimeError("boom"))
    assert _dump(_roadmap(_request(), writer))["text"]["fallbackReason"] == "LLM_ERROR"


def test_fallback_logs_reason_and_rule_codes_but_not_the_text(
    caplog: pytest.LogCaptureFixture,
) -> None:
    sentence = "불합격 걱정 없이 상위권 보장이에요."
    writer, _ = _writer(_rewrite(sentence))
    with caplog.at_level(logging.WARNING, logger="app.visa.roadmap.service"):
        d = _dump(_roadmap(_request(), writer))
    assert d["text"]["fallbackReason"] == "FORBIDDEN_PHRASE"
    assert "reason=FORBIDDEN_PHRASE rules=FAIL,GUARANTEE,PASS,RANK" in caplog.text
    assert sentence not in caplog.text and "불합격" not in caplog.text


def test_unverified_number_fallback_logs_no_rule_codes(caplog: pytest.LogCaptureFixture) -> None:
    writer, _ = _writer(_rewrite("추천서를 받으면 +70점이에요."))
    with caplog.at_level(logging.WARNING, logger="app.visa.roadmap.service"):
        _roadmap(_request(), writer)
    assert "reason=UNVERIFIED_NUMBER rules=-" in caplog.text
    assert "70" not in caplog.text


def test_every_forbidden_phrase_has_a_rule_code() -> None:
    assert set(FORBIDDEN_PHRASE_CODES) == set(FORBIDDEN_PHRASES)


def test_prompt_leaves_disclaimers_to_the_screen() -> None:
    assert "안내·면책 문구(승인 여부, 결과 보장 등)는 쓰지 않아요" in SYSTEM_PROMPT
    assert "화면이 고정 문구로 표시해요" in SYSTEM_PROMPT


def test_numbers_in_reads_commas_and_dates() -> None:
    assert numbers_in("2,500만원, 2026-12-05, +50점") == {2500, 2026, 12, 5, 50}


# --- API ---------------------------------------------------------------------------------


def _client(writer: StepWriter | None = None) -> TestClient:
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        ai_internal_api_token=TOKEN,  # type: ignore[arg-type]
        e74_include_draft_rules=True,
    )
    app = create_app()
    app.state.rag = AppState(
        settings=settings,
        gateway=UnconfiguredPolicyGateway(),
        embedder=BgeM3Embedder(),
        roadmap_writer=writer,
    )
    return TestClient(app)


@pytest.mark.parametrize("headers", [{}, {"X-Internal-Token": "wrong"}])
def test_api_rejects_missing_or_wrong_token(headers: dict[str, str]) -> None:
    assert _client().post(ROADMAP, json=_request(), headers=headers).status_code == 401


def test_api_returns_the_roadmap() -> None:
    response = _client().post(ROADMAP, json=_request(), headers={"X-Internal-Token": TOKEN})
    assert response.status_code == 200
    assert response.json() == _dump(_roadmap(_request()))


def test_api_uses_the_configured_llm_writer() -> None:
    writer, _ = _writer(_rewrite("준비해요."))
    response = _client(writer).post(ROADMAP, json=_request(), headers={"X-Internal-Token": TOKEN})
    assert response.json()["text"]["source"] == "LLM"


def test_api_rejects_stay_already_expired() -> None:
    response = _client().post(
        ROADMAP,
        json=_request(expires="2026-11-26"),
        headers={"X-Internal-Token": TOKEN, "X-Request-Id": "req-roadmap-2"},
    )
    assert response.status_code == 400
    body = response.json()
    assert body["code"] == "VALIDATION_ERROR"
    assert body["requestId"] == "req-roadmap-2"
