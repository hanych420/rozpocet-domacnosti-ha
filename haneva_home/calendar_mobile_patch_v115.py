from pathlib import Path

path = Path('/app/calendar.html')
text = path.read_text(encoding='utf-8')

replacements = [
    (
        '.weekdays{display:grid;grid-template-columns:repeat(7,1fr);border-bottom:1px solid var(--line)}',
        '.weekdays{display:grid;grid-template-columns:repeat(7,minmax(0,1fr));border-bottom:1px solid var(--line);width:100%;min-width:0}'
    ),
    (
        '.calendar-view.hidden{display:none}.calendar-grid{display:grid;grid-template-columns:repeat(7,1fr)}',
        '.calendar-view.hidden{display:none}.calendar-view{min-width:0;width:100%}.calendar-grid{display:grid;grid-template-columns:repeat(7,minmax(0,1fr));width:100%;min-width:0}'
    ),
    (
        '.day{min-height:128px;border-right:1px solid var(--line);border-bottom:1px solid var(--line);padding:9px;background:rgba(255,255,255,.55);position:relative;transition:background .12s ease,box-shadow .12s ease;-webkit-touch-callout:none}',
        '.day{min-height:128px;min-width:0;overflow:hidden;border-right:1px solid var(--line);border-bottom:1px solid var(--line);padding:9px;background:rgba(255,255,255,.55);position:relative;transition:background .12s ease,box-shadow .12s ease;-webkit-touch-callout:none}'
    ),
    (
        '.event{width:100%;border:0;text-align:left;border-radius:8px;padding:5px 7px;margin-top:4px;font-size:12px;font-weight:750;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;color:#263044;transition:opacity .12s ease,transform .12s ease;-webkit-touch-callout:none;user-select:none}',
        '.event{width:100%;min-width:0;max-width:100%;border:0;text-align:left;border-radius:8px;padding:5px 7px;margin-top:4px;font-size:12px;font-weight:750;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;color:#263044;transition:opacity .12s ease,transform .12s ease;-webkit-touch-callout:none;user-select:none}'
    ),
]

for old, new in replacements:
    if old not in text:
        raise SystemExit(f'calendar mobile patch anchor not found: {old[:70]}')
    text = text.replace(old, new, 1)

# On phones the seven day columns must always fit the viewport. Event names stay
# on one line and are ellipsized; tapping the event still opens its full detail.
mobile_anchor = '      .gesture-hint{padding:8px 13px;font-size:11px}.weekdays div{padding:8px 2px;text-align:center;font-size:10px}.day{min-height:82px;padding:4px}.day-number{width:24px;height:24px;font-size:11px;margin-bottom:2px}\n'
mobile_replacement = '      .gesture-hint{padding:8px 13px;font-size:11px}.weekdays div{padding:8px 1px;text-align:center;font-size:10px;min-width:0}.day{min-height:82px;min-width:0;padding:4px;overflow:hidden}.day-number{width:24px;height:24px;font-size:11px;margin-bottom:2px}\n'
if mobile_anchor not in text:
    raise SystemExit('calendar mobile media anchor not found')
text = text.replace(mobile_anchor, mobile_replacement, 1)

path.write_text(text, encoding='utf-8')
