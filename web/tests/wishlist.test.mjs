import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import {once} from 'node:events';
import {createWishlist} from '../wishlist.mjs';
import {createGateway} from '../server.mjs';
test('wishlist persists concurrent additions, deduplicates IDs, separates media kinds and removes selected work',async()=>{
 const dir=await fs.mkdtemp(path.join(os.tmpdir(),'wn-wish-'));
 try{const store=createWishlist({dataDir:dir});
 await Promise.all([store.add({title:'Example',imdb_id:'tt1234567',kind:'movie'}),store.add({title:'Example',kind:'series',year:2020}),store.add({title:'Example',kind:'movie',year:2020})]);
 await store.add({title:'Пример',imdb_id:'tt1234567',kind:'movie',magnet:'not-to-store',token:'private'});
 const rows=(await store.list()).movies;assert.equal(rows.length,3);assert.equal(rows[2].title,'Пример');assert.equal(rows[2].token,undefined);
 const restarted=createWishlist({dataDir:dir});assert.deepEqual(await restarted.list(),await store.list());
 await restarted.remove(rows[0].id);assert.equal((await restarted.list()).movies.length,2);assert.equal((await restarted.list()).movies.some(x=>x.id===rows[0].id),false);
 await assert.rejects(async()=>store.add({title:' '}));assert.throws(()=>store.remove('../../x'));
 }finally{await fs.rm(dir,{recursive:true,force:true});}
});
test('wishlist API rejects cross-origin writes, requires configured token and survives restart',async()=>{
 const dir=await fs.mkdtemp(path.join(os.tmpdir(),'wn-wish-api-'));let server;
 const start=async()=>{server=createGateway({base:'',pickerDataDir:dir,token:'fixture'});server.listen(0,'127.0.0.1');await once(server,'listening');return `http://127.0.0.1:${server.address().port}`;};
 try{let base=await start();const headers={Authorization:'Bearer fixture','Content-Type':'application/json'};
 assert.equal((await fetch(base+'/api/wishlist')).status,401);
 assert.equal((await fetch(base+'/api/wishlist',{method:'POST',headers:{...headers,Origin:'https://other.example'},body:'{}'})).status,403);
 assert.equal((await fetch(base+'/api/wishlist',{method:'POST',headers,body:'{"movie":{}}'})).status,400);
 const added=await(await fetch(base+'/api/wishlist',{method:'POST',headers,body:JSON.stringify({movie:{title:'Fixture',year:2020,kind:'series'}})})).json();assert.equal(added.movies.length,1);
 await new Promise(r=>server.close(r));base=await start();
 assert.equal((await(await fetch(base+'/api/wishlist',{headers})).json()).movies[0].kind,'series');
 const result=await(await fetch(base+'/api/wishlist',{method:'POST',headers,body:JSON.stringify({remove:added.movies[0].id})})).json();assert.equal(result.movies.length,0);
 }finally{if(server)await new Promise(r=>server.close(r));await fs.rm(dir,{recursive:true,force:true});}
});
