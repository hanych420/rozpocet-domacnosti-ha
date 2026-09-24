import os
from datetime import date, timedelta
from http.server import ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse, unquote

import app
import gateway
import profile_gateway
import agenda_gateway
import shopping
import shopping_official
import home_control
import v1_features

VERSION = "0.9.2"
SHOPPING_DEALS_HTML_PATH = "/app/shopping_deals.html"
SMART_HOME_HTML_PATH = "/app/smart_home.html"
FOOD_HTML_PATH = "/app/food.html"
WISHLIST_HTML_PATH = "/app/wishlist.html"
RECEIPTS_HTML_PATH = "/app/receipts.html"
INSIGHTS_HTML_PATH = "/app/insights.html"
WORK_HTML_PATH = "/app/work.html"

# Gateway může při přechodu ještě dočasně používat starý add-on,
# po úspěšné migraci se přepne na embedded server ve stejném kontejneru.
gateway.BUDGET_HOST = os.environ.get("HANEVA_BUDGET_HOST", gateway.BUDGET_HOST)
try:
    gateway.BUDGET_PORT = int(os.environ.get("HANEVA_BUDGET_PORT", str(gateway.BUDGET_PORT)))
except ValueError:
    gateway.BUDGET_PORT = 8099


def _single_validity_date(match):
    if not match:
        return None
    try:
        day = int(match.group("d"))
        month = int(match.group("m"))
        year_text = match.group("y")
        today = date.today()
        year = int(year_text) if year_text else today.year
        value = date(year, month, day)
        if not year_text:
            if value < today - timedelta(days=180):
                value = date(year + 1, month, day)
            elif value > today + timedelta(days=180):
                value = date(year - 1, month, day)
        return value.isoformat()
    except Exception:
        return None


def _parse_kupi_validity(value):
    """Převede textovou platnost z Kupi na data použitelná i v nákupním seznamu."""
    text = str(value or "").strip()
    if not text:
        return None, None

    try:
        valid_from, valid_to = shopping_official._date_range_from_text(text)
    except Exception:
        valid_from, valid_to = None, None
    if valid_from or valid_to:
        return valid_from, valid_to

    try:
        match = shopping_official.DATE_SINGLE_RE.search(text)
    except Exception:
        match = None
    single = _single_validity_date(match)
    if not single:
        return None, None

    normalized = shopping_official._norm(text)
    if "od " in normalized and "do " not in normalized:
        return single, None
    # U Kupi bývá samostatné datum nejčastěji konec platnosti ("do ...").
    return None, single


def _visible_for_filter(valid_from, valid_to, time_filter):
    today = date.today()
    try:
        start = date.fromisoformat(valid_from) if valid_from else None
    except Exception:
        start = None
    try:
        end = date.fromisoformat(valid_to) if valid_to else None
    except Exception:
        end = None

    if time_filter == "next":
        return bool(start and start > today)
    if time_filter == "all":
        return not (end and end < today - timedelta(days=90))
    return not (start and start > today) and not (end and end < today)


def _group_fallback_deals(deals):
    groups = {}
    for deal in deals:
        key = deal.get("group_key") or "ostatni"
        group = groups.setdefault(key, {
            "key": key,
            "label": deal.get("group_label") or "Ostatní",
            "deals": [],
            "stores": set(),
            "prices": [],
        })
        group["deals"].append(deal)
        if deal.get("store") in {"lidl", "albert"}:
            group["stores"].add(deal["store"])
        try:
            price = shopping_official._float_price(deal.get("price"))
        except Exception:
            price = None
        if price is not None:
            group["prices"].append(price)

    output = []
    for group in groups.values():
        group["deals"].sort(key=lambda d: (
            d.get("valid_from") or "",
            shopping_official._float_price(d.get("price")) or 10**9,
            shopping_official._norm(d.get("name")),
        ))
        output.append({
            "key": group["key"],
            "label": group["label"],
            "count": len(group["deals"]),
            "stores": sorted(group["stores"]),
            "from_price": shopping_official._fmt_price(min(group["prices"])) if group["prices"] else "",
            "deals": group["deals"],
        })
    output.sort(key=lambda g: (shopping_official._norm(g["label"]), g["key"]))
    return output


