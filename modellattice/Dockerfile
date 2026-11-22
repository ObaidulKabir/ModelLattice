FROM python:3.11-slim

WORKDIR /app

COPY pyproject.toml ./
COPY app ./app
COPY mcp_server ./mcp_server
COPY scripts ./scripts
COPY migrations ./migrations
COPY alembic.ini ./

RUN pip install --no-cache-dir .

EXPOSE 8008

# Shell form so ${PORT} expands: Railway (and most PaaS hosts) assign a
# port at runtime via $PORT and route to whatever the container actually
# listens on - a hardcoded --port would silently fail their health check.
# Falls back to 8008 for plain `docker run`/local use where $PORT isn't set.
CMD alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8008}
