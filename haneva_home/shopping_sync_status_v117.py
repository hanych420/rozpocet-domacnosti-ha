from pathlib import Path

path = Path('/app/shopping_deals.html')
text = path.read_text(encoding='utf-8')

old = "else if(sync.last_success_at)text+=' · automatická kontrola každých 6 h';$('dealStatus').textContent=text;"
new = """else if(state.store==='albert'&&sync.albert_last_success_at){const d=new Date(sync.albert_last_success_at);if(!Number.isNaN(d.getTime()))text+=` · Albert naposledy synchronizován ${d.toLocaleDateString('cs-CZ',{day:'numeric',month:'numeric',year:'numeric'})} v ${d.toLocaleTimeString('cs-CZ',{hour:'2-digit',minute:'2-digit'})}`;}else if(sync.last_success_at)text+=' · automatická kontrola zdrojů probíhá na pozadí';$('dealStatus').textContent=text;"""

if old not in text:
    raise SystemExit('shopping sync status anchor not found')
text = text.replace(old, new, 1)
path.write_text(text, encoding='utf-8')
