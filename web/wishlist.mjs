import fs from 'node:fs/promises';
import path from 'node:path';
import {createHash} from 'node:crypto';
const norm=s=>s.normalize('NFKC').toLocaleLowerCase('ru').replaceAll('ё','е').replace(/[^\p{L}\p{N}]/gu,'');
export function wishlistMovie(raw){
 if(!raw||typeof raw!=='object'||Array.isArray(raw))throw Error('Нужна карточка фильма или сериала.');
 const title=String(raw.title||'').trim().slice(0,300),kind=raw.kind==='series'?'series':'movie';
 if(!norm(title))throw Error('Укажите название.');
 const year=Number(raw.year),imdb_id=/^tt\d{5,12}$/.test(raw.imdb_id||'')?raw.imdb_id:'',tmdb_id=/^\d{1,12}$/.test(String(raw.tmdb_id||''))?String(raw.tmdb_id):'';
 const movie={title,kind,year:Number.isInteger(year)&&year>=1870&&year<=2100?year:null,imdb_id,tmdb_id};
 movie.id=createHash('sha256').update(imdb_id|| (tmdb_id?kind+':tmdb:'+tmdb_id:kind+':'+norm(title)+':'+movie.year)).digest('hex').slice(0,24);
 return movie;
}
export function createWishlist({dataDir=process.env.PICKER_DATA_DIR||path.resolve('data/picker')}={}){
 const file=path.join(dataDir,'wishlist.json');let rows=[],queue=Promise.resolve();
 const ready=(async()=>{await fs.mkdir(dataDir,{recursive:true});try{const data=JSON.parse(await fs.readFile(file,'utf8'));if(!Array.isArray(data))throw Error('Не удалось прочитать список.');rows=data;}catch(e){if(e.code!=='ENOENT')throw e;}})();ready.catch(()=>{});
 function change(fn){const job=queue.catch(()=>{}).then(async()=>{await ready;const next=fn(rows);if(next.length>5000)throw Error('В списке уже 5000 записей.');await fs.writeFile(file+'.tmp',JSON.stringify(next),{mode:0o600});await fs.rename(file+'.tmp',file);rows=next;return {movies:rows};});queue=job;return job;}
 return {
  async list(){await ready;await queue.catch(()=>{});return {movies:rows};},
  add(raw){const m=wishlistMovie(raw);return change(current=>{const old=current.find(x=>x.id===m.id||(!x.imdb_id&&!x.tmdb_id&&x.kind===m.kind&&x.year===m.year&&norm(x.title)===norm(m.title)));return old?current.map(x=>x===old?{...x,...m,id:x.id}:x):[{...m,addedAt:new Date().toISOString()},...current];});},
  remove(id){if(!/^[a-f0-9]{24}$/.test(id||''))throw Error('Некорректная запись.');return change(current=>current.filter(x=>x.id!==id));}
 };
}
