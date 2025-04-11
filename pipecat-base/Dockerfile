FROM python:3.10-slim
WORKDIR /app
RUN apt update && apt install -y libopenblas-dev libresample1 libresample-dev && apt clean
RUN ln -s /krisp/python/pipecat_ai_krisp /usr/local/lib/python3.10/site-packages/pipecat_ai_krisp
COPY ./requirements.txt /app/base_requirements.txt
RUN pip install --no-cache-dir --upgrade -r /app/base_requirements.txt
COPY ./app.py ./waiting_server.py /app/
CMD ["python", "app.py"]
