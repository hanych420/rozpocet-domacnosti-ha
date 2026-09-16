from pathlib import Path

path = Path('/app/shopping_deals.html')
text = path.read_text(encoding='utf-8')

button = '<button class="refresh" id="refreshDeals" type="button">↻ Zkontrolovat</button>'
if button not in text:
    raise SystemExit('refresh button anchor not found')
text = text.replace(button, '', 1)

handler = "    $('refreshDeals').addEventListener('click',async()=>{const b=$('refreshDeals');b.disabled=true;try{await api('/api/shopping/sync-official',{method:'POST'});toast('Kontrola nových letáků spuštěna.');setTimeout(loadDeals,3500);setTimeout(loadDeals,9000)}catch(err){toast(err.message)}finally{setTimeout(()=>b.disabled=false,1800)}});\n"
if handler not in text:
    raise SystemExit('refresh handler anchor not found')
text = text.replace(handler, '', 1)

path.write_text(text, encoding='utf-8')
