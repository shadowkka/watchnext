// Navigation starts moving on the click itself, without waiting for hashchange.
const pending=new Map();
const reduced=matchMedia('(prefers-reduced-motion: reduce)');
const selector='#mobile-nav,.segmented';
const active=g=>g.querySelector(g.id==='mobile-nav'?'a.active':'.chip[aria-pressed="true"]');
const key=g=>g.id||g.getAttribute('aria-label')+':'+(g.querySelector('[data-action]')?.dataset.action||g.querySelector('[data-explore]')?.dataset.explore||'');
const identity=b=>b?.getAttribute('href')||b?.dataset.value||b?.dataset.mode;
function rectWithin(el,g){const r=el.getBoundingClientRect(),b=g.getBoundingClientRect();return {x:r.left-b.left-g.clientLeft,y:r.top-b.top-g.clientTop,width:r.width,height:r.height};}
function move(indicator,to,from){
 indicator.getAnimations().forEach(a=>a.cancel());
 Object.assign(indicator.style,{width:to.width+'px',height:to.height+'px',transform:`translate3d(${to.x}px,${to.y}px,0)`});
 if(!from||reduced.matches||!to.width||!to.height)return;
 // Stretch around the moving centre, then settle without changing layout.
 const phase=(t,sx=1,sy=1)=>{
  const width=from.width+(to.width-from.width)*t,height=from.height+(to.height-from.height)*t;
  const x=from.x+(to.x-from.x)*t+(width-width*sx)/2;
  const y=from.y+(to.y-from.y)*t+(height-height*sy)/2;
  return {transform:`translate3d(${x}px,${y}px,0) scale(${width*sx/to.width},${height*sy/to.height})`};
 };
 indicator.animate([
  {...phase(0),offset:0},
  {...phase(.48,1.4,1.55),offset:.36},
  {...phase(1,1.06,.96),offset:.76},
  {...phase(1),offset:1}
 ],{duration:704,easing:'cubic-bezier(.25,.6,.3,1)'});
}
function bounceIcon(target,group){
 const glyph=target.querySelector('.nav-icon');if(!glyph||reduced.matches)return;
 group.querySelectorAll('.nav-icon').forEach(el=>el.getAnimations().forEach(a=>a.cancel()));
 glyph.animate([
  {transform:'translate3d(0,0,0) scale(1) rotate(0deg)',offset:0},
  {transform:'translate3d(0,1px,0) scale(.9) rotate(-3deg)',offset:.14},
  {transform:'translate3d(0,-3px,0) scale(1.16) rotate(4deg)',offset:.42},
  {transform:'translate3d(0,1px,0) scale(.98) rotate(-2deg)',offset:.73},
  {transform:'translate3d(0,0,0) scale(1) rotate(0deg)',offset:1}
 ],{duration:768,easing:'ease-out'});
}

function sync(){
 const glass=document.documentElement.dataset.theme?.startsWith('glass-');
 document.querySelectorAll(selector).forEach(g=>{
  const selected=active(g);let indicator=g.querySelector(':scope > .glass-tab-indicator');
  if(!glass||!selected||!g.getBoundingClientRect().width){indicator?.remove();g.classList.remove('glass-tabs-ready');return;}
  if(!indicator){indicator=document.createElement('span');indicator.className='glass-tab-indicator';indicator.setAttribute('aria-hidden','true');g.prepend(indicator);}
  g.classList.add('glass-tabs-ready');
  const intent=pending.get(key(g));
  // Safari may paint between click and hashchange. Keep the destination until
  // the selected tab actually changes; do not consume it on the old selection.
  if(intent&&intent.target!==identity(selected)&&performance.now()-intent.at<1000)return;
  const to=rectWithin(selected,g),signature=JSON.stringify(to);
  if(intent){pending.delete(key(g));if(!intent.started){move(indicator,to,intent.from);indicator.dataset.position=signature;return;}}
  if(indicator.dataset.position!==signature){move(indicator,to);indicator.dataset.position=signature;}
 });
}
let queued=false;
function schedule(){if(queued)return;queued=true;requestAnimationFrame(()=>{queued=false;sync();});}
document.addEventListener('click',event=>{
 if(!document.documentElement.dataset.theme?.startsWith('glass-')||reduced.matches)return;
 const target=event.target.closest('#mobile-nav a,.segmented .chip'),g=target?.closest(selector);
 if(!g||target===active(g)||target.disabled)return;
 const indicator=g.querySelector(':scope > .glass-tab-indicator'),previous=indicator||active(g);
 if(!previous)return;
 const intent={target:identity(target),from:rectWithin(previous,g),at:performance.now(),started:false};
 // The navigation DOM survives page changes; begin its feedback immediately.
 if(g.id==='mobile-nav'&&indicator){bounceIcon(target,g);const to=rectWithin(target,g);move(indicator,to,intent.from);indicator.dataset.position=JSON.stringify(to);intent.started=true;}
 pending.set(key(g),intent);
 schedule();
},true);
const observer=new MutationObserver(schedule);
for(const root of [document.querySelector('#content'),document.querySelector('#mobile-nav')])if(root)observer.observe(root,{subtree:true,childList:true,attributes:true,attributeFilter:['aria-pressed','aria-current']});
new MutationObserver(()=>{pending.clear();schedule();}).observe(document.documentElement,{attributes:true,attributeFilter:['data-theme']});
window.addEventListener('resize',()=>{pending.clear();schedule();});
reduced.addEventListener('change',()=>{if(reduced.matches)document.querySelectorAll('.glass-tab-indicator,.nav-icon').forEach(el=>el.getAnimations().forEach(a=>a.cancel()));pending.clear();schedule();});
schedule();
