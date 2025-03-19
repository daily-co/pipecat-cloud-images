# Voice Bot Starter

A Phone-based conversational agent built with Pipecat. 
Uses Phone numbers purchased on Daily. Additionally, can handle inbound and outbound SIP.

## Features

- Real-time voice conversations powered by:
  - Deepgram (STT)
  - OpenAI (LLM)
  - Cartesia (TTS)
- Voice activity detection with Silero
- Support for interruptions
- Support for SIP and PSTN

## Required API Keys
- `DAILY_API_KEY` 
- `OPENAI_API_KEY`
- `DEEPGRAM_API_KEY`
- `CARTESIA_API_KEY`

## Configuration

To make and receive calls currently you have to host a server to 
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

## Buy a phone number

You can buy a phone number through the Pipecat Cloud Dashboard, navigate to 
`Settings` and then `Telephony`. And set up the webhook url to receive the incoming call. 

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
The API returns the static SIP URI (`sip_uri`) which can be called from other SIP services

### Dial-in

When you call the Phone Number or the static SIP URI, the call is received on the 
Daily infrastructure and immediately put on hold. This triggers the webhook to the 
`room_creation_api`, which calls the `{service}/start` pipecat cloud endpoint with 
the data from the webhook copied into the `dialin_settings`. 

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

When the bot receives this call and `dialin_settings` is present, it passes the contents 
to the  `DailyTransport`. The call is then automatically forwarded to the bot. 
In addition, `dialin_settings` contains `to` and `from` fields that can be used to
lookup who is calling and what number was called.

Since this is a dial-in call, the bot needs to start speaking immediately when the remote
user joins (when `on_first_participant_joined` fires). 

```python
 @transport.event_handler("on_first_participant_joined")
    async def on_first_participant_joined(transport, participant):
        if dialin_settings:
            # the bot will start to speak as soon as the call connects
            await task.queue_frames([context_aggregator.user().get_context_frame()])
```

### Dial-out

In dialout, the bot needs the number to be called, we use `dialout_setings` (an array
of obhect) to pass one or more phone numbers to the bot. See the example below. 

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

The bot receives the `dialout_setting` and begins dialing out as soon as the call's 
`state` moves to `joined`.

```python
@transport.event_handler("on_call_state_updated")
async def on_call_state_updated(transport, state):
    if state == "joined" and dialout_settings:
        await start_dialout(transport, dialout_settings)
```

Since this is a dialout, the bot will wait for the remote party (hopefully a human 
not voicemail) to speak. It will only respond after it is spoken to. In a future
version, we will have voicemail detection that will be inserted at this point.

```python
@transport.event_handler("on_dialout_answered")
    async def on_dialout_answered(transport, data):
        # the bot will wait for the user to speak before responding
        await rtvi.set_bot_ready()
```

## Deployment

See the [top-level README](../README.md) for deployment instructions.
