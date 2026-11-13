FROM python:3.11.17-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY config/runtime-constraints.txt ./config/runtime-constraints.txt
RUN pip install -r config/runtime-constraints.txt

COPY pyproject.toml README.md alembic.ini ./
COPY src ./src
COPY migrations ./migrations
RUN pip install --no-deps .

# Run as an unprivileged user. UID 1000 matches the usual first host user, so a
# bind-mounted secrets file with mode 600 stays readable on Linux hosts.
RUN useradd --system --uid 1000 --no-create-home --shell /usr/sbin/nologin dvl     && mkdir -p /app/runtime     && chown dvl /app/runtime
# The code stays root-owned; the broker SDK writes its log and contract cache
# only under this directory.
ENV SJ_HOME_PATH=/app/runtime     SJ_LOG_PATH=/app/runtime/shioaji.log
USER dvl

EXPOSE 8000

CMD ["sh", "-c", "alembic upgrade head && uvicorn dynamic_leverage.api.app:app --host 0.0.0.0 --port 8000"]
