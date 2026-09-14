from http.server import ThreadingHTTPServer
from urllib.parse import urlparse
import json
import re

import app
import gateway

VERSION = "0.5.3"
ICON_VERSION = "20260914-v3"
ICON_PATH = "/app/app-icon-v3.png"

PWA_HEAD = f"""
<!-- haneva-pwa-v3 -->
<link rel="icon" type="image/png" href="/app-icon-v3.png?v={ICON_VERSION}">
<link rel="apple-touch-icon" sizes="180x180" href="/apple-touch-icon-v3.png?v={ICON_VERSION}">
<link rel="manifest" href="/manifest.webmanifest?v={ICON_VERSION}">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="default">
<meta name="apple-mobile-web-app-title" content="Haneva">
<style id="haneva-brand-icon-v3">
.brand-mark {{
  background-image:url('/app-icon-v3.png?v={ICON_VERSION}')!important;
  background-size:cover!important;
  background-position:center!important;
  background-repeat:no-repeat!important;
  color:transparent!important;
  overflow:hidden!important;
}}
</style>
"""

MANIFEST = {
    "name": "Haneva Home",
    "short_name": "Haneva",
    "start_url": "/",
    "scope": "/",
    "display": "standalone",
    "background_color": "#0f1a2e",
    "theme_color": "#0f1a2e",
    "icons": [
        {
            "src": f"/app-icon-v3.png?v={ICON_VERSION}",
            "sizes": "180x180",
            "type": "image/png",
            "purpose": "any maskable",
        }
    ],
}

# Rozpočet dostává horní lištu z gateway.py, proto přepíšeme i její H na stejné logo.
gateway.BUDGET_HOME_CSS += f"""
<style id="haneva-budget-brand-icon-v3">
.haneva-budget-brand-mark {{
  background-image:url('/app-icon-v3.png?v={ICON_VERSION}')!important;
  background-size:cover!important;
  background-position:center!important;
  background-repeat:no-repeat!important;
  color:transparent!important;
  overflow:hidden!important;
}}
</style>
"""


class IconGatewayHandler(gateway.GatewayHandler):
    server_version = f"HanevaHome/{VERSION}"

    def _serve_icon(self):
        path = urlparse(self.path).path
        icon_paths = {
            "/app-icon-v3.png",
            "/apple-touch-icon-v3.png",
            "/apple-touch-icon.png",
            "/apple-touch-icon-precomposed.png",
            "/favicon.ico",
        }
        if path not in icon_paths:
            return False

        try:
            with open(ICON_PATH, "rb") as handle:
                body = handle.read()
        except OSError:
            self.send_bytes(b"Not found\n", 404, "text/plain; charset=utf-8")
            return True

        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        # Versioned path can be cached forever; legacy auto-discovery paths should revalidate.
        if path in {"/app-icon-v3.png", "/apple-touch-icon-v3.png"}:
            self.send_header("Cache-Control", "public, max-age=31536000, immutable")
        else:
            self.send_header("Cache-Control", "no-cache, must-revalidate")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)
        return True

    def _serve_manifest(self):
        if urlparse(self.path).path != "/manifest.webmanifest":
            return False
        body = json.dumps(MANIFEST, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/manifest+json; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, must-revalidate")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)
        return True

    def send_bytes(self, body, status=200, content_type="text/html; charset=utf-8"):
        if content_type.lower().startswith("text/html"):
            try:
                text = body.decode("utf-8")
                if "haneva-pwa-v3" not in text and re.search(r"</head\s*>", text, re.I):
                    text = re.sub(r"</head\s*>", PWA_HEAD + "</head>", text, count=1, flags=re.I)
                    body = text.encode("utf-8")
            except UnicodeDecodeError:
                pass
        return super().send_bytes(body, status, content_type)

    def do_GET(self):
        if self._serve_icon() or self._serve_manifest():
            return
        return super().do_GET()

    def do_HEAD(self):
        if self._serve_icon() or self._serve_manifest():
            return
        return super().do_HEAD()


if __name__ == "__main__":
    app.init_db()
    server = ThreadingHTTPServer((gateway.HOST, gateway.PORT), IconGatewayHandler)
    print(f"Haneva Home gateway listening on http://{gateway.HOST}:{gateway.PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
