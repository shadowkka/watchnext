import {mediaKind,queryTitle} from './core.js';
const normalized=value=>String(value||'').toLocaleLowerCase().replaceAll('ё','е').replace(/[^\p{L}\p{N}]/gu,'');
const image=value=>{try{const u=new URL(value);return u.protocol==='https:'&&!u.username&&!u.password?u.href:'';}catch{return '';}};
const unique=values=>[...new Set(values.filter(Boolean))];
export function artworkQuery(title){
 const raw=String(title||'');
 const aliases=[...raw.matchAll(/\(([^()]*)\)/g)].map(m=>m[1]).filter(s=>/[a-z]/i.test(s)&&!/web|rip|1080|2160|season/i.test(s));
 return {titles:unique([raw.split(' / ')[0],...aliases,...raw.split(' / ').slice(1)].map(queryTitle).filter(t=>t.length>=2)),year:raw.match(/\b(?:19|20)\d{2}\b/)?.[0]};
}
export async function findArtwork(title,rawKind,json){
 const kind=mediaKind(rawKind),query=artworkQuery(title);if(!query.titles.length)return [];
 if(kind==='movie')for(const name of query.titles){
  const lang=/[а-яё]/i.test(name)?'ru':'en';
  const data=await json(`https://${lang}.wikipedia.org/api/rest_v1/page/summary/${encodeURIComponent(name.replaceAll(' ','_'))}`);
  const description=data?.description||'';
  if(data?.type==='standard'&&/фильм|\bfilm\b/i.test(description)&&!/сериал|series/i.test(description)&&normalized(data.title?.split(' (')[0])===normalized(name)&&(!query.year||`${description} ${data.extract||''}`.includes(query.year))){
   const url=image(data.thumbnail?.source);if(url)return [url];
  }
 }
 async function tvmaze(imdb){
  if(imdb){const show=await json(`https://api.tvmaze.com/lookup/shows?imdb=${imdb}`);const url=image(show?.image?.medium);if(url)return url;}
  const names=[...query.titles];
  for(const name of query.titles.filter(t=>/[а-яё]/i.test(t))){
   const params=new URLSearchParams({action:'query',format:'json',formatversion:'2',prop:'langlinks',lllang:'en',redirects:'1',origin:'*',titles:`${name}|${name} (телесериал)`});
   const wiki=await json(`https://ru.wikipedia.org/w/api.php?${params}`);
   const pages=wiki?.query?.pages;
   for(const page of Array.isArray(pages)?pages:[])for(const link of page.langlinks||[])if(link.title)names.push(link.title.split(' (')[0]);
  }
  for(const name of unique(names).filter(t=>/[a-z]/i.test(t))){
   const show=await json(`https://api.tvmaze.com/singlesearch/shows?q=${encodeURIComponent(name)}`);
   if(normalized(show?.name)===normalized(name)){const url=image(show?.image?.medium);if(url)return url;}
  }
  return '';
 }
 for(const name of query.titles){
  const data=await json(`https://v3-cinemeta.strem.io/catalog/${kind}/top/search=${encodeURIComponent(name)}.json`);
  const candidates=(Array.isArray(data?.metas)?data.metas:[]).filter(m=>m&&typeof m==='object'&&mediaKind(m.type||kind)===kind&&image(m.poster)&&(kind==='series'||!query.year||String(m.releaseInfo||'').startsWith(query.year)));
  const exact=candidates.filter(m=>query.titles.some(t=>normalized(t)===normalized(m.name)));
  const match=exact.length===1?exact[0]:candidates.length===1?candidates[0]:null;
  if(!match)continue;
  const id=match.imdb_id||match.id,imdb=/^tt\d+$/.test(id||'')?id:'';
  const tv=kind==='series'?await tvmaze(imdb):'';
  return unique([tv,image(match.poster),imdb?`https://images.metahub.space/poster/small/${imdb}/img`:'']);
 }
 return kind==='series'?unique([await tvmaze()]):[];
}
