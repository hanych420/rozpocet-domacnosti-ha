from datetime import date
from http.server import ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import app
import gateway
import profile_gateway

VERSION = "0.5.5"

AGENDA_STYLE = """
<style id="haneva-agenda-range-v1">
  .agenda-range{display:flex;gap:6px;align-items:center;justify-content:flex-end;margin:0 0 12px}
  .agenda-range button{border:0;background:#f3f4f6;color:#667085;border-radius:10px;padding:7px 10px;font-size:12px;font-weight:800}
  .agenda-range button.active{background:#111827;color:#fff}
  .agenda-range-note{margin-right:auto;color:#8a92a0;font-size:11px;font-weight:700}
  @media(max-width:560px){
    .agenda-range{align-items:stretch;flex-wrap:wrap}
    .agenda-range-note{width:100%;margin:0 0 2px}
    .agenda-range button{flex:1}
  }
</style>
"""

AGENDA_JS = r"""
<script id="haneva-agenda-range-script-v1">
(() => {
  let agendaRange='2m';
  let agendaEvents=[];
  let agendaLoaded=false;
  let agendaLoading=false;

  function agendaRangeLabel(){
    return agendaRange==='all'?'Všechny budoucí události':'Od dneška na 2 měsíce';
  }

  async function loadAgendaEvents(){
    if(agendaLoading)return;
    agendaLoading=true;
    try{
      const res=await fetch(`/api/agenda?range=${agendaRange}`,{cache:'no-store'});
      const data=await res.json();
      if(!res.ok)throw new Error(data.error||'Agendu se nepodařilo načíst.');
      agendaEvents=data.events||[];
      agendaLoaded=true;
      renderAgenda();
    }catch(err){
      const box=$('agenda');
      if(box)box.innerHTML=`<div class="empty">${escapeHtml(err.message||'Agendu se nepodařilo načíst.')}</div>`;
    }finally{
      agendaLoading=false;
    }
  }

  renderAgenda = function(){
    const box=$('agenda');
    if(!box)return;
    box.innerHTML='';

    const controls=document.createElement('div');
    controls.className='agenda-range';
    controls.innerHTML=`<span class="agenda-range-note">${escapeHtml(agendaRangeLabel())}</span>
      <button type="button" data-agenda-range="2m" class="${agendaRange==='2m'?'active':''}">Následující 2 měsíce</button>
      <button type="button" data-agenda-range="all" class="${agendaRange==='all'?'active':''}">Všechny budoucí</button>`;
    controls.addEventListener('click',e=>{
      const btn=e.target.closest('button[data-agenda-range]');
      if(!btn||btn.dataset.agendaRange===agendaRange)return;
      agendaRange=btn.dataset.agendaRange;
      agendaLoaded=false;
      renderAgenda();
      loadAgendaEvents();
    });
    box.appendChild(controls);

    if(!agendaLoaded){
      const loading=document.createElement('div');
      loading.className='empty';
      loading.textContent='Načítám agendu…';
      box.appendChild(loading);
      return;
    }

    const events=agendaEvents
      .filter(e=>state.filterMode==='all'||state.filters.has(e.calendar))
      .sort((a,b)=>`${a.start_date} ${a.start_time||''} ${a.title}`.localeCompare(`${b.start_date} ${b.start_time||''} ${b.title}`));

    if(!events.length){
      const empty=document.createElement('div');
      empty.className='empty';
      empty.textContent=agendaRange==='all'
        ?'Žádné budoucí události.'
        :'V následujících dvou měsících zatím nic není.';
      box.appendChild(empty);
      return;
    }

    const list=document.createElement('div');
    list.className='agenda-list';
    events.forEach(e=>{
      const btn=document.createElement('button');
      btn.type='button';
      btn.className='agenda-event';
      const time=e.all_day?'Celý den':[e.start_time,e.end_time].filter(Boolean).join('–');
      const loc=e.location?` · ${e.location}`:'';
      const repeat=e.recurrence==='yearly'?' · každý rok':'';
      btn.innerHTML=`<span class="agenda-date-range">${escapeHtml(agendaDateLabel(e))}</span><span class="agenda-bar ${e.calendar}"></span><span class="agenda-main"><span class="agenda-title">${escapeHtml(e.title)}</span><span class="agenda-meta">${escapeHtml(`${time}${loc}`)}${repeat}</span></span>`;
      btn.addEventListener('click',()=>openEdit(e));
      list.appendChild(btn);
    });
    box.appendChild(list);
  };

  document.querySelector('.view-toggle')?.addEventListener('click',e=>{
    const btn=e.target.closest('button[data-view]');
    if(!btn||btn.dataset.view!=='agenda')return;
    if(!agendaLoaded)loadAgendaEvents();
  });

  document.getElementById('filters')?.addEventListener('click',()=>{
    if(state.view==='agenda'&&agendaLoaded)setTimeout(renderAgenda,0);
  });

  const originalLoadEvents=loadEvents;
  loadEvents=async function(){
    await originalLoadEvents();
    if(state.view==='agenda'){
      agendaLoaded=false;
      await loadAgendaEvents();
    }
  };
})();
</script>
"""


