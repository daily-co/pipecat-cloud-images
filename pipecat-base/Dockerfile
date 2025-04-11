ARG PYTHON_VERSION=3.10
FROM python:${PYTHON_VERSION}-slim
WORKDIR /app
RUN apt update && apt install -y libopenblas-dev libresample1 libresample-dev && apt clean
RUN SHORT_PYTHON_VERSION=${PYTHON_VERSION%.*} && \
    ln -s /krisp/python/pipecat_ai_krisp /usr/local/lib/python$SHORT_PYTHON_VERSION/site-packages/pipecat_ai_krisp
COPY ./requirements.txt /app/base_requirements.txt
RUN pip install --no-cache-dir --upgrade -r /app/base_requirements.txt
COPY ./app.py ./waiting_server.py /app/
CMD ["python", "app.py"]
