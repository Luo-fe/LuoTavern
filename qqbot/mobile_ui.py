# -*- coding: utf-8 -*-
"""手机端页面（远程访问用）：一次自包含，无外部依赖，无需单独部署。

- LOGIN_HTML：远程访问的登录页（**不含 token**，只提示输入访问令牌）
- MOBILE_PAGE_HTML：手机端主页（/m）——QQ 二维码 + 聊天流 + 发送 + 状态/停启/重启

设计要点：
- 令牌占位符 `__ADMIN_TOKEN__` 由服务端替换：通过鉴权的远程请求才注入真实 token
- 全部请求走同源 fetch + `X-Admin-Token` 头，不依赖第三方 CDN（离线可用）
- 单手机也能扫码：长按二维码保存到相册 → QQ「扫一扫 → 相册」选图
"""

LOGIN_HTML = """<!doctype html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>LuoTavern 远程访问</title><style>
body{margin:0;background:#0b1020;color:#e6ebff;
font:16px/1.6 system-ui,-apple-system,"Segoe UI",sans-serif;
display:flex;min-height:100vh;align-items:center;justify-content:center;padding:24px}
.card{background:#151c33;border:1px solid #27304d;border-radius:16px;padding:22px;
max-width:420px;width:100%}
h1{font-size:18px;margin:0 0 6px}p{color:#93a0c8;margin:0 0 16px;font-size:14px}
code{background:#0e1428;padding:1px 5px;border-radius:6px}
input{width:100%;box-sizing:border-box;padding:12px;border-radius:10px;
border:1px solid #33406a;background:#0e1428;color:#e6ebff;font-size:16px}
button{margin-top:14px;width:100%;padding:12px;border:0;border-radius:10px;
background:#4f7cff;color:#fff;font-size:16px;font-weight:600}
.hint{color:#6f7ca3;font-size:12px;margin-top:12px}
</style></head><body><div class="card">
<h1>需要访问令牌</h1>
<p>这台设备不是本机。请在电脑上打开管理台 → 设置 → 访问令牌，或复制项目里
<code>data/admin_token</code> 文件的内容，粘贴到下面。</p>
<input id="t" type="password" placeholder="访问令牌" autocomplete="off">
<button id="go">进入手机端</button>
<div class="hint">验证成功后该浏览器会记住 30 天。令牌等同账号权限，请勿外传。</div>
</div><script>
function go(){
  var t=(document.getElementById("t").value||"").trim();
  if(!t)return;
  location.href="/m?token="+encodeURIComponent(t);
}
document.getElementById("go").onclick=go;
document.getElementById("t").addEventListener("keydown",function(e){
  if(e.key==="Enter")go();
});
</script></body></html>"""


