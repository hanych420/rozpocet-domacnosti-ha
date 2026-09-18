from pathlib import Path

path = Path('/app/calendar.html')
text = path.read_text(encoding='utf-8')

# Replace the browser/iOS native date controls with Haneva's own compact picker.
old_dates = '''      <div class="field"><label for="startDate">Od</label><input id="startDate" type="date" required></div>
      <div class="field"><label for="endDate">Do</label><input id="endDate" type="date" required></div>'''
new_dates = '''      <div class="field"><label>Od</label><button class="date-trigger" type="button" data-date-target="startDate"><span id="startDateDisplay">Vyber datum</span><span class="date-trigger-icon">▾</span></button><input id="startDate" type="hidden"></div>
      <div class="field"><label>Do</label><button class="date-trigger" type="button" data-date-target="endDate"><span id="endDateDisplay">Vyber datum</span><span class="date-trigger-icon">▾</span></button><input id="endDate" type="hidden"></div>'''
if old_dates not in text:
    raise SystemExit('calendar v118 date field anchor not found')
text = text.replace(old_dates, new_dates, 1)

actions_anchor = '    <div class="actions"><button class="danger" type="button" id="deleteEvent">Smazat</button>'
picker_markup = '''    <div class="date-picker-overlay" id="datePickerOverlay" hidden>
      <div class="date-picker-card" role="dialog" aria-modal="true" aria-label="Výběr data">
        <div class="date-picker-head">
          <button class="date-picker-nav" type="button" id="datePickerPrev" aria-label="Předchozí měsíc">←</button>
          <div class="date-picker-title" id="datePickerTitle"></div>
          <button class="date-picker-nav" type="button" id="datePickerNext" aria-label="Další měsíc">→</button>
        </div>
        <div class="date-picker-weekdays"><span>Po</span><span>Út</span><span>St</span><span>Čt</span><span>Pá</span><span>So</span><span>Ne</span></div>
        <div class="date-picker-grid" id="datePickerGrid"></div>
        <div class="date-picker-foot"><button class="secondary" type="button" id="datePickerCancel">Zrušit</button><button class="date-picker-today" type="button" id="datePickerToday">Dnes</button></div>
      </div>
    </div>
'''
if actions_anchor not in text:
    raise SystemExit('calendar v118 actions anchor not found')
text = text.replace(actions_anchor, picker_markup + actions_anchor, 1)

css_anchor = '    .field textarea{min-height:86px;resize:vertical}\n'
picker_css = '''    .date-trigger{width:100%;min-height:44px;border:1px solid #dfe3ea;border-radius:12px;padding:11px 12px;background:#fff;color:#172033;display:flex;align-items:center;justify-content:space-between;gap:10px;text-align:left;font-weight:700}
    .date-trigger:focus{border-color:#94a3b8;box-shadow:0 0 0 3px rgba(148,163,184,.15);outline:none}
    .date-trigger-icon{color:#8b93a0;font-size:12px}
    .date-picker-overlay[hidden]{display:none}
    .date-picker-overlay{position:fixed;inset:0;z-index:90;display:grid;place-items:center;padding:16px;background:rgba(15,23,42,.38);backdrop-filter:blur(4px)}
    .date-picker-card{width:min(390px,calc(100vw - 28px));background:#fff;border-radius:22px;padding:16px;box-shadow:0 28px 80px rgba(15,23,42,.28)}
    .date-picker-head{display:grid;grid-template-columns:42px 1fr 42px;align-items:center;gap:8px;margin-bottom:12px}
    .date-picker-title{text-align:center;font-size:17px;font-weight:850;letter-spacing:-.02em}
    .date-picker-nav{width:42px;height:42px;border:0;border-radius:12px;background:#f3f4f6;color:#374151;font-weight:900}
    .date-picker-weekdays,.date-picker-grid{display:grid;grid-template-columns:repeat(7,minmax(0,1fr));gap:4px}
    .date-picker-weekdays{margin-bottom:5px}
    .date-picker-weekdays span{text-align:center;color:#9aa1ad;font-size:10px;font-weight:800;text-transform:uppercase}
    .date-picker-day{aspect-ratio:1;border:0;border-radius:11px;background:transparent;color:#374151;font-size:13px;font-weight:750;display:grid;place-items:center;min-width:0}
    .date-picker-day:hover{background:#f3f4f6}
    .date-picker-day.other{color:#c4c8cf}
    .date-picker-day.today{box-shadow:inset 0 0 0 1.5px #94a3b8}
    .date-picker-day.selected{background:#111827;color:#fff;box-shadow:none}
    .date-picker-foot{display:flex;align-items:center;justify-content:space-between;gap:10px;margin-top:14px;padding-top:12px;border-top:1px solid var(--line)}
    .date-picker-today{border:0;background:#111827;color:#fff;border-radius:11px;padding:9px 14px;font-weight:800}
'''
if css_anchor not in text:
    raise SystemExit('calendar v118 CSS anchor not found')
