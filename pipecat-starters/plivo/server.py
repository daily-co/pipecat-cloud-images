#
# Copyright (c) 2025, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""An example server for Plivo to start WebSocket streaming to Pipecat Cloud."""

import uvicorn
from fastapi import FastAPI, HTTPException, Query
from starlette.responses import Response

app = FastAPI(title="Plivo XML Server", description="Serves XML for Plivo WebSocket streaming")


@app.get("/plivo-xml")
async def plivo_xml(
    agent: str = Query(..., description="Agent name"),
    org: str = Query(..., description="Organization name"),
    # Optional Plivo parameters that are automatically passed by Plivo
    CallUUID: str = Query(None, description="Plivo call UUID"),
    From: str = Query(None, description="Caller's phone number"),
    To: str = Query(None, description="Called phone number"),
):
    """
    Returns XML for Plivo to start WebSocket streaming to Pipecat Cloud

    Required parameters:
    - agent: Your deployed agent name
    - org: Your Pipecat Cloud organization name

    Example: /plivo-xml?agent=my-bot&org=my-org-123
    """
    # Log call details (optional - useful for debugging)
    if CallUUID:
        print(f"Plivo call: {From} → {To}, UUID: {CallUUID}")

    # Basic validation
    if not agent or not org:
        raise HTTPException(
            status_code=400, detail="Both 'agent' and 'org' parameters are required"
        )

    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Stream bidirectional="true" keepCallAlive="true" contentType="audio/x-mulaw;rate=8000">
    wss://api.pipecat.daily.co/ws/plivo?serviceHost={agent}.{org}
  </Stream>
</Response>"""

    return Response(content=xml, media_type="application/xml")


if __name__ == "__main__":
    # Run the server on port 7860
    # Use with ngrok: ngrok http 7860
    uvicorn.run(app, host="0.0.0.0", port=7860)
