// Private IPC. Open only the supplied link in an owned tab of the authorized Chrome.
import {BrowserBridge} from '../vendor/opencli-2.0.3/node_modules/@jackwener/opencli/dist/src/browser/bridge.js';
// Page inspection: only the selected detail entry is read, never other tabs or cookies.
function inspectSelectedNote(expected){
  const path=location.pathname.match(/^\/(?:explore|discovery\/item)\/([a-f0-9]{24})\/?$/i);
  if(['xhslink.com','www.xhslink.com','xhslink.cn','www.xhslink.cn'].includes(location.hostname))return {code:'SELECTED_NOTE_PAGE_NOT_READY'};
  if(location.hostname!=='www.xiaohongshu.com')return {code:'UNEXPECTED_LINK_DESTINATION_STOPPED'};
  const body=document.body?.innerText||'';
  if(/\/login|\/passport/.test(location.pathname)||/登录后查看|请先登录/.test(body))return {code:'AUTH_REQUIRED_STOPPED'};
  if(/安全限制|访问链接异常|请完成验证|验证码/.test(body))return {code:'ACCESS_RESTRICTED_STOPPED'};
  if(/找不到相符的内容|笔记不存在|内容不存在|暂时无法浏览/.test(body))return {code:'CONTENT_UNAVAILABLE_STOPPED'};
  if(!path)return {code:'SELECTED_NOTE_PAGE_NOT_READY'};
  const selected=path[1].toLowerCase();
  if(expected&&selected!==expected)return {code:'RETURNED_WORK_ID_MISMATCH'};
  const state=window.__INITIAL_STATE__?.note;
  const entry=(state?.noteDetailMap||state?.note||{})[selected];
  const note=entry?.note||entry;
  if(!note)return {code:'SELECTED_NOTE_PAGE_NOT_READY'};
  if(note.noteId&&note.noteId!==selected)return {code:'RETURNED_WORK_ID_MISMATCH'};
  const result={code:'SELECTED_NOTE_READ',note_id:selected,note_type:note.type||null,
   title:String(note.title||''),author:String(note.user?.nickname||note.user?.nickName||''),media:[]};
  if(result.note_type!=='video')return result;
  const add=value=>{try{
   if(typeof value!=='string')return;
   const u=new URL(value);if(u.protocol==='http:')u.protocol='https:';
   if(u.protocol==='https:'&&!u.username&&!u.password&&!u.port&&u.hostname.endsWith('.xhscdn.com')&&!result.media.includes(u.href))result.media.push(u.href);
  }catch{}};
  const v=note.video;
  // A default web stream may be video-only. Prefer a declared muxed audio/video
  // stream; keep legacy candidates as fallbacks when audio metadata is absent.
  const streams=Object.values(v?.media?.stream||{}).flatMap(items=>Array.isArray(items)?items:[]);
  for(const s of streams)if(Number(s.audioChannels)>0&&Number(s.audioBitrate)>0){add(s.masterUrl);for(const u of s.backupUrls||[])add(u);}
  const key=v?.consumer?.originVideoKey||v?.originVideoKey;
  if(typeof key==='string')add(/^https?:/.test(key)?key:'https://sns-video-bd.xhscdn.com/'+key);
  add(v?.url);
  // Stream names vary: the verified note uses EF4 rather than h264.
  for(const streams of Object.values(v?.media?.stream||{}))if(Array.isArray(streams))for(const s of streams){add(s.masterUrl);for(const u of s.backupUrls||[])add(u);}
  for(const el of document.querySelectorAll('#noteContainer video, #noteContainer video source, .note-detail-mask video'))add(el.currentSrc||el.src);
  return result;
}

// Browser orchestration: a fresh owned tab keeps the user's current tab untouched.
async function openSelectedLink(page,url,expected,delay=ms=>new Promise(resolve=>setTimeout(resolve,ms))){
 const owned=await page.newTab(); // Extension creates its own empty tab; explicit about: URLs are rejected.
 if(!owned)throw new Error('OWNED_TAB_ID_REQUIRED');
 let keepOpen=false;
 try{
  await page.selectTab(owned);
  await page.goto(url); // Original parameters are passed unchanged; no stripped display URL.
  let result;
  for(let i=0;i<8;i++){
   result=await page.evaluate(inspectSelectedNote,expected);
   if(result.code!=='SELECTED_NOTE_PAGE_NOT_READY'&&!(result.code==='SELECTED_NOTE_READ'&&result.note_type==='video'&&!result.media.length))break;
   if(i<7)await delay(500);
  }
  if(result.code==='SELECTED_NOTE_PAGE_NOT_READY')result={code:'SELECTED_NOTE_LOAD_TIMEOUT_STOPPED'};
  keepOpen=['AUTH_REQUIRED_STOPPED','ACCESS_RESTRICTED_STOPPED'].includes(result.code);
  return {...result,navigation:'agent_opened_selected_link',user_manual_open_required:false};
 }finally{
  if(!keepOpen)await page.closeTab(owned).catch(()=>{});
 }
}

// Private runner: input and media URLs travel only through captured stdin/stdout IPC.
const bridge=new BrowserBridge();
try{
 let text='';for await(const chunk of process.stdin){text+=chunk;if(text.length>65536)throw new Error('INPUT_LIMIT');}
 const input=JSON.parse(text),parsed=new URL(input.url);
 const match=parsed.pathname.match(/^\/(?:explore|discovery\/item)\/([a-f0-9]{24})\/?$/i);
 const full=parsed.hostname==='www.xiaohongshu.com'&&match;
 const short=['xhslink.com','www.xhslink.com','xhslink.cn','www.xhslink.cn'].includes(parsed.hostname)&&parsed.pathname!=='/';
 if(parsed.protocol!=='https:'||(!full&&!short)||!input.contextId||parsed.username||parsed.password||parsed.port){
  console.log(JSON.stringify({code:'SELECTED_LINK_AND_AUTHORIZED_CONTEXT_REQUIRED'}));process.exitCode=2;
 }else{
  const page=await bridge.connect({session:'content-ops-selected-link',contextId:input.contextId,surface:'browser',windowMode:'background'});
  const result=await openSelectedLink(page,input.url,match?.[1].toLowerCase()||null);
  console.log(JSON.stringify(result));
  if(result.code!=='SELECTED_NOTE_READ')process.exitCode=2;
 }
}catch(e){console.log(JSON.stringify({code:'BROWSER_READ_FAILED',error_name:e?.name||'Error',bridge_error_code:e?.errorCode||e?.code||null}));process.exitCode=2;}
finally{await bridge.close().catch(()=>{});}
