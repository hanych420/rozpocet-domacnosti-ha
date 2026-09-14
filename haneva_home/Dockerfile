FROM ghcr.io/home-assistant/base:latest

RUN apk add --no-cache python3

WORKDIR /app
COPY app.py /app/app.py
COPY home.html /app/home.html
COPY calendar.html /app/calendar.html

CMD ["python3", "/app/app.py"]
