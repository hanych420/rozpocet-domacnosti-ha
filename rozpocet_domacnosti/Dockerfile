FROM ghcr.io/home-assistant/base:latest
RUN apk add --no-cache python3 coreutils
WORKDIR /app
COPY payload/ /tmp/payload/
RUN cat /tmp/payload/chunk* | base64 -d | tar xzf - -C /app && rm -rf /tmp/payload
COPY patch_v062.py.gz.b64 /tmp/patch_v062.py.gz.b64
RUN base64 -d /tmp/patch_v062.py.gz.b64 | gzip -d > /tmp/patch_v062.py \
    && python3 /tmp/patch_v062.py \
    && rm -f /tmp/patch_v062.py /tmp/patch_v062.py.gz.b64
CMD ["python3", "/app/app.py"]
