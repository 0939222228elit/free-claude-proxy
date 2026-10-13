FROM debian:bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    git \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

ENV PORT=8082
ENV HOST=0.0.0.0
ENV FCC_OPEN_BROWSER=false
ENV HATCH_VCS_VERSION=6.16.0
ENV SETUPTOOLS_SCM_PRETEND_VERSION=6.16.0
ENV UV_COMPILE_BYTECODE=1

COPY . /app

RUN uv sync --no-dev

EXPOSE 8082

CMD ["sh", "-c", "uv run fcc-server"]
