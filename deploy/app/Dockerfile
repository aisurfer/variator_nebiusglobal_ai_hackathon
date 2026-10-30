# syntax=docker/dockerfile:1.7
# CPU image for the VARIATOR app on a Nebius Serverless AI endpoint (cpu-d3 platform).
# The web app is variator.web.app:app (Starlette, src/variator/web); APP_MODULE defaults to it:
#   docker build -f deploy/app/Dockerfile \
#     --build-arg APP_MODULE=variator.web.app:app \
#     -t cr.eu-north1.nebius.cloud/<registry>/variator-app:latest .
# APP_MODULE can also be overridden at runtime with `--env APP_MODULE=...` on the endpoint,
# and a non-ASGI app (Streamlit, Gradio...) can replace the command with
# `--container-command` / `--args`.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

ARG APP_MODULE=variator.web.app:app
# ASGI server and any web-framework packages the app needs beyond pyproject dependencies.
ARG EXTRA_PACKAGES="uvicorn[standard]>=0.30"
# Use RUN_AS=root only if writes to a mounted bucket fail with "permission denied".
ARG RUN_AS=app

ENV APP_MODULE=${APP_MODULE} \
    HOST=0.0.0.0 \
    PORT=8000

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install . $EXTRA_PACKAGES \
 && useradd --create-home --uid 10001 app

USER ${RUN_AS}
EXPOSE 8000
# The endpoint's managed HTTPS URL terminates TLS and proxies to this port.
CMD ["sh", "-c", "exec uvicorn \"$APP_MODULE\" --host \"$HOST\" --port \"$PORT\" --proxy-headers --forwarded-allow-ips='*'"]
