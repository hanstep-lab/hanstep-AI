"""송금 재질의 채점(scripts/evaluate_chat.py --question-set remit). fake 서버, 네트워크·API 없음."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.integrations.policy_gateway import UnconfiguredPolicyGateway
from app.main import AppState, create_app
from app.rag.embeddings import BgeM3Embedder

pytestmark = pytest.mark.unit

TOKEN = "f" * 40
SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


def _script() -> ModuleType:
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location("evaluate_chat", SCRIPTS / "evaluate_chat.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


EVAL = _script()


def _settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "ai_internal_api_token": TOKEN,
        "chat_fake_delta_delay_ms": 0,
        "chat_allow_config_override": True,
        **overrides,
    }
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


def _client(settings: Settings) -> TestClient:
    app = create_app()
    app.state.rag = AppState(
        settings=settings, gateway=UnconfiguredPolicyGateway(), embedder=BgeM3Embedder()
    )
    return TestClient(app)


def _rows(path: Path) -> dict[str, dict[str, Any]]:
    lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    return {r["questionId"]: r for r in lines if r["type"] == "call"}


def _questions() -> dict[str, dict[str, Any]]:
    spec = EVAL.load_questions(EVAL.REMIT_QUESTIONS)
    return {q["id"]: q for q in spec["questions"]}


def _run_remit(tmp_path: Path, ids: str) -> Path:
    settings = _settings()
    variants = Path(EVAL.AI_ROOT) / "specs" / "chat" / "variants_remit_v1.json"
    output = tmp_path / "remit.jsonl"
    code = EVAL.main(
        ["--base-url", "http://testserver", "--question-set", "remit", "--questions", ids,
         "--variants", str(variants), "--output", str(output)],
        client=_client(settings),
        settings=settings,
        confirm=lambda _: "n",
    )  # fmt: skip
    assert code == 0
    return output


def test_remit_variant_is_one_v3_haiku_setting() -> None:
    variants = Path(EVAL.AI_ROOT) / "specs" / "chat" / "variants_remit_v1.json"
    loaded = EVAL.load_variants(variants, _settings())
    assert [
        (v["name"], v["model"], v["judgmentFacts"], v["promptVersion"], v["maxTokens"])
        for v in loaded
    ] == [("remit-prompt", "claude-haiku-5-5", True, "v3", 2000)]


def test_follow_up_expectations_agree_with_the_question_set() -> None:
    questions = _questions()
    ambiguous = [qid for qid, q in questions.items() if q["category"] == "ambiguous"]
    assert sorted(EVAL.FOLLOW_UP_EXPECTED) == ambiguous
    for qid in ambiguous:
        q = questions[qid]
        final = dict(zip(EVAL.REMIT_SLOTS, EVAL.FOLLOW_UP_EXPECTED[qid], strict=True))
        for slot in EVAL.REMIT_SLOTS:
            # 질문 세트에 적힌 값은 2턴 뒤에도 그대로다.
            assert q[slot] is None or q[slot] == final[slot], (qid, slot)


def test_fake_run_scores_the_scenarios(tmp_path: Path) -> None:
    output = _run_remit(tmp_path, "S01,S04,S10,S14,S18,S21,S25,S29")
    rows = _rows(output)
    conditions = json.loads(output.read_text(encoding="utf-8").splitlines()[0])
    assert conditions["questionSet"] == {"file": "remit_questions_v1.json", "version": "v1"}

    def check(qid: str) -> dict[str, Any]:
        result: dict[str, Any] = rows[qid]["remitCheck"]
        return result

    # 명확: 1턴에 견적·카드.
    assert (check("S01")["outcome"], check("S01")["turns"]) == ("correct", 1)
    assert rows["S01"]["followUp"] is None
    assert rows["S01"]["blocks"][0]["type"] == "REMITTANCE_CONFIRM"
    assert check("S04")["first"]["quote"] == {
        "recipient": "MOTHER",
        "currency": "PHP",
        "amount": 10_000,
    }
    # 애매: 1턴에 맞는 항목을 묻고, 2턴(followUpReply)에 견적·카드.
    for qid, topic in (("S10", "recipient"), ("S14", "currency"), ("S18", "amount")):
        assert check(qid)["askedAbout"] == [topic], qid
        assert (check(qid)["outcome"], check(qid)["turns"]) == ("correct", 2), qid
        assert rows[qid]["followUp"]["message"] == _questions()[qid]["followUpReply"]
    # S21(받는 분·통화 모두 애매)은 받는 분부터 묻는다.
    assert check("S21")["askedAbout"] == ["recipient"]
    assert check("S21")["outcome"] == "correct"
    assert check("S21")["second"]["quote"] == {
        "recipient": "MOTHER",
        "currency": "PHP",
        "amount": 20_000,
    }
    # 범위 밖: 진행하지 않음 → 안내 문구는 사람이 본다.
    for qid in ("S25", "S29"):
        assert check(qid)["outcome"] == "declined", qid
        assert check(qid)["needsReview"] is True
    # 견적 숫자는 근거가 있다.
    assert all(not r["numberCheck"]["unsupported"] for r in rows.values())

    summary = output.with_suffix(".md").read_text(encoding="utf-8")
    assert "## 송금 재질의: remit-prompt" in summary
    assert "| S10 | 1 | ASK_RECIPIENT (?, KRW, 500,000) | 질문(recipient) | " in summary
    assert "카드(MOTHER KRW 500,000) | 정답 | 2 |" in summary
    assert "### 사람 확인 목록: remit-prompt" in summary
    assert "| S29 | 1 | 범위 밖 안내 문구 확인 |" in summary


def _row(qid: str, first: dict[str, Any], follow: dict[str, Any] | None = None) -> dict[str, Any]:
    q = _questions()[qid]
    row: dict[str, Any] = {
        "questionId": qid,
        "status": "done",
        "answer": first.get("answer", ""),
        "tools": first.get("tools", []),
        "blocks": first.get("blocks"),
        "followUp": follow,
        "remitContext": EVAL.remit_context(_settings(), 1, "fake"),
        **{k: q[k] for k in ("expectedAction", *EVAL.REMIT_SLOTS, "followUpReply")},
    }
    return row


def _quote(recipient_id: int, amount: int, currency: str) -> dict[str, Any]:
    args = {"recipientId": recipient_id, "amount": amount, "currency": currency}
    return {
        "tools": [{"toolName": "quote_remittance", "status": "SUCCEEDED", "arguments": args}],
        "blocks": [{"type": "REMITTANCE_CONFIRM"}],
        "answer": "Please check the confirmation card.",
    }


@pytest.mark.parametrize(
    ("qid", "first", "follow", "outcome", "errors"),
    [
        # 애매한데 묻지 않고 진행(S14: 통화를 KRW로 추측).
        ("S14", _quote(11, 20_000, "KRW"), None, "missed_clarification", []),
        # 명확한데 되물음.
        ("S01", {"answer": "Do you mean LORNA REYES (BDO)?"}, None, "unnecessary_question", []),
        # 명확한데 받는 분을 잘못 고름(mama → 본인 BPI).
        ("S01", _quote(12, 500_000, "KRW"), None, "wrong_guess", ["recipient"]),
        # 2턴 뒤 금액을 잘못 씀.
        (
            "S10",
            {"answer": "Who should receive it: LORNA REYES or MARK ANTHONY REYES?"},
            _quote(11, 50_000, "KRW"),
            "wrong_guess",
            ["amount"],
        ),
        # 통화를 물어야 하는데 받는 분을 물음.
        (
            "S14",
            {"answer": "Who should receive it: LORNA REYES or MARK ANTHONY REYES?"},
            _quote(11, 20_000, "PHP"),
            "asked_other",
            [],
        ),
        # 2턴에도 진행하지 않음.
        (
            "S18",
            {"answer": "How much would you like to send?"},
            {"answer": "Is that in KRW or PHP?"},
            "not_completed",
            [],
        ),
        # 범위 밖인데 카드(S25 동생 → 엄마로 연결).
        ("S25", _quote(11, 300_000, "KRW"), None, "out_of_scope_proceeded", []),
    ],  # fmt: skip
)
def test_remit_check_rules(
    qid: str,
    first: dict[str, Any],
    follow: dict[str, Any] | None,
    outcome: str,
    errors: list[str],
) -> None:
    check = EVAL.remit_check(_row(qid, first, follow))
    assert (check["outcome"], check["guessErrors"]) == (outcome, errors)


def test_card_for_a_do_it_now_request_goes_to_a_person() -> None:
    # S27: 카드를 낼 수는 있지만 "바로 보낼 수 없다"를 말했는지는 사람이 본다.
    check = EVAL.remit_check(_row("S27", _quote(11, 500_000, "KRW")))
    assert (check["outcome"], check["needsReview"], check["guessErrors"]) == (
        "out_of_scope_proceeded",
        True,
        [],
    )


def test_asking_two_things_at_once_goes_to_a_person() -> None:
    answer = "Who should receive it, and is 20,000 in KRW or PHP?"
    check = EVAL.remit_check(_row("S21", {"answer": answer}, _quote(11, 20_000, "PHP")))
    assert check["askedAbout"] == ["recipient", "currency"]
    assert (check["outcome"], check["needsReview"], check["reason"]) == (
        "correct",
        True,
        "한 번에 둘 이상을 물음",
    )


def test_unsupported_numbers_cover_both_turns() -> None:
    row = _row("S14", {"answer": "Is 20,000 in KRW or PHP?"}, _quote(11, 20_000, "PHP"))
    row["followUp"]["answer"] = "That is about 446,000 KRW, roughly 900 USD."
    row.update(
        toolResult=EVAL.fixture_context(),
        contextTexts=["send 20000 to mom", "pesos"],
        expectedTools=["get_remittance_recipients"],
        judgmentFacts=False,
    )
    row["followUp"]["blocks"] = [
        {"type": "REMITTANCE_CONFIRM", "sendAmount": {"amount": 446_400, "currency": "KRW"}}
    ]
    EVAL.score(row)
    # 446,000은 카드의 446,400을 1,000원 단위로 반올림한 값이라 근거가 있고,
    # 900은 어디에도 없다.
    assert row["numberCheck"]["unsupported"] == ["900"]


def test_v3_inherits_v2_expected_tools() -> None:
    o01 = next(q for q in EVAL.load_questions()["questions"] if q["id"] == "O01")
    assert EVAL.expected_tools(o01, "v3") == EVAL.expected_tools(o01, "v2")
    assert EVAL.expected_tools(o01, "v3") != EVAL.expected_tools(o01, "v1.1")


def test_remit_set_rejects_the_latency_subset(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        EVAL.main(
            ["--question-set", "remit", "--subset", "latency", "--output", str(tmp_path / "x")],
            settings=_settings(),
        )
