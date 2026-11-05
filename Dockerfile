# syntax=docker/dockerfile:1
# MicroPaw image: uv builds the environment, a slim Python image runs it as a non-root user.
# Build with more channels or features: docker build --build-arg EXTRAS=telegram,pdf,browser .
# No secrets go into the image. Pass keys at run time (env_file) or keep them in the data volume.

FROM python:3.13-slim-trixie AS build
COPY --from=ghcr.io/astral-sh/uv:0.13.1 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=0 UV_PYTHON_PREFERENCE=only-system
ARG EXTRAS=telegram,discord,slack,whatsapp,pdf
WORKDIR /src
COPY pyproject.toml PYPI_README.md ./
COPY src ./src
RUN uv venv /opt/venv \
 && VIRTUAL_ENV=/opt/venv uv pip install --no-cache ".[${EXTRAS}]"

FROM python:3.13-slim-trixie
RUN groupadd --gid 10001 paw \
 && useradd --uid 10001 --gid paw --create-home --home-dir /home/paw --shell /usr/sbin/nologin paw \
 && mkdir /home/paw/.micropaw \
 && chown paw:paw /home/paw/.micropaw
COPY --from=build /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH" PYTHONUNBUFFERED=1 HOME=/home/paw MICROPAW_HOME=/home/paw/.micropaw MICROPAW_IN_DOCKER=1 MICROPAW_SANDBOX=off
USER 10001:10001
WORKDIR /home/paw
VOLUME /home/paw/.micropaw
HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 CMD ["paw", "doctor", "--quiet"]
CMD ["paw", "start", "--headless"]
