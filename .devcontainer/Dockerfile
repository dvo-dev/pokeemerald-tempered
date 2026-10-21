FROM mcr.microsoft.com/devcontainers/base:3-noble AS base

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        binutils-arm-none-eabi \
        gcc-arm-none-eabi \
        libnewlib-arm-none-eabi \
        ca-certificates \
        curl \
        git \
        libpng-dev \
        libelf1t64 \
        pkg-config \
        python3 \
    && rm -rf /var/lib/apt/lists/*

COPY .devcontainer/github-auth.sh /usr/local/share/devcontainer/github-auth.sh

RUN printf '\nsource /usr/local/share/devcontainer/github-auth.sh\n' \
    >> /home/vscode/.bashrc

FROM base AS qa-builder

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        cmake python3-dev python3-venv libffi-dev zlib1g-dev \
        libavcodec-dev libavformat-dev libavutil-dev libavfilter-dev \
        libswresample-dev libswscale-dev \
    && rm -rf /var/lib/apt/lists/*

COPY tools/qa/ /tmp/qa-source/

RUN python3 -m venv /opt/qa-venv \
    && /opt/qa-venv/bin/python -m pip install --no-cache-dir \
        -r /tmp/qa-source/requirements.lock \
    && git init /tmp/mgba \
    && git -C /tmp/mgba remote add origin https://github.com/mgba-emu/mgba.git \
    && git -C /tmp/mgba fetch --depth=1 origin fd9dc5c2cbf234365068855851107e762fc6211b \
    && git -C /tmp/mgba checkout --detach FETCH_HEAD \
    && QA_BUILD_ENVIRONMENT=image bash /tmp/qa-source/build_runtime.sh \
        /tmp/mgba /tmp/mgba-build /opt/mgba /opt/qa-venv/bin/python

FROM base AS devcontainer

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ffmpeg python3-venv libffi8 libpython3.12t64 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=qa-builder /opt/qa-venv /opt/qa-venv
COPY --from=qa-builder /opt/mgba /opt/mgba
COPY tools/qa/bin/qa /usr/local/bin/qa
COPY tools/qa/check_image.py /opt/mgba/share/qa/check_image.py

RUN /opt/qa-venv/bin/python -I -B /opt/mgba/share/qa/check_image.py \
    && chmod 755 /usr/local/bin/qa
