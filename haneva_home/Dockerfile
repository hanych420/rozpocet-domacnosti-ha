FROM ghcr.io/home-assistant/base:latest

RUN apk add --no-cache python3

WORKDIR /app
COPY app.py /app/app.py

CMD ["python3", "/app/app.py"]
