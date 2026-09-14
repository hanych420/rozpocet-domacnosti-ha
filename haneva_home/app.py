from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

HOST = "0.0.0.0"
PORT = 8100

HTML = r'''<!doctype html>
<html lang="cs">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
  <meta name="theme-color" content="#111827">
  <title>Haneva Home</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #f5f7fb;
      --surface: rgba(255,255,255,.88);
      --surface-solid: #ffffff;
      --text: #172033;
      --muted: #6b7280;
      --line: #e8ebf2;
      --shadow: 0 18px 50px rgba(31,41,55,.10);
      --radius: 26px;
    }

    * { box-sizing: border-box; }
    html { -webkit-text-size-adjust: 100%; }
    body {
      margin: 0;
      min-height: 100vh;
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      color: var(--text);
      background:
        radial-gradient(circle at 10% 0%, rgba(147,197,253,.35), transparent 32rem),
        radial-gradient(circle at 100% 10%, rgba(216,180,254,.30), transparent 30rem),
        var(--bg);
    }

    .wrap {
      width: min(1120px, calc(100% - 32px));
      margin: 0 auto;
      padding: 28px 0 48px;
    }

    header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 20px;
      margin-bottom: 54px;
    }

    .brand {
      display: flex;
      align-items: center;
      gap: 12px;
      font-weight: 800;
      letter-spacing: -.02em;
      font-size: 19px;
    }

    .brand-mark {
      width: 42px;
      height: 42px;
      display: grid;
      place-items: center;
      border-radius: 14px;
      background: #111827;
      color: #fff;
      box-shadow: 0 10px 24px rgba(17,24,39,.18);
    }

    .today {
      color: var(--muted);
      font-size: 14px;
      font-weight: 650;
      text-align: right;
    }

    .hero {
      max-width: 760px;
      margin-bottom: 34px;
    }

    .eyebrow {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      padding: 8px 12px;
      border-radius: 999px;
      background: rgba(255,255,255,.68);
      border: 1px solid rgba(255,255,255,.85);
      color: #596174;
      font-size: 13px;
      font-weight: 750;
      box-shadow: 0 8px 28px rgba(31,41,55,.05);
    }

    h1 {
      margin: 18px 0 12px;
      font-size: clamp(40px, 7vw, 72px);
      line-height: .98;
      letter-spacing: -.055em;
      max-width: 820px;
    }

    .lead {
      margin: 0;
      color: var(--muted);
      font-size: clamp(17px, 2.2vw, 21px);
      line-height: 1.55;
      max-width: 680px;
    }

    .grid {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 18px;
    }

    .card {
      position: relative;
      overflow: hidden;
      min-height: 260px;
      padding: 26px;
      border-radius: var(--radius);
      background: var(--surface);
      border: 1px solid rgba(255,255,255,.88);
      box-shadow: var(--shadow);
      backdrop-filter: blur(18px);
      -webkit-backdrop-filter: blur(18px);
      display: flex;
      flex-direction: column;
      justify-content: space-between;
      text-decoration: none;
      color: inherit;
      transition: transform .18s ease, box-shadow .18s ease;
    }

    a.card:hover {
      transform: translateY(-3px);
      box-shadow: 0 24px 60px rgba(31,41,55,.14);
    }

    .card::after {
      content: "";
      position: absolute;
      width: 190px;
      height: 190px;
      border-radius: 50%;
      right: -55px;
      bottom: -70px;
      opacity: .9;
      pointer-events: none;
    }

    .budget::after { background: rgba(110,231,183,.32); }
    .calendar::after { background: rgba(167,139,250,.28); }

    .icon {
      width: 58px;
      height: 58px;
      display: grid;
      place-items: center;
      border-radius: 18px;
      background: var(--surface-solid);
      box-shadow: 0 12px 30px rgba(31,41,55,.10);
      font-size: 28px;
      position: relative;
      z-index: 1;
    }

    .card-content { position: relative; z-index: 1; }
    .card h2 {
      font-size: 30px;
      line-height: 1.08;
      letter-spacing: -.035em;
      margin: 28px 0 8px;
    }

    .card p {
      margin: 0;
      color: var(--muted);
      line-height: 1.5;
      max-width: 410px;
    }

    .bottom {
      position: relative;
      z-index: 1;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      margin-top: 28px;
    }

    .status {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      color: #556070;
      font-size: 13px;
      font-weight: 750;
    }

    .dot {
      width: 8px;
      height: 8px;
      border-radius: 50%;
      background: #34d399;
      box-shadow: 0 0 0 5px rgba(52,211,153,.12);
    }

    .dot.waiting {
      background: #a78bfa;
      box-shadow: 0 0 0 5px rgba(167,139,250,.12);
    }

    .go {
      width: 42px;
      height: 42px;
      border-radius: 14px;
      display: grid;
      place-items: center;
      background: #111827;
      color: #fff;
      font-size: 20px;
      font-weight: 800;
    }

    .disabled { cursor: default; }
    .disabled .go {
      background: #eef0f5;
      color: #9ca3af;
      font-size: 12px;
      width: auto;
      padding: 0 14px;
    }

    footer {
      color: #9aa1ad;
      text-align: center;
      font-size: 12px;
      margin-top: 30px;
    }

    @media (max-width: 720px) {
      .wrap { width: min(100% - 22px, 1120px); padding-top: 18px; }
      header { margin-bottom: 42px; }
      .today { font-size: 12px; }
      .brand { font-size: 17px; }
      .brand-mark { width: 38px; height: 38px; border-radius: 12px; }
      .hero { margin-bottom: 26px; }
      h1 { font-size: clamp(42px, 13vw, 60px); }
      .lead { font-size: 17px; }
      .grid { grid-template-columns: 1fr; gap: 14px; }
      .card { min-height: 235px; padding: 22px; border-radius: 23px; }
      .card h2 { font-size: 28px; }
    }
  </style>
</head>
<body>
  <main class="wrap">
    <header>
      <div class="brand">
        <div class="brand-mark">H</div>
        <span>Haneva</span>
      </div>
      <div class="today" id="today">Haneva Home</div>
    </header>

    <section class="hero">
      <div class="eyebrow">🏠 Hanych + Eva</div>
      <h1>Všechno naše<br>na jednom místě.</h1>
      <p class="lead">Společný domov pro rozpočet, kalendář a další malé aplikace, které nám usnadní každodenní život.</p>
    </section>

    <section class="grid" aria-label="Aplikace Haneva">
      <a class="card budget" href="https://rozpocet.haneva.cz" aria-label="Otevřít Rozpočet domácnosti">
        <div class="card-content">
          <div class="icon">💰</div>
          <h2>Rozpočet</h2>
          <p>Příjmy, osobní a společné výdaje i přehled toho, co nám v měsíci zbývá.</p>
        </div>
        <div class="bottom">
          <span class="status"><span class="dot"></span>Aktivní</span>
          <span class="go">→</span>
        </div>
      </a>

      <article class="card calendar disabled" aria-label="Kalendář se připravuje">
        <div class="card-content">
          <div class="icon">📅</div>
          <h2>Kalendář</h2>
          <p>Hanych, Eva, společné akce, narozeniny a všechno, na co nechceme zapomenout.</p>
        </div>
        <div class="bottom">
          <span class="status"><span class="dot waiting"></span>Připravujeme</span>
          <span class="go">BRZY</span>
        </div>
      </article>
    </section>

    <footer>Haneva Home · běží doma na Raspberry Pi</footer>
  </main>

  <script>
    const el = document.getElementById('today');
    const now = new Date();
    const text = new Intl.DateTimeFormat('cs-CZ', {
      weekday: 'long', day: 'numeric', month: 'long'
    }).format(now);
    el.textContent = text.charAt(0).toUpperCase() + text.slice(1);
  </script>
</body>
</html>'''


