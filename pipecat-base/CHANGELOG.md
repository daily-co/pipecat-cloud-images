# pipecat-base Changelog

All notable changes to the **Pipecat Cloud Base Images** will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## Unreleased

### Added

- Added Python path config to site-packages for locating the `krisp_audio`
  package.

## [0.1.7] - 2025-10-14

### Added

- Added system dependencies for SmallWebRTCTransport to the pipecat-base
  image's Dockerfile.

## [0.1.6] - 2025-10-09

### Added

- Added support for `WhatsApp` in the base image.
## [0.1.5] - 2025-10-02

### Added

- Added support for `SmallWebRTCTransport` in the base image.

## [0.1.4] - 2025-09-12

### Added

- Telnyx and Plivo websocket connections can now optionally receive custom
  `body` information, which is provided via the `WebsocketSessionArguments`.

## [0.1.3] - 2025-09-09

### Added

- Added extra logging after the `bot()` function to tell when the function
  finishes, whether it finishes normally or raises an exception.

## [0.1.2] - 2025-08-29

### Added

- Added the ability to access the `FastAPI` application by extracting it into
  a module named pipecatcloud_system. This allows image developers to implement
  API methods in their bot that are accessible while a session is in progress.

## [0.1.1] - 2025-08-22

### Changed

- Migrated from `pip` to `uv` for dependency management, providing faster
  builds and more reliable dependency resolution.

- Updated base image to a `uv` native version. Updated from `python:3.12-slim`
  to `ghcr.io/astral-sh/uv:python3.12-trixie-slim`.

- Dependencies now installed in virtual environment (`/app/.venv`) for
  optimized Docker layer caching.

## [0.1.0] - 2025-08-18

### Changed

- The default base image now uses Python 3.12. This change brings better
  performance to your bots due to Python performance improvements, namely to
  `asyncio`. This affects the `dailyco/pipecat-base:latest` and
  `dailyco/pipecat-base:0.1.0` tags. Python 3.10 images remain available via
  `dailyco/pipecat-base:latest-py3.10` and `dailyco/pipecat-base:0.1.0-py3.10`.

## [0.0.9] - 2025-07-08

### Added

- Added support for Python 3.11, 3.12, and 3.13 (previously only 3.10)
- New image tags for specific Python versions:
  - `dailyco/pipecat-base:latest-py3.10` / `dailyco/pipecat-base:0.0.9-py3.10`
  - `dailyco/pipecat-base:latest-py3.11` / `dailyco/pipecat-base:0.0.9-py3.11`
  - `dailyco/pipecat-base:latest-py3.12` / `dailyco/pipecat-base:0.0.9-py3.12`
  - `dailyco/pipecat-base:latest-py3.13` / `dailyco/pipecat-base:0.0.9-py3.13`

### Changed

- Default Python version remains 3.10 for backward compatibility
- Existing tags continue to work unchanged:
  - `dailyco/pipecat-base:latest` (Python 3.10)
  - `dailyco/pipecat-base:0.0.9` (Python 3.10)
