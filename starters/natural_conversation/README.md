# Natural Conversation Bot Starter

This repository contains a starter template for building a voice-based AI agent with natural conversation capabilities using Pipecat and deploying it to Pipecat Cloud.

## What's Included

- A ready-to-run voice bot powered by:
  - Dual LLM architecture for natural conversations:
    - Primary LLM (OpenAI) for conversation
    - Secondary LLM (Anthropic) for utterance completion detection
  - Deepgram for speech-to-text (STT)
  - Cartesia for text-to-speech (TTS)
  - Daily for WebRTC audio transport
- Real-time voice activity detection (VAD) using Silero
- Complete Dockerfile for containerization

## Getting Started

This template requires the following API keys:

- OpenAI API key (for the primary conversation LLM)
- Anthropic API key (for the utterance completion detector)
- Deepgram API key
- Cartesia API key
- Daily room URL and token (provided by Pipecat Cloud when deploying)

## Customizing Your Bot

### Modifying Bot Behavior

The main logic for the bot is in `bot.py`. Here are key areas you might want to customize:

1. **Change the System Prompt**: Find the `messages` array and modify the system message to change your bot's personality and behavior.

```python
messages = [
    {
        "role": "system",
        "content": """You are Chatbot, a friendly, helpful robot. Your goal is to demonstrate your capabilities in a succinct way. Your output will be converted to audio so don't include special characters in your answers. Respond to what the user said in a creative and helpful way, but keep your responses brief. Start by introducing yourself.""",
    },
]
```

2. **Change the Services and Options**: You can modify the STT, LLM, and TTS services and options.

```python
stt = DeepgramSTTService(api_key=os.getenv("DEEPGRAM_API_KEY"))
llm = OpenAILLMService(api_key=os.getenv("OPENAI_API_KEY"), model="gpt-4o")
statement_llm = AnthropicLLMService(
    api_key=os.getenv("ANTHROPIC_API_KEY"), model="claude-3-5-sonnet-20240620"
)
tts = CartesiaTTSService(
    api_key=os.getenv("CARTESIA_API_KEY"),
    voice_id="79a125e8-cd45-4c13-8a67-188112f4dd22",
)
```

3. **Adjust Utterance Completion Detection**: The template uses a secondary LLM to detect when a user has finished speaking. You can modify the `classifier_statement` prompt if needed.

### Understanding the Dual-LLM Architecture

This starter uses two LLMs working together:

1. **Primary LLM (OpenAI)**: Handles the actual conversation with the user
2. **Secondary LLM (Anthropic)**: Acts as a judge to determine when the user has completed an utterance

The secondary LLM analyzes incoming user speech in real-time and returns "YES" when it detects a complete thought, which triggers the primary LLM to respond. This creates more natural conversation flow by:

- Allowing the bot to respond at natural conversation boundaries
- Reducing interruptions while users are still forming thoughts
- Handling pauses and filler words appropriately

### Adding New Capabilities

To extend your bot's functionality:

1. **Add Tools/Function Calling**: Add tools by defining them and passing them to the context:

```python
tools = [
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Get current weather",
            "parameters": {...}
        }
    }
]
```

2. **Add New Processors**: Modify the pipeline by adding new processors:

```python
pipeline = Pipeline([
    transport.input(),
    rtvi,
    stt,
    context_aggregator.user(),
    ParallelPipeline(
        # Your customizations here
    ),
    tts,
    user_idle,
    transport.output(),
    context_aggregator.assistant(),
])
```

Popular options include:

- [RTVIProcessor](https://docs.pipecat.ai/server/frameworks/rtvi/rtvi-processor): For client/server messaging and events
- [AudioBufferProcessor](https://docs.pipecat.ai/server/utilities/audio/audio-recording): Record audio in a call
- [TranscriptProcessor](https://docs.pipecat.ai/server/utilities/transcript-processor): Collect user and assistant transcripts
- [STTMuteFilter](https://docs.pipecat.ai/server/utilities/filters/stt-mute): Prevent the bot from being interrupted in specific scenarios
- [UserIdleProcessor](https://docs.pipecat.ai/server/utilities/user-idle-processor): Trigger a response when a user hasn't responded in a set period of time
- [Observers](https://docs.pipecat.ai/server/utilities/observers/observer-pattern): Debug issues by inspecting frames in the pipeline

## Deployment

### Prerequisites

- Docker installed on your system
- [Pipecat Cloud account](https://pipecat.daily.co)
- Python 3.10+

### Building and Deploying

For detailed instructions on building, deploying, and running your agent, please refer to the [Pipecat Cloud documentation](https://docs.pipecat.daily.co/quickstart).

Quick reference:

1. **Build the Docker image**:

   ```shell
   docker build --platform=linux/arm64 -t natural-conversation-bot:latest .
   ```

2. **Push to a container registry**:

   ```shell
   docker tag natural-conversation-bot:latest your-repository/natural-conversation-bot:latest
   docker push your-repository/natural-conversation-bot:latest
   ```

3. **Deploy to Pipecat Cloud**:

   ```shell
   pipecat deploy natural-bot your-repository/natural-conversation-bot:latest --secrets my-secrets
   ```

4. **Start a session**:
   ```shell
   pipecat agent start natural-bot --use-daily
   ```

## Documentation

For more information on the Pipecat framework and Pipecat Cloud, see the official documentation:

- [Pipecat Cloud Documentation](https://docs.pipecat.daily.co)
- [Pipecat Framework Documentation](https://docs.pipecat.ai)

## License

This project is licensed under the BSD 2-Clause License - see the LICENSE file for details.
