<!-- @format -->

# Pipecat Cloud Agent Images

[![Docs](https://img.shields.io/badge/Documentation-blue)](https://docs.pipecat.daily.co) [![Discord](https://img.shields.io/discord/1217145424381743145)](https://discord.gg/dailyco)

This repository contains source code for the official Pipecat Cloud agent images and starter templates.

## Repository Structure

- **[pipecat-base](./pipecat-base)**: Source code for the `dailyco/pipecat-base` Docker image, which serves as the foundation for Pipecat Cloud agents.
- **[pipecat-starters](./pipecat-starters)**: Ready-to-use agent templates for various use cases.

## Base Image

The base image provides the runtime environment and interface required to run agents on Pipecat Cloud. It handles:

- Starting agent processes in response to API calls
- Session management
- Platform integration
- Logging and monitoring

The official base image is available on Docker Hub: [dailyco/pipecat-base](https://hub.docker.com/r/dailyco/pipecat-base)

## Starter Templates

Starter templates offer pre-built configurations for common agent types:

- **[voice](./pipecat-starters/voice)**: Voice conversation agent with STT, LLM and TTS
- **[twilio](./pipecat-starters/twilio)**: Telephony agent that works with Twilio
- **[pstn_sip](./pipecat-starters/pstn_sip)**: Dial-in or out to your agent using Daily's telephony offerings
- **[natural conversation](./pipecat-starters/natural_conversation)**: Text chat agent focused on natural dialogue
- **[openai realtime](./pipecat-starters/openai_realtime)**: Agent using OpenAI's streaming capabilities
- **[gemini multimodal live](./pipecat-starters/gemini_multimodal_live)**: Multimodal agent using Google's Gemini models
- **[vision](./pipecat-starters/vision)**: Computer vision agent that can analyze images

Each starter includes a functioning implementation and Dockerfile. They serve as a starting point for building an agent tailored to your use case.

## Documentation

For detailed instructions on using these images and deploying agents:

- [Agent Images Guide](https://docs.pipecat.daily.co/agents/agent-images)
- [Quickstart Guide](https://docs.pipecat.daily.co/quickstart)
