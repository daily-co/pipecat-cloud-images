# Pipecat Cloud Starters

> **⚠️ DEPRECATION NOTICE**
>
> **The pipecat-starters in this repository are deprecated and will be removed in the near future.** These starter templates are redundant with examples available elsewhere and will no longer be maintained or updated.
>
> **Migration Path:**
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
> **Additional Resources:**
>
> - [Pipecat Documentation](https://docs.pipecat.ai)
> - [Pipecat Cloud Documentation](https://docs.pipecat.ai/deployment/pipecat-cloud/introduction)
> - Existing Docker images will remain available on Docker Hub but will not receive updates
>
> **Timeline:** These starters will be removed from this repository after October 15, 2025.

This repository contains starter templates for building AI agents using the Pipecat framework and deploying them to Pipecat Cloud.

## Available Starters

- **voice** - Voice conversation agent with STT, LLM and TTS
- **twilio** - Telephony agent that works with Twilio
- **natural_conversation** - Text chat agent focused on natural dialogue
- **openai_realtime** - Agent using OpenAI's streaming capabilities
- **gemini_multimodal_live** - Multimodal agent using Google's Gemini models
- **vision** - Computer vision agent that can analyze images

## Getting Started

Each starter contains:

- A functioning `bot.py` file with a complete agent implementation
- A Dockerfile for containerization
- Required dependencies in requirements.txt
- A targeted README with specific customization options

### Prerequisites

- Docker installed on your system
- [Pipecat Cloud account](https://pipecat.daily.co)
- Python 3.10+

### Building and Deploying

For detailed instructions on building, deploying, and running your agent, please refer to the [Pipecat documentation](https://docs.pipecat.ai/getting-started/quickstart).

1. **Choose a starter**: Navigate to the starter that best fits your use case

2. **Customize your agent**: Modify the `bot.py` file to change the behavior of your agent

3. **Build the Docker image**:

   ```shell
   docker build --platform=linux/arm64 -t my-agent:latest .
   ```

4. **Push to a container registry**:

   ```shell
   docker tag my-agent:latest your-repository/my-agent:latest
   docker push your-repository/my-agent:latest
   ```

5. **Deploy to Pipecat Cloud**:

   ```shell
   pcc deploy agent-name your-repository/my-agent:latest --secrets my-secrets
   ```

6. **Start a session**:
   ```shell
   pcc agent start agent-name --use-daily
   ```

## Setting Up API Keys

Different starters require various API keys, which should be added to your Pipecat Cloud secrets.

```shell
pcc secrets set my-secrets \
  OPENAI_API_KEY=sk-... \
  DEEPGRAM_API_KEY=... \
  CARTESIA_API_KEY=...
```

## Documentation

For more information on the Pipecat framework and Pipecat Cloud:

- [Pipecat Cloud Documentation](https://docs.pipecat.ai/deployment/pipecat-cloud/introduction)
- [Pipecat Documentation](https://docs.pipecat.ai)

## License

This project is licensed under the BSD 2-Clause License - see the LICENSE file for details.
