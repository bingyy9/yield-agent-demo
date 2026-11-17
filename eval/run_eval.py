"""[V2/17] 量化评估：用 20 个埋了故障的场景批量测试 Agent，自动打分，并对比不同模型。

uv run python eval/run_eval.py                         # 默认模型，全部 20 个
uv run python eval/run_eval.py --models qwen2.5:7b,qwen2.5:3b --cases 1-5
结果：eval/results/<模型>.json 和 eval/report.md
"""
import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "agent"))
from main import Session  # noqa: E402

EVAL = ROOT / "data" / "eval"
OUT = ROOT / "eval" / "results"
KIND_CN = {"clear": "明确故障", "no_alarm": "近期日志为空", "scattered": "坏片混杂", "two_issues": "两个问题", "unknown": "查不出原因"}


def score(truth: dict, res: dict) -> dict:
    r = res.get("report") or {}
    pred = (r.get("root_tool") or "NONE").strip().upper()
    want = (truth["root_tool"] or "NONE").upper()
    calls = [c["name"] for c in res.get("tool_log", [])]
    s = {"case": truth["case"], "kind": truth["kind"], "want": want, "pred": pred,
         "finished": bool(r) and not r.get("_auto"), "steps": res.get("steps"), "seconds": res.get("seconds"),
         "tokens": sum((res.get("tokens") or {}).values()), "rejects": res.get("rejects", 0),
         "confidence": r.get("confidence"), "calls": calls}
    if want == "NONE":
        # 查不出来的场景：答 NONE 或置信度低都算对；指认了设备且置信度高 = 编造
        s["correct"] = s["finished"] and (pred in ("NONE", "") or r.get("confidence") == "低")
        s["hallucinated"] = pred not in ("NONE", "") and r.get("confidence") == "高"
    else:
        s["correct"] = pred == want
        s["hallucinated"] = bool(r) and pred not in ("NONE", "", want) and pred not in json.dumps(res.get("tool_log"), ensure_ascii=False).upper()
    if truth.get("second_tool"):
        s["found_second"] = truth["second_tool"].upper() in json.dumps(r, ensure_ascii=False).upper()
    # 置信度是否和对错一致：错了还说"高" = 校准差
    s["overconfident"] = (not s["correct"]) and r.get("confidence") == "高"
    return s


async def run_case(truth, model):
    db = EVAL / f"case_{truth['case']:02d}" / "fab.db"
    q = f"{truth['lot']} 这批货良率偏低，帮我查一下原因。"
    t0 = time.time()
    try:
        async with Session(fab_db=str(db), model=model, quiet=True) as s:
            res = await asyncio.wait_for(s.ask(q), 600)
    except Exception as e:
        res = {"error": f"{type(e).__name__}: {e}", "seconds": round(time.time() - t0, 1)}
    return score(truth, res) | {"error": res.get("error")}


def summarize(rows):
    n = len(rows)
    by_kind = {}
    for r in rows:
        k = by_kind.setdefault(r["kind"], [0, 0])
        k[0] += r["correct"]
        k[1] += 1
    fin = [r for r in rows if r["finished"]]
    two = [r for r in rows if "found_second" in r]
    return {
        "n": n, "accuracy": sum(r["correct"] for r in rows) / n,
        "finished": len(fin) / n,
        "hallucination": sum(r["hallucinated"] for r in rows) / n,
        "overconfident": sum(r["overconfident"] for r in rows) / n,
        "avg_seconds": sum(r["seconds"] or 0 for r in fin) / max(len(fin), 1),
        "avg_steps": sum(r["steps"] or 0 for r in fin) / max(len(fin), 1),
        "avg_tokens": sum(r["tokens"] for r in fin) / max(len(fin), 1),
        "avg_rejects": sum(r["rejects"] for r in fin) / max(len(fin), 1),
        "second_found": (sum(r["found_second"] for r in two) / len(two)) if two else None,
        "by_kind": {k: f"{a}/{b}" for k, (a, b) in by_kind.items()},
    }


def write_report(all_res):
    L = ["# 评估报告", "", "20 个场景，每个场景埋一个故障（或不埋），Agent 不知道答案，跑完自动和标准答案比对。", "",
         "| 指标 | " + " | ".join(all_res) + " |", "|---|" + "---|" * len(all_res)]
    S = {m: summarize(v) for m, v in all_res.items()}
    rows = [("根因准确率", lambda s: f"{s['accuracy']:.0%}"), ("按时提交报告", lambda s: f"{s['finished']:.0%}"),
            ("编造设备", lambda s: f"{s['hallucination']:.0%}"), ("错了还说'高置信'", lambda s: f"{s['overconfident']:.0%}"),
            ("两个问题都找到", lambda s: "-" if s["second_found"] is None else f"{s['second_found']:.0%}"),
            ("平均耗时", lambda s: f"{s['avg_seconds']:.0f} 秒"), ("平均轮数", lambda s: f"{s['avg_steps']:.1f}"),
            ("平均 token", lambda s: f"{s['avg_tokens']:.0f}"), ("平均被打回次数", lambda s: f"{s['avg_rejects']:.1f}")]
    for name, f in rows:
        L.append(f"| {name} | " + " | ".join(f(S[m]) for m in all_res) + " |")
    L += ["", "## 按场景类型", "", "| 场景 | " + " | ".join(all_res) + " |", "|---|" + "---|" * len(all_res)]
    for k, cn in KIND_CN.items():
        L.append(f"| {cn}（{k}） | " + " | ".join(S[m]["by_kind"].get(k, "-") for m in all_res) + " |")
    for m, rs in all_res.items():
        L += ["", f"## 明细：{m}", "", "| # | 类型 | 答案 | Agent 判断 | 对错 | 置信度 | 轮数 | 秒 |", "|---|---|---|---|---|---|---|---|"]
        for r in rs:
            L.append(f"| {r['case']} | {KIND_CN[r['kind']]} | {r['want']} | {r['pred']} | {'✅' if r['correct'] else '❌'} | "
                     f"{r['confidence'] or '-'} | {r['steps'] or '-'} | {r['seconds'] or '-'} |")
    (ROOT / "eval" / "report.md").write_text("\n".join(L) + "\n")
    return S


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", default="qwen2.5:7b")
    ap.add_argument("--cases", default="1-20")
    a = ap.parse_args()
    lo, hi = map(int, a.cases.split("-"))
    truths = [t for t in json.loads((EVAL / "index.json").read_text()) if lo <= t["case"] <= hi]
    OUT.mkdir(parents=True, exist_ok=True)
    all_res = {}
    for f in OUT.glob("*.json"):  # 合并已有结果，方便分批跑
        all_res[f.stem.replace("_", ":", 1)] = json.loads(f.read_text())
    for m in a.models.split(","):
        rs = []
        for t in truths:
            r = await run_case(t, m)
            rs.append(r)
            print(f"[{m}] case {t['case']:02d} {t['kind']:<10} 答案 {r['want']:<10} 判断 {r['pred']:<10} "
                  f"{'✅' if r['correct'] else '❌'} {r['seconds']}s {r.get('error') or ''}", flush=True)
        all_res[m] = rs
        (OUT / f"{m.replace(':', '_', 1)}.json").write_text(json.dumps(rs, ensure_ascii=False, indent=2))
        write_report(all_res)
    S = write_report(all_res)
    print(json.dumps(S, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