def _dated_kupi_fallback(store, time_filter, query_text):
    """Vrátí Kupi zálohu, ale zachová její textovou platnost a převede ji na data."""
    try:
        raw = shopping.get_deals(force=False, query=query_text or None)
    except Exception:
        return []

    result = []
    seen = set()
    for source in raw.get("deals", []):
        deal_store = source.get("shop")
        if deal_store not in {"lidl", "albert"}:
            continue
        if store in {"lidl", "albert"} and deal_store != store:
            continue

        name = str(source.get("name") or "").strip()
        if not name:
            continue
        validity_text = str(source.get("validity") or "").strip()
        valid_from, valid_to = _parse_kupi_validity(validity_text)
        if not _visible_for_filter(valid_from, valid_to, time_filter):
            continue

        group_key, group_label = shopping_official._group_for(name)
        status, _ = shopping_official._timing(valid_from, valid_to)
        key = (
            shopping_official._norm(name),
            deal_store,
            str(source.get("price") or ""),
            valid_from or "",
            valid_to or "",
        )
        if key in seen:
            continue
        seen.add(key)

        # Na stránce Akce nechceme oranžové/červené urgence. Pokud se platnost
        # podařila převést na data, samotná stránka ji zobrazí jako běžný rozsah.
        # Když Kupi pošle neobvyklý text, zachováme ho alespoň jako šedou informaci.
        status_message = " " if (valid_from or valid_to) else (f"Platnost: {validity_text}" if validity_text else "")
        result.append({
            "id": None,
            "name": name,
            "shop": deal_store,
            "store": deal_store,
            "price": source.get("price") or "",
            "price_text": source.get("price") or "",
            "original_price": source.get("original_price") or "",
            "discount_percent": source.get("discount_percent"),
            "amount": source.get("amount") or "",
            "valid_from": valid_from,
            "valid_to": valid_to,
            "source_url": source.get("kupi_url") or "",
            "source_kind": "kupi-fallback",
            "status": status,
            "status_message": status_message,
            "group_key": group_key,
            "group_label": group_label,
        })
    return result


def _prepare_deals_payload(store, time_filter, query_text):
    data = shopping_official.get_grouped_deals(
        store=store,
        time_filter=time_filter,
        query=query_text,
    )

    groups = data.get("groups") or []
    returned_deals = [deal for group in groups for deal in (group.get("deals") or [])]
    has_official = any(deal.get("source_kind") != "kupi-fallback" for deal in returned_deals)

    # Pokud oficiální parser pro daný pohled nic nevydal, použijeme stejnou
    # Kupi zálohu jako dřív, ale tentokrát nezahodíme údaj o platnosti.
    if not has_official:
        fallback = _dated_kupi_fallback(store, time_filter, query_text)
        if fallback:
            data["groups"] = _group_fallback_deals(fallback)
            data["count"] = len(fallback)
            data["source"] = "kupi-fallback"
            returned_deals = fallback

    # Urgentní texty typu "končí zítra" patří jen do nákupního seznamu.
    # Na stránce Akce necháváme pouze normální datum/rozsah platnosti.
    for group in data.get("groups") or []:
        for deal in group.get("deals") or []:
            if deal.get("valid_from") or deal.get("valid_to"):
                deal["status_message"] = " "

    # HTML 0.9.x očekává jednodušší názvy těchto stavů; doplníme aliasy,
    # aby bylo vidět, zda kontrola právě běží a zda některý zdroj selhal.
    sync = dict(data.get("sync") or {})
    sync["syncing"] = bool(sync.get("running"))
    sync["last_success_at"] = sync.get("last_finished_at")
    sync["errors"] = [sync["last_error"]] if sync.get("last_error") else []
    sync["official_counts"] = dict(sync.get("last_counts") or {})
    data["sync"] = sync
    return data


