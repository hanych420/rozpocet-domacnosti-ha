FROM ghcr.io/home-assistant/base:latest
RUN apk add --no-cache python3 coreutils
WORKDIR /app
COPY payload/ /tmp/payload/
RUN cat /tmp/payload/chunk* | base64 -d | tar xzf - -C /app && rm -rf /tmp/payload
CMD ["python3", "/app/app.py"]