MOBILE_PAGE_HTML = """<!doctype html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>LuoTavern 手机端</title><style>
:root{--bg:#0b1020;--card:#151c33;--line:#27304d;--fg:#e6ebff;--dim:#93a0c8;
--ok:#37d67a;--warn:#ffb020;--err:#ff5b6e}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
font:15px/1.55 system-ui,-apple-system,"Segoe UI",sans-serif;padding:12px 12px 96px}
h2{font-size:16px;margin:0 0 8px}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;
padding:12px;margin-bottom:12px}
.row{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
button{padding:10px 14px;border:0;border-radius:10px;font-size:15px}
.btn{background:#26304f;color:#dfe6ff}.btn.p{background:#4f7cff;color:#fff}
.btn.d{background:#3a2130;color:#ffb4bf}
.badge{padding:3px 8px;border-radius:999px;font-size:12px;background:#22304f;color:var(--dim)}
.badge.ok{background:#12331f;color:var(--ok)}
.badge.warn{background:#3a2f12;color:var(--warn)}
.badge.err{background:#3a1b22;color:var(--err)}
#qr{background:#fff;border-radius:12px;padding:10px;width:min(70vw,260px);margin:8px auto}
#qr img{width:100%;display:block}
#chat{max-height:46vh;overflow:auto;display:flex;flex-direction:column;gap:8px}
.msg{background:#101733;border:1px solid var(--line);border-radius:12px;padding:8px 10px;
white-space:pre-wrap;word-break:break-word}
.msg .who{font-size:11px;color:var(--dim);margin-bottom:2px}
.msg.mine{background:#132a45}
#say{width:100%;min-height:64px;background:#0e1428;color:var(--fg);
border:1px solid var(--line);border-radius:12px;padding:10px;font-size:15px}
.bar{position:fixed;left:0;right:0;bottom:0;background:#0d1428;padding:10px 12px;
border-top:1px solid var(--line);display:flex;gap:8px}
.hint{color:var(--dim);font-size:12px;margin-top:6px}
</style></head><body>
<div class="card"><h2>状态 <span class="badge" id="st">…</span></h2>
<div class="row">
<button class="btn" id="b-start">启动机器人</button>
<button class="btn d" id="b-stop">停止机器人</button>
<button class="btn" id="b-refresh">刷新</button></div>
<div class="hint" id="st2"></div></div>

<div class="card"><h2>QQ 扫码登录</h2>
<div id="qr"><div class="hint" id="qr-msg">加载中…</div></div>
<div class="row">
<button class="btn p" id="b-qr">刷新二维码</button>
<button class="btn" id="b-restart">重启 NapCat</button></div>
<div class="hint" id="qr-note"></div>
<div class="hint">单手机扫码：长按二维码保存到相册 → 打开 QQ「扫一扫 → 相册」选这张图。</div>
</div>

<div class="card"><h2>聊天</h2><div id="chat"></div></div>

<div class="bar"><textarea id="say" placeholder="以主人身份给机器人发消息…"></textarea>
<button class="btn p" id="b-send">发送</button></div>

<script>
const TOKEN="__ADMIN_TOKEN__";
const q=(s)=>document.querySelector(s);
function hdrs(extra){return Object.assign({"X-Admin-Token":TOKEN},extra||{});}
async function api(path,opt){
  opt=opt||{};opt.headers=hdrs(opt.headers);
  const r=await fetch(path,opt);
  const j=await r.json().catch(()=>({}));
  if(!r.ok||j.ok===false)throw new Error(j.error||("HTTP "+r.status));
  return j.data!==undefined?j.data:j;
}
const apiPost=(p,b)=>api(p,{method:"POST",body:JSON.stringify(b||{})});
let SEQ=0;
function addMsg(m){
  const d=document.createElement("div");
  d.className="msg"+(m.kind==="input"?" mine":"");
  const who=document.createElement("div");
  who.className="who";who.textContent=(m.kind==="input"?"我":"机器人")+" · "+(m.ts||"");
  const body=document.createElement("div");body.textContent=m.text||"";
  d.appendChild(who);d.appendChild(body);
  q("#chat").appendChild(d);q("#chat").scrollTop=q("#chat").scrollHeight;
}
async function pollChat(){
  try{const rows=await api("/api/outbox?after="+SEQ);
    if(rows&&rows.length){SEQ=rows[rows.length-1].seq;rows.forEach(addMsg);}}catch(e){}
}
async function loadHistory(){
  try{const rows=await api("/api/outbox?after=0");
    if(rows&&rows.length){SEQ=rows[rows.length-1].seq;
      q("#chat").innerHTML="";rows.forEach(addMsg);
      if(SEQ>200){}/* 只保留最近：服务端 outbox 本身有上限 */}}catch(e){}
}
async function loadStatus(){
  try{
    const d=await api("/api/desktop/status");
    const run=d&&d.bot&&d.bot.running;
    const b=q("#st");
    b.textContent=run?"运行中":((d&&d.bot&&(d.bot.pid||d.bot.running===false))?"已停止":"未运行");
    b.className="badge "+(run?"ok":"warn");
    let txt="";
    if(d&&d.project_root)txt+="项目："+d.project_root+"　";
    if(d&&d.napcat)txt+=(d.napcat.ready?"NapCat 就绪":"NapCat 未就绪");
    q("#st2").textContent=txt;
  }catch(e){const b=q("#st");b.textContent="无法获取状态";b.className="badge err";}
}
function fmtAgo(sec){
  if(sec==null)return "?";
  sec=Math.max(0,Math.floor(sec));
  if(sec<60)return sec+" 秒";
  if(sec<3600)return Math.floor(sec/60)+" 分钟";
  if(sec<86400)return Math.floor(sec/3600)+" 小时";
  return Math.floor(sec/86400)+" 天";
}
async function loadQr(){
  try{
    const d=await api("/api/desktop/napcat/login");
    const box=q("#qr"),note=q("#qr-note");
    note.textContent="";
    if(d&&d.logged_in){
      box.innerHTML='<div class="hint" style="color:var(--ok);text-align:center">已登录 QQ</div>';
      return;
    }
    if(d&&d.qrcode_png){
      const img=document.createElement("img");
      img.alt="二维码";img.src=d.qrcode_png;
      box.innerHTML="";box.appendChild(img);
    }else{
      box.innerHTML='<div class="hint" style="text-align:center">没有可用二维码（过期或未生成）</div>';
    }
    if(d&&d.kicked){
      note.textContent="上次被踢："+fmtAgo(d.kicked_ago)+"前（第三方协议登录态有寿命，约 2 小时）";
    }else if(d&&d.note){note.textContent=d.note;}
  }catch(e){q("#qr").innerHTML='<div class="hint">'+e.message+'</div>';}
}
q("#b-refresh").onclick=()=>{loadStatus();loadQr();loadHistory();};
q("#b-start").onclick=async()=>{
  try{await apiPost("/api/desktop/start",{});loadStatus();}catch(e){alert(e.message);}
};
q("#b-stop").onclick=async()=>{
  if(!confirm("确定停止机器人？"))return;
  try{await apiPost("/api/desktop/stop",{});loadStatus();}catch(e){alert(e.message);}
};
q("#b-qr").onclick=async()=>{
  try{await apiPost("/api/desktop/napcat/login/refresh",{});loadQr();}catch(e){alert(e.message);}
};
q("#b-restart").onclick=async()=>{
  if(!confirm("确定重启 NapCat？"))return;
  try{await apiPost("/api/desktop/napcat/restart",{});loadQr();}catch(e){alert(e.message);}
};
q("#b-send").onclick=async()=>{
  const t=q("#say").value.trim();if(!t)return;q("#say").value="";
  try{await apiPost("/api/desktop/chat",{text:t});pollChat();}catch(e){alert(e.message);}
};
setInterval(()=>{loadStatus();pollChat();},4000);
setInterval(loadQr,8000);
loadStatus();loadQr();loadHistory();
</script></body></html>"""
