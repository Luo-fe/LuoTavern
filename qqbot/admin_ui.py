# -*- coding: utf-8 -*-
"""LuoTavern 管理台全新界面（单页内嵌 HTML/CSS/JS，离线可用）。

设计综合「启动台 + 运营控制台 + 首次向导」三种思路：
- 首页启动台：人格铭牌 + 平台选择 + 一键启动/停止 + 就绪检查 + 实时日志
- 侧边导航：人格 / 机器人 / 密钥 / 设置 / 记忆 / 世界书 / 用量等
- 快速设置向导：环境 → 密钥 → 依赖 → 人格，覆盖首次使用全流程
"""

HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>LuoTavern 管理台</title>
<style>
:root{
  --bg:#0a0d13; --panel:#11161f; --panel2:#0d1118; --panel3:#161c27;
  --border:#1d2633; --border2:#283447; --text:#e8eef7; --muted:#8a95a8;
  --accent:#6e8bff; --accent2:#9b6eff; --green:#3fbf7f; --red:#f2615f;
  --amber:#e8a23c; --radius:12px; --radius-sm:8px;
  --shadow:0 10px 30px rgba(0,0,0,.35);
  --mono:ui-monospace,SFMono-Regular,Cascadia Mono,Consolas,monospace;
}
*{box-sizing:border-box;margin:0;padding:0}
html,body{height:100%}
body{background:var(--bg);color:var(--text);
  font:14px/1.6 ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,"PingFang SC","Microsoft YaHei",sans-serif;
  overflow:hidden}
