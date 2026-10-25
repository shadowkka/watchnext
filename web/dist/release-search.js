function pause(signal){return new Promise((resolve,reject)=>{const abort=()=>{clearTimeout(timer);signal?.removeEventListener('abort',abort);reject(new DOMException('Aborted','AbortError'));};const timer=setTimeout(()=>{signal?.removeEventListener('abort',abort);resolve();},400);signal?.addEventListener('abort',abort,{once:true});if(signal?.aborted)abort();});}
export async function progressiveSearch({request,query,kind,signal,onUpdate,wait=pause}){
 let page;
 try{page=await request('/api/search/start',{method:'POST',body:{query,kind},signal});}
 catch(error){if(![404,405].includes(error.status))throw error;const results=await request('/api/search?'+new URLSearchParams({query,kind,minSeeders:'0'}),{signal});onUpdate({results,done:true,changed:true,completed:0,total:0,warnings:[]});return;}
 const deadline=Date.now()+65000;
 while(true){if(signal?.aborted)throw new DOMException('Aborted','AbortError');if(page.changed)onUpdate(page);if(page.done)return;if(Date.now()>deadline)throw Error('Поиск ещё не завершён. Повторите запрос.');await wait(signal);page=await request('/api/search/jobs/'+encodeURIComponent(page.id)+'?after='+encodeURIComponent(page.revision),{signal});}
}
