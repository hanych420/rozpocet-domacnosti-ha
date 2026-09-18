from http.server import ThreadingHTTPServer
from urllib.parse import urlparse
import json
import os
import sqlite3

import app
import gateway
import icon_gateway

VERSION = "0.5.4"
PROFILE_DB_PATH = "/data/haneva_profiles.db"
PROP_CALENDAR = "propadleek"
PROP_COLOR = "#e11d48"

# Rozšíření backendu bez zásahu do existující app.py.
app.ALLOWED_CALENDARS.add(PROP_CALENDAR)
app.COLOR_KEYS.add(PROP_CALENDAR)
app.DEFAULT_COLORS[PROP_CALENDAR] = PROP_COLOR

ALLOWED_PROFILE_CALENDARS = set(app.ALLOWED_CALENDARS)
PERSONS = {"hanych", "eva"}
HOME_TILES = ("budget", "calendar", "shopping", "home-control")

PROFILE_STYLE = """
<style id="haneva-calendar-profile-v1">
  .identity-box{margin:0 0 16px;padding:13px 14px;border:1px solid var(--line);border-radius:14px;background:#f8fafc}
  .identity-title{font-size:12px;font-weight:850;color:#596174;margin-bottom:8px}
  .identity-row{display:grid;grid-template-columns:1fr 180px;gap:10px;align-items:center}
  .identity-email{min-width:0;font-size:12px;color:#7b8492;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .identity-select{width:100%;border:1px solid #dfe3ea;border-radius:11px;padding:9px 10px;background:#fff;color:#172033;font-weight:750}
  .identity-note{margin:8px 0 0;font-size:11px;line-height:1.4;color:#9299a5}
  @media(max-width:560px){.identity-row{grid-template-columns:1fr}.identity-select{font-size:16px}}
</style>
"""

IDENTITY_BLOCK = """
      <div class="identity-box" id="identitySettings">
        <div class="identity-title">Přihlášený účet</div>
        <div class="identity-row">
          <div class="identity-email" id="sessionEmail">Zjišťuji účet…</div>
          <select class="identity-select" id="sessionPerson" aria-label="Kdo je tento účet">
            <option value="">Nenastaveno</option>
            <option value="hanych">Tento účet je Hanych</option>
            <option value="eva">Tento účet je Eva</option>
          </select>
        </div>
        <p class="identity-note">Používá přihlášení přes Cloudflare Access. Uložená poslední kategorie má přednost před výchozí osobou.</p>
      </div>
"""

SESSION_JS = r"""
    let sessionState={email:'',access_detected:false,person:'',last_calendar:'',default_calendar:'spolecne'};

    function syncSessionSettings(){
      const emailEl=$('sessionEmail'),personEl=$('sessionPerson');
      if(!emailEl||!personEl)return;
      if(sessionState.access_detected){
        emailEl.textContent=sessionState.email||'Přihlášený účet';
        personEl.disabled=false;
        personEl.value=sessionState.person||'';
      }else{
        emailEl.textContent='Cloudflare účet se nepodařilo rozpoznat';
        personEl.value='';
        personEl.disabled=true;
      }
    }

    async function loadSession(){
      try{
        const res=await fetch('/api/session',{cache:'no-store'});
        const data=await res.json();
        if(!res.ok)throw new Error(data.error||'Relaci se nepodařilo načíst.');
        sessionState={...sessionState,...data};
      }catch(_){
        sessionState={email:'',access_detected:false,person:'',last_calendar:'',default_calendar:'spolecne'};
      }
      syncSessionSettings();
    }

    function preferredCalendar(){
      const key=sessionState.default_calendar||sessionState.person||'spolecne';
      return allCalendars.includes(key)?key:'spolecne';
    }

    async function rememberCalendar(calendar){
      if(!allCalendars.includes(calendar))return;
      sessionState.last_calendar=calendar;
      sessionState.default_calendar=calendar;
      if(!sessionState.access_detected)return;
      try{
        const res=await fetch('/api/session/calendar',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({calendar})});
        const data=await res.json();
        if(res.ok)sessionState={...sessionState,...data};
      }catch(_){ }
    }

    async function saveSessionPerson(person){
      if(!sessionState.access_detected)return;
      try{
        const res=await fetch('/api/session/profile',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({person})});
        const data=await res.json();
        if(!res.ok)throw new Error(data.error||'Účet se nepodařilo nastavit.');
        sessionState={...sessionState,...data};
        syncSessionSettings();
        toast(person==='hanych'?'Účet nastaven jako Hanych.':'Účet nastaven jako Eva.');
      }catch(err){
        toast(err.message||'Účet se nepodařilo nastavit.');
        syncSessionSettings();
      }
    }

"""


