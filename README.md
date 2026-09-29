# Pipecat Cloud Agent Images

[![Docs](https://img.shields.io/badge/Documentation-blue)](https://docs.pipecat.ai/deployment/pipecat-cloud/introduction) [![Discord](https://img.shields.io/discord/1217145424381743145)](https://discord.gg/pipecat)

This repository contains source code for the official Pipecat Cloud base image.

## Repository Structure

- **[pipecat-base](./pipecat-base)**: Source code for the `dailyco/pipecat-base` Docker image, which serves as the foundation for Pipecat Cloud agents.
- **[local-test](./local-test)**: A mock of Pipecat Cloud's session routes, for testing an agent container locally.
- **[versions.yaml](./versions.yaml)**: The Python versions the image is built for, and the default.

## Development

Lint from the repo root, which covers `local-test` too, and test from `pipecat-base`:

```bash
uv run --project pipecat-base ruff format
uv run --project pipecat-base ruff check --fix
cd pipecat-base && uv run pytest
```

`pre-commit install` runs the same ruff version on each commit.

## Base Image

The base image provides the runtime environment and interface required to run agents on Pipecat Cloud. It handles:

- Starting agent processes in response to API calls
- Session management
- Platform integration
- Logging and monitoring

The official base image is available on Docker Hub: [dailyco/pipecat-base](https://hub.docker.com/r/dailyco/pipecat-base)

## Starter Templates (REMOVED)

The starter templates have been removed. Instead, get started by using the Pipecat CLI to [create a new project](https://github.com/pipecat-ai/pipecat-cli?tab=readme-ov-file#create-a-new-project).

## Documentation

For detailed instructions on using these images and deploying agents:

- [Agent Images Guide](https://docs.pipecat.ai/deployment/pipecat-cloud/fundamentals/agent-images)
- [Quickstart Guide](https://docs.pipecat.ai/getting-started/quickstart)
