from pathlib import Path

path = Path('/app/calendar.html')
text = path.read_text(encoding='utf-8')

# On phones show up to two lines before ellipsizing. Grid columns remain fixed by
# the 0.11.5 patch, while the calendar row may grow vertically when needed.
mobile_event_old = '      .event{font-size:9px;padding:3px 4px;margin-top:3px;border-radius:6px}.more{font-size:9px;padding:3px 0}\n'
mobile_event_new = '      .event{font-size:9px;padding:3px 4px;margin-top:3px;border-radius:6px;white-space:normal;display:-webkit-box;-webkit-box-orient:vertical;-webkit-line-clamp:2;line-height:1.25;max-height:calc(2.5em + 6px);overflow:hidden;text-overflow:ellipsis}.more{font-size:9px;padding:3px 0}\n'
if mobile_event_old not in text:
    raise SystemExit('calendar v116 mobile event CSS anchor not found')
text = text.replace(mobile_event_old, mobile_event_new, 1)

# Floating, read-only preview used while an event is held on touch devices.
toast_anchor = '    .toast{position:fixed;right:18px;bottom:18px;'
preview_css = '''    .event-preview{position:fixed;z-index:70;max-width:min(320px,calc(100vw - 24px));background:#111827;color:#fff;border-radius:13px;padding:10px 12px;box-shadow:0 16px 42px rgba(15,23,42,.28);pointer-events:none;opacity:0;transform:translateY(5px);transition:opacity .12s ease,transform .12s ease;line-height:1.3}\n    .event-preview.show{opacity:1;transform:translateY(0)}\n    .event-preview-title{font-size:13px;font-weight:850;overflow-wrap:anywhere}\n    .event-preview-meta{font-size:11px;color:#cbd5e1;margin-top:3px;overflow-wrap:anywhere}\n'''
if toast_anchor not in text:
    raise SystemExit('calendar v116 toast CSS anchor not found')
text = text.replace(toast_anchor, preview_css + toast_anchor, 1)

hint_old = 'Klepni kamkoliv do dne pro novou událost. Podrž a táhni přes více dnů pro rozsah. Událost podrž a přetáhni na jiný den.'
hint_new = 'Klepni kamkoliv do dne pro novou událost. Podrž a táhni přes více dnů pro rozsah. Událost podrž pro celý název; pak táhni pro přesun.'
if hint_old not in text:
    raise SystemExit('calendar v116 gesture hint anchor not found')
text = text.replace(hint_old, hint_new, 1)

old_touch_state = "    const touchGesture={kind:null,timer:null,startX:0,startY:0,startDate:null,currentDate:null,event:null,eventEl:null,active:false};"
new_touch_state = "    const touchGesture={kind:null,timer:null,startX:0,startY:0,startDate:null,currentDate:null,event:null,eventEl:null,active:false,previewing:false};"
if old_touch_state not in text:
    raise SystemExit('calendar v116 touch state anchor not found')
text = text.replace(old_touch_state, new_touch_state, 1)

vibrate_anchor = '    function vibrate(){try{navigator.vibrate&&navigator.vibrate(18)}catch(_){}}\n'
preview_js = '''    let eventPreviewEl=null;\n    function hideEventPreview(){\n      if(eventPreviewEl){eventPreviewEl.remove();eventPreviewEl=null}\n    }\n    function showEventPreview(event,x,y){\n      hideEventPreview();\n      const el=document.createElement('div');\n      el.className='event-preview';\n      const title=document.createElement('div');\n      title.className='event-preview-title';\n      title.textContent=eventLabel(event);\n      el.appendChild(title);\n      if(event.location){\n        const meta=document.createElement('div');\n        meta.className='event-preview-meta';\n        meta.textContent=event.location;\n        el.appendChild(meta);\n      }\n      document.body.appendChild(el);\n      eventPreviewEl=el;\n      const pad=12,rect=el.getBoundingClientRect();\n      let left=Math.min(Math.max(pad,x-rect.width/2),window.innerWidth-rect.width-pad);\n      let top=y-rect.height-18;\n      if(top<pad)top=Math.min(window.innerHeight-rect.height-pad,y+18);\n      el.style.left=`${left}px`;\n      el.style.top=`${Math.max(pad,top)}px`;\n      requestAnimationFrame(()=>{if(eventPreviewEl===el)el.classList.add('show')});\n    }\n'''
if vibrate_anchor not in text:
    raise SystemExit('calendar v116 vibrate anchor not found')
text = text.replace(vibrate_anchor, vibrate_anchor + preview_js, 1)

old_event_touch = '''      btn.addEventListener('touchstart',ev=>{\n        if(ev.touches.length!==1)return;\n        ev.stopPropagation();\n        const t=ev.touches[0];\n        resetTouchGesture();\n        touchGesture.kind='event';touchGesture.startX=t.clientX;touchGesture.startY=t.clientY;touchGesture.event=event;touchGesture.eventEl=btn;touchGesture.currentDate=event.start_date;\n        touchGesture.timer=setTimeout(()=>{\n          if(touchGesture.kind!=='event')return;\n          touchGesture.active=true;gestureOn();btn.classList.add('dragging');highlightDrop(touchGesture.currentDate);vibrate();\n        },450);\n      },{passive:true});\n'''
new_event_touch = '''      btn.addEventListener('touchstart',ev=>{\n        if(ev.touches.length!==1)return;\n        ev.stopPropagation();\n        const t=ev.touches[0];\n        resetTouchGesture();\n        touchGesture.kind='event';touchGesture.startX=t.clientX;touchGesture.startY=t.clientY;touchGesture.event=event;touchGesture.eventEl=btn;touchGesture.currentDate=event.start_date;touchGesture.previewing=false;\n        touchGesture.timer=setTimeout(()=>{\n          if(touchGesture.kind!=='event')return;\n          touchGesture.previewing=true;\n          showEventPreview(event,touchGesture.startX,touchGesture.startY);\n          vibrate();\n        },450);\n      },{passive:true});\n'''
if old_event_touch not in text:
    raise SystemExit('calendar v116 event touchstart anchor not found')
