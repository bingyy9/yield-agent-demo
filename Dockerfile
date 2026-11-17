# [11] 容器化：Agent + MCP 工具 + 网页在一个镜像里；大模型用宿主机的 Ollama（GPU 在宿主机上）
FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir uv
COPY pyproject.toml uv.lock* ./
RUN uv sync --no-dev || uv sync
COPY . .
RUN [ -f data/fab.db ] || uv run python scripts/gen_data.py
ENV OLLAMA_HOST=http://host.docker.internal:11434 \
    YIELD_AGENT_MODEL=qwen2.5:7b \
    PYTHONUNBUFFERED=1
EXPOSE 8000
CMD ["uv", "run", "python", "-c", "import sys; sys.path.insert(0,'web'); import uvicorn, server; uvicorn.run(server.app, host='0.0.0.0', port=8000)"]
