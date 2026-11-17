"""良率异常排查 Agent（V1，命令行版）。

工作原理（Agent 循环）：
  1. 启动 MCP 服务（servers/fab_tools.py），拿到工具清单
  2. 把用户问题 + 工具清单交给本地大模型（Ollama）
  3. 模型要么"调用工具"，要么"给出最终答案"
  4. 如果调用工具：通过 MCP 执行，把结果追加到对话里，回到第 3 步
  5. 直到模型给出最终答案，或达到最大步数

运行：
  uv run python agent/main.py "LOT-A1024 良率掉了，帮我查一下原因"
"""
import asyncio
import json
import os
import sys
from pathlib import Path

import ollama
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parent.parent
MODEL = os.environ.get("YIELD_AGENT_MODEL", "qwen2.5:7b")
MAX_STEPS = 10

SYSTEM_PROMPT = """你是存储芯片工厂的良率分析 Agent，帮助良率工程师排查批次良率异常。

排查方法（按顺序，每一步都必须调用工具拿数据，不要凭空猜测）：
1. 用 get_lot_yield 查批次良率，找出低良率晶圆，按失效图案分组。
2. 对主要失效图案的那组晶圆，用 commonality_analysis 做共性分析，找出它们共同经过的设备/腔室。
3. 对共性分析排名第一的设备，用 get_equipment_alarms 查报警和保养记录。
4. 用 search_8d_reports 搜索历史相似案例（关键词用失效图案、设备类型、关键事件，例如 "Edge-Ring ETCH 保养 RF"）。
5. 给出最终报告。

注意：
- 少数和主要失效图案不同的低良率晶圆可能是独立问题，不要混进共性分析，但要在报告里单独提一句。
- 结论必须来自工具返回的数据。

最终报告用中文，格式：
【结论】一句话根因假设
【证据链】编号列出每条证据及其来自哪个工具
【置信度】高/中/低，并说明理由
【建议动作】具体措施（需工程师确认后执行，Agent 不会自动执行）
【其他发现】与主问题无关的异常（如有）"""


def mcp_tools_to_ollama(tools):
    """把 MCP 的工具描述转成 Ollama（OpenAI 风格）的 function 格式。"""
    return [{
        "type": "function",
        "function": {"name": t.name, "description": t.description or "", "parameters": t.input_schema},
    } for t in tools]


def show(tag, text, color="0"):
    print(f"\033[{color}m{tag}\033[0m {text}")


async def run(question: str, emit=None):
    """emit(event: dict) 可选：网页界面用它实时接收每一步。"""
    emit = emit or (lambda e: None)
    server = StdioServerParameters(command=sys.executable, args=[str(ROOT / "servers" / "fab_tools.py")])
    async with stdio_client(server) as (r, w), ClientSession(r, w) as session:
        await session.initialize()
        tools = (await session.list_tools()).tools
        show("[MCP]", f"已连接 fab-tools，可用工具：{', '.join(t.name for t in tools)}", "36")
        await emit({"type": "tools", "tools": [t.name for t in tools]})

        messages = [{"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": question}]
        ollama_tools = mcp_tools_to_ollama(tools)

        for step in range(1, MAX_STEPS + 1):
            resp = await asyncio.to_thread(ollama.chat, model=MODEL, messages=messages, tools=ollama_tools,
                               options={"temperature": 0})
            msg = resp.message
            messages.append(msg)

            if not msg.tool_calls:  # 没有工具调用 = 最终答案
                show(f"\n[第{step}步·最终报告]", "", "32")
                print(msg.content)
                await emit({"type": "final", "step": step, "content": msg.content})
                return msg.content

            for call in msg.tool_calls:
                name, args = call.function.name, dict(call.function.arguments)
                show(f"\n[第{step}步·调用工具]", f"{name}({json.dumps(args, ensure_ascii=False)})", "33")
                await emit({"type": "call", "step": step, "name": name, "args": args})
                result = await session.call_tool(name, args)
                text = result.content[0].text if result.content else ""
                preview = text if len(text) < 600 else text[:600] + " ...(截断)"
                show("[工具返回]", preview.replace("\n", " "), "90")
                await emit({"type": "result", "step": step, "name": name, "result": text})
                messages.append({"role": "tool", "content": text, "tool_name": name})

        show("[停止]", f"达到最大步数 {MAX_STEPS}，未得到最终报告", "31")
        await emit({"type": "error", "message": f"达到最大步数 {MAX_STEPS}"})


if __name__ == "__main__":
    q = " ".join(sys.argv[1:]) or "LOT-A1024 的良率从 92% 掉到 86%，帮我查一下原因。"
    show("[问题]", q, "1")
    asyncio.run(run(q))
