import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import fs from 'node:fs/promises';
const source=await fs.readFile(new URL('../dist/glass-tabs.js',import.meta.url),'utf8');
function browser(reduce=false){
 const raf=[],listeners={},observers=[];let selected,animations=0,cancels=0,iconAnimations=0,frames,options;
 const glyph={getAnimations:()=>[],animate(){iconAnimations++;}};
 const makeTab=(id,x)=>({dataset:{},disabled:false,querySelector:()=>glyph,getAttribute:()=>`#${id}`,getBoundingClientRect:()=>({left:x,top:0,width:80,height:50}),closest:s=>s.includes(' a')?tabs[id]:group});
 const tabs={queue:makeTab('queue',0),search:makeTab('search',80)};selected=tabs.queue;
 const indicator={dataset:{},style:{},setAttribute(){},getBoundingClientRect:()=>({left:0,top:0,width:80,height:50}),getAnimations:()=>animations?[{cancel(){cancels++;}}]:[],animate(f,o){animations++;frames=f;options=o;}};
 const group={id:'mobile-nav',clientLeft:0,clientTop:0,classList:{add(){},remove(){}},querySelectorAll:()=>[glyph],getBoundingClientRect:()=>({left:0,top:0,width:320,height:60}),querySelector:s=>s==='a.active'?selected:indicator};
 const document={documentElement:{dataset:{theme:'glass-light'}},querySelector:s=>s==='#mobile-nav'?group:null,querySelectorAll:()=>[group],addEventListener:(n,fn)=>listeners[n]=fn};
 vm.runInNewContext(source,{document,window:{addEventListener(){}},matchMedia:()=>({matches:reduce,addEventListener(){}}),requestAnimationFrame:fn=>raf.push(fn),performance:{now:()=>100},MutationObserver:class{constructor(fn){observers.push(fn);}observe(){}}});
 return {flush(){while(raf.length)raf.shift()();},click(){listeners.click({target:tabs.search});},select(){selected=tabs.search;observers[0]();},stats:()=>({animations,cancels,iconAnimations,frames,options,transform:indicator.style.transform})};
}
test('Safari paint between click and hashchange does not cancel or replay navigation movement',()=>{
 const b=browser();b.flush();b.click();assert.equal(b.stats().animations,1);assert.equal(b.stats().iconAnimations,1);assert.equal(b.stats().frames.length,4);assert.equal(b.stats().options.duration,440);b.flush();assert.equal(b.stats().cancels,0);b.select();b.flush();assert.equal(b.stats().animations,1);assert.equal(b.stats().cancels,0);assert.equal(b.stats().transform,'translate3d(80px,0px,0)');b.select();b.flush();assert.equal(b.stats().animations,1);
});
test('reduced motion switches tabs without animation',()=>{const b=browser(true);b.flush();b.click();b.select();b.flush();assert.equal(b.stats().animations,0);assert.equal(b.stats().iconAnimations,0);assert.equal(b.stats().transform,'translate3d(80px,0px,0)');});
