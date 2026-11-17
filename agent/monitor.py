"""[16] 主动巡检：不等人来问，定时扫描新出测试结果的批次，良率低于阈值就自动启动 Agent 排查。

uv run python agent/monitor.py                 # 扫一次
uv run python agent/monitor.py --every 600     # 每 10 分钟扫一次（生产上用 cron / 调度系统）
已排查过的批次记在 traces/monitor_state.json，不重复查。报告里的操作仍然只是"建议"，要人审批。
"""
import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "servers"))
sys.path.insert(0, str(ROOT / "agent"))
import fabdb  # noqa: E402
from main import Session, fmt_report  # noqa: E402

STATE = ROOT / "traces" / "monitor_state.json"


def scan(threshold: float) -> list[dict]:
    return fabdb.rows("""SELECT l.lot, l.product, ROUND(AVG(t.yield_pct),1) avg_yield, COUNT(*) n
                         FROM lots l JOIN test_yield t ON t.lot=l.lot WHERE l.status='TESTED'
                         GROUP BY l.lot HAVING avg_yield < ? ORDER BY avg_yield""", (threshold,))


async def once(threshold, limit):
    STATE.parent.mkdir(exist_ok=True)
    done = set(json.loads(STATE.read_text())) if STATE.exists() else set()
    todo = [r for r in scan(threshold) if r["lot"] not in done][:limit]
    print(f"巡检：平均良率 < {threshold}% 的新批次 {len(todo)} 个")
    for r in todo:
        print(f"\n=== 自动排查 {r['lot']}（{r['product']}，平均 {r['avg_yield']}%）===")
        async with Session(quiet=True) as s:
            res = await s.ask(f"{r['lot']} 平均良率 {r['avg_yield']}%，低于预警线 {threshold}%，请排查原因。")
        print(fmt_report(res["report"]) if res.get("report") else res)
        print(f"轨迹：traces/{res.get('trace')}.jsonl  —— 已推送给值班工程师审批（演示中仅打印）")
        done.add(r["lot"])
        STATE.write_text(json.dumps(sorted(done)))


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--threshold", type=float, default=88)
    ap.add_argument("--limit", type=int, default=3)
    ap.add_argument("--every", type=int, default=0, help="秒；0 = 只扫一次")
    a = ap.parse_args()
    while True:
        await once(a.threshold, a.limit)
        if not a.every:
            break
        await asyncio.sleep(a.every)


if __name__ == "__main__":
    asyncio.run(main())
