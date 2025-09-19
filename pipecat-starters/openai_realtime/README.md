# OpenAI Realtime Beta Bot Starter

> **⚠️ DEPRECATED**: This starter is deprecated and will be removed after October 15, 2025. For current examples, see the [pipecat-quickstart](https://github.com/pipecat-ai/pipecat-quickstart) repository and [Pipecat Documentation](https://docs.pipecat.ai).

This repository contains a starter template for building a voice-based AI agent using OpenAI's Realtime API with Pipecat and deploying it to Pipecat Cloud.

## Features

- A ready-to-run voice bot powered by:
  - OpenAI Realtime Beta API (integrated audio streaming, STT, LLM, and TTS)
  - Daily for WebRTC audio transport
- Real-time voice activity detection (VAD) using Silero
- Complete Dockerfile for containerization

## Required API Keys

- `OPENAI_API_KEY`

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

See the [top-level README](../README.md) for deployment instructions.
