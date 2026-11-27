# python:3.13.14-slim
FROM python@sha256:eb43ff125d8d58d7449dcba7d336c23bcac412f526d861db493b9994d8010280 AS builder

ARG DATA_PROVIDER
RUN test -n "$DATA_PROVIDER" || (echo "DATA_PROVIDER build arg is required" && exit 1)

WORKDIR /tmp

COPY ./poetry.lock ./pyproject.toml /tmp/

RUN set -ex && \
  python -m pip install --disable-pip-version-check --no-cache-dir poetry==2.4.1 && \
  poetry self add poetry-plugin-export==1.10.0 && \
  poetry export -n -f requirements.txt --extras ${DATA_PROVIDER} -o requirements.txt

# python:3.13.14-slim
FROM python@sha256:eb43ff125d8d58d7449dcba7d336c23bcac412f526d861db493b9994d8010280

ARG DATA_PROVIDER

WORKDIR /app

RUN mkdir logs

COPY --from=builder /tmp/requirements.txt ./
COPY ./config/logging.yaml ./config/logging.yaml
COPY ./fable_resolver ./fable_resolver
COPY ./pyproject.toml ./

RUN set -ex && \
  groupadd --system nonroot && \
  useradd --system --gid nonroot --create-home nonroot && \
  chown -R nonroot:nonroot /app

RUN set -ex && \
  python -m pip install --disable-pip-version-check --no-cache-dir -r requirements.txt

ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1
ENV DATA_PROVIDER__SOURCE=${DATA_PROVIDER}

EXPOSE 8080/tcp

ENTRYPOINT [ "/usr/local/bin/python", "-m", "uvicorn", "fable_resolver.main:app" ]
CMD [ "--host", "0.0.0.0", "--port", "8080", "--workers", "4" ]

USER nonroot
