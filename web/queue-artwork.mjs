import {queryTitle,mediaKind} from './dist/core.js';

export function artworkIdentity(pack,library={}) {
 const kind=mediaKind(pack.kind),names=(pack.episodes||[]).map(e=>String(e.name||'').split(/[\\/]/).pop());
 const filename=names.find(n=>/\b(?:19|20)\d{2}\b/.test(n));
 const year=Number(library.year)||Number(((kind==='movie'?filename:'')||pack.title||'').match(/\b(?:19|20)\d{2}\b/)?.[0])||null;
 return {kind,year,tmdbId:library.tmdbId||'',imdbId:library.imdbId||'',titles:[...new Set([library.title,library.originalTitle,pack.title,filename].filter(Boolean).map(queryTitle))]};
}
export function createQueueArtwork({tmdb,registerMovies,now=Date.now}) {
 const cache=new Map(),jobs=new Map();
 function remember(hash,value,ttl){cache.set(hash,{value,expires:now()+ttl});if(cache.size>200)cache.delete(cache.keys().next().value);return value;}
 async function resolve(hash,read){
  if(!/^[a-f0-9]{40}$/i.test(hash))throw Error('Некорректная раздача.');
  if(!tmdb.enabled)return {poster:''};
  const old=cache.get(hash);if(old&&now()<old.expires)return old.value;
  if(jobs.has(hash))return jobs.get(hash);
  const job=(async()=>{
   const pack=await read('/api/packs/'+hash);
   let library={};try{library=await read('/api/jellyfin/link?pack='+hash);}catch{}
   const match=await tmdb.identify(artworkIdentity(pack,library));
   if(!match){const value={poster:''};return remember(hash,value,60000);}
   const rows=await registerMovies([match]);const row=rows[0];
   const value=row?{poster:row.poster,tmdbId:match.tmdb_id,imdbId:match.imdb_id,title:match.title,year:match.year}:{poster:''};
   return remember(hash,value,3600000);
  })().finally(()=>jobs.delete(hash));jobs.set(hash,job);return job;
 }
 return {resolve};
}
