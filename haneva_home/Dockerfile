FROM ghcr.io/home-assistant/base:latest

RUN apk add --no-cache python3 py3-requests py3-beautifulsoup4

WORKDIR /app
COPY app.py /app/app.py
COPY gateway.py /app/gateway.py
COPY seed_holidays.py /app/seed_holidays.py
COPY shopping.py /app/shopping.py
COPY home.html /app/home.html
COPY calendar.html /app/calendar.html
COPY shopping.html /app/shopping.html

CMD ["sh", "-c", "python3 /app/seed_holidays.py && exec python3 /app/gateway.py"]
