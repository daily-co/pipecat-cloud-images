# Use a Python image with uv pre-installed
ARG PYTHON_VERSION=3.12
FROM ghcr.io/astral-sh/uv:python${PYTHON_VERSION}-trixie-slim

# Install the project into `/app`
WORKDIR /app

# Install system dependencies. tini is the runtime's PID 1 (see ENTRYPOINT).
RUN apt update && apt install -y libopenblas-dev libresample1 libresample-dev libgl1 libglib2.0-0 tini && apt clean

# Enable bytecode compilation
ENV UV_COMPILE_BYTECODE=1

# Copy from the cache instead of linking since it's a mounted volume
ENV UV_LINK_MODE=copy

# Install the project's dependencies using the lockfile and settings
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-install-project --no-dev

# Pre-download NLTK data to avoid runtime download issues
RUN /app/.venv/bin/python -m nltk.downloader punkt_tab -d /usr/local/share/nltk_data && \
    /app/.venv/bin/python -c "import nltk; nltk.data.find('tokenizers/punkt_tab')"

# Set up Krisp symlink in the virtual environment's site-packages
RUN VENV_SITE_PACKAGES=$(/app/.venv/bin/python -c "import site; print(site.getsitepackages()[0])") && \
    ln -s /krisp/python/pipecat_ai_krisp ${VENV_SITE_PACKAGES}/pipecat_ai_krisp && \
    echo "/opt/krisp_viva/krisp_audio" > ${VENV_SITE_PACKAGES}/krisp_viva.pth

# Copy application files
COPY ./app.py ./waiting_server.py ./pipecatcloud_system.py ./feature_manager.py ./pcc_observers.py ./pcc_structured_logs.py ./pcc_early_sigterm.py ./shared_state.py /app/

# Place executables in the environment at the front of the path
ENV PATH="/app/.venv/bin:$PATH"

# tini holds PID 1 and runs the command as its child (PCC-1004). This also
# replaces the base image's `uv` entrypoint.
#
# The kernel does not apply default signal dispositions to PID 1: a signal with
# no handler is discarded. The CMD below execs Python into PID 1, and uvicorn
# only installs its SIGTERM handler once every module-level import — the
# customer's bot module included — has finished. A SIGTERM in that window was
# dropped, and the container went on to start and serve on a pod the kubelet
# had already told to stop, until the grace-period SIGKILL up to two hours
# later. As tini's child Python takes the default disposition and dies at once;
# once uvicorn's handler is up tini forwards to it and the drain is unchanged.
# tini also reaps children orphaned by pre-app.sh, which Python as PID 1 never
# did.
#
# -e 143: a SIGTERM that lands before app.py has installed its handler — in
# the interpreter's own startup, or under a CMD that is not app.py — kills the
# child on the default disposition, exit 143. Remapping that to 0 keeps what
# every consumer of the exit code has always seen on a stop: Completed, and a
# Succeeded pod, never Error / Failed. (After startup app.py itself absorbs
# uvicorn's post-drain re-raise, so a normal stop exits 0 as it always did.)
#
# No -g: signals go to Python only. The shutdown drain can last a whole
# session, and a helper backgrounded from pre-app.sh must not be killed at the
# start of it; properly daemonised services are outside the process group
# regardless.
ENTRYPOINT ["/usr/bin/tini", "-e", "143", "--"]

# Set version information
ARG VERSION
ENV IMAGE_VERSION=$VERSION
LABEL daily.co/pipecat-cloud-base-version=$VERSION

# Set default command
CMD ["sh", "-c", "if [ -f /app/pre-app.sh ]; then sh /app/pre-app.sh; fi && exec python app.py"]
