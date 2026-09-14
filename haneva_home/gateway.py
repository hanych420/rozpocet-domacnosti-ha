from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from urllib.parse import urlparse
import re

import app

HOST = app.HOST
PORT = app.PORT
BUDGET_HOST = "192.168.0.60"
BUDGET_PORT = 8099
BUDGET_PREFIX = "/rozpocet"
PUBLIC_HOST = "haneva.cz"
OLD_BUDGET_HOST = "rozpocet.haneva.cz"

HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
}

TEXT_TYPES = (
    "text/html",
    "text/css",
    "application/javascript",
    "text/javascript",
    "application/x-javascript",
)

BUDGET_HOME_CSS = """
<style id="haneva-budget-home-style">
.haneva-budget-homebar{position:sticky;top:0;z-index:99999;display:flex;align-items:center;padding:10px 14px;background:rgba(255,255,255,.94);border-bottom:1px solid #e8ebf2;backdrop-filter:blur(14px);-webkit-backdrop-filter:blur(14px);font-family:Inter,ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
.haneva-budget-home{display:inline-flex;align-items:center;gap:10px;color:#172033!important;text-decoration:none!important;font-size:15px;font-weight:800;line-height:1}
.haneva-budget-home-mark{width:34px;height:34px;display:grid;place-items:center;border-radius:11px;background:#111827;color:#fff;font-size:15px;font-weight:850;box-shadow:0 7px 18px rgba(17,24,39,.15)}
.haneva-budget-home:hover{opacity:.82}
@media(max-width:760px){.haneva-budget-homebar{padding:8px 10px}.haneva-budget-home-mark{width:32px;height:32px;border-radius:10px}.haneva-budget-home{font-size:14px}}
</style>
"""

BUDGET_HOME_HTML = """
<div class="haneva-budget-homebar" id="haneva-budget-homebar"><a class="haneva-budget-home" href="https://haneva.cz/" aria-label="Zpět na hlavní menu Haneva"><span class="haneva-budget-home-mark">H</span><span>Haneva</span></a></div>
"""


def add_budget_home_link(text):
    lower = text.lower()
    if "<body" not in lower or "</head>" not in lower or "haneva-budget-homebar" in text:
        return text
    text = re.sub(r"</head>", BUDGET_HOME_CSS + "</head>", text, count=1, flags=re.IGNORECASE)
    text = re.sub(r"(<body\b[^>]*>)", r"\1" + BUDGET_HOME_HTML, text, count=1, flags=re.IGNORECASE)
    return text


def rewrite_budget_text(text):
    text = text.replace("https://rozpocet.haneva.cz/", "/rozpocet/")
    text = text.replace("http://rozpocet.haneva.cz/", "/rozpocet/")
    text = text.replace("https://haneva.cz/rozpocet/rozpocet/", "/rozpocet/")

    # Root-relative URLs in HTML and JavaScript strings.
    text = re.sub(
        r"([\"'`])/(?!/|rozpocet(?:/|[\"'`]))",
        r"\1/rozpocet/",
        text,
    )
    # CSS url(/asset.ext)
    text = re.sub(
        r"url\(\s*/(?!/|rozpocet/)",
        "url(/rozpocet/",
        text,
        flags=re.IGNORECASE,
    )
    # Rare unquoted HTML attributes: href=/foo
    text = re.sub(
        r"(?i)\b(href|src|action)=/(?!/|rozpocet/)",
        r"\1=/rozpocet/",
        text,
    )

    # Inject the Haneva home link only into actual HTML documents, after URL rewriting
    # so the absolute homepage link remains https://haneva.cz/.
    return add_budget_home_link(text)


def rewrite_location(value):
    if not value:
        return value
    if value.startswith("https://rozpocet.haneva.cz"):
        suffix = value[len("https://rozpocet.haneva.cz"):]
        return f"https://{PUBLIC_HOST}{BUDGET_PREFIX}{suffix or '/'}"
    if value.startswith("http://rozpocet.haneva.cz"):
        suffix = value[len("http://rozpocet.haneva.cz"):]
        return f"https://{PUBLIC_HOST}{BUDGET_PREFIX}{suffix or '/'}"
    if value.startswith("/") and not value.startswith(BUDGET_PREFIX):
        return BUDGET_PREFIX + value
    return value