def add_months(value, months):
    month_index = value.month - 1 + months
    year = value.year + month_index // 12
    month = month_index % 12 + 1
    leap = year % 400 == 0 or (year % 4 == 0 and year % 100 != 0)
    month_lengths = (31, 29 if leap else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)
    day = min(value.day, month_lengths[month - 1])
    return date(year, month, day)


def next_yearly_occurrence(row, start, end=None):
    base_start = date.fromisoformat(row["start_date"])
    base_end = date.fromisoformat(row["end_date"])
    duration = base_end - base_start
    for year in range(start.year - 1, start.year + 3):
        try:
            occ_start = date(year, base_start.month, base_start.day)
        except ValueError:
            continue
        occ_end = occ_start + duration
        if occ_end < start:
            continue
        if end is not None and occ_start > end:
            return None
        item = app.row_to_event(row)
        item["start_date"] = occ_start.isoformat()
        item["end_date"] = occ_end.isoformat()
        item["occurrence_key"] = f'{item["id"]}-{year}'
        return item
    return None


def agenda_events(range_mode):
    start = date.today()
    end = add_months(start, 2) if range_mode == "2m" else None

    result = []
    with app.db() as conn:
        if end is None:
            normal = conn.execute(
                "SELECT * FROM events WHERE recurrence='none' AND end_date >= ? ORDER BY start_date, start_time",
                (start.isoformat(),),
            ).fetchall()
        else:
            normal = conn.execute(
                "SELECT * FROM events WHERE recurrence='none' AND end_date >= ? AND start_date <= ? ORDER BY start_date, start_time",
                (start.isoformat(), end.isoformat()),
            ).fetchall()
        recurring = conn.execute(
            "SELECT * FROM events WHERE recurrence='yearly' ORDER BY start_date, start_time"
        ).fetchall()

    for row in normal:
        event = app.row_to_event(row)
        if event.get("system_kind") == "holiday":
            continue
        result.append(event)

    for row in recurring:
        event = app.row_to_event(row)
        if event.get("system_kind") == "holiday":
            continue
        occurrence = next_yearly_occurrence(row, start, end)
        if occurrence:
            # V Agendě ukazujeme jen nejbližší výskyt roční události.
            # Tím se narozeniny v režimu "Všechny budoucí" neopakují každý další rok.
            result.append(occurrence)

    result.sort(key=lambda e: (e["start_date"], e.get("start_time") or "", e["title"].lower()))
    return result


_original_transform_calendar = profile_gateway.transform_calendar


def transform_calendar_with_agenda(text):
    text = _original_transform_calendar(text)
    if "haneva-agenda-range-v1" in text or "<title>Kalendář · Haneva</title>" not in text:
        return text
    text = text.replace("</head>", AGENDA_STYLE + "</head>", 1)
    text = text.replace("</body>", AGENDA_JS + "</body>", 1)
    return text


profile_gateway.transform_calendar = transform_calendar_with_agenda


class AgendaGatewayHandler(profile_gateway.ProfileGatewayHandler):
    server_version = f"HanevaHome/{VERSION}"

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/agenda":
            query = parse_qs(parsed.query)
            range_mode = query.get("range", ["2m"])[0]
            if range_mode not in {"2m", "all"}:
                self.send_json({"error": "Neplatný rozsah agendy."}, 400)
                return
            self.send_json({"events": agenda_events(range_mode), "range": range_mode})
            return
        return super().do_GET()


if __name__ == "__main__":
    profile_gateway.init_profile_db()
    app.init_db()
    server = ThreadingHTTPServer((gateway.HOST, gateway.PORT), AgendaGatewayHandler)
    print(f"Haneva Home gateway listening on http://{gateway.HOST}:{gateway.PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
