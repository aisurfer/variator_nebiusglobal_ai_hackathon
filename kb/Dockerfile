FROM python:3.12.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/srv/.venv \
    KB_EMBED_CACHE_DIR=/srv/models

COPY --from=ghcr.io/astral-sh/uv:0.9.5 /uv /usr/local/bin/uv
WORKDIR /srv

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project

# The runtime user owns the model and blob directories before the model is written: a `chown -R` after the
# download would copy the whole model (about 1.1 GB) into a second layer.
RUN useradd --uid 10001 --create-home kb \
    && mkdir -p /data/blobs /srv/models \
    && chown -R kb:kb /data /srv/models

# The embedding model is baked into the image so the stack runs without network access.
ARG KB_EMBED_MODEL=sentence-transformers/paraphrase-multilingual-mpnet-base-v2
ENV KB_EMBED_MODEL=${KB_EMBED_MODEL}
USER kb
RUN /srv/.venv/bin/python -c "import os; from fastembed import TextEmbedding; TextEmbedding(os.environ['KB_EMBED_MODEL'], cache_dir=os.environ['KB_EMBED_CACHE_DIR'])"
USER root

COPY app ./app
COPY config ./config
COPY tests ./tests

# No source feeds are baked in: `python -m kb.cli seed|replay` reads fixtures/<tenant>/timeline.jsonl.gz under
# KB_FIXTURES_DIR (default /srv/fixtures); mount a delivery there when seeding from a timeline.
ENV PATH=/srv/.venv/bin:$PATH \
    PYTHONPATH=/srv/app \
    KB_CONFIG_DIR=/srv/config

USER kb
EXPOSE 8000
CMD ["uvicorn", "kb.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