button{font:inherit;cursor:pointer}
.app{display:flex;height:100vh}
.side{width:236px;flex:0 0 236px;background:linear-gradient(180deg,#0d1118,#0a0e14);
  border-right:1px solid var(--border);display:flex;flex-direction:column;padding:14px 10px}
.logo{display:flex;align-items:center;gap:10px;padding:4px 10px 14px;font-weight:700;font-size:15px}
.logo svg{width:26px;height:26px;flex:0 0 auto}
.logo .ver{color:var(--muted);font-size:11px;font-weight:400;margin-left:auto}
.side-status{margin:0 6px 12px;padding:8px 10px;border:1px solid var(--border);border-radius:var(--radius-sm);
  background:var(--panel);font-size:12px;color:var(--muted);display:flex;align-items:center;gap:8px}
.dot{width:8px;height:8px;border-radius:50%;background:var(--muted);flex:0 0 auto}
.dot.on{background:var(--green);box-shadow:0 0 8px rgba(63,191,127,.8)}
.dot.off{background:var(--amber)}
.nav{flex:1;overflow:auto}
.nav-group{font-size:11px;color:var(--muted);padding:12px 10px 4px;letter-spacing:.06em}
.nav a{display:flex;align-items:center;gap:10px;padding:7px 10px;border-radius:var(--radius-sm);
  color:var(--muted);cursor:pointer;font-size:13px;text-decoration:none}
.nav a svg{width:15px;height:15px;flex:0 0 auto;opacity:.75}
.nav a:hover{background:var(--panel);color:var(--text)}
.nav a.active{background:linear-gradient(90deg,rgba(110,139,255,.16),transparent);
  color:var(--text);box-shadow:inset 2px 0 0 var(--accent)}
.side-foot{margin-top:10px;padding:8px 10px;border-top:1px solid var(--border);font-size:11px;color:var(--muted);
  word-break:break-all;line-height:1.5}
.main{flex:1;display:flex;flex-direction:column;min-width:0}
.topbar{display:flex;align-items:center;gap:12px;padding:12px 22px;border-bottom:1px solid var(--border);
  background:rgba(13,17,24,.6);backdrop-filter:blur(8px)}
.crumbs{font-weight:600;font-size:15px;display:flex;align-items:center;gap:8px}
.crumbs .sep{color:var(--muted);font-weight:400}
.top-actions{margin-left:auto;display:flex;align-items:center;gap:8px}
.pill{display:inline-flex;align-items:center;gap:6px;padding:3px 10px;border-radius:999px;font-size:12px;
  background:var(--panel);border:1px solid var(--border);color:var(--muted);white-space:nowrap}
.pill.ok{color:var(--green);border-color:rgba(63,191,127,.4)}
.pill.warn{color:var(--amber);border-color:rgba(232,162,60,.4)}
.pill.bad{color:var(--red);border-color:rgba(242,97,95,.4)}
.view{display:none;padding:22px;overflow:auto;flex:1}
.view.active{display:block}
h1{font-size:18px;font-weight:700;margin-bottom:4px}
.sub{color:var(--muted);font-size:12.5px;margin-bottom:18px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:12px;margin-bottom:16px}
.card{background:var(--panel);border:1px solid var(--border);border-radius:var(--radius);padding:16px;margin-bottom:16px}
.card-head{display:flex;align-items:center;gap:10px;margin-bottom:12px;font-weight:600;font-size:13.5px}
.card-head .hint{margin-left:auto;color:var(--muted);font-size:12px;font-weight:400}
.label{color:var(--muted);font-size:12px;margin-bottom:4px}
.value{font-size:16px;font-weight:700;word-break:break-all}
.value.mono{font-family:var(--mono);font-size:13px;font-weight:400}
.btn{display:inline-flex;align-items:center;justify-content:center;gap:8px;padding:7px 16px;border-radius:var(--radius-sm);
  border:1px solid var(--border2);background:var(--panel3);color:var(--text);font-size:13px;transition:.15s}
.btn:hover{border-color:var(--accent);color:var(--accent)}
.btn.primary{background:linear-gradient(135deg,#5f7bff,#7a5cff);border:none;color:#fff;font-weight:600}
.btn.primary:hover{filter:brightness(1.12);color:#fff}
.btn.danger{border-color:rgba(242,97,95,.5);color:var(--red);background:transparent}
.btn.danger:hover{background:rgba(242,97,95,.12)}
.btn.ghost{background:transparent;border-color:transparent;color:var(--muted)}
.btn.ghost:hover{color:var(--text)}
.btn.sm{padding:4px 12px;font-size:12px}
.btn:disabled{opacity:.5;cursor:not-allowed}
.btn.big{padding:12px 26px;font-size:15px;border-radius:10px;min-width:150px}
input,select,textarea{background:var(--panel2);color:var(--text);border:1px solid var(--border2);
  border-radius:var(--radius-sm);padding:8px 11px;font-size:13px;width:100%;font-family:inherit}
input:focus,select:focus,textarea:focus{outline:none;border-color:var(--accent);box-shadow:0 0 0 3px rgba(110,139,255,.15)}
textarea{min-height:90px;resize:vertical}
label{display:block;color:var(--muted);font-size:12px;margin:10px 0 4px}
.row{display:flex;gap:12px;flex-wrap:wrap}
.row>div{flex:1;min-width:180px}
.hint{color:var(--muted);font-size:12px;margin-top:6px}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{padding:8px 10px;text-align:left;border-bottom:1px solid var(--border);vertical-align:top}
th{color:var(--muted);font-weight:500;font-size:12px;background:var(--panel2)}
tr:last-child td{border-bottom:none}
.mono{font-family:var(--mono)}
.hero{display:flex;gap:20px;align-items:center;padding:22px;border-radius:16px;margin-bottom:16px;
  background:radial-gradient(1200px 300px at 20% -40%,rgba(110,139,255,.14),transparent),var(--panel);
  border:1px solid var(--border)}
.hero-left{display:flex;align-items:center;gap:18px;flex:1}
.ring{width:78px;height:78px;border-radius:50%;display:flex;align-items:center;justify-content:center;flex:0 0 auto;
  background:conic-gradient(var(--accent) 0deg,var(--accent2) 90deg,rgba(110,139,255,.15) 90deg);
  position:relative}
.ring::after{content:"";position:absolute;inset:5px;border-radius:50%;background:var(--panel)}
.avatar{position:relative;z-index:1;width:68px;height:68px;border-radius:50%;display:flex;align-items:center;
  justify-content:center;font-size:28px;font-weight:700;color:#fff;
  background:linear-gradient(135deg,#5f7bff,#7a5cff)}
.hero-left h1{font-size:24px;margin:0 0 2px}
.kicker{font-size:11px;color:var(--muted);letter-spacing:.08em;margin-bottom:4px}
.hero-right{display:flex;flex-direction:column;gap:10px;align-items:stretch;min-width:220px}
.seg{display:flex;background:var(--panel2);border:1px solid var(--border2);border-radius:10px;padding:4px;gap:4px}
.seg button{flex:1;padding:8px;border:none;background:transparent;color:var(--muted);border-radius:7px;font-size:13px}
.seg button.active{background:var(--panel3);color:var(--text);box-shadow:0 2px 8px rgba(0,0,0,.3)}
.badge{display:inline-block;padding:1px 9px;border-radius:999px;font-size:11px;background:var(--panel2);
  border:1px solid var(--border);color:var(--muted);margin-left:6px}
.badge.cur{color:var(--green);border-color:rgba(63,191,127,.4)}
.check{list-style:none}
.check li{display:flex;gap:10px;align-items:flex-start;padding:7px 0;border-bottom:1px dashed var(--border);font-size:13px}
.check li:last-child{border-bottom:none}
.check .ic{width:18px;height:18px;border-radius:50%;flex:0 0 auto;display:flex;align-items:center;justify-content:center;font-size:11px}
.ic.ok{background:rgba(63,191,127,.15);color:var(--green)}
.ic.no{background:rgba(242,97,95,.15);color:var(--red)}
.log{font-family:var(--mono);font-size:12px;line-height:1.75;background:var(--panel2);border:1px solid var(--border);
  border-radius:var(--radius-sm);padding:12px;height:260px;overflow:auto;white-space:pre-wrap;word-break:break-all}
.log .ts{color:var(--muted);margin-right:8px}
.log .dep{color:var(--amber)}
.log .bot{color:var(--green)}
.log .err{color:var(--red)}
.persona-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:14px}
.pcard{background:var(--panel);border:1px solid var(--border);border-radius:14px;padding:18px;cursor:pointer;transition:.15s;position:relative}
.pcard:hover{border-color:var(--accent);transform:translateY(-2px);box-shadow:var(--shadow)}
.pcard.active{border-color:var(--accent);box-shadow:0 0 0 1px var(--accent),var(--shadow)}
.pcard .p-avatar{width:52px;height:52px;border-radius:14px;display:flex;align-items:center;justify-content:center;
  font-size:22px;font-weight:700;color:#fff;background:linear-gradient(135deg,#5f7bff,#7a5cff);margin-bottom:12px}
.pcard .p-name{font-size:15px;font-weight:700;margin-bottom:2px}
.pcard .p-file{font-family:var(--mono);font-size:11px;color:var(--muted);word-break:break-all;margin-bottom:10px}
.pcard .p-actions{display:flex;gap:8px}
.pcard .p-actions .btn{flex:1;padding:6px 8px;font-size:12px}
.krow{display:flex;align-items:center;gap:12px;padding:12px 0;border-bottom:1px solid var(--border);flex-wrap:wrap}
.krow:last-child{border-bottom:none}
.krow .k-info{flex:1;min-width:200px}
.krow .k-name{font-weight:600;font-size:13.5px}
.krow .k-env{font-family:var(--mono);font-size:11px;color:var(--muted)}
.krow .k-desc{font-size:12px;color:var(--muted)}
.krow input{max-width:340px}
.toast{position:fixed;top:18px;right:18px;z-index:99;display:flex;flex-direction:column;gap:8px}
.toast-item{padding:10px 16px;border-radius:10px;background:var(--panel3);border:1px solid var(--border2);
  box-shadow:var(--shadow);font-size:13px;animation:slidein .2s ease}
.toast-item.ok{border-color:rgba(63,191,127,.5)}
.toast-item.err{border-color:rgba(242,97,95,.5)}
@keyframes slidein{from{transform:translateX(20px);opacity:0}}
.modal-wrap{position:fixed;inset:0;background:rgba(4,6,10,.66);backdrop-filter:blur(4px);z-index:50;
  display:flex;align-items:center;justify-content:center;padding:30px}
.modal{background:var(--panel);border:1px solid var(--border2);border-radius:16px;box-shadow:var(--shadow);
  width:min(720px,96vw);max-height:88vh;overflow:auto;padding:24px}
.modal h2{font-size:16px;margin-bottom:14px}
.modal-close{position:absolute;top:18px;right:18px}
.wiz-steps{display:flex;gap:6px;margin-bottom:20px}
.wiz-steps .step{flex:1;text-align:center;padding:8px 4px;border-radius:10px;background:var(--panel2);
  border:1px solid var(--border);color:var(--muted);font-size:12px}
.wiz-steps .step.active{border-color:var(--accent);color:var(--text)}
.wiz-steps .step.done{color:var(--green);border-color:rgba(63,191,127,.4)}
.chat-box{display:flex;flex-direction:column;height:calc(100vh - 190px);min-height:380px}
.chat-log{flex:1;overflow:auto;background:var(--panel);border:1px solid var(--border);border-radius:var(--radius);
  padding:16px;margin-bottom:12px}
.chat-msg{margin-bottom:12px;display:flex;flex-direction:column}
.chat-msg .who{font-size:11px;color:var(--muted);margin-bottom:2px}
.chat-msg .body{white-space:pre-wrap;word-break:break-word;background:var(--panel2);border:1px solid var(--border);
  border-radius:10px;padding:8px 12px;max-width:82%}
.chat-msg.mine{align-items:flex-end}
.chat-msg.mine .body{border-color:rgba(110,139,255,.5);background:rgba(110,139,255,.08)}
.chat-msg.notice .body{color:var(--muted);border-style:dashed;background:transparent}
.chat-input{display:flex;gap:8px}
.chat-input textarea{flex:1;min-height:44px;max-height:120px}
.chat-input .btn{width:100px}
.empty{padding:26px;text-align:center;color:var(--muted)}
.hidden{display:none!important}
.usage-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:16px}
.usage-grid .card{margin-bottom:0}
.stat-num{font-size:22px;font-weight:800}
.stat-num small{font-size:12px;color:var(--muted);font-weight:400}
.toolbar{display:flex;gap:10px;align-items:center;margin-bottom:14px;flex-wrap:wrap}
.toolbar input,.toolbar select{max-width:220px}
.switch{display:inline-flex;align-items:center;gap:8px;cursor:pointer;user-select:none}
.switch input{width:auto}
.mode-pill{display:inline-flex;align-items:center;gap:6px;padding:6px 12px;border:1px solid var(--border2);border-radius:8px;cursor:pointer;color:var(--muted)}
.mode-pill:has(input:checked){border-color:var(--accent);color:var(--text)}
.mode-pill input{width:auto;accent-color:var(--accent)}
.pcard .cover{width:100%;height:128px;border-radius:10px;overflow:hidden;position:relative;margin-bottom:10px;
  background:linear-gradient(135deg,var(--panel3),var(--panel2));display:flex;align-items:center;justify-content:center;
  color:var(--muted);font-size:30px;font-weight:600}
.pcard .cover img{position:absolute;inset:0;width:100%;height:100%;object-fit:cover}
.wb-cover{width:44px;height:44px;border-radius:8px;object-fit:cover;border:1px solid var(--border2)}
.spin{display:inline-block;width:12px;height:12px;border:2px solid rgba(255,255,255,.35);
  border-top-color:currentColor;border-radius:50%;animation:spin .7s linear infinite;
  vertical-align:-2px;margin-right:6px}
@keyframes spin{to{transform:rotate(360deg)}}
::-webkit-scrollbar{width:10px;height:10px}
::-webkit-scrollbar-thumb{background:var(--border2);border-radius:6px;border:2px solid var(--panel)}
::-webkit-scrollbar-track{background:transparent}
@media(max-width:900px){.side{display:none}.hero{flex-direction:column;align-items:stretch}}
</style>
</head>
<body>
<div class="app">
  <aside class="side">
    <div class="logo">
      <svg viewBox="0 0 24 24" fill="none"><path d="M12 2l8 4v6c0 5-3.5 8.5-8 10-4.5-1.5-8-5-8-10V6l8-4z" fill="url(#g1)"/><path d="M12 2v20M4 6l16 0" stroke="#0a0d13" stroke-width="1.4"/><defs><linearGradient id="g1" x1="0" y1="0" x2="24" y2="24"><stop stop-color="#5f7bff"/><stop offset="1" stop-color="#7a5cff"/></linearGradient></defs></svg>
      LuoTavern <span class="ver">v2</span>
    </div>
    <div class="side-status"><span class="dot" id="side-dot"></span><span id="side-status-text">正在检测…</span></div>
    <nav class="nav" id="nav">
      <div class="nav-group">启动</div>
      <a data-view="launch" class="active"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M5 3l14 9-14 9V3z"/></svg>启动台</a>
      <a data-view="overview"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 12h4l2-7 4 14 2-7h6"/></svg>状态总览</a>
      <a data-view="persona"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="8" r="4"/><path d="M4 21c0-4 4-6 8-6s8 2 8 6"/></svg>人格切换</a>
      <a data-view="cards"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="5" width="18" height="14" rx="2"/><path d="M3 10h18M8 15h4"/></svg>角色卡</a>
      <a data-view="bot"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="8" width="18" height="12" rx="3"/><path d="M9 8V6a3 3 0 016 0v2M9 14h.01M15 14h.01M12 8v8"/></svg>机器人</a>
      <a data-view="keys"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="8" cy="15" r="4"/><path d="M11 12L20 3m-4 4l3 3m-6 0l3 3"/></svg>密钥管理</a>
      <a data-view="settings"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="3"/><path d="M12 2v3m0 14v3M4.9 4.9l2.1 2.1m10 10l2.1 2.1M2 12h3m14 0h3M4.9 19.1l2.1-2.1m10-10l2.1-2.1"/></svg>设置</a>
      <div class="nav-group">数据与调试</div>
      <a data-view="chat"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 12a8 8 0 01-8 8H4l2-3a8 8 0 1115-5z"/></svg>聊天控制台</a>
      <a data-view="memories"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M4 6h16M4 12h16M4 18h10"/></svg>长期记忆</a>
      <a data-view="important"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 2l2.6 5.3 5.9.9-4.3 4.1 1 5.9L12 15.9 6.8 18.2l1-5.9L3.5 8.2l5.9-.9L12 2z"/></svg>重要记忆</a>
      <a data-view="affection"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 21C7 16.5 3 13 3 8.5 3 6 5 4 7.5 4c1.8 0 3.4 1 4.5 2.6C13.1 5 14.7 4 16.5 4 19 4 21 6 21 8.5 21 13 17 16.5 12 21z"/></svg>好感度</a>
      <a data-view="corrected"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M20 6L9 17l-5-5"/></svg>纠错记录</a>
      <a data-view="timeline"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 3"/></svg>时间线</a>
      <a data-view="worldbook"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M4 5a2 2 0 012-2h12a2 2 0 012 2v14a2 2 0 01-2 2H6a2 2 0 01-2-2V5z"/><path d="M8 3v18"/></svg>世界书</a>
      <a data-view="usage"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 17l5-6 4 3 6-8 3 4"/><path d="M3 21h18"/></svg>Token 用量</a>
    </nav>
    <div class="side-foot" id="side-project">项目：-</div>
  </aside>
  <main class="main">
    <header class="topbar">
      <div class="crumbs"><span id="view-title">启动台</span></div>
      <div class="top-actions">
        <span class="pill" id="pill-persona"><span class="dot"></span>人格：-</span>
        <span class="pill" id="pill-mode">桌面版</span>
        <button class="btn sm" id="btn-wizard">快速设置</button>
      </div>
    </header>
    <div id="toast" class="toast"></div>

    <section class="view active" id="view-launch">
      <div class="hero">
        <div class="hero-left">
          <div class="ring"><div class="avatar" id="launch-avatar">?</div></div>
          <div>
            <div class="kicker">CURRENT PERSONA</div>
            <h1 id="launch-name">未选择人格</h1>
            <div class="sub" id="launch-card" style="margin-bottom:8px">角色卡：-</div>
            <div style="display:flex;gap:8px;flex-wrap:wrap">
              <span class="pill" id="launch-platform">平台：QQ</span>
        <span class="pill" id="launch-qq-mode"></span>
              <span class="pill" id="launch-status">未启动</span>
            </div>
          </div>
        </div>
        <div class="hero-right">
          <div class="seg" id="launch-seg">
            <button data-platform="qq" class="active">QQ</button>
            <button data-platform="discord">Discord</button>
          </div>
          <button class="btn big primary" id="btn-launch">启动机器人</button>
          <button class="btn big danger hidden" id="btn-stop">停止机器人</button>
        </div>
      </div>
      <div class="grid">
        <div class="card"><div class="label">密钥就绪</div><div class="value" id="stat-keys">-</div></div>
        <div class="card"><div class="label">依赖</div><div class="value" id="stat-deps">-</div></div>
        <div class="card"><div class="label">机器人</div><div class="value" id="stat-bot">未运行</div></div>
        <div class="card"><div class="label">累计 Token 用量</div><div class="value" id="stat-usage">-</div></div>
      </div>
      <div class="card">
        <div class="card-head">启动就绪检查 <span class="hint" id="check-hint"></span>
          <button class="btn sm" id="btn-install" style="float:right">一键安装依赖</button></div>
        <ul class="check" id="readiness"></ul>
      </div>
      <div class="card">
        <div class="card-head">最近日志 <span class="hint"><span class="pill ok">实时</span></span></div>
        <div class="log" id="launch-log"><div class="empty">等待日志…</div></div>
      </div>
    </section>

    <section class="view" id="view-overview">
      <h1>状态总览</h1><div class="sub">当前机器人的运行与配置摘要</div>
      <div class="grid" id="ov-grid"></div>
      <div class="card"><div class="label">运行详情</div><div class="value mono" id="ov-meta" style="font-size:13px"></div></div>
    </section>

    <section class="view" id="view-persona">
      <h1>人格切换</h1>
      <div class="sub">选择本次启动的人格；切换后该人格使用独立的数据目录（记忆 / 聊天记录 / 世界书），机器人运行中会自动重启。</div>
      <div class="persona-grid" id="persona-grid"></div>
    </section>

    <section class="view" id="view-cards">
      <h1>角色卡</h1>
      <div class="sub">导入 / 新建 / 编辑 / 导出酒馆角色卡（支持 .json 与 .png，PNG 默认作为封面，也可自定义封面）</div>
      <div class="toolbar">
        <button class="btn primary" id="btn-card-import">导入角色卡</button>
        <button class="btn" id="btn-card-new">新建角色卡</button>
        <button class="btn" id="btn-card-ai">AI 编写角色卡</button>
        <span class="hint" style="margin-left:auto">支持把 .json / .png 直接拖进本页</span>
      </div>
      <div class="persona-grid" id="card-grid"></div>
    </section>

    <section class="view" id="view-bot">
      <h1>机器人</h1><div class="sub">启动 / 停止 / 重启 QQ 或 Discord 机器人，安装依赖并查看实时日志</div>
      <div class="grid">
        <div class="card"><div class="label">运行状态</div><div class="value" id="bot-status">未运行</div>
          <div class="hint" id="bot-pid">-</div></div>
        <div class="card"><div class="label">Python 环境</div><div class="value mono" id="bot-python">-</div></div>
        <div class="card"><div class="label">项目目录</div><div class="value mono" id="bot-project" style="font-size:12px">-</div></div>
      </div>
      <div class="card">
        <div class="card-head">控制台</div>
        <div class="row" style="gap:10px;flex-wrap:wrap">
          <button class="btn primary" id="btn-bot-start-qq">启动 QQ</button>
          <button class="btn primary" id="btn-bot-start-discord">启动 Discord</button>
          <button class="btn danger" id="btn-bot-restart">重启</button>
          <button class="btn danger" id="btn-bot-stop">停止</button>
          <button class="btn" id="btn-bot-install">一键安装依赖</button>
          <button class="btn" id="btn-napcat-webui">打开 NapCat 管理页</button>
          <button class="btn" id="btn-napcat-qr">扫码登录</button>
        </div>
        <div class="hint" id="bot-install-hint"></div>
      </div>
      <div class="card">
        <div class="card-head">实时日志 <span class="hint" id="log-count"></span></div>
        <div class="log" id="bot-log" style="height:380px"><div class="empty">等待日志…</div></div>
      </div>
    </section>

    <section class="view" id="view-keys">
      <h1>密钥管理</h1>
      <div class="sub">密钥统一存放在 EXE 旁的密钥库中（Windows 下用系统 DPAPI 加密），启动机器人时会自动写回项目 .env。</div>
      <div class="toolbar">
        <button class="btn" id="btn-vault-import">从项目导入密钥</button>
        <button class="btn" id="btn-vault-apply">密钥写入 .env</button>
        <button class="btn" id="btn-vault-add">新增自定义密钥</button>
        <span class="hint mono" id="vault-path" style="margin-left:auto"></span>
      </div>
      <div class="card"><div id="vault-list"></div></div>
    </section>

    <section class="view" id="view-settings">
      <h1>启动设置</h1><div class="sub">模型 / 平台 / 称呼 / 回复档位 / 插件（保存后按需重启）</div>
      <div class="card">
        <div class="row">
          <div><label>AI 厂家</label><select id="set-provider"></select></div>
          <div><label>厂家 API 地址</label><input id="set-provider-base" placeholder="https://api.xxx.com"></div>
          <div><label>厂家模型</label><input id="set-provider-model" placeholder="模型名，留空用默认"></div>
          <div><label>默认平台</label><select id="set-platform"><option value="qq">QQ</option><option value="discord">Discord</option></select></div>
          <div><label>QQ 登录方式</label><select id="set-qq-login-mode">
            <option value="shell">NapCat 无头模式（Shell）</option>
            <option value="framework">本机 QQ 客户端（官方 QQ + Framework，推荐）</option>
          </select><div class="hint" id="qq-mode-hint"></div></div>
        </div>
        <div class="row">
          <div><label>机器人名（bot_name）</label><input id="set-botname"></div>
          <div><label>对你的称呼（user_names，逗号分隔）</label><input id="set-usernames"></div>
          <div><label>代码层面“我”的称呼（owner_name）</label><input id="set-owner"></div>
        </div>
        <div class="row">
          <div><label>温度（0-2）</label><input id="set-temp" type="number" min="0" max="2" step="0.1"></div>
          <div><label>上下文最大字符数</label><input id="set-context" type="number"></div>
          <div><label>最大历史轮数</label><input id="set-turns" type="number"></div>
        </div>
        <div class="hint">回复档位</div>
        <div class="row" id="set-reply"></div>
        <div class="hint">插件开关</div>
        <div class="row" id="set-plugins"></div>
        <div class="hint">世界书文件（全选 = 使用目录下全部）</div>
        <div id="set-worldbook" style="margin-top:8px"></div>
        <div class="hint">角色状态栏（软件强制：每次回复末尾都要带，含服装/表情/身体反应等）</div>
        <div id="set-statusbar" style="margin-top:8px">
          <div class="row">
            <div><label class="switch"><input type="checkbox" id="set-sb-enable">启用强制状态栏</label></div>
            <div><label class="switch"><input type="checkbox" id="set-sb-repair">不合格时自动补写</label></div>
            <div><label class="switch"><input type="checkbox" id="set-sb-inherit">沿用角色卡格式</label></div>
          </div>
          <div class="row">
            <div style="flex:1"><label>字段清单（逗号分隔，顺序即建议顺序）</label>
              <input id="set-sb-fields" placeholder="时间, 地点, 表情, 姿态, 服装, 身体反应, 内心"></div>
            <div style="flex:1"><label>必填字段（缺一即重写）</label>
              <input id="set-sb-required" placeholder="表情, 服装, 身体反应"></div>
          </div>
          <div><label>自定义模板（留空 = 用内建模板；{name} 替换为角色名，{字段名} 替换为写法提示）</label>
            <textarea id="set-sb-template" rows="6"></textarea></div>
        </div>
        <div style="margin-top:16px"><button class="btn primary" id="btn-settings-save">保存设置</button></div>
      </div>
    </section>

    <section class="view" id="view-chat">
      <h1>聊天控制台</h1><div class="sub" id="chat-sub">以主人身份向机器人发送消息</div>
      <div class="chat-box">
        <div class="chat-log" id="chat-log"></div>
        <div class="chat-input">
          <textarea id="chat-text" placeholder="给机器人发一条消息……"></textarea>
          <button class="btn primary" id="chat-send">发送</button>
        </div>
      </div>
    </section>

    <section class="view" id="view-memories">
      <h1>长期记忆</h1><div class="sub">管理机器人对每个身份的长期记忆</div>
      <div class="card" style="padding:10px 14px"><span class="hint" id="mem-stats">记忆库状态加载中…</span></div>
      <div class="toolbar">
        <select id="mem-identity"><option value="">全部身份</option></select>
        <input id="mem-q" placeholder="搜索关键词…">
        <button class="btn sm" id="btn-mem-search">搜索</button>
        <button class="btn sm primary" style="margin-left:auto" id="btn-mem-add">添加记忆</button>
      </div>
      <div class="card"><table><thead><tr><th>ID</th><th>身份</th><th>内容</th><th>关键词</th><th>时间</th><th>操作</th></tr></thead><tbody id="mem-rows"></tbody></table></div>
    </section>

    <section class="view" id="view-important">
      <h1>重要记忆</h1><div class="sub">高优先级记忆，会影响机器人的回应</div>
      <div class="toolbar"><select id="imp-identity"><option value="">全部身份</option></select><button class="btn sm primary" style="margin-left:auto" id="btn-imp-add">添加重要记忆</button></div>
      <div class="card"><table><thead><tr><th>ID</th><th>身份</th><th>内容</th><th>影响</th><th>关键词</th><th>时间</th><th>操作</th></tr></thead><tbody id="imp-rows"></tbody></table></div>
    </section>

    <section class="view" id="view-affection">
      <h1>好感度</h1><div class="sub">每个身份的好感数值</div>
      <div class="card"><table><thead><tr><th>身份</th><th>好感度</th><th>更新时间</th><th>操作</th></tr></thead><tbody id="aff-rows"></tbody></table></div>
    </section>

    <section class="view" id="view-corrected">
      <h1>纠错记录</h1><div class="sub">机器人在对话中被纠正的内容</div>
      <div class="card"><table><thead><tr><th>身份</th><th>序号</th><th>纠错内容</th></tr></thead><tbody id="cor-rows"></tbody></table></div>
    </section>

    <section class="view" id="view-timeline">
      <h1>时间线</h1><div class="sub">世界时钟的时间变更记录</div>
      <div class="card"><table><thead><tr><th>时间</th><th>身份</th><th>来源</th><th>动作</th><th>变更</th></tr></thead><tbody id="time-rows"></tbody></table></div>
    </section>

    <section class="view" id="view-worldbook">
      <h1>世界书编辑</h1><div class="sub">选择世界书文件，编辑条目内容与触发词</div>
      <div class="toolbar">
        <select id="wb-file" style="min-width:320px"></select>
        <img id="wb-cover-preview" class="wb-cover" alt="" hidden>
        <button class="btn primary" id="btn-wb-import">导入世界书</button>
        <button class="btn" id="btn-wb-new">新建世界书</button>
        <button class="btn" id="btn-wb-cover">设置封面</button>
        <button class="btn" id="btn-wb-export-json">导出 JSON</button>
        <button class="btn" id="btn-wb-export-png">导出 PNG</button>
      </div>
      <div class="card"><table><thead><tr><th>ID</th><th>名称</th><th>触发词</th><th>内容</th><th>操作</th></tr></thead><tbody id="wb-rows"></tbody></table></div>
    </section>

    <section class="view" id="view-usage">
      <h1>Token 用量</h1><div class="sub">DeepSeek 调用统计与估算费用</div>
      <div class="usage-grid">
        <div class="card"><div class="label">调用次数</div><div class="stat-num" id="u-calls">0</div></div>
        <div class="card"><div class="label">输入 Tokens</div><div class="stat-num" id="u-in">0</div></div>
        <div class="card"><div class="label">输出 Tokens</div><div class="stat-num" id="u-out">0</div></div>
        <div class="card"><div class="label">缓存命中率</div><div class="stat-num" id="u-cache">0%</div></div>
        <div class="card"><div class="label">估算费用</div><div class="stat-num" id="u-cost">¥0</div></div>
      </div>
      <div class="card"><div class="card-head">按类型</div><table><thead><tr><th>类型</th><th>调用</th><th>输入</th><th>输出</th><th>缓存命中</th><th>命中率</th></tr></thead><tbody id="u-types"></tbody></table></div>
      <div class="card"><div class="card-head">按天</div><table><thead><tr><th>日期</th><th>调用</th><th>输入</th><th>输出</th><th>缓存命中</th></tr></thead><tbody id="u-days"></tbody></table></div>
      <div class="card"><div class="card-head">最近调用</div><table><thead><tr><th>时间</th><th>类型</th><th>输入</th><th>输出</th><th>缓存</th><th>延迟</th></tr></thead><tbody id="u-recent"></tbody></table></div>
    </section>
  </main>
</div>

<div class="modal-wrap hidden" id="wizard">
  <div class="modal" style="width:min(860px,96vw)">
    <div style="display:flex;align-items:center;margin-bottom:6px"><h2 style="margin:0">快速设置向导</h2><button class="btn ghost" id="btn-wizard-close" style="margin-left:auto">关闭</button></div>
    <div class="wiz-steps" id="wiz-steps">
      <div class="step active" data-wstep="1">1 · 环境检测</div>
      <div class="step" data-wstep="2">2 · 密钥导入</div>
      <div class="step" data-wstep="3">3 · 依赖安装</div>
      <div class="step" data-wstep="4">4 · 人格与启动</div>
    </div>
    <div id="wiz-body"></div>
    <div style="display:flex;gap:10px;margin-top:18px;justify-content:flex-end">
      <button class="btn" id="btn-wiz-prev">上一步</button>
      <button class="btn primary" id="btn-wiz-next">下一步</button>
    </div>
  </div>
</div>

<div class="modal-wrap hidden" id="modal">
  <div class="modal">
    <button class="btn ghost modal-close" id="modal-close" style="position:static;float:right">✕</button>
    <h2 id="modal-title">编辑</h2>
    <div id="modal-body"></div>
    <div style="display:flex;gap:10px;margin-top:18px;justify-content:flex-end"><button class="btn" id="modal-cancel">取消</button><button class="btn primary" id="modal-ok">保存</button></div>
  </div>
</div>

<script>
"use strict";
const $=id=>document.getElementById(id);
const esc=s=>String(s==null?"":s).replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
function fmtAgo(sec){
  // "多久以前"的人类可读形式（被踢下线时间等提示用）
  if(sec==null)return "?";
  sec=Math.max(0,Math.floor(sec));
  if(sec<60)return sec+" 秒";
  if(sec<3600)return Math.floor(sec/60)+" 分钟";
  if(sec<86400)return Math.floor(sec/3600)+" 小时";
  return Math.floor(sec/86400)+" 天";
}
const VIEWS={launch:["启动台",""],overview:["状态总览",""],persona:["人格切换",""],cards:["角色卡",""],bot:["机器人",""],keys:["密钥管理",""],
  settings:["设置",""],chat:["聊天控制台",""],memories:["长期记忆",""],important:["重要记忆",""],affection:["好感度",""],
  corrected:["纠错记录",""],timeline:["时间线",""],worldbook:["世界书",""],usage:["Token 用量",""]};

let S={personas:[],persona:null,platform:"qq",model:"",bot:{running:false},install:{running:false},
  vault:{keys:{},meta:[]},readiness:[],project_root:"",python:"",reply:{},worldbook:{},usage:{},secrets:{}};
let EMBEDDED=false;
// 设置页是否已加载完成：未加载就点"保存设置"会把还没填进表单的项写成默认值
// （曾出现：打开页面立刻保存 → reply.mode 被写成 "medium"）
let SETTINGS_LOADED=false;
let LOGS=[],LOG_SEQ=0,CHAT_SEQ=0;
let MEM_ROWS=[],IMP_ROWS=[];
let VAULT_SIG="";
let LAST_BOT_PID=null;
let WB_CACHE=[];

// 服务端启动时注入：window.__ADMIN_TOKEN__="<hex token>"
window.__ADMIN_TOKEN__="";

async function api(path,opts={}){
  const headers=Object.assign({"Content-Type":"application/json",
    "X-Admin-Token":window.__ADMIN_TOKEN__||""},opts.headers||{});
  const r=await fetch(path,Object.assign({},opts,{headers}));
  let d={};try{d=await r.json()}catch(e){}
  if(!r.ok||d.ok===false)throw new Error(d.error||("请求失败："+r.status));
  // 后端有些接口只有 note 没有 data（如人格切换/停止），此时返回整个响应体，
  // 保证调用方读取 n.note 不会拿到 null 报错。
  return d.data!==undefined&&d.data!==null?d.data:d;
}
function toast(text,kind){
  const el=document.createElement("div");el.className="toast-item "+(kind||"");
  el.textContent=text;$("toast").appendChild(el);
  setTimeout(()=>{el.style.opacity="0";el.style.transition="opacity .3s";setTimeout(()=>el.remove(),320)},3200);
}
function fmtUptime(s){
  s=Math.max(0,Math.floor(s||0));
  const h=Math.floor(s/3600),m=Math.floor(s%3600/60),ss=s%60;
  return String(h).padStart(2,"0")+":"+String(m).padStart(2,"0")+":"+String(ss).padStart(2,"0");
}
function mask(v){return v?"已设置（"+v+"）":"未设置"}
function tok(chars){const n=parseInt(chars)||0;return Math.max(64,Math.min(4096,Math.ceil(n*1.2)))}

async function refresh(){
  try{
    const st=await api("/api/status");
    EMBEDDED=!!st.running;
    S.platform=st.platform||S.platform;
    S.model=st.model||S.model;
    S.persona=st.persona||S.persona;
    S.personas=(st.personas&&st.personas.length)?st.personas:S.personas;
    S.reply=st.reply||S.reply;
    S.usage=st.usage||S.usage;
    S.secrets=st.secrets||S.secrets;
    S.qq_login_mode_effective=st.qq_login_mode_effective||S.qq_login_mode_effective;
    S.qq_framework_available=st.qq_framework_available??S.qq_framework_available;
  }catch(e){}
  try{
    const d=await api("/api/desktop/status");
    S={...S,...d};
    if(d.personas&&d.personas.length)S.personas=d.personas;
    if(d.persona)S.persona=d.persona;
    if(d.platform)S.platform=d.platform;
    // 机器人重启（聊天指令切换/管理台重启）后，新进程的 outbox 从 0 开始：
    // 检测到 PID 变化时重置聊天控制台游标并重载历史
    const newPid=(d.bot&&d.bot.pid)||null;
    if(newPid!==LAST_BOT_PID){
      if(LAST_BOT_PID!==null&&newPid!==null){
        CHAT_SEQ=0;
        loadChatHistory();
      }
      LAST_BOT_PID=newPid;
    }
  }catch(e){}
  renderAll();
  maybeAutoNapcatQr();
}

function renderAll(){
  const persona=S.persona||{};
  const running=EMBEDDED?true:!!(S.bot&&S.bot.running);
  const dot=$("side-dot"),sideText=$("side-status-text");
  dot.className="dot "+(running?"on":"off");
  sideText.textContent=running?("运行中 · "+(EMBEDDED?"随进程":"PID "+(S.bot&&S.bot.pid))):"未运行";
  $("pill-persona").innerHTML='<span class="dot '+(running?"on":"")+'"></span>人格：'+esc(persona.name||"未选择");
  $("pill-mode").textContent=EMBEDDED?"随机器人模式":"桌面版";
  $("side-project").textContent="项目："+(S.project_root||"-");
  renderLaunch(running,persona);
  renderOverview(running,persona);
  renderPersonas(persona);
  renderBot(running);
  renderVault();
  renderReadiness();
}

function renderLaunch(running,persona){
  $("launch-name").textContent=persona.name||"未选择人格";
  $("launch-avatar").textContent=(persona.name||"?").slice(0,1);
  $("launch-card").textContent="角色卡："+(persona.card||"-");
  $("launch-platform").textContent="平台："+(S.platform==="discord"?"Discord":"QQ");
  $("launch-qq-mode").textContent=(S.platform==="qq")
    ?("QQ 接入："+(S.qq_login_mode_effective==="framework"?"本机 QQ":"NapCat"))
    :"";
  $("launch-status").textContent=running?"运行中":"未启动";
  $("launch-status").className="pill "+(running?"ok":"");
  document.querySelectorAll("#launch-seg button").forEach(b=>b.classList.toggle("active",b.dataset.platform===S.platform));
  $("btn-launch").classList.toggle("hidden",running||EMBEDDED);
  $("btn-stop").classList.toggle("hidden",!running||EMBEDDED);
  const bot=S.bot||{};
  $("stat-bot").textContent=running?("运行中"+(EMBEDDED?"":" · "+fmtUptime(bot.uptime_sec))):"未运行";
  const keys=S.vault&&S.vault.keys?S.vault.keys:{};
  const need=["DEEPSEEK_API_KEY"];if(S.platform==="discord")need.push("DISCORD_TOKEN");
  const have=need.filter(k=>keys[k]&&keys[k].set).length;
  $("stat-keys").textContent=have+"/"+need.length;
  $("stat-deps").textContent=(S.install&&S.install.running)?"安装中…":(S.python?"已就绪":"未检测");
  const u=(S.usage&&S.usage.total)||{};
  $("stat-usage").textContent=(u.calls||0)+" 次 · ¥"+Number(u.est_cost_yuan||0).toFixed(2);
}

function renderOverview(running,persona){
  const cards=[
    ["平台",S.platform==="discord"?"Discord":"QQ"],
    ["模型",S.model||"-"],
    ["人格",persona.name||"未选择"],
    ["机器人",running?"运行中":"未运行"],
    ["密钥状态",(S.secrets&&S.secrets.deepseek_api_key?"DeepSeek ✓ ":"DeepSeek ✗ ")+(S.secrets&&S.secrets.discord_token?"Discord ✓ ":"Discord ✗ ")+(S.secrets&&S.secrets.onebot_token?"OneBot ✓":"OneBot ✗")],
    ["回复档位",((S.reply&&S.reply.mode)||"medium")+(S.reply&&S.reply.active_max_tokens?" · "+S.reply.active_max_tokens+" tokens":"")],
    ["记忆后端",(function(){const m=S.memory||{};const r=m.remote||{};return (m.backend||"-")+((m.backend==="hybrid"||m.backend==="tencentdb")?(" · "+(r.healthy===true?"远端 ✓":(r.available===false?"远端 ✗":"远端…"))):"")})()],
  ];
  $("ov-grid").innerHTML=cards.map(c=>'<div class="card"><div class="label">'+esc(c[0])+'</div><div class="value">'+esc(c[1])+'</div></div>').join("");
  const bot=S.bot||{};
  $("ov-meta").textContent=[
    "PID: "+(running?(EMBEDDED?"随进程":bot.pid):"-"),
    "平台: "+S.platform,
    "Python: "+(S.python||"-")+(S.python_version?" ("+S.python_version+")":""),
    "项目: "+(S.project_root||"-"),
    "密钥库: "+(S.vault&&S.vault.path||"-"),
  ].join("\n");
}

function renderPersonas(cur){
  const list=S.personas||[];
  $("persona-grid").innerHTML=list.length?list.map(p=>{
    const active=cur&&cur.name===p.name;
    const letter=(p.name||"?").slice(0,1);
    return '<div class="pcard '+(active?"active":"")+'"><div class="p-avatar">'+esc(letter)+'</div>'+
      '<div class="p-name">'+esc(p.name)+(active?'<span class="badge cur">当前</span>':"")+'</div>'+
      '<div class="p-file">'+esc(p.file)+'</div>'+
      '<div class="p-actions"><button class="btn primary" data-persona="'+esc(p.name)+'">切换'+(active?"（当前）":"")+'</button></div></div>';
  }).join(""):'<div class="empty">character 目录里还没有角色卡，把角色卡（.json）放进去后会自动出现。</div>';
}

function renderBot(running){
  const bot=S.bot||{};
  $("bot-status").textContent=running?"运行中":"未运行";
  $("bot-status").style.color=running?"var(--green)":"inherit";
  $("bot-pid").textContent=running&&!EMBEDDED?("PID "+bot.pid+" · 已运行 "+fmtUptime(bot.uptime_sec)):(EMBEDDED?"浏览器模式随机器人进程运行":"-");
  $("bot-python").textContent=(S.python||"未找到")+(S.python_version?" · v"+S.python_version:"");
  $("bot-project").textContent=S.project_root||"-";
  const inst=S.install||{};
  $("bot-install-hint").textContent=inst.running?("依赖安装中… 已运行 "+fmtUptime(inst.elapsed_sec)+"（见下方日志）"):"";
  $("btn-bot-start-qq").disabled=EMBEDDED||running;
  $("btn-bot-start-discord").disabled=EMBEDDED||running;
  $("btn-bot-restart").disabled=EMBEDDED||!running;
  $("btn-bot-stop").disabled=EMBEDDED||!running;
  $("btn-bot-install").disabled=EMBEDDED||inst.running;
}

function renderReadiness(){
  const list=S.readiness||[];
  $("readiness").innerHTML=list.map(r=>{
    const ok=r.ok,label=esc(r.label),detail=esc(r.detail||"");
    return '<li><span class="ic '+(ok?"ok":"no")+'">'+(ok?"✓":"!")+'</span><div><strong>'+label+'</strong><div class="hint">'+detail+'</div></div></li>';
  }).join("")||'<li><span class="ic no">!</span><div>暂无检测项</div></li>';
  const done=list.filter(r=>r.ok).length;
  $("check-hint").textContent=done+"/"+list.length+" 项就绪";
  $("btn-install").disabled=!!(S.install&&S.install.running);
}

function renderVault(){
  const vault=S.vault||{};
  const keys=vault.keys||{};
  const meta=vault.meta||[];
  $("vault-path").textContent="密钥库："+(vault.path||"-");
  // 5 秒轮询会反复调用本函数：密钥数据没变就不要再重建 DOM，
  // 否则正在粘贴/输入的密钥会被整体重渲染清空
  const sig=JSON.stringify([vault.path,meta,keys]);
  if(sig===VAULT_SIG)return;
  VAULT_SIG=sig;
  const rows=[];
  for(const m of meta){
    const k=keys[m.envar]||{set:false,masked:""};
    rows.push('<div class="krow"><div class="k-info"><div class="k-name">'+esc(m.label)+
      (m.required?'<span class="badge" style="color:var(--amber)">必填</span>':"")+
      (k.set?'<span class="badge cur">已配置</span>':'<span class="badge">未配置</span>')+
      '</div><div class="k-env">'+esc(m.envar)+'</div><div class="k-desc">'+esc(m.desc||"")+'</div></div>'+
      '<input data-vkey="'+esc(m.envar)+'" placeholder="'+(k.set?"当前 "+esc(k.masked)+"，留空保持不变":"输入新值")+'" autocomplete="off">'+
      '<button class="btn sm primary" data-vsave="'+esc(m.envar)+'">保存</button>'+
      (k.set?'<button class="btn sm danger" data-vdel="'+esc(m.envar)+'">删除</button>':"")+"</div>");
  }
  for(const [envar,k] of Object.entries(keys)){
    if(meta.some(m=>m.envar===envar))continue;
    rows.push('<div class="krow"><div class="k-info"><div class="k-name">'+esc(envar)+'</div><div class="k-env">自定义密钥</div></div>'+
      '<input data-vkey="'+esc(envar)+'" placeholder="'+(k.set?"当前 "+esc(k.masked)+"，留空保持不变":"输入新值")+'" autocomplete="off">'+
      '<button class="btn sm primary" data-vsave="'+esc(envar)+'">保存</button>'+
      (k.set?'<button class="btn sm danger" data-vdel="'+esc(envar)+'">删除</button>':"")+"</div>");
  }
  $("vault-list").innerHTML=rows.join("")||'<div class="empty">密钥库为空</div>';
}

function renderLogs(){
  const html=LOGS.slice(-500).map(l=>{
    const cls=l.kind==="dep"?"dep":(l.kind==="bot"?"bot":"");
    return '<div><span class="ts">'+esc(l.ts)+'</span><span class="'+cls+'">'+esc(l.text)+'</span></div>';
  }).join("")||'<div class="empty">等待日志…</div>';
  ["launch-log","bot-log","wiz-log"].forEach(id=>{
    const el=$(id);if(!el)return;
    el.innerHTML=html;el.scrollTop=el.scrollHeight;
  });
  $("log-count").textContent=LOGS.length+" 条";
}

async function pollLogs(){
  if(EMBEDDED)return;
  try{
    const rows=await api("/api/desktop/logs?after="+LOG_SEQ);
    if(rows.length){LOGS.push(...rows);LOG_SEQ=rows[rows.length-1].seq;renderLogs();}
  }catch(e){}
}

async function pollChat(){
  // 只在"聊天控制台"页面可见时轮询（此前每 1.2 秒一次，人在别的页面时既刷日志又白跑）；
  // 切回聊天页会 loadChatHistory() 全量补齐，不会漏消息
  const view=$("view-chat");
  if(!view||!view.classList.contains("active")||document.hidden)return;
  try{
    const rows=await api((EMBEDDED?"/api/outbox":"/api/desktop/outbox")+"?after="+CHAT_SEQ);
    if(!rows.length)return;
    CHAT_SEQ=rows[rows.length-1].seq;
    const log=$("chat-log");
    rows.forEach(r=>{
      const div=document.createElement("div");
      const mine=r.kind==="input";
      div.className="chat-msg "+(mine?"mine":"");
      const who=mine?"我":"机器人";
      div.innerHTML='<div class="who">'+esc(who)+' · '+esc(r.ts||"")+'</div><div class="body">'+esc(r.text||"")+'</div>';
      log.appendChild(div);
    });
    log.scrollTop=log.scrollHeight;
  }catch(e){}
}

function switchPersona(name){
  api(EMBEDDED?"/api/persona":"/api/desktop/persona",{method:"POST",body:JSON.stringify({name})})
    .then(n=>{toast(n.note||"已切换","ok");refresh()})
    .catch(e=>toast(e.message,"err"));
}

async function loadSettings(){
  try{
    const s=await api("/api/settings");
    const labels=s.provider_labels||{};
    $("set-provider").innerHTML=Object.entries(labels).map(([k,label])=>
      '<option value="'+esc(k)+'" '+(s.llm_provider===k?"selected":"")+'>'+esc(label)+'</option>').join("");
    const renderProvider=()=>{
      const p=(s.llm_providers||{})[$("set-provider").value]||{};
      $("set-provider-base").value=p.base_url||"";
      $("set-provider-model").value=p.model||"";
    };
    $("set-provider").onchange=renderProvider;
    renderProvider();
    $("set-platform").value=s.platform||"qq";$("set-botname").value=s.bot_name||"";
    $("set-qq-login-mode").value=s.qq_login_mode||"shell";
    $("qq-mode-hint").textContent=(s.qq_framework_available
      ?"已检测到 NapCat.Framework"
      :"未检测到 NapCat.Framework（选择“本机 QQ”前请先安装到项目根）")
      +"；当前生效："+((s.qq_login_mode_effective==="framework")?"本机 QQ":"NapCat");
    $("set-usernames").value=s.user_names||"";$("set-owner").value=s.owner_name||"";
    $("set-temp").value=s.temperature??0.8;$("set-context").value=s.max_context_chars??20000;
    $("set-turns").value=s.max_history_turns??20;
    const presets=s.reply&&s.reply.presets?s.reply.presets:{};
    const mode=(s.reply&&s.reply.mode)||"medium";
    $("set-reply").innerHTML='<div class="row" style="width:100%">'+Object.keys(presets).map(k=>
      '<label class="mode-pill"><input type="radio" name="reply-mode" value="'+esc(k)+'" data-reply-mode="'+esc(k)+'" '+(mode===k?"checked":"")+'>'+esc(k)+'</label>').join("")+"</div>"+
      '<div class="row" style="width:100%">'+Object.entries(presets).map(([k,p])=>
        '<div style="min-width:170px;flex:1"><label>'+esc(k)+'（最大字符）</label><input data-reply="'+esc(k)+'" type="number" value="'+esc(p.max_chars)+'"><div class="hint">≈ '+tok(p.max_chars)+' tokens（约 1.2 token/字）</div></div>').join("")+"</div>";
    const pl=s.plugins||{};
    $("set-plugins").innerHTML=Object.entries(pl).map(([k,v])=>
      '<div><label class="switch"><input type="checkbox" data-plugin="'+esc(k)+'" '+(v?"checked":"")+'>'+esc(k)+'</label></div>').join("");
    const wb=s.worldbook_files||[];
    const all=s.worldbook_dir||"book";
    $("set-worldbook").innerHTML='<div class="hint">目录：'+esc(all)+'</div>';
    if(!WB_CACHE.length){try{WB_CACHE=await api("/api/worldbook")}catch(e){}}
    const files=WB_CACHE.length?WB_CACHE.map(f=>f.file):[];
    if(files.length){
      $("set-worldbook").innerHTML+='<div class="row">'+files.map(f=>
        '<div style="min-width:180px;flex:0 0 auto"><label class="switch"><input type="checkbox" data-wbfile="'+esc(f)+'" '+(wb.includes(f)?"checked":"")+'>'+esc(f)+'</label></div>').join("")+"</div>";
    }
    const sbDef={enable:true,repair:true,inherit_card:true,
      fields:["时间","地点","表情","姿态","服装","身体反应","内心"],
      required:["表情","服装","身体反应"],template:""};
    const sb=Object.assign({},sbDef,s.status_bar||{});
    $("set-sb-enable").checked=!!sb.enable;
    $("set-sb-repair").checked=!!sb.repair;
    $("set-sb-inherit").checked=!!sb.inherit_card;
    $("set-sb-fields").value=(sb.fields||[]).join(", ");
    $("set-sb-required").value=(sb.required||[]).join(", ");
    $("set-sb-template").value=sb.template||"";
    SETTINGS_LOADED=true;
  }catch(e){toast(e.message,"err")}
}

async function saveSettings(){
  if(!SETTINGS_LOADED){
    toast("设置还没加载完，请稍候再保存（避免把未加载的项写成默认值）","err");
    return;
  }
  const body={
    llm_provider:$("set-provider").value,
    llm_providers:{[$("set-provider").value]:{
      base_url:$("set-provider-base").value.trim(),
      model:$("set-provider-model").value.trim(),
    }},
    platform:$("set-platform").value,bot_name:$("set-botname").value.trim(),
    qq_login_mode:$("set-qq-login-mode").value,
    user_names:$("set-usernames").value.trim(),owner_name:$("set-owner").value.trim(),
    temperature:parseFloat($("set-temp").value),max_context_chars:parseInt($("set-context").value),
    max_history_turns:parseInt($("set-turns").value),
    reply:{mode:(document.querySelector("[data-reply-mode]:checked")||{}).value||(S.reply&&S.reply.mode)||"medium",presets:{}},
    plugins:{},memory:{},recall:{},
    status_bar:{
      enable:$("set-sb-enable").checked,
      repair:$("set-sb-repair").checked,
      inherit_card:$("set-sb-inherit").checked,
      fields:$("set-sb-fields").value.split(/[,，、\n]/).map(x=>x.trim()).filter(Boolean),
      required:$("set-sb-required").value.split(/[,，、\n]/).map(x=>x.trim()).filter(Boolean),
      template:$("set-sb-template").value,
    },
  };
  document.querySelectorAll("[data-reply]").forEach(i=>{
    const p=body.reply.presets[i.dataset.reply]=body.reply.presets[i.dataset.reply]||{};
    p.max_chars=parseInt(i.value);
  });
  document.querySelectorAll("[data-plugin]").forEach(i=>body.plugins[i.dataset.plugin]=i.checked);
  const wbfiles=Array.from(document.querySelectorAll("[data-wbfile]")).filter(i=>i.checked).map(i=>i.dataset.wbfile);
  body.worldbook_files=wbfiles;
  try{
    const n=await api("/api/settings",{method:"POST",body:JSON.stringify(body)});
    toast(n.note||"设置已保存","ok");refresh();
  }catch(e){toast(e.message,"err")}
}

async function loadMemoryStats(){
  try{
    const s=await api("/api/memory/stats");
    if(!s)return;
    const tc=s.tencentdb||{};
    const remote=(s.backend==="hybrid"||s.backend==="tencentdb")
      ?(" · TencentDB "+((s.remote&&s.remote.healthy===true)?"已连接":(tc.enabled?"未连通":"未启用"))+(s.remote&&s.remote.endpoint?(" · "+s.remote.endpoint):""))
      :(" · TencentDB 未启用");
    $("mem-stats").textContent=
      "后端 "+s.backend+" · 长期记忆 "+s.longterm+" 条 · 重要记忆 "+s.important+" 条"
      +"（story "+((s.track_counts||{}).story||0)+" / world "+((s.track_counts||{}).world||0)+"）"
      +" · 身份 "+s.identities+" 个"+remote;
  }catch(e){}
}
function loadMemories(){
  const ident=$("mem-identity").value,q=encodeURIComponent($("mem-q").value||"");
  api("/api/memories?identity="+encodeURIComponent(ident||"")+"&q="+q).then(rows=>{
    MEM_ROWS=rows;
    $("mem-rows").innerHTML=rows.map(r=>'<tr><td>'+r.id+'</td><td>'+esc(r.identity)+'</td><td>'+esc(r.content)+'</td><td>'+esc((r.keywords||[]).join("、"))+'</td><td>'+esc(r.created_at)+'</td><td><button class="btn sm" data-medit="'+r.id+'">编辑</button> <button class="btn sm danger" data-mdel="'+r.id+'">删除</button></td></tr>').join("")||'<tr><td colspan="6" class="empty">（空）</td></tr>';
  }).catch(e=>toast(e.message,"err"));
}
function loadImportant(){
  const ident=$("imp-identity").value;
  api("/api/important?identity="+encodeURIComponent(ident||"")).then(rows=>{
    IMP_ROWS=rows;
    $("imp-rows").innerHTML=rows.map(r=>'<tr><td>'+r.id+'</td><td>'+esc(r.identity)+'</td><td>'+esc(r.content)+'</td><td>'+(r.affect_delta>0?"+"+r.affect_delta:r.affect_delta)+'</td><td>'+esc((r.keywords||[]).join("、"))+'</td><td>'+esc(r.created_at)+'</td><td><button class="btn sm" data-iedit="'+r.id+'">编辑</button> <button class="btn sm danger" data-idel="'+r.id+'">删除</button></td></tr>').join("")||'<tr><td colspan="7" class="empty">（空）</td></tr>';
  }).catch(e=>toast(e.message,"err"));
}
function loadAffection(){
  api("/api/affection").then(rows=>{
    $("aff-rows").innerHTML=rows.map(r=>'<tr><td>'+esc(r.identity)+'</td><td><input data-aff="'+esc(r.identity)+'" type="number" value="'+r.score+'" style="max-width:120px"></td><td>'+esc(r.updated_at)+'</td><td><button class="btn sm primary" data-afsave="'+esc(r.identity)+'">保存</button></td></tr>').join("")||'<tr><td colspan="4" class="empty">（空）</td></tr>';
  }).catch(e=>toast(e.message,"err"));
}
function loadCorrected(){
  api("/api/corrected").then(rows=>{
    $("cor-rows").innerHTML=rows.map(r=>'<tr><td>'+esc(r.identity)+'</td><td>'+r.entry+'</td><td>'+esc(r.correction)+'</td></tr>').join("")||'<tr><td colspan="3" class="empty">（空）</td></tr>';
  }).catch(e=>toast(e.message,"err"));
}
function loadTimeline(){
  api("/api/timeline").then(rows=>{
    $("time-rows").innerHTML=rows.map(r=>'<tr><td>'+esc(r.ts)+'</td><td>'+esc(r.identity||"-")+'</td><td>'+esc(r.source||"-")+'</td><td>'+esc(r.action||"-")+'</td><td>'+esc(r.before||"")+' → '+esc(r.after||"")+'</td></tr>').join("")||'<tr><td colspan="5" class="empty">（空）</td></tr>';
  }).catch(e=>toast(e.message,"err"));
}
async function loadWorldbookFiles(){
  WB_CACHE=await api("/api/worldbook");
  $("wb-file").innerHTML=WB_CACHE.map(f=>'<option value="'+esc(f.file)+'">'+esc(f.file)+'</option>').join("");
  renderWorldbook();
  updateWbCover();
}
function renderWorldbook(){
  const f=WB_CACHE.find(x=>x.file===$("wb-file").value);
  $("wb-rows").innerHTML=(f?f.entries:[]).map((e,i)=>'<tr><td>'+esc(e.id==null?"":e.id)+'</td><td>'+esc(e.name)+'</td><td>'+esc((e.keys||[]).join("、"))+'</td><td>'+esc(e.content)+'</td><td><button class="btn sm" data-wbedit="'+esc(e.id==null?i:e.id)+'">编辑</button></td></tr>').join("")||'<tr><td colspan="5" class="empty">（无条目）</td></tr>';
}
function updateWbCover(){
  const f=$("wb-file").value,img=$("wb-cover-preview");
  if(!f){img.hidden=true;return}
  img.hidden=false;
  img.onerror=()=>{img.hidden=true};
  img.src="/api/worldbook/cover?file="+encodeURIComponent(f)+"&t="+Date.now();
}

// ---------- 角色卡 / 世界书 管理 ----------
function fileToB64(f){
  return new Promise((res,rej)=>{
    const r=new FileReader();
    r.onload=()=>{const s=String(r.result||"");res(s.includes(",")?s.split(",")[1]:s)};
    r.onerror=rej;r.readAsDataURL(f);
  });
}
function pickFile(accept){
  return new Promise(res=>{
    const i=document.createElement("input");i.type="file";i.accept=accept;
    i.onchange=()=>res(i.files&&i.files[0]?i.files[0]:null);i.click();
  });
}

async function loadCards(){
  try{
    const rows=await api("/api/cards");
    $("card-grid").innerHTML=rows.map(c=>{
      const cover='<div class="cover"><img src="/api/cards/cover?file='+encodeURIComponent(c.file)+'" onerror="this.remove()"><span>'+esc((c.name||"?").slice(0,1))+'</span></div>';
      return '<div class="pcard">'+cover+
        '<div class="p-name">'+esc(c.name)+'</div>'+
        '<div class="p-file">'+esc(c.file)+' <span class="badge">'+esc(c.format.toUpperCase())+'</span></div>'+
        '<div class="p-actions">'+
        '<button class="btn sm" data-cedit="'+esc(c.file)+'">编辑</button>'+
        '<button class="btn sm" data-cexport="'+esc(c.file)+'" data-fmt="json">导出JSON</button>'+
        '<button class="btn sm" data-cexport="'+esc(c.file)+'" data-fmt="png">导出PNG</button>'+
        '<button class="btn sm" data-ccover="'+esc(c.file)+'">封面</button>'+
        '</div></div>';
    }).join("")||'<div class="empty">还没有角色卡：点「导入角色卡」，或把 .json / .png 卡片直接拖进本页。</div>';
  }catch(e){toast(e.message,"err")}
}

function cardFieldsHtml(f){
  f=f||{};
  const inp=(id,label,val)=>'<label>'+label+'</label><input id="'+id+'" value="'+esc(val||"")+'">';
  const ta=(id,label,val,rows)=>'<label>'+label+'</label><textarea id="'+id+'" rows="'+(rows||4)+'">'+esc(val||"")+'</textarea>';
  return inp("c-name","角色名",f.name)+inp("c-bot","机器人自称 bot_name",f.bot_name)+
    inp("c-user","对你的称呼 user_names（逗号分隔）",f.user_names)+
    ta("c-desc","角色设定 description",f.description,5)+ta("c-personality","性格 personality",f.personality,3)+
    ta("c-scenario","场景 scenario",f.scenario,3)+ta("c-sys","系统提示 system_prompt",f.system_prompt,3)+
    ta("c-mes","对话示例 mes_example",f.mes_example,5)+ta("c-first","开场白 first_mes",f.first_mes,3)+
    ta("c-ph","历史后置指令 post_history_instructions",f.post_history_instructions,2)+
    inp("c-notes","作者备注 creator_notes",f.creator_notes)+inp("c-ver","版本 character_version",f.character_version);
}
function collectCardFields(){
  return {
    name:$("c-name").value.trim(),bot_name:$("c-bot").value.trim(),user_names:$("c-user").value.trim(),
    description:$("c-desc").value,personality:$("c-personality").value,scenario:$("c-scenario").value,
    system_prompt:$("c-sys").value,mes_example:$("c-mes").value,first_mes:$("c-first").value,
    post_history_instructions:$("c-ph").value,creator_notes:$("c-notes").value.trim(),
    character_version:$("c-ver").value.trim(),
  };
}
function newCardModal(){
  openModal("新建角色卡",cardFieldsHtml({})+
    '<label>封面（可选，PNG/JPG）</label><input type="file" id="c-cover" accept="image/*">',
    async()=>{
      const fields=collectCardFields();
      if(!fields.name){toast("请填写角色名","warn");return}
      let cover=null;
      const fi=$("c-cover");
      if(fi&&fi.files&&fi.files[0])cover=await fileToB64(fi.files[0]);
      try{
        const n=await api("/api/cards/new",{method:"POST",body:JSON.stringify({fields,cover_b64:cover})});
        toast(n.note||"已新建","ok");loadCards();refresh();
      }catch(e){toast(e.message,"err")}
    });
}
function editCard(file){
  api("/api/cards/detail?file="+encodeURIComponent(file)).then(d=>{
    openModal("编辑角色卡 · "+esc(d.name),cardFieldsHtml(d.fields),async()=>{
      try{
        const n=await api("/api/cards/save",{method:"POST",body:JSON.stringify({file,fields:collectCardFields()})});
        toast(n.note||"已保存","ok");loadCards();refresh();
      }catch(e){toast(e.message,"err")}
    });
  }).catch(e=>toast(e.message,"err"));
}
function openAiCardModal(){
  api("/api/ai/card").then(d=>{
    const html='<div class="card"><div class="card-head">模型选择</div>'+
      '<div class="row"><div style="flex:1"><label>厂家</label><select id="ai-provider">'+
      d.providers.map(p=>'<option value="'+esc(p.key)+'" '+(p.key===d.provider?"selected":"")+'>'+esc(p.label)+(p.api_key_set?" ✓":"（未配置密钥）")+'</option>').join("")+
      '</select></div><div style="flex:1"><label>模型（留空用厂家默认）</label><input id="ai-model" value="'+esc(d.model)+'"></div>'+
      '<div style="flex:0 0 auto;padding-top:22px"><button class="btn sm" id="btn-ai-save-settings">保存设置</button></div></div></div>'+
      '<div class="card"><div class="card-head">内置提示词 <button class="btn sm ghost" id="btn-ai-toggle-prompt">编辑</button></div>'+
      '<div id="ai-prompt-wrap" hidden><textarea id="ai-prompt" rows="8" style="width:100%">'+esc(d.prompt)+'</textarea>'+
      '<button class="btn sm" id="btn-ai-save-prompt" style="margin-top:6px">保存提示词</button></div>'+
      '<div class="hint" id="ai-prompt-hint">点击「编辑」可改写内置提示词。</div></div>'+
      '<div class="card"><div class="card-head">你的需求</div>'+
      '<textarea id="ai-req" rows="5" placeholder="例如：温柔体贴的邻家学姐，喜欢做甜点，偶尔毒舌但心软……"></textarea>'+
      '<div style="margin-top:10px"><button class="btn primary" id="btn-ai-gen">生成角色卡</button> <span class="hint" id="ai-gen-status"></span></div></div>'+
      '<div class="card" id="ai-result-card" hidden><div class="card-head">生成结果 <button class="btn primary sm" id="btn-ai-save">保存为角色卡</button></div>'+
      '<div class="hint" id="ai-result-name"></div><textarea id="ai-result" rows="12" style="width:100%;font-family:monospace"></textarea></div>';
    openModal("AI 编写角色卡",html,()=>{});
    $("modal-ok").style.display="none";
    $("modal-cancel").textContent="关闭";
    $("btn-ai-save-settings").onclick=async()=>{
      try{
        const n=await api("/api/ai/card/settings",{method:"POST",body:JSON.stringify({provider:$("ai-provider").value,model:$("ai-model").value})});
        toast(n.note||"已保存","ok");
      }catch(e){toast(e.message,"err")}
    };
    $("btn-ai-toggle-prompt").onclick=()=>{
      const w=$("ai-prompt-wrap");
      w.hidden=!w.hidden;
      $("ai-prompt-hint").hidden=!w.hidden;
    };
    $("btn-ai-save-prompt").onclick=async()=>{
      try{
        const n=await api("/api/ai/card/settings",{method:"POST",body:JSON.stringify({prompt:$("ai-prompt").value})});
        toast(n.note||"已保存","ok");
      }catch(e){toast(e.message,"err")}
    };
    $("btn-ai-gen").onclick=async()=>{
      const req=$("ai-req").value.trim();
      if(!req){toast("请先填写需求","warn");return}
      $("ai-gen-status").textContent="生成中…";
      try{
        const r=await api("/api/ai/card/generate",{method:"POST",body:JSON.stringify({requirements:req,provider:$("ai-provider").value,model:$("ai-model").value})});
        $("ai-result-card").hidden=false;
        $("ai-result-name").textContent="角色名："+r.name+"（可继续编辑下方 JSON 后保存）";
        $("ai-result").value=JSON.stringify(r.card,null,2);
        $("ai-gen-status").textContent="完成";
        $("ai-result-card").scrollIntoView({block:"center",behavior:"smooth"});
      }catch(e){$("ai-gen-status").textContent="";toast(e.message,"err")}
    };
    $("btn-ai-save").onclick=async()=>{
      let card;
      try{card=JSON.parse($("ai-result").value)}
      catch(e){toast("生成结果不是有效 JSON，请检查后重试","err");return}
      try{
        const n=await api("/api/ai/card/save",{method:"POST",body:JSON.stringify({card})});
        toast(n.note||"已保存","ok");loadCards();refresh();
      }catch(e){toast(e.message,"err")}
    };
  }).catch(e=>toast(e.message,"err"));
}
// ---------- NapCat 扫码登录 ----------
let QR_MODAL_OPEN=false;
let QR_DISMISSED=false;
let QR_LAST_LOGIN=null;
let QR_LAST_CHECK=0;

function renderNapcatQr(st){
  const wrap=$("napcat-qr-wrap");
  if(!wrap)return;
  if(!st||!st.ok){
    wrap.innerHTML='<div class="empty">无法获取登录状态：'+(st&&st.error?esc(st.error):"NapCat 未运行")+'</div>';
    $("napcat-qr-status").textContent="获取失败";
    return;
  }
  if(st.logged_in){
    wrap.innerHTML='<div class="empty" style="color:var(--green)">已登录 QQ</div>';
    $("napcat-qr-status").textContent="已登录";
    return;
  }
  const png=st.qrcode_png;
  const stale=st.qr_source==="cache";
  wrap.innerHTML=
    (png?'<div style="background:#fff;border-radius:12px;padding:10px;width:200px;margin:0 auto"><img src="'+png+'" style="width:100%;display:block"></div>'
        :'<div class="empty">NapCat 没有可用二维码（已过期或未生成）</div>')+
    (png?'<div class="hint" style="text-align:center;margin-top:10px">请使用手机 QQ 扫描二维码登录</div>':"")+
    (st.note?'<div class="hint" style="text-align:center;color:var(--amber)">'+esc(st.note)+'</div>':"")+
    (st.kicked?'<div class="hint" style="text-align:center;color:var(--amber)">上次被踢：'+esc(fmtAgo(st.kicked_ago))+'前（第三方协议登录态有寿命，本机实测约 2 小时）</div>':"")+
    (st.refresh_error?'<div class="hint" style="text-align:center;color:var(--amber)">刷新失败：'+esc(st.refresh_error)+'</div>':"")+
    (st.login_error?'<div class="hint" style="text-align:center;color:var(--amber)">'+esc(st.login_error)+'</div>':"")+
    ((!png||stale)?'<div style="display:flex;gap:8px;justify-content:center;margin-top:10px">'+
        '<button class="btn primary" id="napcat-qr-restart">重启 NapCat</button></div>':"");
  $("napcat-qr-status").textContent=png?(stale?"二维码可能已过期":"等待扫码…"):"需要重启 NapCat";
  const rb=$("napcat-qr-restart");
  if(rb)rb.onclick=async()=>{
    rb.disabled=true;rb.textContent="重启中（约 10-30 秒）…";
    try{
      const st2=await api("/api/desktop/napcat/restart",{method:"POST",body:"{}"});
      renderNapcatQr(st2);
      toast(st2&&st2.logged_in?"QQ 登录成功":"NapCat 已重启，请扫码","ok");
    }catch(e){
      rb.disabled=false;rb.textContent="重启 NapCat";
      toast(e.message,"err");
    }
  };
}

function pollNapcatQr(){
  if(!QR_MODAL_OPEN)return;
  napcatLoginPayload().then(st=>{
    renderNapcatQr(st);
    if(st&&st.ok&&st.logged_in){
      toast("QQ 登录成功","ok");
      QR_MODAL_OPEN=false;QR_DISMISSED=false;
      $("modal").classList.add("hidden");
      refresh();
      return;
    }
    setTimeout(pollNapcatQr,3000);
  }).catch(()=>setTimeout(pollNapcatQr,3000));
}

function openNapcatQrModal(){
  QR_MODAL_OPEN=true;
  openModal("NapCat 扫码登录",
    '<div class="hint" id="napcat-qr-status" style="text-align:center"></div>'+
    '<div id="napcat-qr-wrap" style="min-height:180px"><div class="empty">获取中…</div></div>'+
    '<div style="display:flex;gap:8px;justify-content:center;margin-top:12px">'+
    '<button class="btn" id="napcat-qr-refresh">刷新二维码</button>'+
    '<button class="btn" id="napcat-qr-webui">打开管理页</button></div>',
    ()=>{});
  $("modal-ok").style.display="none";
  $("modal-cancel").textContent="关闭";
  $("napcat-qr-refresh").onclick=async()=>{
    try{
      const st=await api("/api/desktop/napcat/login/refresh",{method:"POST",body:"{}"});
      renderNapcatQr(st);
      if(st&&st.logged_in){
        toast("QQ 登录成功","ok");QR_MODAL_OPEN=false;QR_DISMISSED=false;
        $("modal").classList.add("hidden");refresh();
      }else if(st&&st.refresh_ok){
        toast("二维码已刷新","ok");
      }else{
        // NapCat 会回 success 却没生成新码：明确告知，并让"重启 NapCat"按钮出现
        toast((st&&(st.refresh_error||st.note))||"NapCat 没有生成新二维码，可点“重启 NapCat”","err");
      }
    }catch(e){toast(e.message,"err")}
  };
  $("napcat-qr-webui").onclick=()=>{
    const url=S.napcat&&S.napcat.webui_url;
    if(url&&window.pywebview&&window.pywebview.api&&window.pywebview.api.open_browser)window.pywebview.api.open_browser(url);
    else if(url)window.open(url,"_blank");
  };
  const closeQr=()=>{QR_MODAL_OPEN=false;QR_DISMISSED=true;$("modal").classList.add("hidden")};
  $("modal-cancel").onclick=closeQr;
  $("modal-close").onclick=closeQr;
  pollNapcatQr();
}

function napcatLoginPayload(){
  return api("/api/desktop/napcat/login").catch(()=>null);
}

function maybeAutoNapcatQr(){
  const now=Date.now();
  if(now-QR_LAST_CHECK<10000)return;
  QR_LAST_CHECK=now;
  const nap=S.napcat||{};
  // 进程在（含端口尚未就绪/未登录）即视为可扫码；端口没起不代表进程不在
  if(!(nap.proc_alive||nap.managed||nap.running)||QR_MODAL_OPEN||QR_DISMISSED)return;
  napcatLoginPayload().then(st=>{
    if(!st||!st.ok)return;
    if(st.logged_in){QR_LAST_LOGIN=true;return;}
    if(QR_LAST_LOGIN!==false){
      QR_LAST_LOGIN=false;
      toast("NapCat 需要扫码登录","warn");
      openNapcatQrModal();
    }
  }).catch(()=>{});
}

function setCoverModal(kind,file){
  openModal("设置封面",'<label>选择图片（PNG/JPG，将作为封面）</label><input type="file" id="cov-file" accept="image/*">',
    async()=>{
      const fi=$("cov-file");
      if(!fi||!fi.files||!fi.files[0]){toast("请选择图片","warn");return}
      const b64=await fileToB64(fi.files[0]);
      try{
        const n=await api(kind==="card"?"/api/cards/cover":"/api/worldbook/cover",
          {method:"POST",body:JSON.stringify({file,data_b64:b64})});
        toast(n.note||"封面已设置","ok");
        if(kind==="card")loadCards();else updateWbCover();
      }catch(e){toast(e.message,"err")}
    });
}
async function exportItem(kind,file,fmt){
  if(!file){toast("请先选择文件","warn");return}
  const base=kind==="card"?"/api/cards":"/api/worldbook";
  const name=file.replace(/\.[^.]+$/,"")+"."+fmt;
  if(window.pywebview&&window.pywebview.api&&window.pywebview.api.pick_save_file){
    try{
      const p=await window.pywebview.api.pick_save_file(name);
      if(!p)return;
      const n=await api(base+"/export/save",{method:"POST",body:JSON.stringify({file,format:fmt,save_path:p})});
      toast(n.note||"已导出","ok");return;
    }catch(e){toast(e.message,"err");return}
  }
  try{
    const r=await fetch(base+"/export?file="+encodeURIComponent(file)+"&format="+fmt,
      {headers:{"X-Admin-Token":window.__ADMIN_TOKEN__||""}});
    if(!r.ok){let d={};try{d=await r.json()}catch(e){}throw new Error(d.error||"导出失败")}
    const blob=await r.blob();
    const a=document.createElement("a");a.href=URL.createObjectURL(blob);a.download=name;
    document.body.appendChild(a);a.click();a.remove();
  }catch(e){toast(e.message,"err")}
}
async function importFile(kind){
  const f=await pickFile(".json,.png");
  if(!f)return;
  try{
    const b64=await fileToB64(f);
    const n=await api(kind==="card"?"/api/cards/import":"/api/worldbook/import",
      {method:"POST",body:JSON.stringify({filename:f.name,data_b64:b64})});
    toast(n.note||"已导入","ok");
    if(kind==="card")loadCards();
    else{await loadWorldbookFiles();if(n.file)$("wb-file").value=n.file;renderWorldbook();updateWbCover();}
  }catch(e){toast(e.message,"err")}
}
function loadUsage(){
  api("/api/usage").then(u=>{
    const t=u.total||{};
    $("u-calls").textContent=t.calls||0;$("u-in").textContent=Number(t.prompt_tokens||0).toLocaleString();
    $("u-out").textContent=Number(t.completion_tokens||0).toLocaleString();
    $("u-cache").textContent=(t.cache_rate||0)+"%";$("u-cost").textContent="¥"+Number(t.est_cost_yuan||0).toFixed(4);
    $("u-types").innerHTML=(u.by_type||[]).map(x=>'<tr><td>'+esc(x.type)+'</td><td>'+x.calls+'</td><td>'+Number(x.prompt_tokens).toLocaleString()+'</td><td>'+Number(x.completion_tokens).toLocaleString()+'</td><td>'+Number(x.cache_hit_tokens).toLocaleString()+'</td><td>'+(x.cache_rate||0)+'%</td></tr>').join("")||'<tr><td colspan="6" class="empty">（空）</td></tr>';
    $("u-days").innerHTML=(u.by_day||[]).map(x=>'<tr><td>'+esc(x.day)+'</td><td>'+x.calls+'</td><td>'+Number(x.prompt_tokens).toLocaleString()+'</td><td>'+Number(x.completion_tokens).toLocaleString()+'</td><td>'+Number(x.cache_hit_tokens).toLocaleString()+'</td></tr>').join("")||'<tr><td colspan="5" class="empty">（空）</td></tr>';
    $("u-recent").innerHTML=(u.recent||[]).slice().reverse().map(x=>'<tr><td>'+esc(x.ts)+'</td><td>'+esc(x.type)+'</td><td>'+Number(x.prompt_tokens).toLocaleString()+'</td><td>'+Number(x.completion_tokens).toLocaleString()+'</td><td>'+Number(x.cache_hit_tokens).toLocaleString()+'</td><td>'+x.latency_ms+' ms</td></tr>').join("")||'<tr><td colspan="6" class="empty">（空）</td></tr>';
  }).catch(e=>toast(e.message,"err"));
}
async function loadIdentities(){
  try{
    const ids=await api("/api/identities");
    ["mem-identity","imp-identity"].forEach(id=>{
      const s=$(id);s.innerHTML='<option value="">全部身份</option>'+ids.map(i=>'<option>'+esc(i)+'</option>').join("");
    });
  }catch(e){}
}

function openModal(title,html,onOk){
  $("modal-ok").style.display="";
  $("modal-cancel").textContent="取消";
  $("modal-title").textContent=title;$("modal-body").innerHTML=html;
  $("modal").classList.remove("hidden");
  $("modal-ok").disabled=false;
  $("modal-ok").onclick=async()=>{
    if($("modal-ok").disabled)return;         // 提交期间禁止重复点击（防重复提交竞态）
    let r;
    try{r=onOk();}catch(e){$("modal").classList.add("hidden");return}
    if(r&&typeof r.then==="function"){
      $("modal-ok").disabled=true;$("modal-ok").textContent="保存中…";
      try{await r}catch(e){}
      $("modal-ok").disabled=false;$("modal-ok").textContent="保存";
    }
    $("modal").classList.add("hidden");
  };
  $("modal-cancel").onclick=$("modal-close").onclick=()=>$("modal").classList.add("hidden");
}

function wizardBody(step){
  const w=$("wiz-body");
  document.querySelectorAll("#wiz-steps .step").forEach(el=>{
    el.classList.toggle("active",parseInt(el.dataset.wstep)===step);
    el.classList.toggle("done",parseInt(el.dataset.wstep)<step);
  });
  if(step===1){
    const py=S.python?("已找到："+S.python+(S.python_version?" (v"+S.python_version+")":"")):"未找到系统 Python（py -3 / python）";
    w.innerHTML='<div class="card"><div class="card-head">Python 环境</div><div class="value mono" style="font-size:13px">'+esc(py)+'</div>'+
      '<div class="hint">没有 Python 请到 python.org 下载 3.10+，安装时勾选 Add to PATH。</div></div>'+
      '<div class="card"><div class="card-head">项目目录</div><div class="row"><div style="flex:1"><input id="wiz-project" value="'+esc(S.project_root||"")+'"></div><div style="flex:0 0 auto"><button class="btn" id="btn-wiz-browse">浏览…</button></div><div style="flex:0 0 auto"><button class="btn" id="btn-wiz-project">保存目录</button></div></div>'+
      '<div class="hint">目录需包含 bot.py；EXE 放在项目根目录时会自动识别。</div></div>';
    $("btn-wiz-browse").onclick=async()=>{
      try{
        if(window.pywebview&&window.pywebview.api&&window.pywebview.api.pick_folder){
          const p=await window.pywebview.api.pick_folder();
          if(p)$("wiz-project").value=p;
        }else{toast("桌面版才支持目录选择，请直接粘贴路径","err")}
      }catch(e){toast(e.message,"err")}
    };
    $("btn-wiz-project").onclick=async()=>{
      try{const n=await api("/api/desktop/project",{method:"POST",body:JSON.stringify({path:$("wiz-project").value})});toast(n.note||"已切换","ok");refresh();}catch(e){toast(e.message,"err")}
    };
  }else if(step===2){
    w.innerHTML='<div class="card"><div class="card-head">密钥导入</div>'+
      '<p class="hint">首次运行会自动读取项目 .env 与 config*.json 中的现有密钥。也可以手动点击导入：</p>'+
      '<div style="margin-top:12px"><button class="btn primary" id="btn-wiz-import">从项目导入密钥</button></div>'+
      '<div class="hint" id="wiz-import-result"></div></div>'+
      '<div class="card"><div class="card-head">当前密钥库</div><div id="wiz-keys"></div></div>';
    $("btn-wiz-import").onclick=async()=>{
      try{const n=await api("/api/desktop/vault/import",{method:"POST",body:"{}"});$("wiz-import-result").textContent=n.note||"完成";toast(n.note||"完成","ok");refresh();}
      catch(e){toast(e.message,"err")}
    };
    const keys=S.vault&&S.vault.keys?S.vault.keys:{};
    $("wiz-keys").innerHTML=Object.entries(keys).map(([k,v])=>'<div class="krow"><div class="k-info"><div class="k-name">'+esc(k)+'</div></div><span class="pill '+(v.set?"ok":"")+'">'+(v.set?"已配置":"未配置")+'</span></div>').join("")||'<div class="empty">密钥库为空</div>';
  }else if(step===3){
    w.innerHTML='<div class="card"><div class="card-head">一键安装依赖</div>'+
      '<p class="hint">将执行 <span class="mono">python -m pip install -r requirements.txt</span>，日志会实时显示在下方。</p>'+
      '<div style="margin-top:12px"><button class="btn primary" id="btn-wiz-install">开始安装依赖</button></div>'+
      '<div class="hint" id="wiz-install-status"></div></div>'+
      '<div class="card"><div class="card-head">安装日志</div><div class="log" id="wiz-log" style="height:220px"></div></div>';
    $("btn-wiz-install").onclick=async()=>{
      try{const n=await api("/api/desktop/install",{method:"POST",body:"{}"});$("wiz-install-status").textContent="安装已开始…";toast(n.note||"开始安装","ok");refresh();}
      catch(e){toast(e.message,"err")}
    };
  }else{
    w.innerHTML='<div class="card"><div class="card-head">选择人格</div><div class="persona-grid" id="wiz-personas" style="grid-template-columns:repeat(auto-fill,minmax(200px,1fr))"></div></div>'+
      '<div class="card"><div class="card-head">启动机器人</div>'+
      '<div class="row"><button class="btn primary" id="btn-wiz-start-qq">启动 QQ</button><button class="btn primary" id="btn-wiz-start-discord">启动 Discord</button>'+
      (EMBEDDED?'<button class="btn" disabled>随进程模式</button>':"")+"</div>"+
      '<div class="hint" id="wiz-start-status"></div></div>';
    const list=S.personas||[];
    const cur=S.persona||{};
    $("wiz-personas").innerHTML=list.map(p=>{
      const active=cur.name===p.name;
      return '<div class="pcard '+(active?"active":"")+'" data-wizpersona="'+esc(p.name)+'"><div class="p-avatar">'+esc((p.name||"?").slice(0,1))+'</div><div class="p-name">'+esc(p.name)+'</div><div class="p-file">'+esc(p.file)+'</div></div>';
    }).join("")||'<div class="empty">暂无角色卡</div>';
    document.querySelectorAll("[data-wizpersona]").forEach(el=>el.onclick=()=>{
      document.querySelectorAll("[data-wizpersona]").forEach(x=>x.classList.remove("active"));el.classList.add("active");
      switchPersona(el.dataset.wizpersona);
    });
    $("btn-wiz-start-qq").onclick=()=>startBot("qq");
    $("btn-wiz-start-discord").onclick=()=>startBot("discord");
  }
}

const START_IDS=["btn-launch","btn-bot-start-qq","btn-bot-start-discord",
  "btn-wiz-start-qq","btn-wiz-start-discord"];
function setPending(ids,on,label){
  ids.forEach(id=>{
    const b=$(id);if(!b)return;
    if(on){
      b.dataset.orig=b.textContent;
      b.disabled=true;
      b.innerHTML='<span class="spin"></span>'+label;
    }else if(b.dataset.orig!==undefined){
      b.textContent=b.dataset.orig;
      b.disabled=false;
      delete b.dataset.orig;
    }
  });
}
async function startBot(platform){
  if(EMBEDDED){toast("随机器人模式已绑定进程","err");return}
  setPending(START_IDS,true,"启动中…");
  try{
    const n=await api("/api/desktop/start",{method:"POST",body:JSON.stringify({platform})});
    toast(n.note||"已启动","ok");refresh();
  }catch(e){toast(e.message,"err")}
  finally{setPending(START_IDS,false,"");refresh();}
}
async function stopBot(){
  if(EMBEDDED){toast("随机器人模式请使用对话指令关闭","err");return}
  try{const n=await api("/api/desktop/stop",{method:"POST",body:"{}"});toast(n.note||"已停止","ok");refresh();}
  catch(e){toast(e.message,"err")}
}
async function restartBot(){
  if(EMBEDDED){toast("随机器人模式请使用对话指令","err");return}
  setPending(["btn-bot-restart"],true,"重启中…");
  try{
    const n=await api("/api/desktop/restart",{method:"POST",body:"{}"});
    toast(n.note||"已重启","ok");refresh();
  }catch(e){toast(e.message,"err")}
  finally{setPending(["btn-bot-restart"],false,"");refresh();}
}
async function installDeps(){
  if(EMBEDDED){toast("随机器人模式请勿安装依赖","err");return}
  try{const n=await api("/api/desktop/install",{method:"POST",body:"{}"});toast(n.note||"开始安装","ok");refresh();}
  catch(e){toast(e.message,"err")}
}

function bind(){
  document.querySelectorAll(".nav a").forEach(a=>a.onclick=()=>{
    document.querySelectorAll(".nav a").forEach(x=>x.classList.remove("active"));a.classList.add("active");
    const v=a.dataset.view;
    document.querySelectorAll(".view").forEach(x=>x.classList.remove("active"));
    $("view-"+v).classList.add("active");
    $("view-title").textContent=VIEWS[v][0];
    if(v==="settings")loadSettings();
    if(v==="cards")loadCards();
    if(v==="memories"){loadMemories();loadMemoryStats();}
    if(v==="important")loadImportant();
    if(v==="affection")loadAffection();
    if(v==="corrected")loadCorrected();
    if(v==="timeline")loadTimeline();
    if(v==="worldbook")loadWorldbookFiles();
    if(v==="usage")loadUsage();
    if(v==="chat"){
      loadChatHistory();
      if(!EMBEDDED&&!(S.bot&&S.bot.running))toast("请先启动机器人，聊天控制台才能收发消息","warn");
    }
  });
  $("btn-launch").onclick=()=>startBot(S.platform==="discord"?"discord":"qq");
  $("btn-stop").onclick=stopBot;
  document.querySelectorAll("#launch-seg button").forEach(b=>b.onclick=()=>{
    document.querySelectorAll("#launch-seg button").forEach(x=>x.classList.remove("active"));b.classList.add("active");
    S.platform=b.dataset.platform;renderAll();
  });
  $("btn-bot-start-qq").onclick=()=>startBot("qq");
  $("btn-bot-start-discord").onclick=()=>startBot("discord");
  $("btn-bot-restart").onclick=restartBot;
  $("btn-bot-stop").onclick=stopBot;
  $("btn-bot-install").onclick=installDeps;
  $("btn-napcat-webui").onclick=()=>{
    const url=S.napcat&&S.napcat.webui_url;
    if(!url){toast("未找到 NapCat 管理页地址（需已安装 NapCat）","warn");return}
    if(window.pywebview&&window.pywebview.api&&window.pywebview.api.open_browser){
      window.pywebview.api.open_browser(url);return;
    }
    window.open(url,"_blank");
  };
  $("btn-napcat-qr").onclick=()=>{
    if(!(S.napcat&&S.napcat.running)){toast("请先启动机器人（NapCat 会自动拉起）","warn");return}
    openNapcatQrModal();
  };
  $("btn-install").onclick=installDeps;
  $("persona-grid").onclick=e=>{
    const b=e.target.closest("[data-persona]");if(b)switchPersona(b.dataset.persona);
  };
  $("card-grid").onclick=e=>{
    const ed=e.target.closest("[data-cedit]"),
          ex=e.target.closest("[data-cexport]"),
          cv=e.target.closest("[data-ccover]");
    if(ed)editCard(ed.dataset.cedit);
    if(ex)exportItem("card",ex.dataset.cexport,ex.dataset.fmt);
    if(cv)setCoverModal("card",cv.dataset.ccover);
  };
  $("btn-card-import").onclick=()=>importFile("card");
  $("btn-card-new").onclick=newCardModal;
  $("btn-card-ai").onclick=openAiCardModal;
  $("btn-wb-import").onclick=()=>importFile("book");
  $("btn-wb-new").onclick=()=>openModal("新建世界书",
    '<label>世界书名称</label><input id="wb-new-name" value="新世界书">'+
    '<label>封面（可选，PNG/JPG）</label><input type="file" id="wb-new-cover" accept="image/*">',
    async()=>{
      const name=$("wb-new-name").value.trim()||"新世界书";
      let cover=null;
      const fi=$("wb-new-cover");
      if(fi&&fi.files&&fi.files[0])cover=await fileToB64(fi.files[0]);
      try{
        const n=await api("/api/worldbook/new",{method:"POST",body:JSON.stringify({name,cover_b64:cover})});
        toast(n.note||"已新建","ok");
        await loadWorldbookFiles();if(n.file)$("wb-file").value=n.file;renderWorldbook();updateWbCover();
      }catch(e){toast(e.message,"err")}
    });
  $("btn-wb-cover").onclick=()=>setCoverModal("book",$("wb-file").value);
  $("btn-wb-export-json").onclick=()=>exportItem("book",$("wb-file").value,"json");
  $("btn-wb-export-png").onclick=()=>exportItem("book",$("wb-file").value,"png");
  $("vault-list").onclick=async e=>{
    const save=e.target.closest("[data-vsave]"),del=e.target.closest("[data-vdel]");
    if(save){
      const key=save.dataset.vsave,
        input=Array.from(document.querySelectorAll("[data-vkey]")).find(el=>el.getAttribute("data-vkey")===key);
      if(!input){toast("未找到密钥输入框","err");return}
      const val=input.value.trim();
      if(!val){toast("留空表示保持不变；如需删除请点删除","warn");return}
      try{const n=await api("/api/desktop/vault",{method:"POST",body:JSON.stringify({key,value:val})});toast(n.note||"已保存","ok");refresh();}
      catch(err){toast(err.message,"err")}
    }
    if(del){
      try{const n=await api("/api/desktop/vault/"+encodeURIComponent(del.dataset.vdel),{method:"DELETE"});toast(n.note||"已删除","ok");refresh();}
      catch(err){toast(err.message,"err")}
    }
  };
  $("btn-vault-import").onclick=async()=>{
    try{const n=await api("/api/desktop/vault/import",{method:"POST",body:"{}"});toast(n.note||"导入完成","ok");refresh();}
    catch(e){toast(e.message,"err")}
  };
  $("btn-vault-apply").onclick=async()=>{
    try{const n=await api("/api/desktop/vault/apply",{method:"POST",body:"{}"});toast(n.note||"已写入 .env","ok");}
    catch(e){toast(e.message,"err")}
  };
  $("btn-vault-add").onclick=()=>openModal("新增自定义密钥",
    '<label>键名（建议用大写 ENV 名）</label><input id="newkey-name" placeholder="MY_SECRET_KEY"><label>密钥值</label><input id="newkey-value" type="password">',
    async()=>{
      const key=$("newkey-name").value.trim(),val=$("newkey-value").value.trim();
      if(!key||!val){toast("请填写键名与值","warn");return}
      try{await api("/api/desktop/vault",{method:"POST",body:JSON.stringify({key,value:val})});toast("已保存 "+key,"ok");refresh();}
      catch(e){toast(e.message,"err")}
    });
  $("btn-settings-save").onclick=saveSettings;
  $("btn-mem-search").onclick=loadMemories;
  $("mem-identity").onchange=loadMemories;$("imp-identity").onchange=loadImportant;
  $("btn-mem-add").onclick=()=>openModal("添加长期记忆",
    '<label>身份</label><input id="mem-add-ident" value="主人"><label>内容</label><textarea id="mem-add-content"></textarea>',
    async()=>{
      try{await api("/api/memories",{method:"POST",body:JSON.stringify({identity:$("mem-add-ident").value||"主人",content:$("mem-add-content").value})});toast("已添加","ok");loadMemories();}
      catch(e){toast(e.message,"err")}
    });
  $("btn-imp-add").onclick=()=>openModal("添加重要记忆",
    '<label>身份</label><input id="imp-add-ident" value="主人"><label>内容</label><textarea id="imp-add-content"></textarea>',
    async()=>{
      try{await api("/api/important",{method:"POST",body:JSON.stringify({identity:$("imp-add-ident").value||"主人",content:$("imp-add-content").value})});toast("已添加","ok");loadImportant();}
      catch(e){toast(e.message,"err")}
    });
  $("mem-rows").onclick=e=>{
    const ed=e.target.closest("[data-medit]"),del=e.target.closest("[data-mdel]");
    if(ed){
      // 必须先回填原文：空 textarea + 后端不校验空内容 = 点一下保存就清空该条记忆
      const cur=(MEM_ROWS||[]).find(r=>String(r.id)===String(ed.dataset.medit));
      openModal("编辑记忆 #"+ed.dataset.medit,'<label>内容</label><textarea id="mem-edit-content">'+esc(cur?cur.content:"")+'</textarea>',async()=>{
        const v=$("mem-edit-content").value;
        if(!v.trim()){toast("内容不能为空","err");return}
        try{await api("/api/memories/"+ed.dataset.medit,{method:"PUT",body:JSON.stringify({content:v})});toast("已保存","ok");loadMemories();}catch(err){toast(err.message,"err")}
      });
    }
    if(del&&confirm("确认删除这条记忆？"))api("/api/memories/"+del.dataset.mdel,{method:"DELETE"}).then(()=>{toast("已删除","ok");loadMemories()}).catch(err=>toast(err.message,"err"));
  };
  $("imp-rows").onclick=e=>{
    const ed=e.target.closest("[data-iedit]"),del=e.target.closest("[data-idel]");
    if(ed){
      const cur=(IMP_ROWS||[]).find(r=>String(r.id)===String(ed.dataset.iedit));
      openModal("编辑重要记忆 #"+ed.dataset.iedit,'<label>内容</label><textarea id="imp-edit-content">'+esc(cur?cur.content:"")+'</textarea>',async()=>{
        const v=$("imp-edit-content").value;
        if(!v.trim()){toast("内容不能为空","err");return}
        try{await api("/api/important/"+ed.dataset.iedit,{method:"PUT",body:JSON.stringify({content:v})});toast("已保存","ok");loadImportant();}catch(err){toast(err.message,"err")}
      });
    }
    if(del&&confirm("确认删除这条重要记忆？"))api("/api/important/"+del.dataset.idel,{method:"DELETE"}).then(()=>{toast("已删除","ok");loadImportant()}).catch(err=>toast(err.message,"err"));
  };
  $("aff-rows").onclick=e=>{
    const b=e.target.closest("[data-afsave]");if(!b)return;
    const input=Array.from(document.querySelectorAll("[data-aff]")).find(el=>el.getAttribute("data-aff")===b.dataset.afsave);
    if(!input){toast("未找到好感度输入框","err");return}
    api("/api/affection",{method:"POST",body:JSON.stringify({identity:b.dataset.afsave,score:parseInt(input.value)})}).then(()=>{toast("已保存","ok");loadAffection()}).catch(err=>toast(err.message,"err"));
  };
  $("wb-file").onchange=()=>{renderWorldbook();updateWbCover()};
  $("wb-rows").onclick=e=>{
    const b=e.target.closest("[data-wbedit]");if(!b)return;
    const f=WB_CACHE.find(x=>x.file===$("wb-file").value);
    const entry=f&&f.entries.find(x=>String(x.id==null?x.idx:x.id)===String(b.dataset.wbedit));
    // 触发词必须用逗号回填：后端按逗号/顿号/分号拆分，用"、"回填再原样保存
    // 会把多个触发词粘成一个键，条目从此匹配不到任何消息
    openModal("编辑世界书条目",'<label>触发词（逗号分隔）</label><input id="wb-edit-keys" value="'+esc((entry&&(entry.keys||[]).join(","))||"")+'"><label>内容</label><textarea id="wb-edit-content">'+esc((entry&&entry.content)||"")+'</textarea>',async()=>{
      try{await api("/api/worldbook/edit",{method:"POST",body:JSON.stringify({file:$("wb-file").value,id:b.dataset.wbedit,keys:$("wb-edit-keys").value,content:$("wb-edit-content").value})});toast("已保存","ok");loadWorldbookFiles();}catch(err){toast(err.message,"err")}
    });
  };
  $("chat-send").onclick=async()=>{
    const t=$("chat-text").value.trim();if(!t)return;
    try{await api(EMBEDDED?"/api/chat":"/api/desktop/chat",{method:"POST",body:JSON.stringify({text:t})});$("chat-text").value="";}catch(e){toast(e.message,"err")}
  };
  ["view-cards","view-worldbook"].forEach(id=>{
    const el=$(id);
    el.addEventListener("dragover",e=>{e.preventDefault();e.dataTransfer.dropEffect="copy"});
    el.addEventListener("drop",async e=>{
      e.preventDefault();
      const f=e.dataTransfer.files&&e.dataTransfer.files[0];
      if(!f)return;
      const kind=id==="view-cards"?"card":"book";
      try{
        const b64=await fileToB64(f);
        const n=await api(kind==="card"?"/api/cards/import":"/api/worldbook/import",
          {method:"POST",body:JSON.stringify({filename:f.name,data_b64:b64})});
        toast(n.note||"已导入","ok");
        if(kind==="card")loadCards();
        else{await loadWorldbookFiles();if(n.file)$("wb-file").value=n.file;renderWorldbook();updateWbCover();}
      }catch(err){toast(err.message,"err")}
    });
  });
  $("btn-wizard").onclick=()=>{wizStep=1;$("wizard").classList.remove("hidden");wizardBody(1)}
  $("btn-wizard-close").onclick=()=>$("wizard").classList.add("hidden");
  let wizStep=1;
  $("btn-wiz-prev").onclick=()=>{if(wizStep>1){wizStep--;wizardBody(wizStep)}}
  $("btn-wiz-next").onclick=()=>{
    if(wizStep<4){wizStep++;wizardBody(wizStep)}
    else{$("wizard").classList.add("hidden");toast("设置完成，可以启动机器人了","ok")}
  };
}

function loadChatHistory(){
  const log=$("chat-log");log.innerHTML='<div class="empty">聊天记录将实时显示…</div>';
  CHAT_SEQ=0;
  api((EMBEDDED?"/api/outbox":"/api/desktop/outbox")+"?after=0").then(rows=>{
    if(rows.length)CHAT_SEQ=rows[rows.length-1].seq;
    rows.forEach(r=>{
      const div=document.createElement("div");
      const mine=r.kind==="input";
      div.className="chat-msg "+(mine?"mine":"");
      div.innerHTML='<div class="who">'+(mine?"我":"机器人")+' · '+esc(r.ts||"")+'</div><div class="body">'+esc(r.text||"")+'</div>';
      log.appendChild(div);
    });
  }).catch(e=>toast(e.message,"err"));
}

document.addEventListener("DOMContentLoaded",async()=>{
  bind();
  await loadIdentities();
  await refresh();
  await loadUsage();
  setInterval(refresh,5000);
  setInterval(pollLogs,2000);
  setInterval(pollChat,1200);
});
</script>
</body>
</html>
"""
