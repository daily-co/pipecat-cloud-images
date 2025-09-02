# Use a Python image with uv pre-installed
ARG PYTHON_VERSION=3.12
FROM ghcr.io/astral-sh/uv:python${PYTHON_VERSION}-trixie-slim

# Install the project into `/app`
WORKDIR /app

# Install system dependencies
RUN apt update && apt install -y libopenblas-dev libresample1 libresample-dev && apt clean

# Enable bytecode compilation
ENV UV_COMPILE_BYTECODE=1

# Copy from the cache instead of linking since it's a mounted volume
ENV UV_LINK_MODE=copy

# Install the project's dependencies using the lockfile and settings
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-install-project --no-dev

# Set up Krisp symlink in the virtual environment's site-packages
RUN VENV_SITE_PACKAGES=$(/app/.venv/bin/python -c "import site; print(site.getsitepackages()[0])") && \
    ln -s /krisp/python/pipecat_ai_krisp ${VENV_SITE_PACKAGES}/pipecat_ai_krisp

# Copy application files
COPY ./app.py ./waiting_server.py ./pipecatcloud_system.py /app/

# Place executables in the environment at the front of the path
ENV PATH="/app/.venv/bin:$PATH"

# Reset the entrypoint, don't invoke `uv`
ENTRYPOINT []

# Set version environment variable
ARG VERSION
ENV IMAGE_VERSION=$VERSION

# Set default command
CMD ["sh", "-c", "if [ -f /app/pre-app.sh ]; then sh /app/pre-app.sh; fi && exec python app.py"]
