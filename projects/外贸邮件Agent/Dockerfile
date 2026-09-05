# 外贸邮件 Agent · Dockerfile
# ============================
# 零重依赖，仅 requests，可直接 docker run 启动
#
# 构建：
#   docker build -t mail-agent:latest .
# 运行（需挂载配置文件和 API Key）：
#   docker run -p 7860:7860 \
#     -e DEEPSEEK_API_KEY=your-key \
#     -e ZHIPU_API_KEY=your-key \
#     -v $(pwd)/config:/app/config \
#     mail-agent:latest
#
# 健康检查：
#   curl http://localhost:7860/api/health
# 指标：
#   curl http://localhost:7860/api/metrics

FROM python:3.11-slim

WORKDIR /app

# 复制依赖并安装（仅 requests）
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制项目代码
COPY . .

# config/llm_config.json 已在 .dockerignore 排除，需运行时注入
# 使用环境变量或挂载 config 目录均可

EXPOSE 7860

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:7860/api/health')" || exit 1

CMD ["python", "src/demo_server.py", "--port", "7860"]
