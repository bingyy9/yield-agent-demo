"""[7] 检索对比：关键词检索 vs 语义检索（bge-m3）。

工程师描述问题时用的是口语（"外圈一圈坏了"），而 8D 报告里写的是术语（"Edge-Ring"），
关键词检索对不上字就找不到。这里用 12 条口语化提问，每条标好"应该找到的报告"，看 top-3 命中率。
uv run python eval/rag_compare.py   → 结果写到 eval/rag_report.md
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "servers"))
import fabdb  # noqa: E402
from fab_tools import keyword_search, semantic_search  # noqa: E402

QUERIES = [
    ("刻蚀机保养后晶圆边缘一圈坏了", {"8D-2025-031", "8D-2025-107"}),
    ("外圈的芯片挂了一圈，刚换过零件", {"8D-2025-031", "8D-2025-107"}),
    ("片子上有一道长长的划痕", {"8D-2025-128", "8D-2024-121", "8D-2026-198"}),
    ("中间一坨坏点", {"8D-2024-142", "8D-2026-135"}),
    ("每个曝光区同一个位置都有缺陷", {"8D-2026-156"}),
    ("整片都不行，参数全偏了", {"8D-2025-170", "8D-2024-163"}),
    ("RF 反射功率报警，刻蚀不均匀", {"8D-2024-184", "8D-2025-031"}),
    ("洗完腔体之后颗粒变多", {"8D-2025-191"}),
    ("测试机换了针卡以后良率掉了", {"8D-2024-226"}),
    ("刻蚀刻过头了，没停下来", {"8D-2026-177"}),
    ("吸盘温度不稳，边上良率低", {"8D-2026-114"}),
    ("机械手把片子刮花了", {"8D-2026-198"}),
]


def hit(results, want, k=3):
    return any(r["id"] in want for _, r in results[:k])


def main():
    kb = fabdb.load_kb()
    L = ["# 检索对比：关键词 vs 语义（bge-m3）", "", "12 条口语化提问，看正确报告是否出现在前 3 条。", "",
         "| 提问 | 关键词 top3 | 语义 top3 | 语义第一名 |", "|---|---|---|---|"]
    kw = sem = 0
    for q, want in QUERIES:
        a, b = keyword_search(q, kb), semantic_search(q, kb)
        ha, hb = hit(a, want), hit(b, want)
        kw += ha; sem += hb
        top = f"{b[0][1]['id']} {b[0][1]['title']}（{b[0][0]:.2f}）" if b else "-"
        L.append(f"| {q} | {'✅' if ha else '❌'} | {'✅' if hb else '❌'} | {top} |")
        print(f"{'✅' if ha else '❌'} {'✅' if hb else '❌'}  {q}")
    n = len(QUERIES)
    L += ["", f"**关键词 top-3 命中率 {kw}/{n}（{kw/n:.0%}），语义 top-3 命中率 {sem}/{n}（{sem/n:.0%}）**", "",
          "结论：口语描述和报告术语字面对不上时，关键词检索基本失效；语义检索靠向量相似度能对上意思。",
          "工具里默认用语义检索，向量模型不可用时自动降级为关键词检索。"]
    (ROOT / "eval" / "rag_report.md").write_text("\n".join(L) + "\n")
    print(f"关键词 {kw}/{n}  语义 {sem}/{n}")


if __name__ == "__main__":
    main()
