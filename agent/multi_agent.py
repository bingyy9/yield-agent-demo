"""[15] 多 Agent 分工（对照实验版）。

主控（代码写死的流程，不是 LLM）按顺序调度三个"专家"，每个专家只看得到自己需要的工具和上下文：
  1. 数据分析 Agent：get_lot_yield + commonality_analysis → 输出嫌疑设备和坏片
  2. 设备专家 Agent：get_equipment_alarms + search_8d_reports + find_impacted_lots → 输出设备证据和影响范围
  3. 报告撰写 Agent：不调任何工具，只把前两步的结论整理成 submit_report 格式

和单 Agent 版本对比的意义：
  - 好处：每个 Agent 的提示词短、工具少，小模型更不容易跑偏；每段可单独测试和替换模型。
  - 代价：多了交接，信息会在交接中丢；总调用次数和耗时更高；流程固定，遇到意外情况没法灵活跳步。
  - 结论：本场景流程基本固定、工具只有 7 个，单 Agent + 校验器已经够用；多 Agent 留作工具数量膨胀后的演进方向。
uv run python agent/multi_agent.py LOT-A1024
"""
import asyncio
import json
import sys
import time

import ollama

from main import MODEL, Session, SUBMIT_TOOL, fmt_report

EXPERTS = {
    "数据分析": (["get_lot_yield", "commonality_analysis"],
             "你是良率数据分析员。先用 get_lot_yield 看坏片分布，再只拿主要失效图案的坏片做 commonality_analysis。"
             "最后用中文输出：主要失效图案、坏片列表、嫌疑设备（得分最高的）、证据强度、其他失效图案的晶圆。不要猜测原因。"),
    "设备专家": (["get_equipment_alarms", "search_8d_reports", "find_impacted_lots"],
             "你是设备工程师。根据数据分析结论，查嫌疑设备的报警和保养记录（没记录就把 days 扩到 14），"
             "检索相似的历史 8D 报告，再用 find_impacted_lots 从异常开始时间查影响范围。用中文输出事实，不要编造。"),
}


async def run_expert(sess: Session, role: str, task: str, max_steps=6) -> tuple[str, list]:
    names, prompt = EXPERTS[role]
    tools = [t for t in sess.tools if t["function"]["name"] in names]
    msgs = [{"role": "system", "content": prompt}, {"role": "user", "content": task}]
    log = []
    for _ in range(max_steps):
        resp = await asyncio.to_thread(ollama.chat, model=sess.model, messages=msgs, tools=tools, options={"temperature": 0})
        m = resp.message
        msgs.append({"role": "assistant", "content": m.content or "", "tool_calls": [
            {"function": {"name": c.function.name, "arguments": dict(c.function.arguments)}} for c in (m.tool_calls or [])]})
        if not m.tool_calls:
            return m.content, log
        for c in m.tool_calls:
            args = dict(c.function.arguments)
            args.pop("thought", None)
            text, parsed = await sess.call_tool(c.function.name, args)
            log.append({"name": c.function.name, "args": args, "result": parsed})
            print(f"  [{role}] {c.function.name}({json.dumps(args, ensure_ascii=False)[:80]})")
            msgs.append({"role": "tool", "tool_name": c.function.name, "content": text})
    return "（步数用完）", log


async def main(lot: str):
    t0 = time.time()
    async with Session(quiet=True) as sess:
        print("① 数据分析 Agent")
        a, log1 = await run_expert(sess, "数据分析", f"分析 {lot} 的低良率晶圆。")
        print(a, "\n② 设备专家 Agent")
        b, log2 = await run_expert(sess, "设备专家", f"批次 {lot}。数据分析结论：\n{a}")
        print(b, "\n③ 报告撰写 Agent")
        resp = await asyncio.to_thread(ollama.chat, model=sess.model, tools=[SUBMIT_TOOL], options={"temperature": 0}, messages=[
            {"role": "system", "content": "你是报告撰写员。只根据给你的材料调用 submit_report，证据每条以 [工具名] 开头，不能添加材料里没有的事实。"},
            {"role": "user", "content": f"数据分析：\n{a}\n\n设备排查：\n{b}"}])
        calls = resp.message.tool_calls or []
        report = dict(calls[0].function.arguments) if calls else {"conclusion": resp.message.content}
        print(fmt_report(report))
        print(f"\n多 Agent 用时 {time.time() - t0:.0f}s，工具调用 {len(log1) + len(log2)} 次，LLM 调用至少 3 段")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "LOT-A1024"))
