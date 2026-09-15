import os
import re
from http.server import ThreadingHTTPServer

import app
import gateway
import profile_gateway
import agenda_gateway

VERSION = "0.6.1"

# Gateway může při přechodu ještě dočasně používat starý add-on,
# po úspěšné migraci se přepne na embedded server ve stejném kontejneru.
gateway.BUDGET_HOST = os.environ.get("HANEVA_BUDGET_HOST", gateway.BUDGET_HOST)
try:
    gateway.BUDGET_PORT = int(os.environ.get("HANEVA_BUDGET_PORT", str(gateway.BUDGET_PORT)))
except ValueError:
    gateway.BUDGET_PORT = 8099


MONTHLY_FIXED_COSTS_SCRIPT = r"""
<script id="haneva-monthly-household-fixed-costs-v1">
(() => {
  const norm = value => (value || '').replace(/\s+/g, ' ').trim();

  function parseCzk(value) {
    const match = (value || '').match(/-?\d[\d\s\u00a0]*(?:[.,]\d+)?(?=\s*Kč)/);
    if (!match) return null;
    const number = Number(match[0].replace(/[\s\u00a0]/g, '').replace(',', '.'));
    return Number.isFinite(number) ? number : null;
  }

  function formatCzk(value) {
    return new Intl.NumberFormat('cs-CZ', {
      minimumFractionDigits: 2,
      maximumFractionDigits: 2,
    }).format(value) + ' Kč';
  }

  function exactElements(text) {
    return [...document.querySelectorAll('body *')]
      .filter(el => norm(el.textContent) === text)
      .sort((a, b) => a.querySelectorAll('*').length - b.querySelectorAll('*').length);
  }

  function nearestSingleMoneyBox(labelEl) {
    let node = labelEl && labelEl.parentElement;
    for (let i = 0; node && node !== document.body && i < 7; i += 1, node = node.parentElement) {
      const text = norm(node.innerText);
      const monies = text.match(/-?\d[\d\s\u00a0]*(?:[.,]\d+)?\s*Kč/g) || [];
      if (monies.length === 1 && text.length < 140) return node;
    }
    return labelEl && labelEl.parentElement;
  }

  function personFor(labelEl) {
    let node = labelEl;
    for (let i = 0; node && node !== document.body && i < 8; i += 1, node = node.parentElement) {
      const text = norm(node.innerText);
      const hasHanych = /\bHanych\b/.test(text);
      const hasEva = /\bEva\b/.test(text);
      if (hasHanych !== hasEva) return hasHanych ? 'Hanych' : 'Eva';
    }
    return '';
  }

  function targetSummaryBox(labelEl) {
    let node = labelEl && labelEl.parentElement;
    for (let i = 0; node && node !== document.body && i < 7; i += 1, node = node.parentElement) {
      const text = norm(node.innerText);
      const monies = text.match(/-?\d[\d\s\u00a0]*(?:[.,]\d+)?\s*Kč/g) || [];
      if (monies.length >= 1 && text.length < 240) return node;
    }
    return labelEl && labelEl.parentElement;
  }

  function updateMonthlyFixedCosts() {
    const fixedLabels = exactElements('Fixní + nájem');
    if (fixedLabels.length < 2) return;

    const amounts = {};
    fixedLabels.forEach((label, index) => {
      const box = nearestSingleMoneyBox(label);
      const value = parseCzk(box ? box.innerText : '');
      if (value === null) return;
      const person = personFor(label) || (index === 0 ? 'Hanych' : 'Eva');
      amounts[person] = value;
    });

    if (amounts.Hanych === undefined || amounts.Eva === undefined) return;

    const targetLabel = exactElements('Fixní náklady + nájem celkem')[0]
      || exactElements('Měsíční fixní náklady domácnosti')[0];
    if (!targetLabel) return;

    const box = targetSummaryBox(targetLabel);
    if (!box) return;

    targetLabel.textContent = 'Měsíční fixní náklady domácnosti';

    const total = amounts.Hanych + amounts.Eva;
    const leaves = [...box.querySelectorAll('*')].filter(el => el.children.length === 0);

    const totalEl = leaves.find(el => /^\s*-?\d[\d\s\u00a0]*(?:[.,]\d+)?\s*Kč\s*$/.test(el.textContent || ''));
    if (totalEl) totalEl.textContent = formatCzk(total);

    const breakdownEl = leaves.find(el => /Hanych/.test(el.textContent || '') && /Eva/.test(el.textContent || ''));
    if (breakdownEl) {
      breakdownEl.textContent = `Hanych ${formatCzk(amounts.Hanych)} · Eva ${formatCzk(amounts.Eva)}`;
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', updateMonthlyFixedCosts, {once: true});
  } else {
    updateMonthlyFixedCosts();
  }
  setTimeout(updateMonthlyFixedCosts, 250);
  setTimeout(updateMonthlyFixedCosts, 1000);
})();
</script>
"""

_original_rewrite_budget_text = gateway.rewrite_budget_text


def rewrite_budget_text_with_monthly_costs(text):
    text = _original_rewrite_budget_text(text)
    if (
        "haneva-monthly-household-fixed-costs-v1" not in text
        and "</body" in text.lower()
        and (
            "Fixní náklady + nájem celkem" in text
            or "Měsíční fixní náklady domácnosti" in text
        )
    ):
        text = re.sub(
            r"</body\s*>",
            MONTHLY_FIXED_COSTS_SCRIPT + "</body>",
            text,
            count=1,
            flags=re.IGNORECASE,
        )
    return text


gateway.rewrite_budget_text = rewrite_budget_text_with_monthly_costs


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
