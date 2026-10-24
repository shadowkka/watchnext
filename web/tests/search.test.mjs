import test from 'node:test';import assert from 'node:assert/strict';
import {releaseSeasons,searchResultMagnet} from '../dist/core.js';
test('season filter includes ranges and lists without confusing episode counts, dates or quality',()=>{
 for(const title of ['Сезоны: 1-12 / E1-280 of 280','[S01-12 + Special]','S01–S12','Сезоны 1–12'])assert.ok(releaseSeasons(title).includes(6),title);
 assert.deepEqual(releaseSeasons('S6E1-24 of 24 (2018) 1080p'),[6]);
 assert.deepEqual(releaseSeasons('Сезон: 6 / Серии: 1-24'),[6]);
 assert.deepEqual(releaseSeasons('Сезоны 1, 3 и 6'),[1,3,6]);
 assert.deepEqual(releaseSeasons('S01, S03, S06'),[1,3,6]);
 assert.ok(!releaseSeasons('S07E1-24').includes(6));
 assert.deepEqual(releaseSeasons('Сериал (2007-2019) 1080p'),[]);
 assert.deepEqual(releaseSeasons('S12-1 S1-9999'),[]);
});
const magnet='magnet:?xt=urn:btih:'+'a'.repeat(40);
test('search additions resolve torrent links and preserve existing magnets',async()=>{
 let calls=0;assert.equal(await searchResultMagnet({magnet},()=>{calls++;}),magnet);assert.equal(calls,0);
 assert.equal(await searchResultMagnet({magnet:'',link:'http://jackett:9117/dl/tracker/test'},async link=>{calls++;assert.match(link,/\/dl\//);return {magnet};}),magnet);assert.equal(calls,1);
 await assert.rejects(searchResultMagnet({link:'test'},async()=>({magnet:'bad'})),/не смог/);
 await assert.rejects(searchResultMagnet({},async()=>({magnet})),/нет ссылки/);
 await assert.rejects(searchResultMagnet({link:'test'},async()=>{throw new Error('Трекер не ответил');}),/Трекер не ответил/);
});

test('queue title strips release metadata and respects backend limit',async()=>{
 const {queueTitle}=await import('../dist/core.js');
 const release='Теория большого взрыва / The Big Bang Theory / S12E1-24 (24) + Special (Марк Сендроуски, Энтони Джозеф Рич, Питер Чакос) [2018-2019, США, Мелодрама, комедия, WEB-DL 1080p] VO (kurazh) + Original + Sub (Rus, Eng)';
 assert.equal(queueTitle(release),'Теория большого взрыва');assert.equal(queueTitle('The.Big.Bang.Theory.S12E22.1080p'),'The Big Bang Theory');
 assert.equal([...queueTitle('Я'.repeat(150))].length,100);assert.equal(queueTitle('...','Сериал'),'Сериал');assert.equal(queueTitle('...','...'),'Раздача');
});
test('size sort keeps original result identity and puts unknown sizes last',async()=>{
 const {sortReleases}=await import('../dist/core.js');const raw=[{size:50},{size:10},{size:50},{size:0},{size:null},{size:'bad'}];
 assert.deepEqual(sortReleases(raw,'size-asc').map(x=>x.i),[1,0,2,3,4,5]);assert.deepEqual(sortReleases(raw,'size-desc').map(x=>x.i),[0,2,1,3,4,5]);assert.deepEqual(sortReleases(raw).map(x=>x.i),[0,1,2,3,4,5]);assert.equal(sortReleases(raw,'size-asc')[0].r,raw[1]);assert.equal(raw[0].size,50);
});
