import os
from http.server import ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import app
import gateway
import profile_gateway
import agenda_gateway
import shopping
import shopping_official

VERSION = "0.9.1"
SHOPPING_DEALS_HTML_PATH = "/app/shopping_deals.html"

# Gateway může při přechodu ještě dočasně používat starý add-on,
# po úspěšné migraci se přepne na embedded server ve stejném kontejneru.
gateway.BUDGET_HOST = os.environ.get("HANEVA_BUDGET_HOST", gateway.BUDGET_HOST)
try:
    gateway.BUDGET_PORT = int(os.environ.get("HANEVA_BUDGET_PORT", str(gateway.BUDGET_PORT)))
except ValueError:
    gateway.BUDGET_PORT = 8099


class ConsolidatedGatewayHandler(agenda_gateway.AgendaGatewayHandler):
    server_version = f"HanevaHome/{VERSION}"

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path in ("/nakupy/akce", "/nakupy/akce/"):
            try:
                self.send_bytes(app.read_page(SHOPPING_DEALS_HTML_PATH))
            except OSError:
                self.send_bytes(b"Shopping deals page not found\n", 500, "text/plain; charset=utf-8")
            return

        if path == "/api/shopping/state":
            self.send_json(shopping_official.enrich_state(shopping.get_state()))
            return

        if path == "/api/shopping/official":
            query = parse_qs(parsed.query)
            store = (query.get("store", ["all"])[0] or "all").lower()
            if store not in {"all", "lidl", "albert"}:
                store = "all"
            time_filter = (query.get("time", ["current"])[0] or "current").lower()
            if time_filter not in {"current", "next", "all"}:
                time_filter = "current"
            text = (query.get("q", [""])[0] or "").strip()[:120]
            self.send_json(shopping_official.get_grouped_deals(store=store, time_filter=time_filter, query=text))
            return

        # Legacy Kupi search stays available as a fallback/debug endpoint.
        if path == "/api/shopping/search":
            query = parse_qs(parsed.query)
            text = (query.get("q", [""])[0] or "").strip()
            force = query.get("refresh", ["0"])[0] in {"1", "true", "yes"}
            if not text:
                self.send_json({"deals": [], "queries": [], "updated_at": None, "errors": [], "source": "Kupi.cz"})
                return
            self.send_json(shopping.get_deals(force=force, query=text))
            return

        super().do_GET()

    def do_POST(self):
        path = urlparse(self.path).path

        if path == "/api/shopping/sync-official":
            self.send_json({"sync": shopping_official.request_sync()}, 202)
            return

        if path == "/api/shopping/deal-add":
            try:
                payload = self.read_json()
                item = shopping.add_item({
                    "name": payload.get("name", ""),
                    "quantity": payload.get("quantity", ""),
                    "preferred_store": payload.get("preferred_store") or payload.get("store") or "any",
                })
                shopping_official.attach_item(item.get("id"), payload)
                self.send_json({"item": item}, 201)
            except (ValueError, TypeError) as exc:
                self.send_json({"error": str(exc)}, 400)
            return

        super().do_POST()


if __name__ == "__main__":
    profile_gateway.init_profile_db()
    app.init_db()
    shopping_official.init_db()
    shopping_official.start_worker()
    server = ThreadingHTTPServer((gateway.HOST, gateway.PORT), ConsolidatedGatewayHandler)
    print(
        f"Haneva Home {VERSION} listening on http://{gateway.HOST}:{gateway.PORT}; "
        f"budget={gateway.BUDGET_HOST}:{gateway.BUDGET_PORT}"
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
