from http.server import ThreadingHTTPServer
from urllib.parse import urlparse

import app
import gateway

ASSETS = {
    "/favicon.ico": ("/app/favicon.ico", "image/x-icon"),
    "/apple-touch-icon.png": ("/app/apple-touch-icon.png", "image/png"),
    "/apple-touch-icon-precomposed.png": ("/app/apple-touch-icon.png", "image/png"),
}


class IconGatewayHandler(gateway.GatewayHandler):
    server_version = "HanevaHome/0.5.2"

    def _serve_haneva_asset(self):
        path = urlparse(self.path).path
        asset = ASSETS.get(path)
        if not asset:
            return False

        file_path, content_type = asset
        try:
            with open(file_path, "rb") as handle:
                body = handle.read()
        except OSError:
            self.send_bytes(b"Not found\n", 404, "text/plain; charset=utf-8")
            return True

        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "public, max-age=604800")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)
        return True

    def do_GET(self):
        if self._serve_haneva_asset():
            return
        return super().do_GET()

    def do_HEAD(self):
        if self._serve_haneva_asset():
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
