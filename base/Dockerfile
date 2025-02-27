FROM python:3.10-slim
WORKDIR /app
COPY ./requirements.txt /app/base_requirements.txt
RUN pip install --no-cache-dir --upgrade -r /app/base_requirements.txt
COPY ./app.py /app/app.py
CMD ["fastapi", "run", "app.py", "--port", "8080"]