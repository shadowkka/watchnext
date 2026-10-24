import fs from 'node:fs';
import path from 'node:path';
export function createIntegrations({file=process.env.WEB_SETTINGS_FILE||path.resolve('data/integrations.json'),fetchJSON=fetch}={}){
 let data;try{data=JSON.parse(fs.readFileSync(file,'utf8'));}catch(err){if(err.code!=='ENOENT')throw err;data={};}
 const token=()=>Object.hasOwn(data,'tmdbToken')?data.tmdbToken:process.env.TMDB_READ_TOKEN||'';
 const publicConfig=()=>({tmdbConfigured:!!token(),jellyfinUrl:data.jellyfinUrl||process.env.JELLYFIN_PUBLIC_URL||'',pollSeconds:data.pollSeconds??10});
 async function apply(body){const next={...data};if(body.pollSeconds!==undefined){if(!Number.isInteger(body.pollSeconds)||body.pollSeconds<1||body.pollSeconds>30)throw Error('Интервал обновления должен быть целым числом от 1 до 30 секунд.');next.pollSeconds=body.pollSeconds;}if(body.tmdbToken!==undefined&&typeof body.tmdbToken!=='string')throw Error('Некорректный токен TMDB.');if(body.tmdbToken?.trim()){const value=body.tmdbToken.trim();if(value.length>2000||!/^[\w.-]+$/.test(value))throw Error('Укажите API Read Access Token TMDB.');let response;try{response=await fetchJSON('https://api.themoviedb.org/3/authentication',{headers:{Authorization:'Bearer '+value,Accept:'application/json'},signal:AbortSignal.timeout(8000)});}catch{throw Error('TMDB не ответил. Настройки не изменены.');}if(!response.ok)throw Error('TMDB отклонил токен. Настройки не изменены.');next.tmdbToken=value;}
 if(body.removeTmdb===true)next.tmdbToken='';
 if(body.jellyfinUrl!==undefined){const value=String(body.jellyfinUrl).trim();if(value){let u;try{u=new URL(value);}catch{throw Error('Укажите полный адрес Jellyfin.');}if(!['http:','https:'].includes(u.protocol)||u.username||u.password||u.search||u.hash)throw Error('Укажите адрес Jellyfin без пароля и параметров.');next.jellyfinUrl=u.href.replace(/\/$/,'');}else next.jellyfinUrl='';}
 await fs.promises.mkdir(path.dirname(file),{recursive:true,mode:0o700});const temporary=file+'.'+process.pid+'.tmp';await fs.promises.writeFile(temporary,JSON.stringify(next),{mode:0o600});await fs.promises.rename(temporary,file);data=next;return publicConfig();
 }
 let pending=Promise.resolve();function update(body){const job=pending.catch(()=>{}).then(()=>apply(body));pending=job;return job;}
 return {token,publicConfig,update};
}