def init_profile_db():
    os.makedirs(os.path.dirname(PROFILE_DB_PATH), exist_ok=True)
    with sqlite3.connect(PROFILE_DB_PATH) as conn:
        conn.execute(
            '''CREATE TABLE IF NOT EXISTS profiles (
                email TEXT PRIMARY KEY,
                person TEXT NOT NULL DEFAULT '',
                last_calendar TEXT NOT NULL DEFAULT '',
                home_layout TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )'''
        )
        columns = {row[1] for row in conn.execute("PRAGMA table_info(profiles)").fetchall()}
        if "home_layout" not in columns:
            conn.execute("ALTER TABLE profiles ADD COLUMN home_layout TEXT NOT NULL DEFAULT ''")
        conn.commit()


def normalize_email(value):
    return (value or "").strip().lower()


def access_email(handler):
    # Cloudflare Access posílá ověřený e-mail uživatele na origin v tomto headeru.
    # X-User-Email je jen kompatibilní fallback, kdyby se identita později posílala přes Gateway rule.
    for header in ("Cf-Access-Authenticated-User-Email", "X-User-Email"):
        value = normalize_email(handler.headers.get(header))
        if value:
            return value
    return ""


def _normalize_home_layout(value):
    raw = value
    if isinstance(raw, str):
        try:
            raw = json.loads(raw) if raw else []
        except Exception:
            raw = []
    if not isinstance(raw, (list, tuple)):
        raw = []
    result = []
    for item in raw:
        key = str(item or "").strip()
        if key in HOME_TILES and key not in result:
            result.append(key)
    for key in HOME_TILES:
        if key not in result:
            result.append(key)
    return result


