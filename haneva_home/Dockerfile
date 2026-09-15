FROM ghcr.io/home-assistant/base:latest

RUN apk add --no-cache python3 py3-requests py3-beautifulsoup4 coreutils

# Nejprve sestavíme původní Rozpočet domácnosti přesně stejným způsobem
# jako v samostatném add-onu a uložíme ho pod /app/budget.
WORKDIR /app
COPY legacy_budget_payload/ /tmp/legacy_budget_payload/
RUN cat /tmp/legacy_budget_payload/chunk* | base64 -d | tar xzf - -C /app \
    && rm -rf /tmp/legacy_budget_payload
COPY legacy_budget_patch_v062.py.gz.b64 /tmp/legacy_budget_patch_v062.py.gz.b64
RUN base64 -d /tmp/legacy_budget_patch_v062.py.gz.b64 | gzip -d > /tmp/legacy_budget_patch_v062.py \
    && python3 /tmp/legacy_budget_patch_v062.py \
    && rm -f /tmp/legacy_budget_patch_v062.py /tmp/legacy_budget_patch_v062.py.gz.b64

# v0.7.0 přidává položkovou správu pravidelných nákladů přímo do rozpočtu.
COPY legacy_budget_patch_v070.py.gz.b64 /tmp/legacy_budget_patch_v070.py.gz.b64
RUN base64 -d /tmp/legacy_budget_patch_v070.py.gz.b64 | gzip -d > /tmp/legacy_budget_patch_v070.py \
    && python3 /tmp/legacy_budget_patch_v070.py \
    && rm -f /tmp/legacy_budget_patch_v070.py /tmp/legacy_budget_patch_v070.py.gz.b64

WORKDIR /tmp
RUN mkdir -p /tmp/legacy_budget_ready \
    && cp -a /app/. /tmp/legacy_budget_ready/ \
    && rm -rf /app \
    && mkdir -p /app/budget \
    && cp -a /tmp/legacy_budget_ready/. /app/budget/ \
    && rm -rf /tmp/legacy_budget_ready

# Haneva Home běží nad tímto embedded rozpočtem.
WORKDIR /app
COPY app.py /app/app.py
COPY gateway.py /app/gateway.py
COPY icon_gateway.py /app/icon_gateway.py
COPY profile_gateway.py /app/profile_gateway.py
COPY agenda_gateway.py /app/agenda_gateway.py
COPY consolidated_gateway.py /app/consolidated_gateway.py
COPY seed_holidays.py /app/seed_holidays.py
COPY shopping.py /app/shopping.py
COPY home.html /app/home.html
COPY calendar.html /app/calendar.html
COPY shopping.html /app/shopping.html
COPY favicon.ico /app/favicon.ico
COPY apple-touch-icon.png /app/apple-touch-icon.png
COPY app-icon-v3.png /app/app-icon-v3.png
COPY run.sh /app/run.sh

# Pokud starý rozpočet používá absolutní cesty typu /app/index.html nebo /app/static,
# zpřístupníme jeho pomocné soubory i na původním místě. Existující soubory Hanevy
# se nikdy nepřepisují; app.py rozpočtu zůstává pod /app/budget/app.py.
RUN for item in /app/budget/* /app/budget/.[!.]* /app/budget/..?*; do \
      [ -e "$item" ] || continue; \
      name="$(basename "$item")"; \
      [ "$name" = "app.py" ] && continue; \
      [ -e "/app/$name" ] || ln -s "$item" "/app/$name"; \
    done \
    && chmod +x /app/run.sh

CMD ["/app/run.sh"]
