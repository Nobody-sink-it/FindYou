FROM node:24-bookworm-slim AS life-ui
WORKDIR /frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM python:3.13-slim@sha256:b3575dcf816cdacc5292df77a6e3e59b8941631078373a045efa2ac1e9c5c3b2 AS sqlite-build
ARG SQLITE_VERSION=3510300
RUN apt-get update && apt-get install -y --no-install-recommends build-essential curl ca-certificates \
    && curl -fSL "https://www.sqlite.org/2026/sqlite-autoconf-${SQLITE_VERSION}.tar.gz" -o /tmp/sqlite.tar.gz \
    && mkdir /tmp/sqlite && tar xzf /tmp/sqlite.tar.gz -C /tmp/sqlite --strip-components=1 \
    && cd /tmp/sqlite && ./configure --prefix=/opt/sqlite --enable-fts5 && make -j2 && make install

FROM python:3.13-slim@sha256:b3575dcf816cdacc5292df77a6e3e59b8941631078373a045efa2ac1e9c5c3b2
ENV PYTHONUNBUFFERED=1 PYTHONUTF8=1 LD_LIBRARY_PATH=/opt/sqlite/lib
COPY --from=sqlite-build /opt/sqlite /opt/sqlite
WORKDIR /app
COPY requirements.lock requirements-bilibili.lock pyproject.toml ./
RUN pip install --no-cache-dir -r requirements.lock \
    && python -m venv /opt/bili && /opt/bili/bin/pip install --no-cache-dir -r requirements-bilibili.lock
COPY src ./src
COPY --from=life-ui /frontend/dist ./frontend/dist
RUN pip install --no-deps . \
    && python -c "import sqlite3; assert sqlite3.sqlite_version_info >= (3,51,3)" \
    && useradd --uid 10001 --create-home findyou && mkdir /app/data && chown findyou:findyou /app/data
USER findyou
ENV FINDYOU_DATA_DIR=/app/data FINDYOU_BILI_PYTHON=/opt/bili/bin/python
EXPOSE 8765
CMD ["findyou", "serve"]
