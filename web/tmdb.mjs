const clean=(value,max=3500)=>String(value||'').trim().slice(0,max);
export function createTMDB({token=process.env.TMDB_READ_TOKEN||'',fetchJSON=fetch}={}){
 const lanes=Array.from({length:3},()=>Promise.resolve());let nextLane=0,retryAt=0;
 let lastToken;const readToken=()=>{const current=typeof token==='function'?token():token;if(current!==lastToken){retryAt=0;lastToken=current;}return current;};
 async function api(route){let response;try{response=await fetchJSON('https://api.themoviedb.org/3/'+route,{headers:{Accept:'application/json',Authorization:'Bearer '+readToken()},signal:AbortSignal.timeout(6000)});}catch{throw Error('TMDB не ответил вовремя. Повторите запрос.');}if(!response.ok)throw Error([401,403].includes(response.status)?'TMDB отклонил доступ. Проверьте токен на сервере.':'TMDB временно недоступен. Повторите запрос.');try{return await response.json();}catch{throw Error('Не удалось прочитать ответ TMDB.');}}
 function metadata(movie){if(!readToken()||!/^tt\d{5,12}$/.test(movie.imdb_id||''))return Promise.resolve(null);const lane=nextLane++%lanes.length;const job=lanes[lane].catch(()=>{}).then(async()=>{if(Date.now()<retryAt)return null;try{const found=await api('find/'+movie.imdb_id+'?external_source=imdb_id&language=ru-RU');const type=movie.kind==='series'?'tv':'movie';const match=found[type==='tv'?'tv_results':'movie_results']?.find(m=>Number.isSafeInteger(m.id)&&m.id>0);if(!match)return {missing:true};const d=await api(type+'/'+match.id+'?language=ru-RU&append_to_response=credits,external_ids');if(!d||d.id!==match.id||d.external_ids?.imdb_id&&d.external_ids.imdb_id!==movie.imdb_id)return {missing:true};const poster=/^\/[a-zA-Z0-9_-]+\.(jpg|png|webp)$/.test(d.poster_path||'')?'https://image.tmdb.org/t/p/w500'+d.poster_path:'';return {id:String(d.id),title:clean(d.title||d.name,300),originalTitle:clean(d.original_title||d.original_name,300),overview:clean(d.overview),poster,rating:Number.isFinite(d.vote_average)?d.vote_average:null,runtime:Number(d.runtime||d.episode_run_time?.[0])||null,genres:(d.genres||[]).map(g=>clean(g.name,80)).slice(0,20),countries:(d.production_countries||[]).map(c=>clean(c.name,100)).slice(0,20),cast:(d.credits?.cast||[]).slice(0,8).map(p=>clean(p.name,150)),directors:(d.credits?.crew||[]).filter(p=>p.job==='Director').map(p=>clean(p.name,150)).slice(0,8),url:'https://www.themoviedb.org/'+type+'/'+d.id};}catch{retryAt=Date.now()+5*60000;return null;}});lanes[lane]=job;return job;}
 async function discover(filters,{page=1,year}={}){const params=new URLSearchParams({language:'ru-RU',include_adult:'false',include_video:'false',sort_by:'primary_release_date.asc',page:String(page),'primary_release_date.gte':(year||filters.yearFrom)+'-01-01','primary_release_date.lte':year?Math.min(year,new Date().getFullYear())+'-12-31':Math.min(filters.yearTo,new Date().getFullYear())+'-12-31','vote_average.gte':String(filters.rating),'vote_count.gte':filters.rating?'50':'0'});const today=new Date().toISOString().slice(0,10);if(params.get('primary_release_date.lte')>today)params.set('primary_release_date.lte',today);if(filters.genreId)params.set('with_genres',String(filters.genreId));if(filters.excludeGenreId)params.set('without_genres',String(filters.excludeGenreId));if(filters.countryCode)params.set('with_origin_country',filters.countryCode);const d=await api('discover/movie?'+params);if(!Array.isArray(d.results)||!Number.isFinite(d.total_pages)||!Number.isFinite(d.total_results))throw Error('TMDB вернул неподдерживаемый результат поиска.');return {movies:d.results,totalPages:d.total_pages,totalResults:d.total_results};}
 async function movie(id){if(!Number.isSafeInteger(id)||id<1)throw Error('Некорректный ID фильма.');return api('movie/'+id+'?language=ru-RU&append_to_response=credits,external_ids');}
 async function identify({titles=[],year,kind='movie',tmdbId='',imdbId=''}={}){
  if(!readToken())return null;
  const type=kind==='series'?'tv':'movie',yearKey=type==='tv'?'first_air_date':'release_date';
  const norm=s=>clean(s,300).toLowerCase().replaceAll('ё','е').replace(/[^\p{L}\p{N}]/gu,'');
  let id=/^[1-9]\d{0,9}$/.test(String(tmdbId))?Number(tmdbId):null;
  const imdb=/^tt\d{5,12}$/.test(imdbId)?imdbId:'';
  const hasProvider=!!(id||imdb);
  if(!id&&imdb){const d=await api('find/'+imdb+'?external_source=imdb_id&language=ru-RU');id=d[type==='tv'?'tv_results':'movie_results']?.[0]?.id;if(!id)return null;}
  if(!id){
   const names=[...new Set(titles.map(t=>clean(t,300)).filter(Boolean))].slice(0,3),candidates=new Map();
   for(const name of names){const q=new URLSearchParams({query:name,language:'ru-RU',include_adult:'false'});if(year)q.set(type==='tv'?'first_air_date_year':'year',String(year));const d=await api('search/'+type+'?'+q);if(d.total_pages>1)return null;
    for(const m of d.results||[])if(Number.isSafeInteger(m.id)&&m.id>0&&!m.adult&&(!year||String(m[yearKey]||'').startsWith(String(year)))&&names.some(n=>[m.title,m.name,m.original_title,m.original_name].some(t=>t&&norm(t)===norm(n))))candidates.set(m.id,m);
   }
   if(candidates.size!==1)return null;id=[...candidates.keys()][0];
  }
  if(!Number.isSafeInteger(id)||id<1)return null;
  const d=await api(type+'/'+id+'?language=ru-RU&append_to_response=external_ids');
  if(d.id!==id||d.adult||(!hasProvider&&year&&!String(d[yearKey]||'').startsWith(String(year)))||!/^tt\d{5,12}$/.test(d.external_ids?.imdb_id||'')||imdb&&d.external_ids.imdb_id!==imdb)return null;
  return {imdb_id:d.external_ids.imdb_id,tmdb_id:String(id),kind,title:clean(d.title||d.name,300),original_title:clean(d.original_title||d.original_name,300),year:Number(String(d[yearKey]||'').slice(0,4))||null};
 }
 return {get enabled(){return !!readToken();},metadata,discover,movie,identify};
}
