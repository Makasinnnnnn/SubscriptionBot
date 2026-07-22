FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY pyproject.toml README.md ./
COPY app ./app
RUN pip install --no-cache-dir . \
    && groupadd --system app \
    && useradd --system --gid app --home-dir /app app
COPY --chown=app:app alembic ./alembic
COPY --chown=app:app alembic.ini main.py ./
COPY --chown=app:app legal ./legal

USER app

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
