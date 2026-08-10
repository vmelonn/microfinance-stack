# ---- Build stage: install dependencies only, nothing else ----
FROM python:3.12-slim AS builder
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ---- Runtime stage: a clean image, no build tools, no pip cache ----
FROM python:3.12-slim
WORKDIR /app

# Installed system-wide (not into a specific named user's home directory).
# This matters specifically for OpenShift: it runs containers under a
# RANDOMLY assigned UID by default, not necessarily any user defined in
# this image -- so anything tied to a fixed user's home directory can end
# up unreadable/unexecutable to whatever UID actually runs the container,
# even though the file genuinely exists (this surfaced as a confusing
# "executable file not found in $PATH" error, not a permission error).
COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

COPY . .

# OpenShift's randomly-assigned runtime UID is always a member of GID 0
# (the root group), regardless of which UID it actually is. Granting GID 0
# the same permissions the owner has is the standard, documented pattern
# for making an image work correctly under that model.
RUN chgrp -R 0 /app && chmod -R g=u /app

# A concrete non-root numeric UID (not a named user) -- respected by plain
# Docker/Compose for defense-in-depth there; OpenShift will override it
# with its own random UID regardless, which is exactly what the GID 0
# permissions above are for.
USER 1001

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