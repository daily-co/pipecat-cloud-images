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

**Supported Python versions:** 3.11, **3.12 (default/recommended)**, 3.13, 3.14

0.1.32 was the last release with a Python 3.10 image. Its `-py3.10` tags remain
published but no longer update.

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

1. Install pipecat-ai 0.0.78 or newer. When the agent starts, the image checks
   that pipecat-ai is installed before importing `bot.py`, and that the session
   arguments it passes to `bot()` build right after; it refuses to start,
   saying why, if either fails. SmallWebRTC's are the exception: if they do not
   build, the agent starts without SmallWebRTC (and WhatsApp, which needs it),
   with a warning saying why. Releases older than 0.0.91 are deprecated: they
   work, with a warning at startup, and a future release of the image will
   require 0.0.91.

2. Include a `bot.py` file with an async `bot()` function that follows this signature:

   ```python
   async def bot(args: DailySessionArguments):
       """Main bot entry point"""
       # Access: args.room_url, args.token, args.session_id, args.body
       # Your agent implementation here
   ```

3. For WebSocket-based agents (like Twilio), implement an alternate signature:
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
   - `/pcc/capabilities`, which tells Pipecat Cloud what the agent can serve.
     The path is reserved: the image answers HTTP requests for it before they
     reach your app, so an HTTP route your bot module adds there is never
     called.
2. When Pipecat Cloud receives a request to start your agent, it calls the appropriate endpoint
3. The base image invokes your `bot()` function, passing room details and config
4. Your agent code runs in its own process, managed by the platform

## Releasing New Versions

**The `version` in `pipecat-base/pyproject.toml` is what releases the image.**
When a merge to `main` changes it to a version that has not been released yet,
GitHub Actions publishes that version. Changing `version = "0.2.0"` to
`version = "0.2.1"`, with the steps below, releases 0.2.1.

Every other merge that touches the image builds it and runs its checks, but
pushes nothing. So a change can land over several pull requests and ship in one
release:

- **A change to the image**, anything under `pipecat-base/` or
  `versions.yaml`, adds its entry under `[Unreleased]` in `CHANGELOG.md` and
  leaves the version alone.
- **A release** is a pull request that bumps the version, as below.

Keep `main` releasable: anything merged ships with the next release, so a
change that is not ready to ship waits on its own branch.

The workflow tags each release `vX.Y.Z` itself, and that tag is how it knows a
version is already out. Do not create or push `v` tags by hand: a tag for a
version that has not been published yet makes the workflow skip publishing it.

To release a new version of the base image:

1. **Bump the version** (from the `pipecat-base` directory):

   ```bash
   cd pipecat-base
   uv version --bump patch --no-sync    # For fixes and new features (0.2.0 → 0.2.1)
   uv version --bump minor --no-sync    # For breaking changes (0.1.32 → 0.2.0)
   ```

   Below 1.0.0, a breaking change bumps the minor version, as
   [Semantic Versioning](https://semver.org/#how-should-i-deal-with-revisions-in-the-0yz-initial-development-phase)
   allows for 0.y.z releases, and everything else bumps the patch version. From
   1.0.0, a breaking change bumps the major version (`--bump major`).

2. **Update the lock file**:

   ```bash
   uv lock
   ```

   The lock records the image's own version, and the image builds with
   `uv sync --locked`, so the two must agree or the build fails and nothing is
   published. `uv version` updates both, which makes this a no-op after step 1;
   it matters if the version was edited in `pyproject.toml` by hand.

3. **Update the changelog**: Rename the `[Unreleased]` section to `[X.Y.Z] - YYYY-MM-DD` in `CHANGELOG.md`, and start a new, empty `[Unreleased]` section above it

4. **Create release PR**:

   ```bash
   git checkout -b release/vX.Y.Z
   git add pyproject.toml uv.lock CHANGELOG.md
   git commit -m "Release vX.Y.Z"
   git push origin release/vX.Y.Z
   ```

   Then open a PR from `release/vX.Y.Z` to `main`. After approval and merge,
   GitHub Actions builds and pushes the images for every Python version, then
   tags the merge commit `vX.Y.Z`. If an image fails to push, there is no tag:
   re-run the failed jobs to finish the release, before merging anything else
   that touches the image. Until the tag exists, the version reads as
   unreleased, and the next such merge would publish it from its own commit.
   A version is published once, so a fix after a release needs a new version.

## Third-Party Software

The image is built on Debian 13 with Python and uv, and includes the Python
packages in `/app/.venv`. Their licenses ship in the image, and
[THIRD_PARTY_NOTICES.md](./THIRD_PARTY_NOTICES.md), also at
`/usr/share/doc/pipecat-base/THIRD_PARTY_NOTICES.md` in the image, says where
to find each license and the Debian source.

## More Information

For detailed documentation on agent development:

- [Agent Images Guide](https://docs.pipecat.ai/deployment/pipecat-cloud/fundamentals/agent-images)
- [Custom Base Image](https://docs.pipecat.ai/deployment/pipecat-cloud/fundamentals/agent-images#using-a-custom-image)
