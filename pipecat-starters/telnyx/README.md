# Telnyx Voice Bot Starter

A telephone-based conversational agent built with Pipecat that connects to Telnyx for voice calls.

## Features

- Telephone voice conversations powered by:
  - Deepgram (STT)
  - OpenAI (LLM)
  - Cartesia (TTS)
- Voice activity detection with Silero
- FastAPI WebSocket connection with Telnyx
- 8kHz audio sampling optimized for telephone calls

## Required API Keys

- `OPENAI_API_KEY`
- `DEEPGRAM_API_KEY`
- `CARTESIA_API_KEY`
- `TELNYX_API_KEY`
- Telnyx account with Media Streaming configured

## Quick Customization

### Change Bot Personality

Modify the system prompt in `bot.py`:

```python
messages = [
    {
        "role": "system",
        "content": "You are Chatbot, a friendly, helpful robot..."
    },
]
```

### Change Voice

Update the voice ID in the TTS service:

```python
tts = CartesiaTTSService(
    api_key=os.getenv("CARTESIA_API_KEY"),
    voice_id="71a7ad14-091c-4e8e-a314-022ece01c121", # Change this
)
```

### Adjust Audio Parameters

The pipeline is configured for telephone-quality audio (8kHz). If your Telnyx configuration uses different parameters, adjust these values:

```python
task = PipelineTask(
    pipeline,
    params=PipelineParams(
        audio_in_sample_rate=8000,  # Input sample rate
        audio_out_sample_rate=8000, # Output sample rate
        # Other parameters...
    ),
)
```

## Telnyx Setup

To connect this agent to Telnyx:

1. [Purchase a number from Telnyx](https://telnyx.com/resources/purchase-a-phone-number-with-telnyx), if you haven't already

2. Collect your Pipecat Cloud organization name:

   ```bash
   pcc organizations list
   ```

   You'll use this information in the next step.

3. Create a [TeXML Application](https://developers.telnyx.com/docs/voice/programmable-voice/texml-setup):

   ```xml
   <?xml version="1.0" encoding="UTF-8"?>
   <Response>
     <Connect>
       <Stream url="wss://api.pipecat.daily.co/ws/telnyx?serviceHost=AGENT_NAME.ORGANIZATION_NAME" bidirectionalMode="rtp"></Stream>
     </Connect>
     <Pause length="40"/>
   </Response>
   ```

   where:

   - `AGENT_NAME` is your agent's name (the name you used when deploying)
   - `ORGANIZATION_NAME` is the value returned in the previous step

4. Assign the TeXML Application to your phone number:

   - Navigate to Voice → Programmable Voice in your Telnyx dashboard
   - In the TeXML Applications tab, select the pencil icon for the TeXML Application you created in step 3
   - In the Numbers tab, select Assign numbers
   - Select the number you would like to assign the TeXML Application to
   - Save your configuration

## Deployment

See the [top-level README](../README.md) for deployment instructions.
