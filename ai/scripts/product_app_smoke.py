"""앱 경로 상품 추천 스모크(이슈 #81): Spring 채팅 API로 상품 질문을 보내 결과를 기록한다.

문항마다 대화방을 새로 만들고(POST /api/v1/chat/sessions) 메시지 스트림
(POST /api/v1/chat/sessions/{id}/messages/stream)을 끝까지 읽는다. 문항별로 남기는 값:
상품 도구 호출 여부·상태·오류 코드, chat.sources의 PRODUCT_DOCUMENT 수(= 추천 상품 수),
답변의 조건표 상품명, 판정 불가 안내 여부(답변에 visa·age 언급, 사람 확인용), 금리 값 언급.

- 프로필은 그 토큰 사용자의 실제 값(Spring product-profile·INT-06)이다.
  문항 파일의 기대 결과는 fixture 프로필(P1~P5) 기준이라 참고로만 같이 적는다.
- 서버가 실제 LLM으로 답하면 비용이 든다(문항당 1회). --dry-run은 호출 없이 보낼 문항만 출력한다.
- 토큰은 출력·파일에 남기지 않는다.

    uv run python scripts/product_app_smoke.py --base-url <Spring 주소> --token <앱 진입 JWT>
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import uuid
from collections.abc import Iterator, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import _bootstrap
import httpx
from _product_check import PRODUCT_TOOL, catalog_names, rate_value

AI_ROOT = _bootstrap.AI_ROOT
QUESTIONS_PATH = AI_ROOT / "specs" / "chat" / "product_questions_v1.json"
DEFAULT_OUTPUT_DIR = AI_ROOT / "artifacts" / "chat"
# 추천·조건 위반이 나기 쉬운 문항·기권·범위 밖을 섞은 10문항(결과 문서 8절에서 본 유형).
SMOKE_IDS = ("Q01", "Q04", "Q07", "Q10", "Q13", "Q15", "Q18", "Q19", "Q27", "Q31")
PRODUCT_DOCUMENT = "PRODUCT_DOCUMENT"
# 판정 불가 안내(사람 확인용): 답변이 비자·나이를 언급하는지.
UNCHECKED_MENTION = re.compile(r"\bvisa\b|\bage\b|비자|나이", re.IGNORECASE)


def iter_sse(lines: Iterator[str]) -> Iterator[tuple[str, Any]]:
    """event:/data: 줄을 (이벤트 이름, JSON 데이터)로 묶는다. 빈 줄이 이벤트 끝이다."""
    name: str | None = None
    data: list[str] = []
    for line in lines:
        if line == "":
            if name and data:
                yield name, json.loads("\n".join(data))
            name, data = None, []
        elif line.startswith(":"):
            continue
        elif line.startswith("event:"):
            name = line[6:].strip()
        elif line.startswith("data:"):
            data.append(line[5:].strip())
    if name and data:
        yield name, json.loads("\n".join(data))


def _sources(data: Any) -> list[Mapping[str, Any]]:
    items = data.get("sources") if isinstance(data, Mapping) else data
    return [s for s in items or [] if isinstance(s, Mapping)]


def load_questions(path: Path, ids: Sequence[str]) -> list[dict[str, Any]]:
    questions = {q["id"]: q for q in json.loads(path.read_text(encoding="utf-8"))["questions"]}
    missing = [qid for qid in ids if qid not in questions]
    if missing:
        raise SystemExit(f"FAILED: question ids not in {path.name}: {missing}")
    for qid in ids:
        if questions[qid].get("history"):
            raise SystemExit(f"FAILED: {qid} needs history; the app path starts a new session")
    return [questions[qid] for qid in ids]


def create_session(client: httpx.Client, base: str, auth: Mapping[str, str]) -> Any:
    response = client.post(f"{base}/api/v1/chat/sessions", json={"title": None}, headers=auth)
    response.raise_for_status()
    body = response.json()
    session_id = body.get("id", body.get("sessionId"))
    if session_id is None:
        raise ValueError(f"no session id in response keys {sorted(body)}")
    return session_id


def ask(client: httpx.Client, base: str, token: str, question: Mapping[str, Any]) -> dict[str, Any]:
    """대화방 하나를 만들고 질문 하나를 보내 스트림을 읽는다."""
    auth = {"Authorization": f"Bearer {token}"}
    started = time.perf_counter()
    row: dict[str, Any] = {
        "id": question["id"],
        "profileId": question.get("profileId"),
        "expectedOutcome": question.get("expectedOutcome"),
        "events": [],
        "tools": [],
        "sources": [],
        "answer": "",
    }
    try:
        session_id = create_session(client, base, auth)
    except (httpx.HTTPError, ValueError) as exc:
        row.update(status="session_error", error=type(exc).__name__)
        return finish(row, started)
    body = {"clientMessageId": str(uuid.uuid4()), "content": question["text"]}
    url = f"{base}/api/v1/chat/sessions/{session_id}/messages/stream"
    headers = {**auth, "Accept": "text/event-stream"}
    deltas: list[str] = []
    try:
        with client.stream("POST", url, json=body, headers=headers, timeout=180) as response:
            row["httpStatus"] = response.status_code
            if response.status_code != 200:
                response.read()
                row["status"] = "http_error"
                return finish(row, started)
            for name, data in iter_sse(response.iter_lines()):
                row["events"].append(name)
                if name == "chat.delta":
                    deltas.append(data.get("text", ""))
                elif name == "chat.tool_call":
                    row["tools"].append(data)
                elif name == "chat.sources":
                    row["sources"].extend(_sources(data))
                elif name == "chat.done":
                    row["status"] = "done"
                    if not deltas and isinstance(data, Mapping):
                        deltas.append(str(data.get("content") or ""))
                elif name == "chat.error":
                    row["status"] = "error"
                    row["errorCode"] = data.get("code") if isinstance(data, Mapping) else None
    except httpx.HTTPError as exc:
        row.update(status="transport_error", error=type(exc).__name__)
    row.setdefault("status", "incomplete")
    row["answer"] = "".join(deltas)
    return finish(row, started)


def finish(row: dict[str, Any], started: float) -> dict[str, Any]:
    """기록 값을 채운다(답변 글과 이벤트만으로 판정, API 호출 없음)."""
    row["latencyMs"] = round((time.perf_counter() - started) * 1000)
    product_calls = [t for t in row["tools"] if t.get("toolName") == PRODUCT_TOOL]
    documents = {
        s.get("chunkRef") or s.get("id") or i
        for i, s in enumerate(row["sources"])
        if s.get("type") == PRODUCT_DOCUMENT
    }
    answer = row["answer"]
    row.update(
        productToolCalled=bool(product_calls),
        productToolStatus=[t.get("status") for t in product_calls],
        productToolErrorCode=[t.get("errorCode") for t in product_calls if t.get("errorCode")],
        productDocuments=len(documents),
        answerCatalogNames=catalog_names(answer),
        mentionsUnchecked=bool(UNCHECKED_MENTION.search(answer)),
        rateValue=rate_value(answer),
    )
    return row


def summary_table(rows: Sequence[Mapping[str, Any]]) -> str:
    lines = [
        "| 문항 | 기대(fixture 기준, 참고) | 상태 | 상품 도구 | PRODUCT_DOCUMENT "
        "| 답변 상품명 | 판정 불가 언급 | 금리 값 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        expected = r.get("expectedOutcome") or {}
        want = expected.get("type", "")
        if expected.get("reason"):
            want += f" {expected['reason']}"
        tool = ",".join(r["productToolStatus"] + r["productToolErrorCode"]) or "–"
        status = r["status"] + (f" {r['errorCode']}" if r.get("errorCode") else "")
        cells = [
            r["id"],
            want,
            status,
            tool,
            str(r["productDocuments"]),
            str(len(r["answerCatalogNames"])),
            "예" if r["mentionsUnchecked"] else "아니오",
            str(len(r["rateValue"])),
        ]
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None, client: httpx.Client | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base-url", required=True, help="Spring 주소(예: http://localhost:8080)")
    parser.add_argument("--token", required=True, help="앱 진입 JWT(출력·파일에 남기지 않음)")
    parser.add_argument("--questions", type=Path, default=QUESTIONS_PATH)
    parser.add_argument("--ids", default=",".join(SMOKE_IDS), help="쉼표로 구분한 문항 id")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--label", default="", help="결과 파일 이름에 붙일 표시(예: before-102)")
    parser.add_argument("--dry-run", action="store_true", help="호출 없이 보낼 문항만 출력")
    args = parser.parse_args(argv)
    ids = [i.strip() for i in args.ids.split(",") if i.strip()]
    questions = load_questions(args.questions, ids)
    if args.dry_run:
        for q in questions:
            print(f"{q['id']} {q.get('profileId')} {q.get('expectedOutcome')} {q['text']}")
        print(f"\n{len(questions)} questions, no calls made (--dry-run)")
        return 0
    base = args.base_url.rstrip("/")
    started = datetime.now(UTC)
    own = client is None
    http = client or httpx.Client()
    try:
        rows = [ask(http, base, args.token, q) for q in questions]
    finally:
        if own:
            http.close()
    stamp = started.strftime("%Y%m%dT%H%M%SZ")
    label = f"-{args.label}" if args.label else ""
    args.output_dir.mkdir(parents=True, exist_ok=True)
    path = args.output_dir / f"product_app_smoke{label}-{stamp}.jsonl"
    meta = {
        "type": "conditions",
        "startedAt": started.isoformat(timespec="seconds"),
        "baseUrl": base,
        "questions": args.questions.as_posix(),
        "ids": ids,
        "label": args.label,
    }
    with path.open("w", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(meta, ensure_ascii=False) + "\n")
        for row in rows:
            fh.write(json.dumps({"type": "question", **row}, ensure_ascii=False) + "\n")
    print(summary_table(rows))
    print(f"\nwrote {path}")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    raise SystemExit(main())
