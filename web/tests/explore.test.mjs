import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import {personRecord,workRecord,createExplore} from '../explore.mjs';
import {filterFilmography,randomSelection} from '../dist/explore.js';
import {matchesMovie,pickerFilters} from '../picker.mjs';
const claims=value=>({mainsnak:{datavalue:{value}}});
const human={id:'Q25191',labels:{ru:{value:'Кристофер Нолан'}},claims:{P31:[claims({id:'Q5'})]}};
const film={id:'Q100',labels:{ru:{value:'Начало'},en:{value:'Inception'}},claims:{P345:[claims('tt1375666')],P577:[claims({time:'+2010-07-16T00:00:00Z'})],P31:[claims({id:'Q11424'})]}};
test('source records require humans and valid IMDb works, distinguish series and omit episodes',()=>{
 assert.equal(personRecord({id:'Q9',claims:{}}),null);assert.equal(personRecord(human).name,'Кристофер Нолан');
 assert.equal(workRecord({...film,claims:{}}),null);assert.equal(workRecord(film,['director','director']).roles.length,1);
 assert.equal(workRecord({...film,claims:{...film.claims,P31:[claims({id:'Q21191270'})]}}),null);
 const series=workRecord({...film,claims:{...film.claims,P31:[claims({id:'Q5398426'})]}});assert.equal(series.kind,'series');
 assert.equal(matchesMovie({...series,rating:9,genres:[],countries:[]},pickerFilters({yearFrom:2000,yearTo:2020,rating:0,count:1})),false);
});
test('filmography filters combine role, type, inclusive years and Russian title search; sorting keeps unknown dates last',()=>{
 const rows=[{...workRecord(film,['director']),year:2010},{...workRecord(film,['actor']),title:'Ещё фильм',year:2001},{...workRecord(film,['actor']),year:null}];
 assert.deepEqual(filterFilmography(rows,{role:'director',kind:'movie',yearFrom:2010,yearTo:2010}).map(x=>x.year),[2010]);
 assert.equal(filterFilmography(rows,{query:'еще'}).length,1);assert.deepEqual(filterFilmography(rows,{sort:'oldest'}).map(x=>x.year),[2001,2010,null]);assert.equal(rows[0].year,2010);
});
test('filmography merges duplicate roles, persists cache and rejects injected identifiers before upstream calls',async()=>{
 const dir=await fs.mkdtemp(path.join(os.tmpdir(),'watchnext-explore-'));let calls=0;
 const json=async url=>{calls++;const u=new URL(url);if(u.hostname==='query.wikidata.org')return {results:{bindings:['director','actor'].map(role=>({work:{value:'http://www.wikidata.org/entity/Q100'},role:{value:role}}))}};if(u.searchParams.get('action')==='wbsearchentities')return {search:[{id:'Q25191'},{id:'Q9'}]};return {entities:u.searchParams.get('ids').includes('Q100')?{Q100:film}:{Q25191:human,Q9:{id:'Q9',claims:{}}}};};
 try{const api=createExplore({dataDir:dir,json});assert.equal((await api.search('Нолан')).people.length,1);const d=await api.person('Q25191');assert.deepEqual(d.movies[0].roles,['director','actor']);const previous=calls;const offline=createExplore({dataDir:dir,json:()=>{throw Error('offline');}});assert.equal((await offline.person('Q25191')).movies.length,1);await assert.rejects(api.person('Q1 } UNION {'),/Некорректный/);await assert.rejects(api.collection('arbitrary'),/не найдена/);assert.equal(calls,previous);}finally{await fs.rm(dir,{recursive:true,force:true});}
});

test('editorial collections expose provenance and correct ordered IMDb records without an upstream request',async()=>{
 const dir=await fs.mkdtemp(path.join(os.tmpdir(),'watchnext-editorial-'));
 try{const api=createExplore({dataDir:dir,json:()=>{throw Error('must not fetch');}});const summaries=api.collections();assert.equal(summaries.length,7);assert.ok(summaries.every(c=>c.group&&c.source&&c.url&&!c.movies));for(const [id,count]of [['bfi-scifi',10],['bfi-heist',10],['bfi-critics',20]]){const d=await api.collection(id);assert.equal(d.movies.length,count);assert.equal(new Set(d.movies.map(m=>m.imdb_id)).size,count);assert.deepEqual(d.movies.map(m=>m.rank),Array.from({length:count},(_,i)=>i+1));}const critics=await api.collection('bfi-critics');assert.equal(critics.movies[6].imdb_id,'tt0209933');assert.match(critics.movies[6].original_title,/Beau/);}finally{await fs.rm(dir,{recursive:true,force:true});}
});
test('popular feeds omit future, adult, duplicate, malformed and nonmovie records and retain source order offline',async()=>{
 const dir=await fs.mkdtemp(path.join(os.tmpdir(),'watchnext-popular-'));const base={id:'tt1375666',name:'Inception',type:'movie',year:'2010',imdbRating:'8.8'};try{const api=createExplore({dataDir:dir,json:async()=>({metas:[base,base,{...base,id:'tt0000001',adult:true},{...base,id:'tt0000002',year:'2099'},{...base,id:'tt0000003',type:'series'},{...base,id:'bad'},{...base,id:'tt0000004',name:'Another',imdbRating:''}]})});const d=await api.collection('popular-now');assert.deepEqual(d.movies.map(m=>m.imdb_id),['tt1375666','tt0000004']);assert.equal(d.movies[1].rating,null);const offline=createExplore({dataDir:dir,json:()=>{throw Error('offline');}});assert.equal((await offline.collection('popular-now')).movies.length,2);}finally{await fs.rm(dir,{recursive:true,force:true});}
});

test('random three uses unique filtered unwatched candidates and respects lists smaller than three',()=>{const rows=[{imdb_id:'tt00001',title:'One',year:2001,rank:2,watched:true},{imdb_id:'tt00002',title:'Two',year:2002,rank:1},{imdb_id:'tt00003',title:'Three',year:2003,rank:3}];const eligible=filterFilmography(rows,{excludeWatched:true,sort:'source'});assert.deepEqual(eligible.map(m=>m.imdb_id),['tt00002','tt00003']);assert.equal(randomSelection([...eligible,...eligible],3,()=>0).length,2);assert.equal(randomSelection([],3).length,0);assert.equal(rows.length,3);});