def read_profile(email):
    if not email:
        return {"person": "", "last_calendar": "", "home_layout": list(HOME_TILES)}
    with sqlite3.connect(PROFILE_DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT person, last_calendar, home_layout FROM profiles WHERE email=?", (email,)).fetchone()
    if not row:
        return {"person": "", "last_calendar": "", "home_layout": list(HOME_TILES)}
    return {
        "person": row["person"] or "",
        "last_calendar": row["last_calendar"] or "",
        "home_layout": _normalize_home_layout(row["home_layout"]),
    }


def save_person(email, person):
    if person not in PERSONS:
        raise ValueError("Vyber Hanych nebo Eva.")
    with sqlite3.connect(PROFILE_DB_PATH) as conn:
        conn.execute(
            '''INSERT INTO profiles(email, person, last_calendar, updated_at)
               VALUES(?, ?, '', CURRENT_TIMESTAMP)
               ON CONFLICT(email) DO UPDATE SET person=excluded.person, updated_at=CURRENT_TIMESTAMP''',
            (email, person),
        )
        conn.commit()


def save_last_calendar(email, calendar):
    if calendar not in ALLOWED_PROFILE_CALENDARS:
        raise ValueError("Neplatná kategorie kalendáře.")
    with sqlite3.connect(PROFILE_DB_PATH) as conn:
        conn.execute(
            '''INSERT INTO profiles(email, person, last_calendar, updated_at)
               VALUES(?, '', ?, CURRENT_TIMESTAMP)
               ON CONFLICT(email) DO UPDATE SET last_calendar=excluded.last_calendar, updated_at=CURRENT_TIMESTAMP''',
            (email, calendar),
        )
        conn.commit()


def save_home_layout(email, layout):
    normalized = _normalize_home_layout(layout)
    if set(normalized) != set(HOME_TILES) or len(normalized) != len(HOME_TILES):
        raise ValueError("Neplatné pořadí dlaždic.")
    with sqlite3.connect(PROFILE_DB_PATH) as conn:
        conn.execute(
            '''INSERT INTO profiles(email, person, last_calendar, home_layout, updated_at)
               VALUES(?, '', '', ?, CURRENT_TIMESTAMP)
               ON CONFLICT(email) DO UPDATE SET home_layout=excluded.home_layout, updated_at=CURRENT_TIMESTAMP''',
            (email, json.dumps(normalized, ensure_ascii=False)),
        )
        conn.commit()
    return normalized


def session_payload(email):
    profile = read_profile(email)
    person = profile["person"] if profile["person"] in PERSONS else ""
    last_calendar = profile["last_calendar"] if profile["last_calendar"] in ALLOWED_PROFILE_CALENDARS else ""
    if last_calendar:
        default_calendar = last_calendar
    elif person in PERSONS:
        default_calendar = person
    else:
        default_calendar = "spolecne"
    return {
        "email": email,
        "access_detected": bool(email),
        "person": person,
        "last_calendar": last_calendar,
        "default_calendar": default_calendar,
        "home_layout": profile.get("home_layout") or list(HOME_TILES),
    }


def transform_calendar(text):
    if "<title>Kalendář · Haneva</title>" not in text or "haneva-calendar-profile-v1" in text:
        return text

    # Propadleek: barva a vizuální styly.
    text = text.replace(
        "      --kumi:#6b7280;--kumi-bg:#e5e7eb;\n      --svatky:#ef4444;--svatky-bg:#fee2e2",
        "      --kumi:#6b7280;--kumi-bg:#e5e7eb;\n      --propadleek:#e11d48;--propadleek-bg:#ffe4e6;\n      --svatky:#ef4444;--svatky-bg:#fee2e2",
        1,
    )
    text = text.replace(
        ".filter-dot.hanych{background:var(--hanych)}.filter-dot.eva{background:var(--eva)}.filter-dot.spolecne{background:var(--spolecne)}.filter-dot.narozeniny{background:var(--narozeniny)}.filter-dot.kumi{background:var(--kumi)}",
        ".filter-dot.hanych{background:var(--hanych)}.filter-dot.eva{background:var(--eva)}.filter-dot.spolecne{background:var(--spolecne)}.filter-dot.narozeniny{background:var(--narozeniny)}.filter-dot.kumi{background:var(--kumi)}.filter-dot.propadleek{background:var(--propadleek)}",
        1,
    )
    text = text.replace(
        "    .event.kumi{background:var(--kumi-bg);border-left:3px solid var(--kumi)}\n    .event.svatky",
        "    .event.kumi{background:var(--kumi-bg);border-left:3px solid var(--kumi)}\n    .event.propadleek{background:var(--propadleek-bg);border-left:3px solid var(--propadleek)}\n    .event.svatky",
        1,
    )
    text = text.replace(
        ".agenda-bar.hanych{background:var(--hanych)}.agenda-bar.eva{background:var(--eva)}.agenda-bar.spolecne{background:var(--spolecne)}.agenda-bar.narozeniny{background:var(--narozeniny)}.agenda-bar.kumi{background:var(--kumi)}",
        ".agenda-bar.hanych{background:var(--hanych)}.agenda-bar.eva{background:var(--eva)}.agenda-bar.spolecne{background:var(--spolecne)}.agenda-bar.narozeniny{background:var(--narozeniny)}.agenda-bar.kumi{background:var(--kumi)}.agenda-bar.propadleek{background:var(--propadleek)}",
        1,
    )

    # Propadleek ve filtrech a ve formuláři nové události.
    text = text.replace(
        "        <button class=\"filter active\" data-calendar=\"kumi\"><span class=\"filter-dot kumi\"></span>🐕 Hlídání Kumi<span class=\"filter-check\">✓</span></button>\n      </div>",
        "        <button class=\"filter active\" data-calendar=\"kumi\"><span class=\"filter-dot kumi\"></span>🐕 Hlídání Kumi<span class=\"filter-check\">✓</span></button>\n        <button class=\"filter active\" data-calendar=\"propadleek\"><span class=\"filter-dot propadleek\"></span>Propadleek<span class=\"filter-check\">✓</span></button>\n      </div>",
        1,
    )
    text = text.replace(
        "<option value=\"kumi\">🐕 Hlídání Kumi</option></select>",
        "<option value=\"kumi\">🐕 Hlídání Kumi</option><option value=\"propadleek\">Propadleek</option></select>",
        1,
    )

    # Nastavení identity podle Cloudflare Access účtu.
    settings_intro = "      <p class=\"settings-intro\">Barvy se ukládají společně na Haneva Home, takže budou stejné na telefonu i počítači. Státní svátky mají vlastní barvu, ale nejsou samostatným filtrem.</p>\n"
    text = text.replace(settings_intro, settings_intro + IDENTITY_BLOCK, 1)

    # JavaScriptová konfigurace kalendáře.
    text = text.replace(
        "    const calendarNames={hanych:'Hanych',eva:'Eva',spolecne:'Společné',narozeniny:'Narozeniny',kumi:'🐕 Hlídání Kumi',svatky:'🇨🇿 Státní svátky'};",
        "    const calendarNames={hanych:'Hanych',eva:'Eva',spolecne:'Společné',narozeniny:'Narozeniny',kumi:'🐕 Hlídání Kumi',propadleek:'Propadleek',svatky:'🇨🇿 Státní svátky'};",
        1,
    )
    text = text.replace(
        "    const allCalendars=['hanych','eva','spolecne','narozeniny','kumi'];",
        "    const allCalendars=['hanych','eva','spolecne','narozeniny','kumi','propadleek'];",
        1,
    )
    text = text.replace(
        "    const defaultColors={hanych:'#60a5fa',eva:'#c084fc',spolecne:'#34d399',narozeniny:'#fb923c',kumi:'#6b7280',svatky:'#ef4444'};",
        "    const defaultColors={hanych:'#60a5fa',eva:'#c084fc',spolecne:'#34d399',narozeniny:'#fb923c',kumi:'#6b7280',propadleek:'#e11d48',svatky:'#ef4444'};",
        1,
    )
    text = text.replace("    function tint(hex,amount=.82){", SESSION_JS + "    function tint(hex,amount=.82){", 1)

    # Nová událost: poslední volba má přednost, jinak Hanych/Eva podle relace.
    text = text.replace("      $('calendar').value='spolecne';", "      $('calendar').value=preferredCalendar();", 1)
    text = text.replace(
        "    $('calendar').addEventListener('change',()=>{\n      const cal=$('calendar').value;\n      const isBirthday=cal==='narozeniny';",
        "    $('calendar').addEventListener('change',()=>{\n      const cal=$('calendar').value;\n      rememberCalendar(cal);\n      const isBirthday=cal==='narozeniny';",
        1,
    )
    text = text.replace(
        "    $('settingsForm').addEventListener('submit',saveSettings);\n\n    async function bootstrap(){",
        "    $('settingsForm').addEventListener('submit',saveSettings);\n    $('sessionPerson').addEventListener('change',()=>saveSessionPerson($('sessionPerson').value));\n\n    async function bootstrap(){",
        1,
    )
    text = text.replace(
        "    async function bootstrap(){\n      syncTimeFields();\n      await loadColors();",
        "    async function bootstrap(){\n      syncTimeFields();\n      await loadSession();\n      await loadColors();",
        1,
    )

    # Samostatný styl vložíme až nakonec, ať se nepere s existujícím CSS.
    text = text.replace("</head>", PROFILE_STYLE + "</head>", 1)
    return text


class ProfileGatewayHandler(icon_gateway.IconGatewayHandler):
    server_version = f"HanevaHome/{VERSION}"

    def send_bytes(self, body, status=200, content_type="text/html; charset=utf-8"):
        if content_type.lower().startswith("text/html"):
            try:
                text = body.decode("utf-8")
                text = transform_calendar(text)
                body = text.encode("utf-8")
            except UnicodeDecodeError:
                pass
        return super().send_bytes(body, status, content_type)

    def do_GET(self):
        if urlparse(self.path).path == "/api/session":
            self.send_json(session_payload(access_email(self)))
            return
        return super().do_GET()

    def do_PUT(self):
        path = urlparse(self.path).path
        if path not in {"/api/session/profile", "/api/session/calendar", "/api/session/home-layout"}:
            return super().do_PUT()

        email = access_email(self)
        if not email:
            self.send_json({"error": "Přihlášený e-mail z Cloudflare Access není dostupný."}, 400)
            return

        try:
            payload = self.read_json()
            if path == "/api/session/profile":
                save_person(email, str(payload.get("person", "")).strip().lower())
            elif path == "/api/session/calendar":
                save_last_calendar(email, str(payload.get("calendar", "")).strip().lower())
            else:
                save_home_layout(email, payload.get("layout"))
            self.send_json(session_payload(email))
        except (ValueError, TypeError) as exc:
            self.send_json({"error": str(exc)}, 400)


if __name__ == "__main__":
    init_profile_db()
    app.init_db()
    server = ThreadingHTTPServer((gateway.HOST, gateway.PORT), ProfileGatewayHandler)
    print(f"Haneva Home gateway listening on http://{gateway.HOST}:{gateway.PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
