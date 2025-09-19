# Pipecat Cloud Agent Images

[![Docs](https://img.shields.io/badge/Documentation-blue)](https://docs.pipecat.ai/deployment/pipecat-cloud/introduction) [![Discord](https://img.shields.io/discord/1217145424381743145)](https://discord.gg/pipecat)

This repository contains source code for the official Pipecat Cloud agent images and starter templates.

## Repository Structure

- **[pipecat-base](./pipecat-base)**: Source code for the `dailyco/pipecat-base` Docker image, which serves as the foundation for Pipecat Cloud agents.
- **[pipecat-starters](./pipecat-starters)**: ⚠️ **DEPRECATED** - Ready-to-use agent templates for various use cases. These will be removed after October 15, 2025

## Base Image

The base image provides the runtime environment and interface required to run agents on Pipecat Cloud. It handles:

- Starting agent processes in response to API calls
- Session management
- Platform integration
- Logging and monitoring

The official base image is available on Docker Hub: [dailyco/pipecat-base](https://hub.docker.com/r/dailyco/pipecat-base)

## Starter Templates (DEPRECATED)

> **⚠️ DEPRECATION NOTICE**: The starter templates below are deprecated and will be removed from this repository after October 15, 2025.
>
> **Specific Alternatives:**
>
> - **General quickstart**: [pipecat-quickstart](https://github.com/pipecat-ai/pipecat-quickstart)
> - **Twilio**: [twilio-chatbot example](https://github.com/pipecat-ai/pipecat-examples/tree/main/twilio-chatbot)
> - **Telnyx**: [telnyx-chatbot example](https://github.com/pipecat-ai/pipecat-examples/tree/main/telnyx-chatbot)
> - **Plivo**: [plivo-chatbot example](https://github.com/pipecat-ai/pipecat-examples/tree/main/plivo-chatbot)
> - **Exotel**: [exotel-chatbot example](https://github.com/pipecat-ai/pipecat-examples/tree/main/exotel-chatbot)
> - **Daily PSTN**: [phone-chatbot example](https://github.com/pipecat-ai/pipecat-examples/tree/main/phone-chatbot)
>
> For additional resources, visit the [Pipecat Documentation](https://docs.pipecat.ai) and [Pipecat Cloud Documentation](https://docs.pipecat.ai/deployment/pipecat-cloud/introduction).

## Documentation

For detailed instructions on using these images and deploying agents:

- [Agent Images Guide](https://docs.pipecat.ai/deployment/pipecat-cloud/fundamentals/agent-images)
- [Quickstart Guide](https://docs.pipecat.ai/getting-started/quickstart)
