ARG PYTHON_VERSION=3.10
FROM python:${PYTHON_VERSION}-slim
WORKDIR /app
RUN apt update && apt install -y libopenblas-dev libresample1 libresample-dev && apt clean
RUN PYTHON_SITE_PACKAGES=$(python -c "import site; print(site.getsitepackages()[0])") && \
    ln -s /krisp/python/pipecat_ai_krisp ${PYTHON_SITE_PACKAGES}/pipecat_ai_krisp
COPY ./requirements.txt /app/base_requirements.txt
RUN pip install --no-cache-dir --upgrade -r /app/base_requirements.txt
COPY ./app.py ./waiting_server.py /app/
ARG VERSION
ENV IMAGE_VERSION=$VERSION
CMD ["sh", "-c", "if [ -f /app/pre-app.sh ]; then sh /app/pre-app.sh; fi && exec python app.py"]
