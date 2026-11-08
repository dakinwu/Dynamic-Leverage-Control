FROM python:3.11-slim

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

EXPOSE 8000

CMD ["sh", "-c", "alembic upgrade head && uvicorn dynamic_leverage.api.app:app --host 0.0.0.0 --port 8000"]
