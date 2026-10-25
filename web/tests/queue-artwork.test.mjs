import test from 'node:test';
import assert from 'node:assert/strict';
import {artworkIdentity,createQueueArtwork} from '../queue-artwork.mjs';
import {createTMDB} from '../tmdb.mjs';
const hash='a'.repeat(40),pack={kind:'MOVIE',title:'Игра',episodes:[{name:'The.Game.1997.Open.Matte.1080p.mkv'}]};
test('queue artwork retains original filename year and library provider IDs',()=>{
 assert.deepEqual(artworkIdentity(pack),{kind:'movie',year:1997,tmdbId:'',imdbId:'',titles:['Игра','The Game']});
 assert.equal(artworkIdentity(pack,{tmdbId:'2649',imdbId:'tt0119174'}).imdbId,'tt0119174');
});
test('exact ID avoids title search and rejects conflicting IMDb identity',async()=>{
 const calls=[];const tmdb=createTMDB({token:'synthetic',fetchJSON:async url=>{calls.push(url);return Response.json({id:2649,title:'Игра',release_date:'1997-09-12',external_ids:{imdb_id:'tt0119174'}});}});
 assert.equal((await tmdb.identify({tmdbId:'2649',imdbId:'tt0119174'})).year,1997);
 assert.equal(calls.length,1);assert.ok(calls[0].includes('/movie/2649?'));
 assert.equal(await tmdb.identify({tmdbId:'2649',imdbId:'tt1234567'}),null);
});
test('matching checks year and refuses multiple exact candidates',async()=>{
 let ambiguous=false;const tmdb=createTMDB({token:'synthetic',fetchJSON:async url=>{
 if(url.includes('/search/')){assert.equal(new URL(url).searchParams.get('year'),'1997');return Response.json({results:[{id:1,title:'Игра',release_date:'2011-01-01'},{id:2649,title:'Игра',original_title:'The Game',release_date:'1997-09-12'},...(ambiguous?[{id:2,title:'Игра',release_date:'1997-01-01'}]:[])]});}
 return Response.json({id:2649,title:'Игра',release_date:'1997-09-12',external_ids:{imdb_id:'tt0119174'}});
 }});
 assert.equal((await tmdb.identify({titles:['Игра','The Game'],year:1997})).imdb_id,'tt0119174');
 ambiguous=true;assert.equal(await tmdb.identify({titles:['Игра'],year:1997}),null);
});
test('queue resolution deduplicates requests, registers IDs and retries transient errors',async()=>{
 let calls=0,fail=true,registered;
 const art=createQueueArtwork({tmdb:{enabled:true,identify:async identity=>{calls++;if(fail)throw Error('offline');assert.equal(identity.year,1997);return {imdb_id:'tt0119174',tmdb_id:'2649',title:'Игра',year:1997};}},registerMovies:async rows=>{registered=rows;return [{...rows[0],poster:'/api/discover/poster/tt0119174'}];}});
 const read=async route=>route.startsWith('/api/packs/')?pack:{available:false};
 await assert.rejects(()=>art.resolve(hash,read));fail=false;
 const [a,b]=await Promise.all([art.resolve(hash,read),art.resolve(hash,read)]);
 assert.deepEqual(a,b);assert.equal(calls,2);assert.equal(a.poster,'/api/discover/poster/tt0119174');assert.equal(registered[0].tmdb_id,'2649');
 await art.resolve(hash,read);assert.equal(calls,2);await assert.rejects(()=>art.resolve('../invalid',read));
});
