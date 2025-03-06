# Twilio Voice Bot Starter

A telephone-based conversational agent built with Pipecat that connects to Twilio for voice calls.

## Features

- Telephone voice conversations powered by:
  - Deepgram (STT)
  - OpenAI (LLM)
  - Cartesia (TTS)
- Voice activity detection with Silero
- FastAPI WebSocket connection with Twilio
- 8kHz audio sampling optimized for telephone calls

## Required API Keys

- `OPENAI_API_KEY`
- `DEEPGRAM_API_KEY`
- `CARTESIA_API_KEY`
- Twilio account with Media Streams configured

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
    voice_id="79a125e8-cd45-4c13-8a67-188112f4dd22", # Change this
)
```

### Adjust Audio Parameters

The pipeline is configured for telephone-quality audio (8kHz). If your Twilio configuration uses different parameters, adjust these values:

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

## Twilio Setup

To connect this agent to Twilio:

1. [Purchase a number from Twilio](https://help.twilio.com/articles/223135247-How-to-Search-for-and-Buy-a-Twilio-Phone-Number-from-Console), if you haven't already

2. Collect your Pipecat Cloud organization name:

```bash
pcc organizations list
```

You'll use this information in the next step.

3. Create a [TwiML Bin](https://help.twilio.com/articles/360043489573-Getting-started-with-TwiML-Bins):

```xml
<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Connect>
    <Stream url="wss://api.pipecat.daily.co/ws/twilio">
      <Parameter name="_pipecatCloudServiceHost" value="AGENT_NAME.ORGANIZATION_NAME"/>
    </Stream>
  </Connect>
</Response>
```

where:

- AGENT_NAME is your agent's name (the name you used when deploying)
- ORGANIZATION_NAME is the value returned in the previous step

4. Assign the TwiML Bin to your phone number:

- Select your number from the Twilio dashboard
- In the `Configure` tab, set `A call comes in` to `TwiML Bin`
- Set `TwiML Bin` to the Bin you created in the previous step
- Save your configuration

## Deployment

See the [top-level README](../README.md) for deployment instructions.