class ConsolidatedGatewayHandler(agenda_gateway.AgendaGatewayHandler):
    server_version = f"HanevaHome/{VERSION}"

    def send_download(self, body, filename, content_type="application/octet-stream"):
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        v1_pages = {
            "/jidlo": FOOD_HTML_PATH, "/jidlo/": FOOD_HTML_PATH,
            "/wishlist": WISHLIST_HTML_PATH, "/wishlist/": WISHLIST_HTML_PATH,
            "/uctenky": RECEIPTS_HTML_PATH, "/uctenky/": RECEIPTS_HTML_PATH,
            "/prehledy": INSIGHTS_HTML_PATH, "/prehledy/": INSIGHTS_HTML_PATH,
        }
        if path in v1_pages:
            try:
                self.send_bytes(app.read_page(v1_pages[path]))
            except OSError:
                self.send_bytes(b"Page not found\n", 500, "text/plain; charset=utf-8")
            return

        if path in ("/kalendar/prace", "/kalendar/prace/"):
            self.send_response(302)
            self.send_header("Location", "/kalendar")
            self.end_headers()
            return

        if path == "/api/v1/recipes":
            self.send_json({"recipes": v1_features.list_recipes()})
            return
        if path == "/api/v1/recipes/template.xlsx":
            self.send_download(
                v1_features.recipe_template_xlsx(),
                "haneva-recepty-sablona.xlsx",
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
            return
        if path == "/api/v1/recipes/image-export.xlsx":
            self.send_download(
                v1_features.recipe_image_export_xlsx(),
                "haneva-recepty-pro-obrazky.xlsx",
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
            return
        if path.startswith("/api/v1/recipe-images/"):
            filename = unquote(path.rsplit("/", 1)[-1])
            try:
                body, mime = v1_features.read_recipe_image(filename)
                self.send_bytes(body, 200, mime)
            except FileNotFoundError:
                self.send_json({"error": "Obrázek nebyl nalezen."}, 404)
            return
        if path == "/api/v1/planned":
            self.send_json(v1_features.planned_recipes())
            return
        if path == "/api/v1/tinder":
            person = (parse_qs(parsed.query).get("person", [""])[0] or "").strip().lower()
            self.send_json(v1_features.tinder_state(person))
            return
        if path == "/api/v1/shopping-preview":
            self.send_json(v1_features.shopping_preview())
            return
        if path == "/api/v1/wishlist":
            self.send_json({"items": v1_features.wishlist_list()})
            return
        if path == "/api/v1/prices":
            self.send_json({"items": v1_features.price_history()})
            return
        if path == "/api/v1/receipts":
            self.send_json({"receipts": v1_features.receipt_list()})
            return
        if path == "/api/v1/work":
            query = parse_qs(parsed.query)
            self.send_json({"shifts": v1_features.work_shifts(query.get("start", [None])[0], query.get("end", [None])[0])})
            return
        if path == "/api/v1/work/scans":
            query = parse_qs(parsed.query)
            try:
                limit = int(query.get("limit", ["10"])[0])
            except ValueError:
                limit = 10
            self.send_json({"scans": v1_features.work_scan_logs(limit)})
            return
        if path == "/api/v1/work/health":
            self.send_json(v1_features.work_ocr_health())
            return
        if path == "/api/v1/insights":
            self.send_json(v1_features.finance_insights())
            return

        if path in ("/domov", "/domov/", "/jidlo", "/jidlo/", "/wishlist", "/wishlist/", "/uctenky", "/uctenky/", "/prehledy", "/prehledy/"):
            try:
                self.send_bytes(app.read_page(SMART_HOME_HTML_PATH))
            except OSError:
                self.send_bytes(b"Smart home page not found\n", 500, "text/plain; charset=utf-8")
            return

        if path == "/api/domov":
            try:
                self.send_json(home_control.get_overview())
            except Exception as exc:
                self.send_json({"error": str(exc)}, 502)
            return

        if path == "/api/domov/config":
            try:
                self.send_json(home_control.get_config())
            except Exception as exc:
                self.send_json({"error": str(exc)}, 500)
            return

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
            self.send_json(_prepare_deals_payload(store, time_filter, text))
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

        try:
            if path == "/api/v1/recipes":
                self.send_json({"recipe": v1_features.save_recipe(self.read_json())}, 201)
                return
            if path == "/api/v1/recipes/import-xlsx/preview":
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    length = 0
                if length <= 0 or length > v1_features.MAX_UPLOAD:
                    raise ValueError("XLSX chybí nebo je příliš velké.")
                filename = unquote(self.headers.get("X-Filename", "recepty.xlsx"))
                body = self.rfile.read(length)
                if len(body) != length:
                    raise ValueError("XLSX nebylo nahráno celé.")
                self.send_json(v1_features.preview_recipe_xlsx(filename, body))
                return
            if path == "/api/v1/recipes/import-xlsx/commit":
                self.send_json(v1_features.commit_recipe_xlsx_import(self.read_json()), 201)
                return
            if path == "/api/v1/recipe-images/import-zip":
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    length = 0
                if length <= 0 or length > v1_features.RECIPE_IMAGE_ZIP_MAX:
                    raise ValueError("ZIP chybí nebo je příliš velký.")
                filename = unquote(self.headers.get("X-Filename", "recepty-obrazky.zip"))
                body = self.rfile.read(length)
                if len(body) != length:
                    raise ValueError("ZIP nebyl nahrán celý.")
                self.send_json(v1_features.import_recipe_images_zip(filename, body), 201)
                return
            if path == "/api/v1/tinder/vote":
                payload = self.read_json()
                self.send_json(v1_features.vote_recipe(payload.get("recipe_id"), payload.get("person"), payload.get("vote")))
                return
            if path == "/api/v1/planned/add":
                payload = self.read_json()
                self.send_json(v1_features.add_plan(payload.get("recipe_id")))
                return
            if path == "/api/v1/shopping-preview/add":
                self.send_json(v1_features.add_preview_to_shopping(self.read_json()))
                return
            if path == "/api/v1/wishlist":
                payload = self.read_json()
                payload["created_by"] = profile_gateway.session_payload(profile_gateway.access_email(self)).get("person", "")
                self.send_json({"item": v1_features.save_wishlist(payload)}, 201)
                return
            if path == "/api/v1/work-xlsx/preview":
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    length = 0
                if length <= 0 or length > v1_features.MAX_UPLOAD:
                    raise ValueError("Soubor chybí nebo je příliš velký.")
                filename = unquote(self.headers.get("X-Filename", "smeny.xlsx"))
                body = self.rfile.read(length)
                if len(body) != length:
                    raise ValueError("Soubor nebyl nahrán celý.")
                self.send_json(v1_features.parse_work_xlsx(filename, body))
                return
            if path == "/api/v1/work-xlsx/commit":
                self.send_json(v1_features.sync_work_xlsx(self.read_json()), 201)
                return
            if path in {"/api/v1/receipts/scan", "/api/v1/work/scan"}:
                try:
                    try:
                        length = int(self.headers.get("Content-Length", "0"))
                    except ValueError:
                        length = 0
                    if length <= 0 or length > v1_features.MAX_UPLOAD:
                        raise ValueError("Soubor chybí nebo je příliš velký.")
                    filename = unquote(self.headers.get("X-Filename", "upload"))
                    body = self.rfile.read(length)
                    if len(body) != length:
                        raise ValueError("Soubor nebyl nahrán celý.")
                    if path.endswith("/receipts/scan"):
                        self.send_json({"receipt": v1_features.scan_receipt(filename, body)})
                    else:
                        self.send_json(v1_features.scan_work(filename, body))
                except (ValueError, TypeError, KeyError):
                    raise
                except Exception as exc:
                    print(f"[haneva-v1] scan failed path={path}: {type(exc).__name__}: {exc}", flush=True)
                    self.send_json({
                        "error": "OCR zpracování selhalo na serveru.",
                        "detail": f"{type(exc).__name__}: {exc}",
                    }, 500)
                return
            if path == "/api/v1/receipts/commit":
                self.send_json(v1_features.commit_receipt(self.read_json()), 201)
                return
            if path == "/api/v1/work/commit":
                self.send_json(v1_features.commit_work(self.read_json()), 201)
                return
        except (ValueError, TypeError, KeyError) as exc:
            message = exc.args[0] if isinstance(exc, KeyError) and exc.args else str(exc)
            self.send_json({"error": str(message)}, 400)
            return

        if path == "/api/domov/entity":
            try:
                payload = self.read_json()
                entity_id = payload.get("entity_id")
                turn_on = payload.get("turn_on") if "turn_on" in payload else None
                brightness_pct = payload.get("brightness_pct")
                rgb_color = payload.get("rgb_color")
                hvac_mode = payload.get("hvac_mode")
                temperature = payload.get("temperature")
                entity = home_control.set_entity(
                    entity_id,
                    turn_on=turn_on,
                    brightness_pct=brightness_pct,
                    rgb_color=rgb_color,
                    hvac_mode=hvac_mode,
                    temperature=temperature,
                )
                if turn_on is not None:
                    name = str(entity.get("name") or "").lower()
                    icon = str(entity.get("icon") or "")
                    is_light_like = (
                        entity.get("domain") == "light"
                        or (
                            entity.get("domain") == "switch"
                            and (icon == "💡" or "světlo" in name or "svetlo" in name or "light" in name)
                        )
                    )
                    if is_light_like:
                        profile_gateway.record_light_usage(profile_gateway.access_email(self), entity_id)
                self.send_json({"entity": entity})
            except ValueError as exc:
                self.send_json({"error": str(exc)}, 400)
            except KeyError as exc:
                self.send_json({"error": str(exc.args[0])}, 404)
            except Exception as exc:
                self.send_json({"error": f"Home Assistant nereaguje: {exc}"}, 502)
            return

        if path == "/api/domov/config/entity":
            try:
                self.send_json({"entity": home_control.add_entity(self.read_json())}, 201)
            except ValueError as exc:
                self.send_json({"error": str(exc)}, 400)
            except KeyError as exc:
                self.send_json({"error": str(exc.args[0])}, 404)
            except Exception as exc:
                self.send_json({"error": f"Zařízení se nepodařilo přidat: {exc}"}, 502)
            return

        if path == "/api/domov/config/entity/remove":
            try:
                payload = self.read_json()
                home_control.remove_entity(payload.get("entity_id"))
                self.send_json({"ok": True})
            except KeyError as exc:
                self.send_json({"error": str(exc.args[0])}, 404)
            except Exception as exc:
                self.send_json({"error": str(exc)}, 400)
            return

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

    def do_PUT(self):
        path = urlparse(self.path).path
        try:
            match = __import__("re").fullmatch(r"/api/v1/recipes/(\d+)", path)
            if match:
                self.send_json({"recipe": v1_features.save_recipe(self.read_json(), int(match.group(1)))})
                return
            match = __import__("re").fullmatch(r"/api/v1/planned/(\d+)", path)
            if match:
                payload = self.read_json()
                v1_features.set_plan_cooked(int(match.group(1)), bool(payload.get("cooked", True)))
                self.send_json(v1_features.planned_recipes())
                return
            match = __import__("re").fullmatch(r"/api/v1/wishlist/(\d+)", path)
            if match:
                self.send_json({"item": v1_features.save_wishlist(self.read_json(), int(match.group(1)))})
                return
            if path == "/api/v1/settings/reset-day":
                payload = self.read_json()
                day = int(payload.get("day"))
                if not 0 <= day <= 6:
                    raise ValueError("Den musí být 0 až 6.")
                v1_features.set_setting("recipe_reset_day", day)
                self.send_json(v1_features.planned_recipes())
                return
        except (ValueError, TypeError, KeyError) as exc:
            message = exc.args[0] if isinstance(exc, KeyError) and exc.args else str(exc)
            self.send_json({"error": str(message)}, 400)
            return
        return super().do_PUT()

    def do_DELETE(self):
        path = urlparse(self.path).path
        try:
            match = __import__("re").fullmatch(r"/api/v1/recipes/(\d+)", path)
            if match:
                v1_features.delete_recipe(int(match.group(1)))
                self.send_json({"ok": True})
                return
            match = __import__("re").fullmatch(r"/api/v1/wishlist/(\d+)", path)
            if match:
                v1_features.delete_wishlist(int(match.group(1)))
                self.send_json({"ok": True})
                return
        except KeyError as exc:
            self.send_json({"error": str(exc.args[0])}, 404)
            return
        return super().do_DELETE()

    def do_HEAD(self):
        path = urlparse(self.path).path
        if path in ("/domov", "/domov/"):
            self.send_common_headers(200, "text/html; charset=utf-8", 0)
            return
        super().do_HEAD()


if __name__ == "__main__":
    profile_gateway.init_profile_db()
    app.init_db()
    home_control.init_db()
    v1_features.init_db()
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
