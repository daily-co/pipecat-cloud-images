# Plivo Voice Bot Starter

A telephone-based conversational agent built with Pipecat that connects to Plivo for voice calls.

## Features

- Telephone voice conversations powered by:
  - Deepgram (STT)
  - OpenAI (LLM)
  - Cartesia (TTS)
- Voice activity detection with Silero
- FastAPI WebSocket connection with Plivo
- 8kHz audio sampling optimized for telephone calls

## Required API Keys

- `OPENAI_API_KEY`
- `DEEPGRAM_API_KEY`
- `CARTESIA_API_KEY`
- Plivo account with WebSocket streaming configured

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

The pipeline is configured for telephone-quality audio (8kHz). If your Plivo configuration uses different parameters, adjust these values:

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

## Plivo Setup

To connect this agent to Plivo:

1. [Purchase a number from Plivo](https://www.plivo.com/docs/numbers/quickstart/), if you haven't already

2. Collect your Pipecat Cloud organization name:

   ```bash
   pcc organizations list
   ```

   You'll use this information in the next step.

3. Set up an XML server to respond to Plivo webhooks. Create a file called `server.py`:

   ```python
   from fastapi import FastAPI, Query, HTTPException
   from starlette.responses import Response
   import uvicorn

   app = FastAPI(title="Plivo XML Server")

   @app.get("/plivo-xml")
   async def plivo_xml(
       agent: str = Query(..., description="Agent name"),
       org: str = Query(..., description="Organization name"),
       CallUUID: str = Query(None, description="Plivo call UUID"),
       From: str = Query(None, description="Caller's phone number"),
       To: str = Query(None, description="Called phone number"),
   ):
       if not agent or not org:
           raise HTTPException(status_code=400, detail="Both 'agent' and 'org' parameters are required")

       xml = f"""<?xml version="1.0" encoding="UTF-8"?>
   <Response>
     <Stream bidirectional="true" keepCallAlive="true" contentType="audio/x-mulaw;rate=8000">
       wss://api.pipecat.daily.co/ws/plivo?serviceHost={agent}.{org}
     </Stream>
   </Response>"""

       return Response(content=xml, media_type="application/xml")

   if __name__ == "__main__":
       uvicorn.run(app, host="0.0.0.0", port=7860)
   ```

4. Run your XML server and make it publicly accessible:

   ```bash
   # Install dependencies
   pip install fastapi uvicorn

   # Run the server
   python server.py

   # In another terminal, expose it publicly (for testing)
   ngrok http 7860
   ```

   This gives you a public URL like: `https://abc123.ngrok.io`

5. Create an XML Application in Plivo:

   - Navigate to Voice → XML in your Plivo dashboard
   - Add New Application with these settings:

     - **Answer URL**: `https://abc123.ngrok.io/plivo-xml?agent=AGENT_NAME&org=ORGANIZATION_NAME`
     - **HTTP Method**: `GET`

       where:

       - `AGENT_NAME` is your agent's name (the name you used when deploying)
       - `ORGANIZATION_NAME` is the value returned in step 2

6. Assign the XML Application to your phone number:

   - Navigate to Phone Numbers and select your number
   - Set Application Type to "XML Application"
   - Select your XML Application from the dropdown
   - Save your configuration

## Production Considerations

For production use:

- Deploy your XML server to a reliable hosting platform (Railway, Render, Fly.io, etc.)
- Update your Plivo XML Application to use your production server URL
- Consider adding authentication and logging to your XML server

## Deployment

See the [top-level README](../README.md) for deployment instructions.
