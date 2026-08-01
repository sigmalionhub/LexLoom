FROM python:3.12-slim AS builder
COPY --from=ghcr.io/astral-sh/uv:0.5.11 /uv /bin/uv
WORKDIR /build
COPY pyproject.toml ./
RUN uv pip install --system --no-cache -r pyproject.toml

FROM python:3.12-slim AS runtime
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    OMP_NUM_THREADS=1
RUN useradd --create-home --uid 10001 lexloom
COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin
WORKDIR /app
COPY --chown=lexloom:lexloom . .
USER lexloom
EXPOSE 8100
# --no-proxy-headers is load-bearing, not tidiness. uvicorn enables
# --proxy-headers by default and, for a peer in --forwarded-allow-ips
# (default: 127.0.0.1), REWRITES scope["client"] from X-Forwarded-For before
# the application sees it. `app.limits.client_ip` then reads an address uvicorn
# chose by uvicorn's rules — a second trust list nobody configured, whose
# fallback branch returns the *first* entry of the header, which is the one a
# caller controls under an appending proxy. Two components deriving the client
# address is one too many; this leaves exactly one, and TRUSTED_PROXIES is the
# only place it is configured.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8100", "--workers", "1", \
     "--no-proxy-headers"]
