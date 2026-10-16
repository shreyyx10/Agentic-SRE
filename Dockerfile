# One image, two services. Cloud Run sets ASRE_SERVICE=ingest|analyse.
# Deployed by digest (never by tag) — see .github/workflows/ci.yml.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PORT=8080

WORKDIR /app

COPY requirements.lock ./
RUN pip install --no-cache-dir -r requirements.lock

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir --no-deps . \
 && useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin app


COPY docker/start.sh /usr/local/bin/start
RUN chmod 0555 /usr/local/bin/start

USER 10001
EXPOSE 8080
CMD ["start"]
