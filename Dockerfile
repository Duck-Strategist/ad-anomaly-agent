# 前端和 seed 共用的镜像（SPEC Q23）。密钥不在镜像里，运行时由 compose 的 env_file 注入。
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.11.7 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONUNBUFFERED=1 PYTHONIOENCODING=utf-8

WORKDIR /app

# 先只装依赖：只改代码时这一层走缓存，不用重新装
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY adagent ./adagent
COPY datagen ./datagen
ENV PATH="/app/.venv/bin:$PATH"

# 用非 root 用户运行
RUN useradd --create-home app && chown -R app /app
USER app

EXPOSE 8000
CMD ["uvicorn", "adagent.web.server:app", "--host", "0.0.0.0", "--port", "8000"]
