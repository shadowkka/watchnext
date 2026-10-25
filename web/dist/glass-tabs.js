// Animate the selection surface, independently from page content rendering.
const pending=new Map();
const reduced=matchMedia('(prefers-reduced-motion: reduce)');
const selector='#mobile-nav,.segmented';
const active=group=>group.querySelector(group.id==='mobile-nav'?'a.active':'.chip[aria-pressed="true"]');
const key=group=>group.id||group.getAttribute('aria-label')+':'+(group.querySelector('[data-action]')?.dataset.action||group.querySelector('[data-explore]')?.dataset.explore||'');
function rectWithin(element,group){const r=element.getBoundingClientRect(),g=group.getBoundingClientRect();return {x:r.left-g.left-group.clientLeft,y:r.top-g.top-group.clientTop,width:r.width,height:r.height};}
function frame(r){return {transform:`translate(${r.x}px,${r.y}px)`,width:r.width+'px',height:r.height+'px'};}
function sync(){
 const glass=document.documentElement.dataset.theme?.startsWith('glass-');
 document.querySelectorAll(selector).forEach(group=>{
  const selected=active(group);let indicator=group.querySelector(':scope > .glass-tab-indicator');
  if(!glass||!selected||!group.getBoundingClientRect().width){indicator?.remove();group.classList.remove('glass-tabs-ready');return;}
  if(!indicator){indicator=document.createElement('span');indicator.className='glass-tab-indicator';indicator.setAttribute('aria-hidden','true');group.prepend(indicator);}
  group.classList.add('glass-tabs-ready');
  const to=rectWithin(selected,group),from=pending.get(key(group));pending.delete(key(group));
  Object.assign(indicator.style,frame(to));
  if(from&&!reduced.matches){
   indicator.getAnimations().forEach(animation=>animation.cancel());
   indicator.animate([frame(from),frame(to)],{duration:360,easing:'cubic-bezier(.22,.8,.24,1)'});
   const glyph=selected.querySelector('.nav-icon');
   glyph?.animate([{transform:'scale(.88)'},{transform:'scale(1.07)',offset:.65},{transform:'scale(1)'}],{duration:300,easing:'ease-out'});
  }
 });
 pending.clear();
}
let queued=false;
function schedule(){if(queued)return;queued=true;requestAnimationFrame(()=>{queued=false;sync();});}
document.addEventListener('click',event=>{
 if(!document.documentElement.dataset.theme?.startsWith('glass-')||reduced.matches)return;
 const target=event.target.closest('#mobile-nav a,.segmented .chip'),group=target?.closest(selector);
 if(!group||target===active(group)||target.disabled)return;
 const previous=group.querySelector(':scope > .glass-tab-indicator')||active(group);
 if(previous)pending.set(key(group),rectWithin(previous,group));
 schedule();
},true);
new MutationObserver(schedule).observe(document.documentElement,{subtree:true,childList:true,attributes:true,attributeFilter:['aria-pressed','aria-current','data-theme']});
window.addEventListener('resize',()=>{pending.clear();schedule();});
reduced.addEventListener('change',()=>{if(reduced.matches)document.querySelectorAll('.glass-tab-indicator,.nav-icon').forEach(el=>el.getAnimations().forEach(a=>a.cancel()));pending.clear();schedule();});
schedule();
