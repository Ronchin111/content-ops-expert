// Private stdin/stdout IPC. Caller must not persist or display media URLs.
import {BrowserBridge} from '../vendor/opencli-2.0.3/node_modules/@jackwener/opencli/dist/src/browser/bridge.js';
let input='';for await(const chunk of process.stdin)input+=chunk;
const {profile,url,noteId}=JSON.parse(input);
const parsed=new URL(url);
if(parsed.protocol!=='https:'||parsed.hostname!=='www.xiaohongshu.com'||parsed.username||parsed.password||parsed.port||!parsed.pathname.endsWith('/'+noteId)||!parsed.searchParams.get('xsec_token')){
 console.log(JSON.stringify({ok:false,code:'SIGNED_SELECTED_URL_REQUIRED'}));process.exit(1);
}
const browser=new BrowserBridge();
try{
 const page=await browser.connect({session:'payathon-signed-media',contextId:profile,surface:'browser'});
 await page.goto(url);await page.wait({time:2});
 const data=await page.evaluate(`(()=>{
  const id=${JSON.stringify(noteId)};
  const body=document.body?.innerText||'';
  if(/安全限制|访问链接异常|website-login\\/error|error_code=300017|error_code=300031/.test(body+' '+location.href))return {ok:false,code:'SECURITY_BLOCK'};
  if(/登录后查看|请登录/.test(body))return {ok:false,code:'AUTH_REQUIRED'};
  if(!location.pathname.endsWith('/'+id))return {ok:false,code:'NOTE_ID_MISMATCH'};
  const entry=window.__INITIAL_STATE__?.note?.noteDetailMap?.[id];
  const note=entry?.note||entry;
  if(note?.noteId&&note.noteId!==id)return {ok:false,code:'NOTE_ID_MISMATCH'};
  const media=[];
  const add=(type,url)=>{if(typeof url==='string'&&url.startsWith('https://')&&!media.some(x=>x.url===url))media.push({type,url});};
  const v=note?.video;
  const key=v?.url||v?.originVideoKey||v?.consumer?.originVideoKey;
  if(typeof key==='string')add('video',key.startsWith('https://')?key:'https://sns-video-bd.xhscdn.com/'+key);
  for(const x of v?.media?.stream?.h264||[])add('video',x.masterUrl);
  for(const x of note?.imageList||[])add('image',x.urlDefault||x.urlPre||x.url);
  if(!media.some(x=>x.type==='video'))for(const el of document.querySelectorAll('#noteContainer video, #noteContainer video source'))add('video',el.currentSrc||el.src);
  return {ok:true,noteId:id,media};
 })()`);
 console.log(JSON.stringify(data));
}catch{console.log(JSON.stringify({ok:false,code:'MEDIA_PAGE_READ_FAILED'}));}
finally{await browser.close().catch(()=>{});}
