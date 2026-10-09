# T12 交付基线：单实例本机应用镜像。
# 镜像只包含运行所需的 src/、db/ 与 requirements.runtime.txt；
# 不含测试代码、可选嵌入模型栈，也不含任何密钥（密钥来自运行环境变量）。
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app/src

WORKDIR /app

COPY requirements.runtime.txt ./requirements.runtime.txt
RUN pip install --no-cache-dir -r requirements.runtime.txt

COPY src/ ./src/
COPY db/ ./db/

# 容器内不落盘业务数据（导出与检查点都在内存/数据库），以非 root 运行。
RUN useradd --system --uid 10001 appuser
USER 10001

EXPOSE 8000

# 就绪检查走 /health/ready：要求 checkpoint 可用且业务 schema 完整，
# 不只是端口可连。
HEALTHCHECK --interval=10s --timeout=5s --start-period=20s --retries=10 \
    CMD python -c "import sys, urllib.request; r = urllib.request.urlopen('http://127.0.0.1:8000/health/ready', timeout=4); sys.exit(0 if r.status == 200 else 1)"

# 每次启动先执行幂等迁移（新库和已有数据卷都适用），迁移失败则不启动服务。
CMD ["sh", "-c", "python -m applypilot.db && exec python -m uvicorn applypilot.api:app --app-dir /app/src --host 0.0.0.0 --port 8000"]
