"""예외 사진 정확도 측정 (로컬 CPU, 정확도만 기록)

실행: ocr 폴더에서  python scripts/eval_exception.py
"""
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

# ocr 폴더를 import 경로에 추가 (scripts 안에서 실행해도 pipeline을 찾게)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pipeline import engine  # noqa: E402

SILENT_CONF = 0.8  # 이 이상인데 틀리면-조용한 오답
LABEL = sys.argv[1] if len(sys.argv) > 1 else "baseline"  # 예: python scripts/eval_exception.py fix-nationality

HERE = Path(__file__).resolve().parent
spec = json.loads((HERE / "exception_answers.json").read_text(encoding="utf-8"))
answer = spec["answer"]
photo_dir = Path(spec["photo_dir"])
out_dir = photo_dir / "results"  # 결과는 레포 밖에 저장
out_dir.mkdir(exist_ok=True)


def judge(expected, value, conf):
    if value == expected:
        return "정답"
    if value is None:
        return "빈칸"
    if conf is not None and conf >= SILENT_CONF:
        return "조용한오답"
    return "오답"


rows = []
model_version = None
for p in sorted(photo_dir.glob("*.jpg")):
    r = engine.recognize(str(p))
    model_version = r.get("modelVersion")
    got = {f["name"]: f for f in r["fields"]}
    for name, expected in answer.items():
        f = got.get(name, {})
        value, conf = f.get("value"), f.get("confidence")
        rows.append({
            "photo": p.stem,
            "condition": p.stem.rsplit("_", 1)[0],  # 01_normal_a → 01_normal
            "field": name,
            "expected": expected,
            "value": value,
            "confidence": conf,
            "result": judge(expected, value, conf),
        })
    print(f"[완료] {p.stem}")

# 1) 필드별 요약
print("\n=== 필드별 (사진 수 기준) ===")
for name in answer:
    c = Counter(r["result"] for r in rows if r["field"] == name)
    print(f"{name:16} 정답 {c['정답']:2} | 빈칸 {c['빈칸']:2} | 오답 {c['오답']:2} | 조용한오답 {c['조용한오답']:2}")

# 2) 조건별 요약
print("\n=== 조건별 (필드 정답 수 / 전체) ===")
by_cond = defaultdict(list)
for r in rows:
    by_cond[r["condition"]].append(r)
for cond, items in sorted(by_cond.items()):
    ok = sum(1 for r in items if r["result"] == "정답")
    silent = sum(1 for r in items if r["result"] == "조용한오답")
    print(f"{cond:16} {ok:2}/{len(items):2}  조용한오답 {silent}")

# 3) 틀린 것만 상세
print("\n=== 정답이 아닌 항목 ===")
for r in rows:
    if r["result"] != "정답":
        print(f"{r['photo']:18} {r['field']:16} {r['result']:6} 기대={r['expected']} 결과={r['value']} 신뢰도={r['confidence']}")

stamp = datetime.now().strftime("%Y%m%d_%H%M")
out = out_dir / f"{LABEL}_{stamp}.json"
out.write_text(json.dumps({
    "measured_at": stamp,
        "condition": f"로컬 CPU, 견본 합성 카드 1종, 사진 19장, 라벨={LABEL}",
    "silent_conf": SILENT_CONF,
    "rows": rows,
}, ensure_ascii=False, indent=2), encoding="utf-8")
print(f"\n저장: {out}")