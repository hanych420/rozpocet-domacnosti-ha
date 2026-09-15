import os
from http.server import ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import app
import gateway
import profile_gateway
import agenda_gateway
import shopping

VERSION = "0.8.0"
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


if __name__ == "__main__":
    profile_gateway.init_profile_db()
    app.init_db()
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