class Handler(BaseHTTPRequestHandler):
    server_version = "HanevaHome/0.1"

    def send_common_headers(self, status=200, content_type="text/html; charset=utf-8", length=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")
        if length is not None:
            self.send_header("Content-Length", str(length))
        self.end_headers()

    def do_GET(self):
        path = urlparse(self.path).path

        if path == "/health":
            body = b"ok\n"
            self.send_common_headers(200, "text/plain; charset=utf-8", len(body))
            self.wfile.write(body)
            return

        if path not in ("/", "/index.html"):
            body = b"Not found\n"
            self.send_common_headers(404, "text/plain; charset=utf-8", len(body))
            self.wfile.write(body)
            return

        body = HTML.encode("utf-8")
        self.send_common_headers(200, "text/html; charset=utf-8", len(body))
        self.wfile.write(body)

    def do_HEAD(self):
        path = urlparse(self.path).path
        if path in ("/", "/index.html", "/health"):
            self.send_common_headers(200, "text/html; charset=utf-8", 0)
        else:
            self.send_common_headers(404, "text/plain; charset=utf-8", 0)

    def log_message(self, fmt, *args):
        print(f"{self.address_string()} - {fmt % args}")


if __name__ == "__main__":
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Haneva Home listening on http://{HOST}:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
