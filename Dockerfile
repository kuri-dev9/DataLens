FROM python:3.11-slim-bookworm AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1
WORKDIR /build
COPY requirements.lock build-requirements.lock ./
RUN python -m pip wheel --no-deps --wheel-dir /wheels -r requirements.lock
RUN python -m pip install --no-deps -r build-requirements.lock
COPY pyproject.toml README.md ./
COPY src ./src
RUN python -m pip wheel --no-deps --no-build-isolation --wheel-dir /wheels .

FROM python:3.11-slim-bookworm AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

RUN groupadd --system datalens \
    && useradd --system --gid datalens --home-dir /nonexistent --no-create-home datalens
COPY --from=builder /wheels /wheels
RUN python -m pip install --no-index --find-links=/wheels datalens==0.1.0 \
    && rm -rf /wheels

USER datalens
EXPOSE 8000
ENTRYPOINT ["datalens"]
