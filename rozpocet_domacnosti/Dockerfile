FROM ghcr.io/home-assistant/base:latest
RUN apk add --no-cache python3 coreutils
WORKDIR /app
COPY payload.tgz /tmp/payload.tgz
RUN tar xzf /tmp/payload.tgz -C /app && rm /tmp/payload.tgz
CMD ["python3", "/app/app.py"]