class GatewayHandler(app.Handler):
    server_version = "HanevaHome/0.4.1"

    def _request_host(self):
        return (self.headers.get("Host") or "").split(":", 1)[0].lower()

    def _redirect_old_budget_host(self):
        if self._request_host() != OLD_BUDGET_HOST:
            return False
        parsed = urlparse(self.path)
        suffix = parsed.path if parsed.path and parsed.path != "/" else "/"
        target = f"https://{PUBLIC_HOST}{BUDGET_PREFIX}{suffix}"
        if parsed.query:
            target += "?" + parsed.query
        self.send_response(308)
        self.send_header("Location", target)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", "0")
        self.end_headers()
        return True

    def _is_budget_path(self):
        path = urlparse(self.path).path
        return path == BUDGET_PREFIX or path.startswith(BUDGET_PREFIX + "/")

    def _proxy_budget(self):
        parsed = urlparse(self.path)
        if parsed.path == BUDGET_PREFIX:
            target = BUDGET_PREFIX + "/"
            if parsed.query:
                target += "?" + parsed.query
            self.send_response(308)
            self.send_header("Location", target)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        origin_path = parsed.path[len(BUDGET_PREFIX):] or "/"
        if not origin_path.startswith("/"):
            origin_path = "/" + origin_path
        if parsed.query:
            origin_path += "?" + parsed.query

        try:
            length = int(self.headers.get("Content-Length", "0") or "0")
        except ValueError:
            length = 0
        request_body = self.rfile.read(length) if length > 0 else None

        request_headers = {}
        for key, value in self.headers.items():
            lower = key.lower()
            if lower in HOP_BY_HOP_HEADERS or lower in {"host", "content-length", "accept-encoding"}:
                continue
            request_headers[key] = value
        request_headers["Host"] = f"{BUDGET_HOST}:{BUDGET_PORT}"
        request_headers["Accept-Encoding"] = "identity"
        request_headers["X-Forwarded-Host"] = self.headers.get("Host", PUBLIC_HOST)
        request_headers["X-Forwarded-Prefix"] = BUDGET_PREFIX
        request_headers["X-Forwarded-Proto"] = "https"
        if request_body is not None:
            request_headers["Content-Length"] = str(len(request_body))

        conn = HTTPConnection(BUDGET_HOST, BUDGET_PORT, timeout=25)
        try:
            conn.request(self.command, origin_path, body=request_body, headers=request_headers)
            response = conn.getresponse()
            body = response.read()
            response_headers = response.getheaders()
        except Exception as exc:
            message = f"Rozpočet není dostupný: {exc}".encode("utf-8", "replace")
            self.send_response(502)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(message)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(message)
            return
        finally:
            conn.close()

        content_type = ""
        content_encoding = ""
        for key, value in response_headers:
            lower = key.lower()
            if lower == "content-type":
                content_type = value.lower()
            elif lower == "content-encoding":
                content_encoding = value.lower()

        rewritten = False
        if not content_encoding and any(content_type.startswith(t) for t in TEXT_TYPES):
            try:
                text = body.decode("utf-8")
                new_text = rewrite_budget_text(text)
                if new_text != text:
                    body = new_text.encode("utf-8")
                    rewritten = True
            except UnicodeDecodeError:
                pass

        self.send_response(response.status, response.reason)
        for key, value in response_headers:
            lower = key.lower()
            if lower in HOP_BY_HOP_HEADERS or lower in {"content-length", "location"}:
                continue
            if lower == "set-cookie":
                value = re.sub(r"(?i)(;\s*Path=)/(?=;|$)", r"\1/rozpocet/", value)
            if rewritten and lower in {"etag", "content-md5"}:
                continue
            self.send_header(key, value)

        location = response.getheader("Location")
        if location:
            self.send_header("Location", rewrite_location(location))
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_GET(self):
        if self._redirect_old_budget_host():
            return
        if self._is_budget_path():
            return self._proxy_budget()
        return super().do_GET()

    def do_POST(self):
        if self._redirect_old_budget_host():
            return
        if self._is_budget_path():
            return self._proxy_budget()
        return super().do_POST()

    def do_PUT(self):
        if self._redirect_old_budget_host():
            return
        if self._is_budget_path():
            return self._proxy_budget()
        return super().do_PUT()

    def do_DELETE(self):
        if self._redirect_old_budget_host():
            return
        if self._is_budget_path():
            return self._proxy_budget()
        return super().do_DELETE()

    def do_PATCH(self):
        if self._redirect_old_budget_host():
            return
        if self._is_budget_path():
            return self._proxy_budget()
        self.send_bytes(b"Not found\n", 404, "text/plain; charset=utf-8")

    def do_HEAD(self):
        if self._redirect_old_budget_host():
            return
        if self._is_budget_path():
            return self._proxy_budget()
        return super().do_HEAD()


if __name__ == "__main__":
    app.init_db()
    server = ThreadingHTTPServer((HOST, PORT), GatewayHandler)
    print(f"Haneva Home gateway listening on http://{HOST}:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
