# Voice Bot Starter

A phone-based conversational agent built with Pipecat. 
This starter bot enables you to create phone bots that can handle both 
inbound and outbound calls via PSTN (regular phone numbers) and SIP.

## Features

- Real-time voice conversations powered by:
  - [Deepgram](https://deepgram.com/) for Speech-to-Text (STT)
  - [OpenAI](https://openai.com/) for Large Language Model (LLM) processing
  - [Cartesia](https://cartesia.ai/) for Text-to-Speech (TTS)
- Voice activity detection with Silero
- Support for interruptions
- Support for SIP and PSTN

## Prerequisites

### API Keys
You'll need the following API keys to get started:

- `DAILY_API_KEY` 
- `OPENAI_API_KEY` 
- `DEEPGRAM_API_KEY` 
- `CARTESIA_API_KEY` 


### Phone number setup

You can buy a phone number through the Pipecat Cloud Dashboard:
1. Go to `Settings` > `Telephony`
2. Follow the UI to purchase a phone number
3. Configure the webhook URL to receive incoming calls

Or purchase the number using Daily's [PhoneNumbers API](https://docs.daily.co/reference/rest-api/phone-numbers).

```bash
curl --request POST \
--url https://api.daily.co/v1/domain-dialin-config \
--header 'Authorization: Bearer $TOKEN' \
--header 'Content-Type: application/json' \
--data-raw '{
	"type": "pinless_dialin",
	"name_prefix": "Customer1",
    "phone_number": "+1PURCHASED_NUM",
	"room_creation_api": "https://example.com/dial",
    "hold_music_url": "https://example.com/static/ringtone.mp3",
	"timeout_config": {
		"message": "No agent is available right now"
	}
}'
```

The API will return a static SIP URI (`sip_uri`) that can be called from other SIP services.

### Handling dial-in webhook (`room_creation_api`)

To make and receive calls currently you have to host a server t 
handle incoming calls. In the coming weeks, incoming calls will be 
directly handled within Daily and we will expose an endpoint similar 
to `{service}/start` that will manage this for you.

In the mean time, you will need to pass the custom fields to
[{service}/start](https://docs.pipecat.daily.co/agents/active-sessions#using-rest) 

For handling PSTN/SIP within this starter image, we recommend sending 
the following custom values:
    
```python
# Pass the values that you received in the pinless webhook to start
# The callId and callDomain are specific to the
# From is the user
"dialin_settings": {
    "to": "+14152251493",
    "from": "+14158483432",
    "callId": "string",
    "callDomain": "string"
}

# Pass the phoneNumber that you want the bot to call
"dialout_settings": [{
    "phoneNumber": "+14158483432",
    "callerId": "uuid of the phone-number on Daily"}]

# Pass the sipUri to make an outbound SIP call
"dialout_settings": [{"sipUri": "sip:anotheruser@sip.example.com"}]
```

## Configuration

### Dial-in setup (receiving calls on the bot)

When a user calls your purchased phone number, the call is received by the Daily 
infrastructure and put on hold. This triggers a webhook to your `room_creation_api`, 
which should then call the Pipecat Cloud endpoint with the necessary data:

```bash
curl --request POST \
--url https://api.pipecat.daily.co/v1/public/{service}/start \
--header 'Authorization: Bearer $TOKEN$' \
--header 'Content-Type: application/json' \
--data-raw '{
    'createDailyRoom': true, 
    'dailyRoomProperties': {
        'enable_dialout': false, 
        'sip': {'display_name': 'sip-dialin', 'sip_mode': 'dial-in', 'num_endpoints': 1}, 
        'exp': 1742353314
    }, 
    'body': {
        'dialin_settings': {
            'from': '+1CALLER', 
            'to': '+1PURCHASED', 
            'call_id': 'callid-uuid', 
            'call_domain': 'domain-uui'
        }
    }
}
```

The forwards the incoming request to bot, it passes the contents of the
`dialin_settings` to the  `DailyTransport`. The PSTN or SIP call is then automatically 
forwarded to the bot. 

The call flow looks like this:
1. User calls your purchased phone number, Daily receives the call and puts it on hold
2. Webhook triggers the `room_creation_api`. 
3. Your server calls the Pipecat Cloud endpoint (`{service}/start` ) with `dialin_settings`
4. Bot starts speaking immediately when the remote user joins

```python
 @transport.event_handler("on_first_participant_joined")
    async def on_first_participant_joined(transport, participant):
        if dialin_settings:
            # the bot will start to speak as soon as the call connects
            await task.queue_frames([context_aggregator.user().get_context_frame()])
```
In a future update, steps 2 and 3 will be handled by the Daily/bot.

### Dial-out setup (making calls from the bot)

To make outbound calls, you need to provide the target phone number(s) in the `dialout_settings` array:

```bash
curl --request POST \
--url https://api.pipecat.daily.co/v1/public/{service}/start \
--header 'Authorization: Bearer $TOKEN$' \
--header 'Content-Type: application/json' \
--data-raw '{
    'createDailyRoom': true, 
    'dailyRoomProperties': {
        'enable_dialout': false, 
        'sip': {'display_name': 'dialin', 'sip_mode': 'dial-in', 'num_endpoints': 1}, 
        'exp': 1742353929
    }, 
    'body': {
        'dialout_settings': [{'phoneNumber': '+1TARGET', 'callerId': 'UUID_OF_PURCHASED_NUM'}]
    }
}
```
The call flow looks like this:
1. Your application calls the Pipecat Cloud endpoint with `dialout_settings`
2. Bot begins dialing out when the call state moves to `joined`
3. Bot waits for the remote party to speak before responding

```python
@transport.event_handler("on_call_state_updated")
async def on_call_state_updated(transport, state):
    if state == "joined" and dialout_settings:
        await start_dialout(transport, dialout_settings)

@transport.event_handler("on_dialout_answered")
    async def on_dialout_answered(transport, data):
        # the bot will wait for the user to speak before responding
        await rtvi.set_bot_ready()
```

## Coming Soon

- Native handling of incoming calls directly within Daily
- Voicemail detection for outbound calls


## Deployment

See the [top-level README](../README.md) for deployment instructions.
