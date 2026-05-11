FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    wget xz-utils ca-certificates \
    libgtk-3-0t64 libdrm2 libgbm1 libxcb-dri3-0 \
    inkscape \
    && rm -rf /var/lib/apt/lists/*

# install inkstitch (detect arch)
ARG INKSTITCH_VERSION=3.2.2
RUN ARCH=$(dpkg --print-architecture) && \
    if [ "$ARCH" = "arm64" ]; then INKARCH="aarch64"; else INKARCH="x86_64"; fi && \
    wget -q "https://github.com/inkstitch/inkstitch/releases/download/v${INKSTITCH_VERSION}/inkstitch-${INKSTITCH_VERSION}-linux-${INKARCH}.tar.xz" \
    -O /tmp/inkstitch.tar.xz && \
    mkdir -p /opt/inkstitch && \
    tar xJf /tmp/inkstitch.tar.xz -C /opt/inkstitch && \
    rm /tmp/inkstitch.tar.xz
ENV INKSTITCH_BIN=/opt/inkstitch/inkstitch/bin/inkstitch

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt vtracer

COPY pipeline/ pipeline/
COPY server.py worker.py ./

ENV RESULTS_DIR=/data/results
ENV VTRACER_BIN=vtracer

EXPOSE 8000
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000"]
