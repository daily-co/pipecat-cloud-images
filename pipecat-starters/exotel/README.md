# Exotel Voice Bot Starter

> **⚠️ DEPRECATED**: This starter is deprecated and will be removed after October 15, 2025. For current examples, see the [exotel-chatbot example](https://github.com/pipecat-ai/pipecat-examples/tree/main/exotel-chatbot) and [Pipecat Documentation](https://docs.pipecat.ai).

A telephone-based conversational agent built with Pipecat that connects to Exotel for voice calls.

## Features

- Telephone voice conversations powered by:
  - Deepgram (STT)
  - OpenAI (LLM)
  - Cartesia (TTS)
- Voice activity detection with Silero
- FastAPI WebSocket connection with Exotel
- 8kHz audio sampling optimized for telephone calls

## Required API Keys

- `OPENAI_API_KEY`
- `DEEPGRAM_API_KEY`
- `CARTESIA_API_KEY`
- Exotel account with Voice Streaming enabled

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

## Exotel Setup

To connect this agent to Exotel:

1. **Purchase a number from Exotel**, if you haven't already:

   - Log in to the Exotel dashboard: https://my.exotel.com/
   - Navigate to ExoPhones and purchase a number
   - Complete KYC verification if required

2. **Enable Voice Streaming** (if not already enabled):

   - Contact Exotel support at `hello@exotel.com`
   - Request: "Enable Voicebot Applet for voice streaming for account [Your Account SID]"
   - Include your use case: "AI voice bot integration"

3. **Collect your Pipecat Cloud organization name**:

   ```bash
   pcc organizations list
   ```

   You'll use this information in the next step.

4. **Create a Custom App in App Bazaar**:

   - Navigate to App Bazaar in your Exotel dashboard
   - Click "Create Custom App" or edit an existing app
   - Drag the "Voicebot" applet to your call flow
   - Configure the Voicebot Applet:
     - **URL**: `wss://api.pipecat.daily.co/ws/exotel?serviceHost=AGENT_NAME.ORGANIZATION_NAME`
     - **Record**: Enable if you want call recordings (optional)

   where:

   - AGENT_NAME is your agent's name (the name you used when deploying)
   - ORGANIZATION_NAME is the value returned in the previous step

5. **Link your phone number to the app**:
   - Navigate to "ExoPhones" in your dashboard
   - Find your purchased number and click the edit/pencil icon
   - Under "App", select the custom app you created
   - Save the configuration

## Deployment

See the [top-level README](../README.md) for deployment instructions.