text = text.replace(css_anchor, css_anchor + picker_css, 1)

mobile_css_anchor = '      #eventDialog .field input,#eventDialog .field select,#eventDialog .field textarea{background:#fff;font-size:16px;min-height:46px}\n'
mobile_css_new = mobile_css_anchor + '      #eventDialog .date-trigger{font-size:16px;min-height:46px}\n      #eventDialog .date-picker-card{width:min(390px,calc(100vw - 24px));padding:14px;border-radius:20px}\n      #eventDialog .date-picker-day{font-size:14px;border-radius:10px}\n'
if mobile_css_anchor not in text:
    raise SystemExit('calendar v118 mobile CSS anchor not found')
text = text.replace(mobile_css_anchor, mobile_css_new, 1)

helpers_anchor = '''    function syncTimeFields(){
      const disabled=$('allDay').checked;
      document.querySelectorAll('.time-field input').forEach(i=>i.disabled=disabled);
      document.querySelectorAll('.time-field').forEach(el=>el.style.opacity=disabled?'.45':'1');
    }
'''
date_js = '''
    let datePickerTarget=null;
    let datePickerCursor=new Date();

    function formatDateLabel(value){
      if(!value)return'Vyber datum';
      try{return new Intl.DateTimeFormat('cs-CZ',{day:'numeric',month:'long',year:'numeric'}).format(parseIso(value))}
      catch(_){return value}
    }

    function setDateValue(id,value){
      $(id).value=value||'';
      const display=$(id+'Display');
      if(display)display.textContent=formatDateLabel(value);
    }

    function closeDatePicker(){
      $('datePickerOverlay').hidden=true;
      datePickerTarget=null;
    }

    function renderDatePicker(){
      const selected=datePickerTarget?$(datePickerTarget).value:'';
      const y=datePickerCursor.getFullYear(),m=datePickerCursor.getMonth();
      $('datePickerTitle').textContent=`${months[m]} ${y}`;
      const grid=$('datePickerGrid');
      grid.innerHTML='';
      const first=new Date(y,m,1);
      const offset=(first.getDay()+6)%7;
      const start=new Date(y,m,1-offset);
      const today=isoLocal(new Date());
      for(let i=0;i<42;i++){
        const d=new Date(start);d.setDate(start.getDate()+i);
        const key=isoLocal(d);
        const btn=document.createElement('button');
        btn.type='button';
        btn.className='date-picker-day';
        if(d.getMonth()!==m)btn.classList.add('other');
        if(key===today)btn.classList.add('today');
        if(key===selected)btn.classList.add('selected');
        btn.textContent=d.getDate();
        btn.setAttribute('aria-label',formatDateLabel(key));
        btn.addEventListener('click',()=>{
          const target=datePickerTarget;
          if(!target)return;
          setDateValue(target,key);
          if(target==='startDate'&&(!$('endDate').value||$('endDate').value<key))setDateValue('endDate',key);
          closeDatePicker();
        });
        grid.appendChild(btn);
      }
    }

    function openDatePicker(target){
      datePickerTarget=target;
      const value=$(target).value;
      datePickerCursor=value?parseIso(value):new Date();
      datePickerCursor=new Date(datePickerCursor.getFullYear(),datePickerCursor.getMonth(),1);
      renderDatePicker();
      $('datePickerOverlay').hidden=false;
    }
'''
if helpers_anchor not in text:
    raise SystemExit('calendar v118 syncTimeFields anchor not found')
text = text.replace(helpers_anchor, helpers_anchor + date_js, 1)

# Keep button labels in sync whenever new/edit forms populate ISO values.
text = text.replace("      $('startDate').value=start;\n      $('endDate').value=end;", "      setDateValue('startDate',start);\n      setDateValue('endDate',end);", 1)
text = text.replace("      $('startDate').value=e.start_date;\n      $('endDate').value=e.end_date;", "      setDateValue('startDate',e.start_date);\n      setDateValue('endDate',e.end_date);", 1)

