# Vision Bot Starter

This repository contains a starter template for building a voice-based AI agent with vision capabilities using Pipecat and deploying it to Pipecat Cloud.

## Features

- A ready-to-run voice and vision bot powered by:
  - Anthropic Claude for language understanding and vision analysis (LLM)
  - Deepgram for speech-to-text (STT)
  - Cartesia for text-to-speech (TTS)
  - Daily for WebRTC audio/video transport
- Custom "get_image" tool to request and analyze video frames
- Real-time voice activity detection (VAD) using Silero
- Complete Dockerfile for containerization

## Required API Keys

- `ANTHROPIC_API_KEY`
- `DEEPGRAM_API_KEY`
- `CARTESIA_API_KEY`

## Customizing Your Bot

### Modifying Bot Behavior

The main logic for the bot is in `bot.py`. Here are key areas you might want to customize:

1. **Change the System Prompt**: Find the `system_prompt` variable and modify it to change your bot's personality and behavior.

```python
system_prompt = """You are a helpful assistant who converses with a user and answers questions. Respond concisely to general questions.

Your response will be turned into speech so use only simple words and punctuation.

You have access to one tools: get_image.

You can answer questions about the user's video stream using the get_image tool. Some examples of phrases that indicate you should use the get_image tool are:
- What do you see?
- What's in the video?
- Can you describe the video?
- Tell me about what you see.
- Tell me something interesting about what you see.
- What's happening in the video?

If you need to use a tool, simply use the tool. Do not tell the user the tool you are using. Be brief and concise."""
```

2. **Change the Services and Options**: You can modify the STT, LLM, and TTS services and options.

```python
stt = DeepgramSTTService(api_key=os.getenv("DEEPGRAM_API_KEY"))
llm = AnthropicLLMService(
    api_key=os.getenv("ANTHROPIC_API_KEY"),
    model="claude-3-5-sonnet-latest",
    enable_prompt_caching_beta=True,
)
tts = CartesiaTTSService(
    api_key=os.getenv("CARTESIA_API_KEY"),
    voice_id="79a125e8-cd45-4c13-8a67-188112f4dd22",
)
```

3. **Change Video Frame Rate**: Adjust the frame rate of the video capture to balance performance and quality.

```python
# Adjust framerate (0 for on-demand frames, higher values for continuous capture)
await transport.capture_participant_video(video_participant_id, framerate=0)
```

### Understanding Vision Capabilities

This starter includes a custom implementation of the `AnthropicContextWithVisionTool` class that provides:

1. A "get_image" tool for requesting current video frames
2. Integration between the Daily transport and Claude's vision capabilities
3. Dynamic frame capture based on conversation context

The bot can see what's on the user's camera and analyze visual content on-demand, allowing it to:

- Describe what it sees in the video
- Answer questions about visual content
- Provide insights based on the visual stream

### Adding New Capabilities

To extend your bot's functionality:

1. **Add Tools/Function Calling**: Add additional tools by extending the existing tool implementation:

```python
def _add_additional_tools(self):
    # Add your custom tools here
    self._tools.append(
        {
            "name": "your_tool_name",
            "description": "Description of your tool",
            "input_schema": {
                "type": "object",
                "properties": {
                    # Your tool properties
                },
                "required": ["required_properties"],
            },
        }
    )
```

2. **Add New Processors**: Modify the pipeline by adding new processors:

```python
pipeline = Pipeline([
    transport.input(),
    rtvi,
    stt,
    # Add your custom processors here
    context_aggregator.user(),
    llm,
    tts,
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
