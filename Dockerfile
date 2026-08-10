# ---- Build stage: install dependencies only, nothing else ----
FROM python:3.12-slim AS builder
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir --user -r requirements.txt

# ---- Runtime stage: a clean image, no build tools, no pip cache ----
FROM python:3.12-slim

# Never run the app as root inside the container.
RUN useradd --create-home --shell /bin/bash appuser

WORKDIR /app

# Only the installed Python packages come from the build stage -- not the
# build tools, not pip's cache, keeping the final image lean.
COPY --from=builder /root/.local /home/appuser/.local
COPY . .

RUN chown -R appuser:appuser /app
USER appuser
ENV PATH=/home/appuser/.local/bin:$PATH
ENV PYTHONUNBUFFERED=1

# Hits our own real /health endpoint, which already reports whether the
# switch connection is genuinely up -- not just "is the process running."
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" || exit 1

EXPOSE 8000

# No secrets here -- JWT_SECRET, REDIS_URL, HSM_MASTER_KEY_HEX all come
# from the environment at `docker run` / compose time, exactly like they
# already do when running this directly with uvicorn.
CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]
