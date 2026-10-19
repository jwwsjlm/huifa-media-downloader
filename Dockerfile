# syntax=docker/dockerfile:1.7
ARG USE_PREBUILT_FRONTEND=0
FROM node:24-bookworm-slim AS frontend-0
WORKDIR /source/frontend
COPY frontend/package*.json ./
RUN npm ci
COPY frontend/ ./
COPY languages/ /source/languages/
RUN npm run build

FROM scratch AS frontend-1
COPY frontend/dist/ /source/frontend/dist/

FROM frontend-${USE_PREBUILT_FRONTEND} AS frontend

FROM rust:1.99-bookworm AS rust
ARG TARGETARCH
WORKDIR /source
COPY Cargo.toml Cargo.lock runtime-tool-lock.json ./
COPY crates/ ./crates/
COPY src-tauri/Cargo.toml ./src-tauri/Cargo.toml
COPY src-tauri/src/ ./src-tauri/src/
RUN --mount=type=cache,target=/usr/local/cargo/registry --mount=type=cache,target=/usr/local/cargo/git --mount=type=cache,id=huifa-rust-target-${TARGETARCH},target=/source/target \
    cargo build --release --locked --package huifa-server && mkdir /out && cp target/release/huifa-server /out/huifa-server

FROM debian:bookworm-slim AS runtime
ARG TARGETARCH
RUN apt-get update && apt-get install --no-install-recommends -y ca-certificates curl unzip jq xz-utils chromium fonts-noto-cjk tini \
    && rm -rf /var/lib/apt/lists/*
COPY docker/install-tools.sh /tmp/install-tools.sh
COPY runtime-tool-lock.json /tmp/runtime-tool-lock.json
RUN sh /tmp/install-tools.sh "$TARGETARCH" /tmp/runtime-tool-lock.json && rm /tmp/install-tools.sh /tmp/runtime-tool-lock.json \
    && apt-get purge -y curl unzip jq xz-utils && apt-get autoremove -y \
    && groupadd --gid 10001 huifa && useradd --uid 10001 --gid 10001 --home-dir /data --no-create-home huifa \
    && mkdir -p /app/frontend /data /downloads && chown huifa:huifa /data /downloads
COPY --from=rust /out/huifa-server /app/huifa-server
COPY --from=frontend /source/frontend/dist/ /app/frontend/
ENV HUIFA_DATA_DIR=/data HUIFA_DOWNLOAD_DIR=/downloads HUIFA_TOOL_DIR=/usr/local/bin \
    HUIFA_FRONTEND_DIR=/app/frontend HUIFA_CHROMIUM=/usr/bin/chromium HUIFA_CONTAINER=1 \
    HOME=/data XDG_CACHE_HOME=/data/cache
WORKDIR /app
USER 10001:10001
EXPOSE 19491
VOLUME ["/data", "/downloads"]
STOPSIGNAL SIGTERM
ENTRYPOINT ["/usr/bin/tini", "--", "/app/huifa-server"]
