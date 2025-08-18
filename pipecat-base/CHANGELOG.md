# pipecat-base Changelog

All notable changes to the **Pipecat Cloud Base Images** will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