text = text.replace(old_event_touch, new_event_touch, 1)

old_touchmove = '''    document.addEventListener('touchmove',ev=>{\n      if(!touchGesture.kind||ev.touches.length!==1)return;\n      const t=ev.touches[0],distance=Math.hypot(t.clientX-touchGesture.startX,t.clientY-touchGesture.startY);\n      if(!touchGesture.active){\n        if(distance>10)resetTouchGesture();\n        return;\n      }\n      ev.preventDefault();\n      const day=dayFromPoint(t.clientX,t.clientY);\n      if(!day)return;\n      touchGesture.currentDate=day.dataset.date;\n      if(touchGesture.kind==='range')highlightRange(touchGesture.startDate,touchGesture.currentDate);else highlightDrop(touchGesture.currentDate);\n    },{passive:false});\n'''
new_touchmove = '''    document.addEventListener('touchmove',ev=>{\n      if(!touchGesture.kind||ev.touches.length!==1)return;\n      const t=ev.touches[0],distance=Math.hypot(t.clientX-touchGesture.startX,t.clientY-touchGesture.startY);\n      if(!touchGesture.active){\n        if(touchGesture.kind==='event'&&touchGesture.previewing){\n          if(distance<=8)return;\n          touchGesture.previewing=false;\n          hideEventPreview();\n          touchGesture.active=true;\n          gestureOn();\n          if(touchGesture.eventEl)touchGesture.eventEl.classList.add('dragging');\n        }else{\n          if(distance>10)resetTouchGesture();\n          return;\n        }\n      }\n      ev.preventDefault();\n      const day=dayFromPoint(t.clientX,t.clientY);\n      if(!day)return;\n      touchGesture.currentDate=day.dataset.date;\n      if(touchGesture.kind==='range')highlightRange(touchGesture.startDate,touchGesture.currentDate);else highlightDrop(touchGesture.currentDate);\n    },{passive:false});\n'''
if old_touchmove not in text:
    raise SystemExit('calendar v116 touchmove anchor not found')
text = text.replace(old_touchmove, new_touchmove, 1)

old_touchend = '''    document.addEventListener('touchend',ev=>{\n      if(!touchGesture.kind)return;\n      const snapshot={...touchGesture};\n      resetTouchGesture();\n      if(!snapshot.active)return;\n      ev.preventDefault();\n      state.suppressClickUntil=Date.now()+700;\n      gestureOff();\n      if(snapshot.eventEl)snapshot.eventEl.classList.remove('dragging');\n      if(snapshot.kind==='range'){\n        const[start,end]=normalizeRange(snapshot.startDate,snapshot.currentDate||snapshot.startDate);\n        openNew(start,end);\n      }else if(snapshot.kind==='event'&&snapshot.event){\n        moveEventToDate(snapshot.event,snapshot.currentDate||snapshot.event.start_date);\n      }\n    },{passive:false});\n'''
new_touchend = '''    document.addEventListener('touchend',ev=>{\n      if(!touchGesture.kind)return;\n      const snapshot={...touchGesture};\n      resetTouchGesture();\n      if(snapshot.previewing&&!snapshot.active){\n        ev.preventDefault();\n        state.suppressClickUntil=Date.now()+700;\n        return;\n      }\n      if(!snapshot.active)return;\n      ev.preventDefault();\n      state.suppressClickUntil=Date.now()+700;\n      gestureOff();\n      if(snapshot.eventEl)snapshot.eventEl.classList.remove('dragging');\n      if(snapshot.kind==='range'){\n        const[start,end]=normalizeRange(snapshot.startDate,snapshot.currentDate||snapshot.startDate);\n        openNew(start,end);\n      }else if(snapshot.kind==='event'&&snapshot.event){\n        moveEventToDate(snapshot.event,snapshot.currentDate||snapshot.event.start_date);\n      }\n    },{passive:false});\n'''
if old_touchend not in text:
    raise SystemExit('calendar v116 touchend anchor not found')
text = text.replace(old_touchend, new_touchend, 1)

old_reset = '''    function resetTouchGesture(){\n      if(touchGesture.timer)clearTimeout(touchGesture.timer);\n      touchGesture.kind=null;touchGesture.timer=null;touchGesture.startX=0;touchGesture.startY=0;touchGesture.startDate=null;touchGesture.currentDate=null;touchGesture.event=null;touchGesture.eventEl=null;touchGesture.active=false;\n    }\n'''
new_reset = '''    function resetTouchGesture(){\n      if(touchGesture.timer)clearTimeout(touchGesture.timer);\n      hideEventPreview();\n      touchGesture.kind=null;touchGesture.timer=null;touchGesture.startX=0;touchGesture.startY=0;touchGesture.startDate=null;touchGesture.currentDate=null;touchGesture.event=null;touchGesture.eventEl=null;touchGesture.active=false;touchGesture.previewing=false;\n    }\n'''
if old_reset not in text:
    raise SystemExit('calendar v116 reset touch anchor not found')
text = text.replace(old_reset, new_reset, 1)

path.write_text(text, encoding='utf-8')
