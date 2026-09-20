# 비서를 담는 이미지. Oracle Cloud A1(arm64, Ubuntu 24.04)에서 돌린다.
#
# - 서버가 arm64라 **서버에서 직접 빌드한다.** 다른 아키텍처에서 흉내 내 만들지 않는다.
# - Playwright는 Ubuntu 22.04/24.04의 x86-64·arm64를 공식 지원한다 (playwright.dev, 2026-09-20 확인).
#   그래서 서버와 같은 Ubuntu 24.04를 바탕으로 삼는다.
# - 파이썬은 uv가 받아 온다. 우분투가 주는 파이썬은 3.12라 이 프로젝트(3.14)에 맞지 않는다.
# - 개인 파일(private/)은 이미지에 넣지 않는다. 볼륨으로 붙인다 (절대 규칙 12).

FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive \
    TZ=Asia/Seoul \
    PYTHONUNBUFFERED=1 \
    UV_PYTHON_INSTALL_DIR=/opt/python \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    # 브라우저를 모두가 읽을 수 있는 곳에 둔다. 비서는 root가 아닌 사용자로 돈다.
    PLAYWRIGHT_BROWSERS_PATH=/opt/playwright

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates tzdata fonts-nanum \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# 의존성을 먼저 넣는다. 코드만 고쳤을 때 이 층을 다시 만들지 않는다.
COPY pyproject.toml uv.lock .python-version ./
RUN uv python install && uv sync --frozen --no-dev

# 크로미움과 그것이 필요로 하는 시스템 패키지. root일 때 해야 apt를 쓸 수 있다.
RUN uv run --no-dev playwright install --with-deps chromium \
    && chmod -R a+rX /opt/playwright \
    && rm -rf /var/lib/apt/lists/*

COPY app ./app
COPY prompts ./prompts
COPY config.toml ./

# root로 돌리지 않는다. private/는 볼륨으로 붙으므로 이미지에는 없다.
RUN useradd --create-home --uid 10001 assistant && chown -R assistant:assistant /app
USER assistant

# 종료 신호를 파이썬이 직접 받아야 스케줄러가 정리하고 내려간다
STOPSIGNAL SIGINT

CMD ["uv", "run", "--no-dev", "python", "-m", "app.main"]
