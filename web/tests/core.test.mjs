import test from 'node:test';import assert from 'node:assert/strict';import {bytes,numeric,normalizeServer,connectionProblem,queryTitle,escapeHTML,apiURL,apiError} from '../dist/core.js';
test('sizes keep number and unit together',()=>{assert.equal(bytes(16.5*1024**3),'16,5\u00a0ГБ');assert.equal(bytes(0),'0\u00a0Б');});
test('numbers validate range, decimals, integers and nonfinite input',()=>{assert.equal(numeric('16,5',0,100),16.5);for(const invalid of ['NaN','Infinity','','999','2.5'])assert.throws(()=>numeric(invalid,1,30,true));});
test('API base normalization and mixed content guard',()=>{assert.equal(normalizeServer('http://192.168.2.11:8000/api/'),'http://192.168.2.11:8000');assert.equal(apiURL('','/api/packs'),'/api/packs');assert.throws(()=>normalizeServer('javascript:alert(1)'));assert.throws(()=>normalizeServer('https://user:secret@example.com'));assert.match(connectionProblem('http://192.168.1.1','https:'),/HTTPS/);});
test('untrusted titles are escaped',()=>{assert.equal(escapeHTML('<img src=x onerror="alert(1)">'),'&lt;img src=x onerror=&quot;alert(1)&quot;&gt;');});
test('release titles are normalized for posters',()=>{assert.equal(queryTitle('Криминальное чтиво (1994) BDRip 1080p | Дубляж'),'Криминальное чтиво');assert.equal(queryTitle('The.Big.Bang.Theory.S12E22.1080p'),'The Big Bang Theory');});

test('search failure exposes the actionable backend reason safely',()=>{assert.equal(apiError(502,{detail:'Трекеры не ответили: rutor.'}),'Трекеры не ответили: rutor.');assert.match(apiError(502,{}),/502/);assert.match(apiError(422,{detail:[]}),/422/);assert.match(apiError(401,{detail:'internal value'}),/токен/);});

test('episode delete actions follow file availability instead of historical progress',async()=>{
 const {episodeView}=await import('../dist/core.js');
 assert.deepEqual(episodeView({status:'PLAYED',progress:1,isPublished:false}),{label:'Просмотрено',color:'',fileRemoved:true,canDelete:false});
 assert.equal(episodeView({status:'PLAYED',progress:1,isPublished:true}).canDelete,true);
 assert.equal(episodeView({status:'COMPLETED',progress:1,isPublished:true}).canDelete,true);
 assert.equal(episodeView({status:'DOWNLOADING',progress:.4,isPublished:false}).canDelete,true);
 assert.equal(episodeView({status:'REMOVED',progress:1}).canDelete,false);
 assert.equal(episodeView({status:'SKIPPED',progress:1}).canDelete,false);
 assert.equal(episodeView({status:'WAITING_LIMIT',progress:0}).canDelete,false);
});
