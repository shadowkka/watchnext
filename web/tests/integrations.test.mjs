import test from 'node:test';import assert from 'node:assert/strict';import fs from 'node:fs/promises';import os from 'node:os';import path from 'node:path';
import {createIntegrations} from '../integrations.mjs';import {createTMDB} from '../tmdb.mjs';import {jellyfinBase,jellyfinItemURL} from '../dist/integrations.js';
test('integration tokens validate before persistence, survive restart and never appear in public configuration',async()=>{const dir=await fs.mkdtemp(path.join(os.tmpdir(),'watchnext-settings-'));try{const file=path.join(dir,'settings.json');let ok=false;const settings=createIntegrations({file,fetchJSON:async()=>({ok})});await assert.rejects(()=>settings.update({tmdbToken:'synthetic-token',jellyfinUrl:'http://media.local'}),/отклонил/);await assert.rejects(()=>fs.stat(file));ok=true;await settings.update({tmdbToken:'synthetic-token',jellyfinUrl:'http://media.local',pollSeconds:10,theme:'graphite'});assert.equal(settings.token(),'synthetic-token');assert.deepEqual(settings.publicConfig(),{tmdbConfigured:true,jellyfinUrl:'http://media.local',pollSeconds:10,theme:'graphite'});assert.equal((await fs.stat(file)).mode&0o777,0o600);assert.equal(createIntegrations({file}).token(),'synthetic-token');await settings.update({tmdbToken:''});assert.equal(settings.token(),'synthetic-token');await assert.rejects(()=>settings.update({jellyfinUrl:'javascript:alert(1)'}));assert.equal(settings.publicConfig().jellyfinUrl,'http://media.local');await settings.update({removeTmdb:true});assert.equal(settings.token(),'');}finally{await fs.rm(dir,{recursive:true,force:true});}});
test('TMDB reads changed token at runtime without restarting picker',async()=>{let value='';const tmdb=createTMDB({token:()=>value,fetchJSON:async(url,options)=>{assert.equal(options.headers.Authorization,'Bearer replacement');return {ok:true,json:async()=>({id:123})};}});assert.equal(tmdb.enabled,false);value='replacement';assert.equal(tmdb.enabled,true);assert.equal((await tmdb.movie(123)).id,123);value='';assert.equal(tmdb.enabled,false);});
test('Jellyfin links follow local domain or IP and point to exact library item without credentials',()=>{assert.equal(jellyfinBase('',{hostname:'watchnext.local',protocol:'http:'}),'http://jellyfin.local');assert.equal(jellyfinBase('',{hostname:'192.168.2.11',protocol:'http:'}),'http://192.168.2.11:8096');assert.equal(jellyfinItemURL('http://media.local',{available:true,itemId:'episode123',serverId:'server123'}),'http://media.local/web/index.html#!/details?id=episode123&serverId=server123');});
test('concurrent token and URL saves preserve both changes',async()=>{const dir=await fs.mkdtemp(path.join(os.tmpdir(),'watchnext-parallel-settings-'));try{let release;const gate=new Promise(r=>release=r);const settings=createIntegrations({file:path.join(dir,'settings.json'),fetchJSON:async()=>{await gate;return {ok:true};}});const a=settings.update({tmdbToken:'synthetic-token'});const b=settings.update({jellyfinUrl:'http://media.local',pollSeconds:10});release();await Promise.all([a,b]);assert.deepEqual(settings.publicConfig(),{tmdbConfigured:true,jellyfinUrl:'http://media.local',pollSeconds:10,theme:'graphite'});}finally{await fs.rm(dir,{recursive:true,force:true});}});

test('global polling interval persists across restart and independent clients without changing secrets',async()=>{
 const dir=await fs.mkdtemp(path.join(os.tmpdir(),'watchnext-poll-'));
 try {
  const file=path.join(dir,'settings.json');
  const first=createIntegrations({file,fetchJSON:async()=>({ok:true})});
  await first.update({tmdbToken:'synthetic-token',jellyfinUrl:'http://media.local'});
  assert.equal(first.publicConfig().pollSeconds,10);
  await first.update({pollSeconds:3});
  const second=createIntegrations({file});
  assert.equal(second.publicConfig().pollSeconds,3);
  assert.equal(second.token(),'synthetic-token');
  assert.equal(second.publicConfig().jellyfinUrl,'http://media.local');
  for(const pollSeconds of [0,31,1.5,'5',null])await assert.rejects(()=>first.update({pollSeconds}));
  assert.equal(createIntegrations({file}).publicConfig().pollSeconds,3);
  await first.update({jellyfinUrl:'http://new-media.local'});
  assert.equal(createIntegrations({file}).publicConfig().pollSeconds,3);
 } finally {await fs.rm(dir,{recursive:true,force:true});}
});

test('theme persists on the server, rejects unknown values, and survives unrelated updates',async()=>{
 const dir=await fs.mkdtemp(path.join(os.tmpdir(),'watchnext-theme-'));
 try {
  const file=path.join(dir,'settings.json');
  const settings=createIntegrations({file,fetchJSON:async()=>({ok:true})});
  await settings.update({tmdbToken:'synthetic-token',theme:'light',pollSeconds:3});
  assert.equal(createIntegrations({file}).publicConfig().theme,'light');
  await Promise.all([settings.update({theme:'midnight'}),settings.update({jellyfinUrl:'http://media.local'})]);
  const restored=createIntegrations({file});
  assert.equal(restored.publicConfig().theme,'midnight');
  assert.equal(restored.publicConfig().pollSeconds,3);
  assert.equal(restored.token(),'synthetic-token');
  for(const theme of ['unknown','',null,{},42])await assert.rejects(()=>settings.update({theme}));
  assert.equal(settings.publicConfig().theme,'midnight');
  await settings.update({theme:'system'});
  assert.equal(createIntegrations({file}).publicConfig().theme,'system');
 }finally{await fs.rm(dir,{recursive:true,force:true});}
});