# Clear hidden date values and their visible labels on every fresh form.
reset_anchor = "      $('allDay').checked=true;\n      $('deleteEvent').style.display='none';"
reset_new = "      $('allDay').checked=true;\n      setDateValue('startDate','');\n      setDateValue('endDate','');\n      $('deleteEvent').style.display='none';"
if reset_anchor not in text:
    raise SystemExit('calendar v118 reset form anchor not found')
text = text.replace(reset_anchor, reset_new, 1)

# Guarantee birthdays are yearly in the UI even on iOS select behaviour.
old_calendar_listener = '''    $('calendar').addEventListener('change',()=>{
      const cal=$('calendar').value;
      const isBirthday=cal==='narozeniny';
      const isKumi=cal==='kumi';
      if(isBirthday&&$('recurrence').value==='none'){
        $('recurrence').value='yearly';
        state.autoBirthdayRecurrence=true;
      }else if(!isBirthday&&state.autoBirthdayRecurrence&&$('recurrence').value==='yearly'){
        $('recurrence').value='none';
        state.autoBirthdayRecurrence=false;
      }
      if(isKumi&&!$('title').value.trim()){
        $('title').value='Kumi';
        state.autoKumiTitle=true;
      }else if(!isKumi&&state.autoKumiTitle&&$('title').value.trim()==='Kumi'){
        $('title').value='';
        state.autoKumiTitle=false;
      }
    });
'''
new_calendar_listener = '''    function applyCalendarDefaults(){
      const cal=$('calendar').value;
      const isBirthday=cal==='narozeniny';
      const isKumi=cal==='kumi';
      if(isBirthday&&$('recurrence').value!=='yearly'){
        $('recurrence').value='yearly';
        state.autoBirthdayRecurrence=true;
      }else if(!isBirthday&&state.autoBirthdayRecurrence&&$('recurrence').value==='yearly'){
        $('recurrence').value='none';
        state.autoBirthdayRecurrence=false;
      }
      if(isKumi&&!$('title').value.trim()){
        $('title').value='Kumi';
        state.autoKumiTitle=true;
      }else if(!isKumi&&state.autoKumiTitle&&$('title').value.trim()==='Kumi'){
        $('title').value='';
        state.autoKumiTitle=false;
      }
    }
    $('calendar').addEventListener('input',applyCalendarDefaults);
    $('calendar').addEventListener('change',applyCalendarDefaults);
'''
if old_calendar_listener not in text:
    raise SystemExit('calendar v118 birthday listener anchor not found')
text = text.replace(old_calendar_listener, new_calendar_listener, 1)

# Final safety: a birthday can never be saved as a one-off by accident.
payload_anchor = "        recurrence:$('recurrence').value,\n        start_date:$('startDate').value,"
payload_new = "        recurrence:$('calendar').value==='narozeniny'?'yearly':$('recurrence').value,\n        start_date:$('startDate').value,"
if payload_anchor not in text:
    raise SystemExit('calendar v118 save payload anchor not found')
text = text.replace(payload_anchor, payload_new, 1)

# Picker controls.
events_anchor = "    $('allDay').addEventListener('change',syncTimeFields);\n"
events_js = '''    document.querySelectorAll('.date-trigger').forEach(btn=>btn.addEventListener('click',()=>openDatePicker(btn.dataset.dateTarget)));
    $('datePickerPrev').addEventListener('click',()=>{datePickerCursor.setMonth(datePickerCursor.getMonth()-1);renderDatePicker()});
    $('datePickerNext').addEventListener('click',()=>{datePickerCursor.setMonth(datePickerCursor.getMonth()+1);renderDatePicker()});
    $('datePickerToday').addEventListener('click',()=>{
      const target=datePickerTarget;
      if(!target)return;
      const today=isoLocal(new Date());
      setDateValue(target,today);
      if(target==='startDate'&&(!$('endDate').value||$('endDate').value<today))setDateValue('endDate',today);
      closeDatePicker();
    });
    $('datePickerCancel').addEventListener('click',closeDatePicker);
    $('datePickerOverlay').addEventListener('click',e=>{if(e.target===$('datePickerOverlay'))closeDatePicker()});
'''
if events_anchor not in text:
    raise SystemExit('calendar v118 event bindings anchor not found')
text = text.replace(events_anchor, events_js + events_anchor, 1)

path.write_text(text, encoding='utf-8')
