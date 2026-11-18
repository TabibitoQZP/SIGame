FROM node:22-bookworm-slim@sha256:e9f565d32859f0cbfb52125eafe8be39390f24dac302dc3cdf566e8ba3ab21ee AS node
FROM mcr.microsoft.com/playwright/python:v1.62.0-noble@sha256:aa81288e738725378becba5b3e06cb0f3a7f012a610e87e8d767a090ea3f740d AS dependencies
ARG CLAUDE_CODE_VERSION=2.1.325
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules/npm /usr/local/lib/node_modules/npm
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm
RUN apt-get update && apt-get install -y --no-install-recommends python3-venv libseccomp2 \
    && rm -rf /var/lib/apt/lists/*
RUN if [ -n "$CLAUDE_CODE_VERSION" ]; then npm install -g "@anthropic-ai/claude-code@$CLAUDE_CODE_VERSION"; fi
RUN python3 -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH" VIRTUAL_ENV="/opt/venv" PYTHONUNBUFFERED=1
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    pip install uv==0.11.32 && uv sync --frozen --no-dev --active --no-install-project
COPY src/gpa/execution/bridge.py /opt/rgbgame/bridge.py
COPY src/gpa/testing/claude.py /opt/rgbgame/fake_claude.py
RUN mkdir -p /work && chown pwuser:pwuser /work

FROM dependencies AS agent
USER pwuser
WORKDIR /work
CMD ["/opt/venv/bin/python", "-c", "import time; time.sleep(86400)"]

FROM dependencies AS browser
COPY src/gpa/execution/browser_server.py /opt/rgbgame/browser_server.py
USER pwuser
CMD ["/opt/venv/bin/python", "/opt/rgbgame/browser_server.py"]
