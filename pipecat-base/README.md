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

Currently supported Python versions:

- Python 3.10: `dailyco/pipecat-base:latest-py3.10` or `dailyco/pipecat-base:latest` (default)
- Python 3.11: `dailyco/pipecat-base:latest-py3.11`
- Python 3.12: `dailyco/pipecat-base:latest-py3.12`
- Python 3.13: `dailyco/pipecat-base:latest-py3.13`

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

You can also pin to specific versions:

```Dockerfile
FROM dailyco/pipecat-base:0.0.9-py3.11
```

### Requirements

When using this base image, your project must:

1. Include a `bot.py` file with an async `bot()` function that follows this signature:

   ```python
   async def bot(args: DailySessionArguments):
       """Main bot entry point"""
       # Your agent implementation here
   ```

2. For WebSocket-based agents (like Twilio), implement an alternate signature:
   ```python
   async def bot(args: WebSocketSessionArguments):
       """WebSocket bot entry point"""
       # Your WebSocket agent implementation here
   ```

### How It Works

1. The base image exposes an HTTP API on port 8080 with a `/bot` endpoint
2. When Pipecat Cloud receives a request to start your agent, it calls this endpoint
3. The base image invokes your `bot()` function, passing room details and config
4. Your agent code runs in its own process, managed by the platform

## More Information

For detailed documentation on agent development:

- [Agent Images Guide](https://docs.pipecat.daily.co/agents/agent-images)
- [Custom Base Image](https://docs.pipecat.daily.co/agents/agent-images#using-a-custom-image)
