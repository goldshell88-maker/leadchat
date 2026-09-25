# fake-avito: маленький standalone-мок API Авито (docs/07-TESTING-SECURITY.md §2).
# Один файл + state в памяти; лок-файл не нужен — зависимости ставятся из pyproject.

FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.5 /uv /usr/local/bin/uv

WORKDIR /srv/fake-avito

COPY pyproject.toml ./
RUN uv pip install --system --no-cache -r pyproject.toml

COPY main.py ./

RUN groupadd -r fakeavito && useradd -r -g fakeavito fakeavito
USER fakeavito

EXPOSE 8020

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8020"]
