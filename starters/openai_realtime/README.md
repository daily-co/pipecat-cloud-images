# OpenAI Realtime Beta Bot Starter

This repository contains a starter template for building a voice-based AI agent using OpenAI's Realtime API with Pipecat and deploying it to Pipecat Cloud.

## What's Included

- A ready-to-run voice bot powered by:
  - OpenAI Realtime Beta API (integrated audio streaming, STT, LLM, and TTS)
  - Daily for WebRTC audio transport
- Real-time voice activity detection (VAD) using Silero
- Complete Dockerfile for containerization

## Getting Started

This template requires the following API keys:

- OpenAI API key (with access to Realtime API)
- Daily room URL and token (provided by Pipecat Cloud when deploying)

## Customizing Your Bot

### Modifying Bot Behavior

The main logic for the bot is in `bot.py`. Here are key areas you might want to customize:

1. **Change the System Instructions**: Modify the instructions in the `SessionProperties` to change your bot's personality and behavior.

```python
session_properties = SessionProperties(
    input_audio_transcription=InputAudioTranscription(),
    turn_detection=TurnDetection(silence_duration_ms=1000),
    instructions="""You are Chatbot, a friendly, helpful robot. Your goal is to demonstrate your capabilities in a succinct way. Your output will be converted to audio so don't include special characters in your answers. Respond to what the user said in a creative and helpful way, but keep your responses brief. Start by introducing yourself.""",
)
```

2. **Adjust Turn Detection Settings**: You can customize how the bot detects when a user has finished speaking.

```python
# Adjust silence duration for turn detection (in milliseconds)
turn_detection=TurnDetection(silence_duration_ms=1000)

# Or disable OpenAI's turn detection and use transport VAD instead
# turn_detection=False
```

### Adding New Capabilities

To extend your bot's functionality:

1. **Add Tools/Function Calling**: Add tools by defining them and passing them to the session properties:

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
    # Add your custom processors here
    context_aggregator.user(),
    llm,
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
   docker build --platform=linux/arm64 -t openai-realtime-bot:latest .
   ```

2. **Push to a container registry**:

   ```shell
   docker tag openai-realtime-bot:latest your-repository/openai-realtime-bot:latest
   docker push your-repository/openai-realtime-bot:latest
   ```

3. **Deploy to Pipecat Cloud**:

   ```shell
   pipecat deploy realtime-bot your-repository/openai-realtime-bot:latest --secrets my-secrets
   ```

4. **Start a session**:
   ```shell
   pipecat agent start realtime-bot --use-daily
   ```

## Documentation

For more information on the Pipecat framework and Pipecat Cloud, see the official documentation:

- [Pipecat Cloud Documentation](https://docs.pipecat.daily.co)
- [Pipecat Framework Documentation](https://docs.pipecat.ai)

## License

This project is licensed under the BSD 2-Clause License - see the LICENSE file for details.
