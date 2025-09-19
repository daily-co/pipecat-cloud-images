# Pipecat Cloud Base Image

Source code for the official Pipecat Cloud base agent image (`dailyco/pipecat-base`).

## Overview

This image provides the foundational runtime environment for running agents on Pipecat Cloud. It includes:

- An HTTP API server based on FastAPI for receiving agent start requests
- Integration with the platform's session management
- Automatic handling of room URLs and tokens
- Logging infrastructure optimized for cloud environments

## Python Version Support

We provide base images for multiple Python versions. See `versions.yaml` for the current list of supported versions.

**Supported Python versions:** 3.10, 3.11, **3.12 (default/recommended)**, 3.13

**Image naming patterns:**

- `dailyco/pipecat-base:latest` - Latest version with Python 3.12 (recommended)
- `dailyco/pipecat-base:latest-py3.X` - Latest version with specific Python version
- `dailyco/pipecat-base:VERSION` - Pinned version with Python 3.12 (recommended for production)
- `dailyco/pipecat-base:VERSION-py3.X` - Pinned version with specific Python version

## Usage

When creating your own agent, use this base image in your Dockerfile:

```Dockerfile
# Using default Python version
FROM dailyco/pipecat-base:latest
COPY ./requirements.txt requirements.txt
RUN pip install --no-cache-dir --upgrade -r requirements.txt
COPY ./bot.py bot.py

# Or specify a specific Python version
FROM dailyco/pipecat-base:latest-py3.12
COPY ./requirements.txt requirements.txt
RUN pip install --no-cache-dir --upgrade -r requirements.txt
COPY ./bot.py bot.py
```

### Versioned Images

For production use, we recommend pinning to specific versions:

```Dockerfile
# Recommended: Pin to specific version (uses Python 3.12)
FROM dailyco/pipecat-base:VERSION

# Or specify both version and Python version explicitly
FROM dailyco/pipecat-base:VERSION-py3.12
```

### Requirements

When using this base image, your project must:

1. Include a `bot.py` file with an async `bot()` function that follows this signature:

   ```python
   async def bot(args: DailySessionArguments):
       """Main bot entry point"""
       # Access: args.room_url, args.token, args.session_id, args.body
       # Your agent implementation here
   ```

2. For WebSocket-based agents (like Twilio), implement an alternate signature:
   ```python
   async def bot(args: WebSocketSessionArguments):
       """WebSocket bot entry point"""
       # Access: args.websocket, args.session_id
       # Your WebSocket agent implementation here
   ```

### How It Works

1. The base image exposes an HTTP API on port 8080 with:
   - `/bot` endpoint for HTTP-based agents (Daily.co integration)
   - `/ws` endpoint for WebSocket-based agents (Twilio, custom WebSocket)
2. When Pipecat Cloud receives a request to start your agent, it calls the appropriate endpoint
3. The base image invokes your `bot()` function, passing room details and config
4. Your agent code runs in its own process, managed by the platform

## Releasing New Versions

To release a new version of the base image:

1. **Bump the version** (from the `pipecat-base` directory):

   ```bash
   cd pipecat-base
   uv version --bump patch --no-sync    # For bug fixes (0.1.1 → 0.1.2)
   uv version --bump minor --no-sync    # For new features (0.1.1 → 0.2.0)
   uv version --bump major --no-sync    # For breaking changes (0.1.1 → 1.0.0)
   ```

2. **Update the lock file**:

   ```bash
   uv lock
   ```

3. **Update the changelog**: Move `[Unreleased]` section to `[X.Y.Z] - YYYY-MM-DD` in `CHANGELOG.md`

4. **Create release PR**:

   ```bash
   git checkout -b release/vX.Y.Z
   git add pyproject.toml uv.lock CHANGELOG.md
   git commit -m "Release vX.Y.Z"
   git push origin release/vX.Y.Z
   ```

   Then open a PR from `release/vX.Y.Z` to `main`. After approval and merge, GitHub Actions will automatically build and publish the new version.

## More Information

For detailed documentation on agent development:

- [Agent Images Guide](https://docs.pipecat.ai/deployment/pipecat-cloud/fundamentals/agent-images)
- [Custom Base Image](https://docs.pipecat.ai/deployment/pipecat-cloud/fundamentals/agent-images#using-a-custom-image)
