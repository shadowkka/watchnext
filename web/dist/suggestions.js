export function createPersonSuggestions({search,onChange,delay=350,setTimer=setTimeout,clearTimer=clearTimeout}){
 let state={query:'',items:[],busy:false,error:'',active:-1},timer,controller,version=0;
 const emit=()=>onChange({...state});
 function cancel(){version++;clearTimer(timer);controller?.abort();state={...state,items:[],busy:false,error:'',active:-1};emit();}
 function update(value){cancel();const query=String(value||'').trim();state={...state,query};if(query.length<2){emit();return;}const current=version;state.busy=true;emit();timer=setTimer(async()=>{controller=new AbortController();try{const data=await search(query,controller.signal);if(current!==version)return;state={...state,items:(data.people||[]).slice(0,6),busy:false};}catch(err){if(current!==version)return;state={...state,items:[],busy:false,error:'Подсказки недоступны. Нажмите «Найти персону».'};}if(current===version)emit();},delay);}
 function move(delta){if(!state.items.length)return;state={...state,active:state.active<0?(delta<0?state.items.length-1:0):(state.active+delta+state.items.length)%state.items.length};emit();}
 return {update,cancel,move,getState:()=>state,selected:()=>state.items[state.active]};
}
