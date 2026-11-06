# librus-watch: obraz usługi (E8, ADR-006).
#
# Budowa poza serwerem docelowym, przenoszony jest tylko obraz (bez repozytorium):
#   docker buildx build --platform linux/amd64 -t librus-watch:<wersja> --load .
#   docker save librus-watch:<wersja> | ssh <serwer> podman load
#
# W obrazie nie ma plików źródłowych projektu: pakiet `librus_watch` jest
# zainstalowany z koła (wheel) i skompilowany do kodu bajtowego, a pliki .py
# są usuwane. Zależności są przypięte w `uv.lock` (hashe).

ARG PYTHON_IMAGE=python:3.12-slim@sha256:4840d514c583a4d3d8cad3940e68dee6f0f6f3635c12fcd0a3d74c21a5b93a54

FROM ${PYTHON_IMAGE} AS build
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
RUN pip install --no-cache-dir uv==0.7.3
WORKDIR /src
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src
RUN uv venv /opt/venv \
 && uv export --frozen --no-dev --no-emit-project --format requirements-txt > /tmp/req.txt \
 && VIRTUAL_ENV=/opt/venv uv pip install --require-hashes -r /tmp/req.txt \
 && uv build --wheel --out-dir /tmp/dist \
 && VIRTUAL_ENV=/opt/venv uv pip install --no-deps /tmp/dist/*.whl \
 && pkg=$(/opt/venv/bin/python -c "import librus_watch, pathlib; print(pathlib.Path(librus_watch.__file__).parent)") \
 && /opt/venv/bin/python -m compileall -q -b "$pkg" \
 && find "$pkg" -name '*.py' -delete \
 && find "$pkg" -name '__pycache__' -type d -prune -exec rm -rf {} +

FROM ${PYTHON_IMAGE}
LABEL org.opencontainers.image.title="librus-watch" \
      org.opencontainers.image.licenses="GPL-3.0-or-later" \
      org.opencontainers.image.source="https://github.com/hretheum/librus-watch"
RUN groupadd --system --gid 10001 app && useradd --system --uid 10001 --gid app --no-create-home app \
 && install -d -o app -g app -m 700 /data /stats
COPY --from=build /opt/venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    LIBRUS_WATCH_CONFIG=/config/config.yaml
USER 10001:10001
VOLUME ["/data"]
ENTRYPOINT ["librus-watch"]
CMD ["run"]
