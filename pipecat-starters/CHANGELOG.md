# pipecat-starters Changelog

All notable changes to the **Pipecat Cloud Starter Images** will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [2025-08-08]

### Fixed

- Fixed an issue in a number of starts where an error was through due to
  `reason` being included as an arg in the `on_client_disconnected` event
  handler.

### Updated Images

- `gemini_multimodal_live`: 0.0.10 → 0.0.11
- `natural_conversation`: 0.0.10 → 0.0.11
- `openai_realtime`: 0.0.10 → 0.0.11
- `vision`: 0.0.10 → 0.0.11
- `voice`: 0.0.9 → 0.0.10

## [2025-08-06]

### Changed

- **All Images**: Updated to use the new Pipecat runner
- **All Images**: Enabled Krisp for production deployment (set `enable_krisp = true` in your pcc-deploy.toml file)

### Updated Images

- `gemini_multimodal_live`: 0.0.9 → 0.0.10
- `natural_conversation`: 0.0.9 → 0.0.10
- `openai_realtime`: 0.0.9 → 0.0.10
- `pstn_sip`: 0.0.8 → 0.0.9
- `twilio`: 0.0.8 → 0.0.9
- `vision`: 0.0.9 → 0.0.10
- `voice`: 0.0.8 → 0.0.9

## [2025-07-22]

### gemini_multimodal_live (0.0.9)

#### Fixed

- Pipeline runner uses `handle_sigint=False` and forced garbage collection.

### natural_conversation (0.0.9)

#### Fixed

- Pipeline runner uses `handle_sigint=False` and forced garbage collection.

### openai_realtime (0.0.9)

#### Fixed

- Pipeline runner uses `handle_sigint=False` and forced garbage collection.

### vision (0.0.9)

#### Fixed

- Pipeline runner uses `handle_sigint=False` and forced garbage collection.

## [2025-07-08]

### gemini_multimodal_live (0.0.8)

- Initial changelog entry.

### natural_conversation (0.0.8)

- Initial changelog entry.

### openai_realtime (0.0.8)

- Initial changelog entry.

### pstn_sip (0.0.8)

- Initial changelog entry.

### twilio (0.0.8)

- Initial changelog entry.

### vision (0.0.8)

- Initial changelog entry.

### voice (0.0.8)

- Initial changelog entry.
