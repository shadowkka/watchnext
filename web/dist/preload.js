export function createCardPreloader({details,poster}){
 const ready=new Map(),jobs=new Map();
 async function ensure(ids){const unique=[...new Set(ids)],fresh=unique.filter(id=>!ready.has(id)&&!jobs.has(id));for(let start=0;start<fresh.length;start+=10){const batch=fresh.slice(start,start+10);const job=(async()=>{const data=await details(batch);const records=new Map(data.movies.map(m=>[m.imdb_id,m]));return Promise.all(batch.map(async id=>{if(!records.has(id))throw Error('Не удалось подготовить карточку фильма.');let posterReady=false;try{posterReady=await poster(id);}catch{}const result={movie:records.get(id),posterReady};ready.set(id,result);return result;}));})();for(const id of batch)jobs.set(id,job);job.finally(()=>{for(const id of batch)if(jobs.get(id)===job)jobs.delete(id);}).catch(()=>{});}
  await Promise.all([...new Set(unique.map(id=>jobs.get(id)).filter(Boolean))]);return unique.map(id=>ready.get(id));
 }
 return {ensure,isReady:ids=>ids.every(id=>ready.has(id)),get:id=>ready.get(id)};
}
export function preloadPoster(id){return new Promise(resolve=>{const img=new Image();let done=false;const finish=value=>{if(done)return;done=true;clearTimeout(timer);img.onload=null;img.onerror=null;resolve(value);};const timer=setTimeout(()=>finish(false),15000);img.onload=()=>{if(img.decode)img.decode().catch(()=>{}).then(()=>finish(true));else finish(true);};img.onerror=()=>finish(false);img.src='/api/discover/poster/'+encodeURIComponent(id);});}
