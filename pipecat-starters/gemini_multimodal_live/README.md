# Gemini Multimodal Live Bot Starter

> **⚠️ DEPRECATED**: This starter is deprecated and will be removed after October 15, 2025. For current examples, see the [pipecat-quickstart](https://github.com/pipecat-ai/pipecat-quickstart) repository and [Pipecat Documentation](https://docs.pipecat.ai).

This repository contains a starter template for building a voice-based AI agent using Pipecat and deploying it to Pipecat Cloud.

## Features

- A ready-to-run voice bot powered by:
  - Google Gemini Multimodal Live LLM (includes built-in speech-to-text and text-to-speech)
  - Daily for WebRTC audio/video transport
- Real-time voice activity detection (VAD) using Silero
- Complete Dockerfile for containerization

## Required API Keys

- `GOOGLE_API_KEY`

## Customizing Your Bot

### Modifying Bot Behavior

The main logic for the bot is in `bot.py`. Here are key areas you might want to customize:

1. **Change the System Prompt**: Find the `messages` array and modify the user message to change your bot's personality and behavior.

```python
messages = [
    {
        "role": "user",
        "content": "You are Chatbot, a friendly, helpful robot. Your goal is to demonstrate your capabilities in a succinct way. Your output will be converted to audio so don't include special characters in your answers. Respond to what the user said in a creative and helpful way, but keep your responses brief. Start by introducing yourself.",
    }
]
```

2. **Change the Voice**: You can select from multiple voice options:

```python
llm = GeminiMultimodalLiveLLMService(
    api_key=os.getenv("GOOGLE_API_KEY"),
    voice_id="Aoede",  # Options: Puck, Charon, Kore, Fenrir, Aoede
    transcribe_user_audio=True,
    transcribe_model_audio=True,
)
```

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
