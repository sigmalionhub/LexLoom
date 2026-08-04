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

# Bake the ONNX weights into the image. Two things here are load-bearing.
#
# Placement: this must not go in the builder stage. Only site-packages and
# /usr/local/bin are copied forward, so a model downloaded there is discarded
# and the runtime image silently falls back to fetching from HuggingFace on
# first use — a container that is Up, answers /health/live, and fails every
# search. A misplaced RUN still builds successfully; the `test -d` below is
# what turns that into a build failure instead of a runtime one.
#
# Ownership: after USER lexloom, so the cache belongs to uid 10001. fastembed
# writes lock and temp files into this directory on every load, not only on
# download, so a root-owned cache gives a permission error at the first
# request rather than at build. Under /home/lexloom rather than under the
# working directory, for that reason twice over: /app itself is owned by root
# (WORKDIR runs before the --chown COPY), so nothing written at runtime can
# live there.
ENV FASTEMBED_CACHE_PATH=/home/lexloom/.cache/fastembed
# Downloaded through the application's own loader rather than by calling
# TextEmbedding directly here, so the build populates exactly the directory the
# runtime reads from. Two spellings of the cache path would be two paths, and
# the disagreement only shows up on a box with no network.
RUN python -c "from app.chat.embedding import get_embedder, model_is_cached; \
        get_embedder()._ensure(); \
        assert model_is_cached(), 'weights did not land in FASTEMBED_CACHE_PATH'" \
 && test -d "$FASTEMBED_CACHE_PATH" \
 && du -sh "$FASTEMBED_CACHE_PATH"

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
