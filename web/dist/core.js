export const escapeHTML = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
export function bytes(value){let n=Number(value)||0;if(n<=0)return '0\u00a0Б';let i=Math.min(4,Math.floor(Math.log(n)/Math.log(1024)));return `${i?(n/1024**i).toLocaleString('ru-RU',{minimumFractionDigits:1,maximumFractionDigits:1}):n}\u00a0${['Б','КБ','МБ','ГБ','ТБ'][i]}`;}
export const clamp = value => Math.max(0,Math.min(1,Number(value)||0));
export function numeric(value,min,max,integer=false){const n=Number(String(value).trim().replace(',','.'));if(String(value).trim()===''||!Number.isFinite(n)||n<min||n>max||(integer&&!Number.isInteger(n)))throw new Error(`Введите ${integer?'целое число':'число'} от ${min} до ${max}`);return n;}
export function normalizeServer(value){const raw=String(value||'').trim().replace(/\/+$/,'').replace(/\/api$/,'');if(!raw)return '';let url;try{url=new URL(raw);}catch{throw new Error('Укажите полный адрес сервера: https://…');}if(!['http:','https:'].includes(url.protocol)||url.username||url.password||url.search||url.hash)throw new Error('Нужен HTTP или HTTPS адрес без логина и параметров');return url.href.replace(/\/$/,'');}
export function connectionProblem(server,pageProtocol){if(pageProtocol==='https:'&&server.startsWith('http:'))return 'Эта страница открыта по HTTPS. Для подключения нужен HTTPS-адрес API или локальный шлюз из комплекта веб-версии.';return '';}
export function isReady(pack){return ['READY','COMPLETE'].includes(String(pack.state).toUpperCase());}
export function queryTitle(value){return String(value||'').split(' / ')[0].replace(/[._]/g,' ').replace(/\([^)]*\)|\[[^\]]*\]/g,'').replace(/\b(?:S\d{1,2}(?:E\d+)?|19\d{2}|20\d{2}|1080p|2160p|720p|WEB[ -]?DL|BDRip)\b.*$/i,'').replace(/(?:сезон|серии|серия)\s*\d.*$/i,'').split('|')[0].trim();}
export function apiURL(base,path){return `${normalizeServer(base)}${path.startsWith('/')?path:`/${path}`}`;}

export const mediaKind = value => String(value||'series').toLowerCase()==='movie'?'movie':'series';

export function apiError(status,payload){
 if([401,403].includes(status))return 'Сервер отклонил токен. Проверьте его в настройках.';
 const detail=typeof payload?.detail==='string'?payload.detail.trim():'';
 return detail?detail.slice(0,700):`Сервер ответил ошибкой ${status}`;
}

export function releaseSeasons(title){
 const text=String(title||'').replace(/[–—−]/g,'-');const found=new Set();
 const add=(a,b=a)=>{a=Number(a);b=Number(b);if(a>=1&&b>=a&&b<=999)for(let n=a;n<=b;n++)found.add(n);};
 for(const m of text.matchAll(/(?:^|[^a-zа-яё0-9])s(\d+)(?:\s*-\s*s?(\d+))?/gi))add(m[1],m[2]||m[1]);
 for(const m of text.matchAll(/сезон(?:ы|ов|а)?\s*[:№]?\s*((?:\d+)(?:\s*(?:-|,|и|\/)\s*\d+)*)/gi)){
  for(const part of m[1].split(/[,и\/]/)) {const range=part.trim().split('-');add(range[0],range[1]||range[0]);}
 }
 return [...found].sort((a,b)=>a-b);
}
export function validMagnet(value){return /^magnet:\?[^\s]*xt=urn:btih:(?:[0-9a-f]{40}|[a-z2-7]{32})(?:&|$)/i.test(String(value||'').trim());}
export async function searchResultMagnet(result,resolve){
 if(validMagnet(result?.magnet))return result.magnet.trim();
 if(!result?.link)throw new Error('У этой раздачи нет ссылки для загрузки. Выберите другую.');
 const resolved=await resolve(result.link);
 if(!validMagnet(resolved?.magnet))throw new Error('Сервер не смог получить magnet-ссылку этой раздачи. Попробуйте другую.');
 return resolved.magnet.trim();
}

export function queueTitle(release,fallback='Раздача'){
 const clean=value=>[...queryTitle(value).replace(/[^\p{L}\p{N}_ .()-]/gu,' ').replace(/\s+/g,' ').trim().replace(/^[ .]+|[ .]+$/g,'')].slice(0,100).join('').trim().replace(/[ .]+$/g,'');
 return clean(release)||clean(fallback)||'Раздача';
}
export function sortReleases(results,order=''){
 const indexed=results.map((r,i)=>({r,i}));if(!['size-asc','size-desc'].includes(order))return indexed;
 const size=r=>Number.isFinite(Number(r.size))&&Number(r.size)>0?Number(r.size):null;
 return indexed.sort((a,b)=>{const x=size(a.r),y=size(b.r);if(x===null||y===null)return x===y?a.i-b.i:x===null?1:-1;return (order==='size-asc'?x-y:y-x)||a.i-b.i;});
}

export function episodeView(ep){
 const status=String(ep.status||'').toUpperCase();
 const fileRemoved=ep.isRemoved===true||status==='REMOVED'||(status==='PLAYED'&&ep.isPublished===false);
 const [label,color]=({COMPLETED:['Готово','green'],DOWNLOADING:['Загрузка','blue'],WAITING_LIMIT:['В очереди','amber'],PLAYED:['Просмотрено',''],SKIPPED:['Пропущено',''],REMOVED:['Удалено','']})[status]||['Ожидается',''];
 return {label,color,fileRemoved,canDelete:!fileRemoved&&status!=='SKIPPED'&&(Number(ep.progress)>0||ep.isPublished===true)};
}
