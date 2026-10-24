import http from 'node:http';
import https from 'node:https';
import fs from 'node:fs';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {normalizeServer} from './dist/core.js';
import {createPicker} from './picker.mjs';
import {createExplore} from './explore.mjs';
import {createIntegrations} from './integrations.mjs';
import {createTMDB} from './tmdb.mjs';
const root=path.join(path.dirname(fileURLToPath(import.meta.url)),'dist');
const mime={'.html':'text/html; charset=utf-8','.css':'text/css; charset=utf-8','.js':'text/javascript; charset=utf-8','.svg':'image/svg+xml','.png':'image/png','.webmanifest':'application/manifest+json'};
const endpoint=/^\/api\/(?:status|limits|integrations|jellyfin\/link|search(?:\/(?:resolve|start|jobs\/[a-f0-9]{32}))?|shows\/search|queue\/(?:toggle-pause|pause|resume)|packs(?:\/add|\/[^/]+(?:\/(?:from|finish|retry)|\/episodes\/\d+)?)?|subscriptions(?:\/[^/]+(?:\/import)?)?)$/;
export function createGateway({base=process.env.API_BASE||'',token=process.env.API_TOKEN||'',certificate=process.env.TLS_CERT,key=process.env.TLS_KEY,pickerDataDir=process.env.PICKER_DATA_DIR,settingsFile=process.env.WEB_SETTINGS_FILE}={}){
 const target=base?normalizeServer(base):'';let picker,explore;const integrations=createIntegrations({file:settingsFile});const tmdb=createTMDB({token:integrations.token});
 const handler=async(req,res)=>{
  const origin=req.headers.origin;
  if(origin){try{if(new URL(origin).host!==req.headers.host){res.writeHead(403);res.end('Cross-origin requests are not allowed');return;}}catch{res.writeHead(403);res.end();return;}}
  const url=new URL(req.url,'http://local');
  if(url.pathname==='/web-config'){res.writeHead(200,{'Content-Type':'application/json','Cache-Control':'no-store'});res.end(JSON.stringify({gateway:!!target,tmdb:!!integrations.token()}));return;}
  if(url.pathname==='/api/web-settings'){
   if(!['GET','POST'].includes(req.method)){res.writeHead(405);res.end();return;}
   // A configured API credential is also required for integration administration.
   if(token&&req.headers.authorization!==`Bearer ${token}`){res.writeHead(401);res.end();return;}
   try{let result=integrations.publicConfig();if(req.method==='POST'){let body='',size=0;for await(const chunk of req){size+=chunk.length;if(size>8192)throw Error('Слишком большой запрос.');body+=chunk;}const parsed=JSON.parse(body);if(!parsed||typeof parsed!=='object'||Array.isArray(parsed))throw Error('Некорректный запрос.');result=await integrations.update(parsed);}res.writeHead(200,{'Content-Type':'application/json','Cache-Control':'no-store'});res.end(JSON.stringify(result));}catch(error){res.writeHead(400,{'Content-Type':'application/json','Cache-Control':'no-store'});res.end(JSON.stringify({detail:error.code?'Не удалось сохранить настройки на сервере.':error.message}));}return;
  }
  if(url.pathname.startsWith('/api/explore/')){
   const route=url.pathname.slice('/api/explore/'.length);if(req.method!=='GET'){res.writeHead(405);res.end();return;}
   try{picker ||= createPicker({dataDir:pickerDataDir,tmdb});explore ||= createExplore({dataDir:pickerDataDir,registerMovies:picker.registerMovies});let result;
    if(route==='people')result=await explore.search(url.searchParams.get('query'));
    else if(route==='collections')result={collections:explore.collections()};
    else if(/^person\/Q[1-9]\d{0,10}$/.test(route))result=await explore.person(route.split('/')[1]);
    else if(/^collection\/[a-z-]+$/.test(route))result=await explore.collection(route.split('/')[1]);
    else{res.writeHead(404);res.end();return;}
    res.writeHead(200,{'Content-Type':'application/json; charset=utf-8','Cache-Control':'no-store'});res.end(JSON.stringify(result));
   }catch(error){res.writeHead(400,{'Content-Type':'application/json; charset=utf-8','Cache-Control':'no-store'});res.end(JSON.stringify({detail:error.code?'Не удалось прочитать или сохранить каталог.':String(error.message).slice(0,300)}));}return;
  }
  if(url.pathname.startsWith('/api/discover/')){
   const route=url.pathname.slice('/api/discover/'.length);
   const posterMatch=route.match(/^poster\/(tt\d{5,12})$/);
   if(req.method==='GET'&&posterMatch){try{picker ||= createPicker({dataDir:pickerDataDir,tmdb});const image=await picker.poster(posterMatch[1]);res.writeHead(200,{'Content-Type':image.type,'Content-Length':image.buffer.length,'Cache-Control':'public, max-age=604800','X-Content-Type-Options':'nosniff'});res.end(image.buffer);}catch{res.writeHead(404,{'Cache-Control':'no-store'});res.end();}return;}

   if(!((req.method==='GET'&&['status','watched'].includes(route))||(req.method==='POST'&&['pick','details','watched','seen'].includes(route)))){res.writeHead(404);res.end();return;}
   try{let body={};if(req.method==='POST'){let size=0,chunks=[];for await(const chunk of req){size+=chunk.length;if(size>4*1024*1024){res.writeHead(413);res.end();return;}chunks.push(chunk);}try{body=JSON.parse(Buffer.concat(chunks).toString('utf8'));}catch{throw Error('Не удалось прочитать JSON.');}if(!body||typeof body!=='object'||Array.isArray(body))throw Error('Некорректный запрос.');}
    picker ||= createPicker({dataDir:pickerDataDir,tmdb});
    const result=route==='status'?await picker.status():route==='pick'?await picker.pick(body):route==='details'?await picker.details(body.ids):route==='seen'?await picker.seen(body.movie):req.method==='GET'?await picker.exportWatched():await picker.importWatched(body.rows);
    res.writeHead(200,{'Content-Type':'application/json; charset=utf-8','Cache-Control':'no-store','X-Content-Type-Options':'nosniff'});res.end(JSON.stringify(result));
   }catch(error){res.writeHead(400,{'Content-Type':'application/json; charset=utf-8','Cache-Control':'no-store'});res.end(JSON.stringify({detail:error.code?'Не удалось прочитать или сохранить каталог на мини-ПК.':String(error.message).slice(0,300)}));}return;
  }
  if(url.pathname.startsWith('/api/')){
   if(!target){res.writeHead(503,{'Content-Type':'application/json'});res.end(JSON.stringify({error:'Set API_BASE to the WatchNext server address'}));return;}
   if(!endpoint.test(url.pathname)||!['GET','POST','DELETE'].includes(req.method)){res.writeHead(404);res.end();return;}
   try{
    let body=undefined;
    if(['POST','DELETE'].includes(req.method)){const chunks=[];let size=0;for await(const chunk of req){size+=chunk.length;if(size>256*1024){res.writeHead(413);res.end();return;}chunks.push(chunk);}if(size)body=Buffer.concat(chunks);}
    const headers={Accept:'application/json'};
    if(body)headers['Content-Type']='application/json';
    if(token)headers.Authorization=`Bearer ${token}`;else if(req.headers.authorization)headers.Authorization=req.headers.authorization;
    const response=await fetch(target+url.pathname+url.search,{method:req.method,headers,body,redirect:'error',signal:AbortSignal.timeout(url.pathname.startsWith('/api/search')?42000:35000)});
    res.writeHead(response.status,{'Content-Type':'application/json; charset=utf-8','Cache-Control':'no-store','X-Content-Type-Options':'nosniff'});res.end(Buffer.from(await response.arrayBuffer()));
   }catch{res.writeHead(502,{'Content-Type':'application/json','Cache-Control':'no-store'});res.end(JSON.stringify({error:'WatchNext API is unavailable'}));}
   return;
  }
  if(!['GET','HEAD'].includes(req.method)){res.writeHead(405);res.end();return;}
  let name;try{name=decodeURIComponent(url.pathname);}catch{res.writeHead(400);res.end();return;}
  const file=path.resolve(root,'.'+(name==='/'?'/index.html':name));
  if(!file.startsWith(root+path.sep)){res.writeHead(403);res.end();return;}
  try{const stat=await fs.promises.stat(file);if(!stat.isFile())throw new Error();res.writeHead(200,{'Content-Type':mime[path.extname(file)]||'application/octet-stream','X-Content-Type-Options':'nosniff','Referrer-Policy':'no-referrer','Cache-Control':name.endsWith('.png')?'public, max-age=86400':'no-cache',...(name==='/sw.js'?{'Service-Worker-Allowed':'/'}:{})});if(req.method==='HEAD')res.end();else fs.createReadStream(file).pipe(res);}catch{res.writeHead(404);res.end('Not found');}
 };
 return certificate&&key?https.createServer({cert:fs.readFileSync(certificate),key:fs.readFileSync(key)},handler):http.createServer(handler);
}
if(process.argv[1]&&path.resolve(process.argv[1])===fileURLToPath(import.meta.url)){
 const host=process.env.HOST||'127.0.0.1',port=Number(process.env.PORT||8787);const server=createGateway();
 server.listen(port,host,()=>{console.log(`Local: ${process.env.TLS_CERT?'https':'http'}://${host==='0.0.0.0'?'localhost':host}:${port}`);console.log(process.env.API_BASE?'WatchNext API gateway enabled.':'Preview ready. Connect an HTTPS API in Settings, or set API_BASE for a local gateway.');});
}
