import os
import re
from http.server import ThreadingHTTPServer

from bs4 import BeautifulSoup

import app
import gateway
import profile_gateway
import agenda_gateway

VERSION = "0.6.3"

# Gateway může při přechodu ještě dočasně používat starý add-on,
# po úspěšné migraci se přepne na embedded server ve stejném kontejneru.
gateway.BUDGET_HOST = os.environ.get("HANEVA_BUDGET_HOST", gateway.BUDGET_HOST)
try:
    gateway.BUDGET_PORT = int(os.environ.get("HANEVA_BUDGET_PORT", str(gateway.BUDGET_PORT)))
except ValueError:
    gateway.BUDGET_PORT = 8099


_CZK_RE = re.compile(r"-?\d[\d\s\u00a0]*(?:[.,]\d+)?\s*Kč")
_CZK_NUMBER_RE = re.compile(r"-?\d[\d\s\u00a0]*(?:[.,]\d+)?(?=\s*Kč)")


def _norm(value):
    return re.sub(r"\s+", " ", value or "").strip()


def _parse_czk(value):
    match = _CZK_NUMBER_RE.search(value or "")
    if not match:
        return None
    try:
        return float(
            match.group(0)
            .replace("\u00a0", "")
            .replace(" ", "")
            .replace(",", ".")
        )
    except ValueError:
        return None


def _format_czk(value):
    formatted = f"{value:,.2f}"
    formatted = formatted.replace(",", "X").replace(".", ",").replace("X", "\u00a0")
    return f"{formatted} Kč"


def _visible_strings(soup, exact_text):
    result = []
    for node in soup.find_all(string=True):
        parent_name = getattr(node.parent, "name", "")
        if parent_name in {"script", "style"}:
            continue
        if _norm(str(node)) == exact_text:
            result.append(node)
    return result


def _nearest_box_with_money(text_node, minimum_money=1, maximum_length=300):
    node = text_node.parent if text_node else None
    for _ in range(8):
        if node is None or getattr(node, "name", None) == "body":
            break
        text = _norm(node.get_text(" ", strip=True))
        if len(_CZK_RE.findall(text)) >= minimum_money and len(text) <= maximum_length:
            return node
        node = node.parent
    return None


def _person_for_fixed_label(text_node):
    node = text_node.parent if text_node else None
    for _ in range(9):
        if node is None or getattr(node, "name", None) == "body":
            break
        text = _norm(node.get_text(" ", strip=True))
        has_hanych = bool(re.search(r"\bHanych\b", text))
        has_eva = bool(re.search(r"\bEva\b", text))
        if has_hanych != has_eva:
            return "Hanych" if has_hanych else "Eva"
        node = node.parent
    return None


def _replace_monthly_fixed_cost_card(html):
    """Upraví pouze serverem vyrenderovanou kartu na dashboardu.

    Žádný JavaScript se do stránky nevkládá. Pokud se struktura dashboardu
    nepodaří bezpečně rozpoznat, vrátí se původní HTML beze změny.
    """
    if "Fixní náklady + nájem celkem" not in html:
        return html
    if html.count("Fixní + nájem") < 2:
        return html

    try:
        soup = BeautifulSoup(html, "html.parser")

        fixed_labels = _visible_strings(soup, "Fixní + nájem")
        if len(fixed_labels) < 2:
            return html

        amounts = {}
        for index, label in enumerate(fixed_labels):
            box = _nearest_box_with_money(label, minimum_money=1, maximum_length=160)
            if box is None:
                continue
            value = _parse_czk(box.get_text(" ", strip=True))
            if value is None:
                continue
            person = _person_for_fixed_label(label)
            if person is None and index < 2:
                person = "Hanych" if index == 0 else "Eva"
            if person in {"Hanych", "Eva"}:
                amounts[person] = value

        if "Hanych" not in amounts or "Eva" not in amounts:
            return html

        target_labels = _visible_strings(soup, "Fixní náklady + nájem celkem")
        if not target_labels:
            return html

        target_label = target_labels[0]
        target_box = _nearest_box_with_money(target_label, minimum_money=1, maximum_length=320)
        if target_box is None:
            return html

        total = amounts["Hanych"] + amounts["Eva"]
        total_text = _format_czk(total)
        breakdown_text = (
            f"Hanych {_format_czk(amounts['Hanych'])} · "
            f"Eva {_format_czk(amounts['Eva'])}"
        )

        # Název karty.
        target_label.replace_with("Měsíční fixní náklady domácnosti")

        # Hlavní částka karty: první samostatný text obsahující pouze částku.
        money_nodes = []
        for node in target_box.find_all(string=True):
            parent_name = getattr(node.parent, "name", "")
            if parent_name in {"script", "style"}:
                continue
            if re.fullmatch(r"\s*-?\d[\d\s\u00a0]*(?:[.,]\d+)?\s*Kč\s*", str(node) or ""):
                money_nodes.append(node)
        if not money_nodes:
            return html
        money_nodes[0].replace_with(total_text)

        # Rozpad Hanych / Eva pod hlavní částkou.
        breakdown_nodes = []
        for node in target_box.find_all(string=True):
            text = str(node)
            if "Hanych" in text and "Eva" in text:
                breakdown_nodes.append(node)
        if breakdown_nodes:
            breakdown_nodes[0].replace_with(breakdown_text)

        return str(soup)
    except Exception as exc:
        # Dashboard má vždy přednost před kosmetickou úpravou karty.
        print(f"Haneva: monthly fixed-cost transform skipped: {exc}")
        return html


_original_rewrite_budget_text = gateway.rewrite_budget_text


def rewrite_budget_text_with_monthly_fixed_costs(text):
    text = _original_rewrite_budget_text(text)
    return _replace_monthly_fixed_cost_card(text)


gateway.rewrite_budget_text = rewrite_budget_text_with_monthly_fixed_costs


class ConsolidatedGatewayHandler(agenda_gateway.AgendaGatewayHandler):
    server_version = f"HanevaHome/{VERSION}"


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
