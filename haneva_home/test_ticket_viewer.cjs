// Logic tests without a browser. Run: node haneva_home/test_ticket_viewer.cjs
const fs=require('fs'),vm=require('vm'),assert=require('assert/strict');
const html=fs.readFileSync(__dirname+'/calendar.html','utf8');
for(const [,script] of html.matchAll(/<script[^>]*>([\s\S]*?)<\/script>/g))new Function(script);
assert(html.indexOf('<div class="view-toggle">')<html.indexOf('<div class="nav">'));
assert(!html.includes('data-ticket-file='));
assert.equal((html.match(/id="showTicket"/g)||[]).length,1);
const elements=new Map();
function $(id){
  if(!elements.has(id))elements.set(id,{hidden:false,open:false,value:'',textContent:'',
    removeAttribute(name){delete this[name]},showModal(){this.open=true},close(){this.open=false}});
  return elements.get(id);
}
const context=vm.createContext({$,document:{visibilityState:'visible',querySelector:()=>$('.ticket-pager'),
  querySelectorAll:()=>[]},navigator:{},state:{tickets:[],ticketFile:null,removeTicketBundle:false},Date});
const helpers=html.slice(html.indexOf('    function setTickets('),html.indexOf('    function resetForm('));
vm.runInContext(helpers,context);
function run(code){return vm.runInContext(code,context)}
context.fixtures=[{qr_url:'/qr/1',original_url:'/original',owner:'hanych'},{qr_url:'/qr/2',original_url:'/original',owner:'eva'},
  {qr_url:'/qr/3',original_url:'/original',owner:'3'}];
run('setTickets(fixtures.slice(0,1));showTicket()');
assert.equal($('ticketViewerTitle').textContent,'Vstupenka');
assert.equal($('.ticket-pager').hidden,true);
assert.equal($('ticketQr').hidden,true);
$('ticketQr').onload();assert.equal($('ticketQr').hidden,false);
run('closeTicket();releaseTicketViewer();setTickets(fixtures);showTicket()');
assert.equal($('ticketViewerTitle').textContent,'Vstupenka 1');
assert.equal($('.ticket-pager').hidden,false);
const oldOnload=$('ticketQr').onload;
run('turnTicket(1)');
assert.equal($('ticketViewerTitle').textContent,'Vstupenka 2');
assert($('ticketQr').src.startsWith('/qr/2?'));
oldOnload();assert.equal($('ticketQr').hidden,true,'stale load must not show previous QR');
$('ticketQr').onload();assert.equal($('ticketQr').hidden,false);
run('turnTicket(1)');assert.equal($('ticketViewerTitle').textContent,'Vstupenka 3');
run('turnTicket(1)');assert.equal($('ticketViewerTitle').textContent,'Vstupenka 1');
run('turnTicket(-1)');assert.equal($('ticketViewerTitle').textContent,'Vstupenka 3');
assert.equal($('ticketOriginalViewer').href,'/original');
$('ticketQr').onerror();assert.equal($('ticketQr').hidden,true);assert.equal($('ticketNoQr').hidden,false);
run('closeTicket();releaseTicketViewer()');assert.equal($('ticketQr').src,undefined);
console.log('Ticket viewer: single/multiple, order, arrows, stale image, fallback and close OK');

// Exercise the same touch handlers that implement the swipe gesture.
const handlers={};
$('ticketStage').addEventListener=(type,fn)=>{handlers[type]=fn};
const touch=html.slice(html.indexOf('    let ticketTouch=null;'),html.indexOf("    document.addEventListener('visibilitychange'"));
run(touch);run('showTicket()');
handlers.touchstart({touches:[{clientX:280,clientY:100}]});
handlers.touchend({changedTouches:[{clientX:100,clientY:115}]});
assert.equal($('ticketViewerTitle').textContent,'Vstupenka 2');
handlers.touchstart({touches:[{clientX:200,clientY:100}]});
handlers.touchend({changedTouches:[{clientX:205,clientY:300}]});
assert.equal($('ticketViewerTitle').textContent,'Vstupenka 2','vertical scroll is not swipe');
handlers.touchstart({touches:[{clientX:100,clientY:100}]});
handlers.touchend({changedTouches:[{clientX:270,clientY:110}]});
assert.equal($('ticketViewerTitle').textContent,'Vstupenka 1');
console.log('Ticket swipe: left/right and vertical-scroll guard OK');
