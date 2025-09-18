# TODO: replace for pipecat-base:latest, only for local testing
# I have built the local pipecat base image like this
# docker build -t dailyco/pipecat-base:pcc .
FROM dailyco/pipecat-base:pcc

# Install system dependencies required for OpenCV
RUN apt-get update && apt-get install -y \
    libgl1 \
    libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

COPY ./requirements.txt requirements.txt

RUN pip install --no-cache-dir --upgrade -r requirements.txt

# Only for local development with the local versions of Pipecat and Pipecat Cloud
# First you need to build both Pipecat and Pipecat Cloud
# python -m build
COPY ./pipecatcloud-0.2.4.tar.gz pipecatcloud-0.2.4.tar.gz
RUN pip install ./pipecatcloud-0.2.4.tar.gz
COPY ./pipecat_ai-0.0.86.dev38.tar.gz pipecat_ai-0.0.86.dev38.tar.gz
RUN pip install ./pipecat_ai-0.0.86.dev38.tar.gz
# COPY ./.env .env
# --------- end of local development

COPY ./bot.py bot.py
