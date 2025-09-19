# Natural Conversation Bot Starter

> **⚠️ DEPRECATED**: This starter is deprecated and will be removed after October 15, 2025. For current examples, see the [pipecat-quickstart](https://github.com/pipecat-ai/pipecat-quickstart) repository and [Pipecat Documentation](https://docs.pipecat.ai).

> Seperately, this approach to interruption handling is no longer recommended. Use [Pipecat's smart-turn V3 model](https://www.daily.co/blog/announcing-smart-turn-v3-with-cpu-inference-in-just-12ms/) instead.

This repository contains a starter template for building a voice-based AI agent with natural conversation capabilities using Pipecat and deploying it to Pipecat Cloud.

## Features

- A ready-to-run voice bot powered by:
  - Dual LLM architecture for natural conversations:
    - Primary LLM (OpenAI) for conversation
    - Secondary LLM (Anthropic) for utterance completion detection
  - Deepgram for speech-to-text (STT)
  - Cartesia for text-to-speech (TTS)
  - Daily for WebRTC audio transport
- Real-time voice activity detection (VAD) using Silero
- Complete Dockerfile for containerization

## Required API Keys

- `ANTHROPIC_API_KEY`
- `OPENAI_API_KEY`
- `DEEPGRAM_API_KEY`
- `CARTESIA_API_KEY`

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

See the [top-level README](../README.md) for deployment instructions.
