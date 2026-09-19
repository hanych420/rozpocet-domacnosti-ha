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
COPY home_control.py /app/home_control.py
COPY gateway_patch_v105.py /tmp/gateway_patch_v105.py
COPY seed_holidays.py /app/seed_holidays.py
COPY shopping.py /app/shopping.py
# Původní shopping_official drží databázi/grupování; data už nečteme z HTML/PDF.
COPY shopping_official.py /app/shopping_official_base.py
COPY shopping_official_wrapper.py /app/shopping_official.py
COPY shopping_integrations_v100.py /app/shopping_integrations_v100.py
COPY shopping_integrations_v102.py /app/shopping_integrations_v102.py
COPY shopping_translation_v104.py /app/shopping_translation_v104.py
COPY shopping_list_v104.py /app/shopping_list_v104.py
COPY shopping_albert_v110.py /app/shopping_albert_v110.py
COPY shopping_albert_runtime_v110.py /app/shopping_albert_runtime_v110.py
COPY shopping_albert_packaging_v114.py /app/shopping_albert_packaging_v114.py
COPY shopping_albert_list_meta_v110.py /app/shopping_albert_list_meta_v110.py
COPY shopping_popularity_v117.py /app/shopping_popularity_v117.py
COPY shopping_name_patch_v110.py /tmp/shopping_name_patch_v110.py
COPY shopping_deals_patch_v113.py /tmp/shopping_deals_patch_v113.py
COPY shopping_sync_status_v117.py /tmp/shopping_sync_status_v117.py
COPY calendar_mobile_patch_v115.py /tmp/calendar_mobile_patch_v115.py
COPY calendar_event_preview_v116.py /tmp/calendar_event_preview_v116.py
COPY calendar_datepicker_v118.py /tmp/calendar_datepicker_v118.py
COPY home.html /app/home.html
COPY smart_home.html /app/smart_home.html
COPY calendar.html /app/calendar.html
COPY shopping.html /app/shopping.html
COPY shopping_deals.html /app/shopping_deals.html
COPY shopping_list_v104.snippet /app/shopping_list_v104.snippet
COPY shopping_deals_v104.snippet /app/shopping_deals_v104.snippet
COPY favicon.ico /app/favicon.ico
COPY apple-touch-icon.png /app/apple-touch-icon.png
COPY app-icon-v3.png /app/app-icon-v3.png
COPY run.sh /app/run.sh
RUN python3 /tmp/gateway_patch_v105.py \
    && python3 /tmp/shopping_name_patch_v110.py \
    && python3 /tmp/shopping_deals_patch_v113.py \
    && python3 /tmp/shopping_sync_status_v117.py \
    && python3 /tmp/calendar_mobile_patch_v115.py \
    && python3 /tmp/calendar_event_preview_v116.py \
    && python3 /tmp/calendar_datepicker_v118.py \
    && rm -f /tmp/gateway_patch_v105.py /tmp/shopping_name_patch_v110.py /tmp/shopping_deals_patch_v113.py /tmp/shopping_sync_status_v117.py /tmp/calendar_mobile_patch_v115.py /tmp/calendar_event_preview_v116.py /tmp/calendar_datepicker_v118.py \
    && sed -i 's/VERSION = "0.9.2"/VERSION = "0.13.1"/' /app/consolidated_gateway.py \
    && sed -i 's/VERSION = "0.10.0"/VERSION = "0.13.1"/' /app/shopping_integrations_v100.py \
    && sed -i 's/VERSION = "0.10.3"/VERSION = "0.13.1"/' /app/shopping_integrations_v102.py \
    && sed -i 's#Haneva kontroluje oficiální nabídky Lidlu a Albertu několikrát denně a ukládá jen textová data, ne obrázky letáků. Pokud se struktura některého webu změní, Kupi.cz zůstává jako záložní zdroj.#Lidl Haneva přebírá z integrace Lidl Weekly Offers v Home Assistantu. Albert používá spravované strukturované zdroje Parse.bot; PDF/OCR ani Kupi se už nepoužívají.#' /app/shopping_deals.html \
    && python3 -c 'from pathlib import Path; pairs=[("/app/shopping.html","/app/shopping_list_v104.snippet"),("/app/shopping_deals.html","/app/shopping_deals_v104.snippet")]; [(lambda p,s: p.write_text(p.read_text(encoding="utf-8").replace("</body>",s.read_text(encoding="utf-8")+"\n</body>",1),encoding="utf-8"))(Path(page),Path(snippet)) for page,snippet in pairs]'

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
