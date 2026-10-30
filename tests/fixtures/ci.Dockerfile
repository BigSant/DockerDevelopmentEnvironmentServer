FROM ubuntu:22.04

# Match the original CI's Python 3.10 and command availability. Tests stub host
# mutations; the Docker socket and the runner's home/credentials are not mounted.
RUN printf '#!/bin/sh\nexit 101\n' > /usr/sbin/policy-rc.d \
    && chmod +x /usr/sbin/policy-rc.d \
    && apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        ca-certificates docker.io git make openssh-client php-cli python3 systemd \
    && rm -rf /var/lib/apt/lists/*

ARG CI_UID=1000
ARG CI_GID=1000
RUN groupadd --gid "$CI_GID" ci \
    && useradd --uid "$CI_UID" --gid "$CI_GID" --create-home --shell /bin/bash ci
ENV HOME=/home/ci USER=ci LOGNAME=ci
USER ci
WORKDIR /workspace
