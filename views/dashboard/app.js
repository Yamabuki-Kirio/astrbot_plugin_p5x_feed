(() => {
  'use strict';
  const $ = s => document.querySelector(s);
  const form = $('#config-form');
  let snapshot = null, state = null, dirty = false, busy = false, removeSession = null;
  let posts = [], selected = new Set(), targets = [], historyOffset = 0, sendPayload = null;
  const labels = {submitted:'已提交平台', unknown:'结果未知 · 不自动重发', sending:'发送中'};
  const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const date = v => v ? new Intl.DateTimeFormat('zh-CN',{timeZone:'Asia/Shanghai',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false}).format(new Date(v*1000)) : '尚无记录';
  function notice(text, error=false) { const n=$('#message'); n.textContent=text;n.hidden=false;n.classList.toggle('error',error); }
  function unwrap(v) { if(v?.status==='error'||v?.success===false) throw new Error(v.message||'操作失败');if(v?.status==='ok')return v.data;if(v?.data?.status)return unwrap(v.data);return v; }
  async function api(method,path,body={}) {
    const bridge=window.AstrBotPluginView||window.AstrBotPluginPage;
    if(!bridge) throw new Error('请从 AstrBot 后台的插件页面打开控制台。');
    await bridge.ready();
    return unwrap(await (method==='GET'?bridge.apiGet('page/'+path,body):bridge.apiPost('page/'+path,body)));
  }
  function fill(cfg) { for(const [k,v] of Object.entries(cfg)){const el=form.elements.namedItem(k);if(!el)continue;if(el.type==='checkbox')el.checked=v;else el.value=Array.isArray(v)?v.join('\n'):v;} setDirty(false); }
  function values(){const out={};for(const [k,v] of Object.entries(snapshot)){const el=form.elements.namedItem(k);out[k]=el.type==='checkbox'?el.checked:Array.isArray(v)?el.value.split(/\r?\n/).map(s=>s.trim()).filter(Boolean):el.type==='number'?Number(el.value):el.value.trim();}return out;}
  function setDirty(value){dirty=value;$('#dirty-dot').classList.toggle('dirty',value);$('#save-status').textContent=value?'有未保存的修改':'配置已同步';$('#save').disabled=!value||busy;$('#reset').disabled=!value||busy;}
  function postLink(p){try{const u=new URL(p.url);return u.protocol==='https:'&&['tieba.baidu.com','api.xiaoheihe.cn','www.xiaoheihe.cn'].includes(u.hostname)?u.href:null;}catch{return null;}}
  function postLabel(p){return p.source==='tieba'?(p.good?'贴吧精华':'贴吧热门'):'小黑盒';}
  function renderPosts(){
    posts=$('#post-mode').value==='test'?(state?.test_posts||[]):(state?.posts||[]);selected=new Set();
    $('#post-list').innerHTML=posts.length?posts.map((p,i)=>`<article class="card post-card"><div class="post-top"><span class="chip">${esc(postLabel(p))}</span><label><input type="checkbox" data-post="${i}" aria-label="选择帖子 ${esc(p.title)}"> 选择</label></div><h3>${esc(p.title||'无标题')}</h3><p>${esc(p.summary||'该帖子暂无文字简介')}</p><div class="post-bottom"><span>赞 ${esc(p.likes??'未知')} · 回复 ${esc(p.comments)} · ${date(p.created)}</span>${postLink(p)?`<a href="${esc(postLink(p))}" target="_blank" rel="noopener noreferrer">查看原帖 ↗</a>`:''}</div></article>`).join(''):'<div class="card empty">暂无帖子。可以点击“测试爬取”查看当前来源返回的内容。</div>';
    updateSelection();
  }
  function updateSelection(){ $('#selected-count').textContent=`已选 ${selected.size} 篇`;$('#manual-send').disabled=busy||!selected.size||!$('#target-group').value; }
  function render(data){
    state=data;for(const k of ['groups','qualified','tracked'])$('#stat-'+k).textContent=data.stats[k];$('#updated').textContent='更新于 '+date(data.server_time);
    $('#source-cards').innerHTML=data.sources.map(s=>{const text=!s.enabled?'已关闭':s.error?'读取异常':!s.ready?'等待检查':'运行正常';return `<article class="card source-status"><div class="source-name"><span class="source-icon ${s.name==='tieba'?'tieba':'heybox'}">${s.name==='tieba'?'贴':'盒'}</span><div><h3>${s.name==='tieba'?'百度贴吧':'小黑盒'}</h3><p>${esc(s.error||s.warning||('最近成功 '+date(s.success)))}</p></div></div><span class="badge ${!s.enabled?'off':s.error?'warn':''}">${text}</span></article>`;}).join('');
    $('#sub-count').textContent=data.subscriptions.length+' 个群';
    $('#subscription-list').innerHTML=data.subscriptions.length?data.subscriptions.map((s,i)=>`<div class="sub-row"><div><h3>群 ${esc(s.session.split(':').slice(2).join(':'))}</h3><small>机器人 ${esc(s.session.split(':')[0])} · 待推 ${s.pending} 篇</small></div><div class="sub-time"><input type="time" aria-label="群推送时间" data-time="${i}" value="${esc(s.time)}"></div><small class="sub-meta">${s.latest?esc(s.latest.day+' '+(labels[s.latest.state]||s.latest.state)):'尚无自动推送记录'}</small><div class="sub-actions"><button class="button light" data-sub-save="${i}">保存时间</button><button class="button light" data-sub-remove="${i}">退订</button></div></div>`).join(''):'<div class="empty">还没有订阅群聊。在 OneBot 群内发送 /p5x订阅 即可添加。</div>';
    $('#test-note').textContent=data.test_at?'上次测试 '+date(data.test_at)+' · '+data.test_posts.length+' 篇':'尚未进行爬取测试';renderPosts();
  }
  async function refresh(keepDraft=true){const data=await api('GET','state');if(!snapshot||!dirty||!keepDraft){snapshot=data.config;fill(snapshot);}render(data);}
  async function loadTargets(){const data=await api('GET','targets');targets=data.targets;const old=$('#target-group').value;$('#target-group').innerHTML='<option value="">请选择目标群聊</option>'+targets.map(t=>`<option value="${esc(t.session)}">${esc(t.name)} (${esc(t.group_id)}) · ${esc(t.bot)}</option>`).join('');if(targets.some(t=>t.session===old))$('#target-group').value=old;if(data.errors.length)notice('部分机器人群列表读取失败：'+data.errors.join('、'),true);updateSelection();}
  async function loadHistory(){const data=await api('POST','history',{offset:historyOffset});$('#history-total').textContent=data.total+' 条记录';$('#history-page').textContent=`第 ${Math.floor(historyOffset/25)+1} 页`;$('#history-prev').disabled=historyOffset===0;$('#history-next').disabled=historyOffset+25>=data.total;$('#history-list').innerHTML=data.rows.length?data.rows.map(r=>`<details class="card history-card"><summary><span><b>${r.trigger==='manual'?'手动推送':'定时推送'}</b><small>${date(r.created)} · 群 ${esc(r.session.split(':').slice(2).join(':'))} · ${r.posts.length} 篇</small></span><span class="badge ${r.state==='unknown'?'warn':''}">${esc(labels[r.state]||r.state)}</span></summary><div class="history-content"><small>机器人 ${esc(r.session.split(':')[0])} · 以下为发送时保存的内容</small><pre class="sent-message">${esc(r.message||'')}</pre>${r.posts.map(p=>`<article><h3>${esc(p.title)}</h3><p>${esc(p.summary)}</p><small>${esc(postLabel(p))} · 赞 ${esc(p.likes??'未知')} · 回复 ${esc(p.comments)}</small>${postLink(p)?` <a href="${esc(postLink(p))}" target="_blank" rel="noopener noreferrer">原帖 ↗</a>`:''}</article>`).join('')}</div></details>`).join(''):'<div class="card empty">最近七天还没有推送记录。</div>';}
  async function action(fn){if(busy)return;busy=true;$('#config-fields').disabled=true;$('#crawl').disabled=true;$('#save').disabled=true;$('#reset').disabled=true;updateSelection();try{await fn();}catch(e){notice(e.message||'操作失败，请重试。',true);}finally{busy=false;$('#config-fields').disabled=!snapshot;$('#crawl').disabled=!snapshot;setDirty(dirty);updateSelection();}}
  form.addEventListener('input',()=>{if(snapshot)setDirty(JSON.stringify(values())!==JSON.stringify(snapshot));});
  form.addEventListener('submit',e=>{e.preventDefault();if(!form.reportValidity())return;action(async()=>{await api('POST','config',values());dirty=false;await refresh(false);notice('配置已保存并生效，下轮采集使用新设置。');});});
  $('#reset').onclick=()=>fill(snapshot);
  $('#refresh').onclick=()=>action(async()=>{await refresh();await loadHistory();notice(dirty?'状态已刷新，未保存的配置仍保留。':'状态已刷新。');});
  $('#subscription-list').onclick=e=>{const save=e.target.closest('[data-sub-save]'),remove=e.target.closest('[data-sub-remove]');if(save)action(async()=>{const i=Number(save.dataset.subSave);await api('POST','subscription',{session:state.subscriptions[i].session,action:'update',time:$(`[data-time="${i}"]`).value});await refresh();notice('群推送时间已保存。');});if(remove&&!busy){removeSession=state.subscriptions[Number(remove.dataset.subRemove)].session;$('#remove-dialog').showModal();}};
  $('#cancel-remove').onclick=()=>$('#remove-dialog').close();$('#confirm-remove').onclick=()=>action(async()=>{await api('POST','subscription',{session:removeSession,action:'remove'});$('#remove-dialog').close();await refresh();notice('已取消该群订阅。');});
  $('#crawl').onclick=()=>action(async()=>{$('#crawl').textContent='正在爬取…';try{const result=await api('POST','crawl');state.test_posts=result.posts;state.test_at=result.tested_at;$('#post-mode').value='test';render(state);const failures=Object.values(result.errors);notice(`测试完成，取得 ${result.posts.length} 篇帖子。`+(failures.length?' '+failures.join(' '):'未触发推送。'),!!failures.length);}finally{$('#crawl').textContent='测试爬取';}});
  $('#post-mode').onchange=renderPosts;
  $('#post-list').onchange=e=>{if(!e.target.matches('[data-post]'))return;const i=Number(e.target.dataset.post);if(e.target.checked){if(selected.size>=10){e.target.checked=false;notice('一次最多选择 10 篇。',true);return;}selected.add(i);}else selected.delete(i);updateSelection();};
  $('#select-all').onclick=()=>{selected=new Set(posts.slice(0,10).map((_,i)=>i));document.querySelectorAll('[data-post]').forEach(el=>el.checked=selected.has(Number(el.dataset.post)));updateSelection();};
  $('#target-group').onchange=updateSelection;$('#reload-targets').onclick=()=>action(loadTargets);
  $('#manual-send').onclick=()=>{if(busy||!selected.size)return;const session=$('#target-group').value;const target=targets.find(t=>t.session===session);if(!target)return;sendPayload={request_id:Array.from(crypto.getRandomValues(new Uint8Array(16)),x=>x.toString(16).padStart(2,'0')).join(''),session,keys:[...selected].map(i=>posts[i].source+':'+posts[i].id)};$('#send-confirm-text').textContent=`将 ${selected.size} 篇帖子发送至「${target.name}」（${target.group_id}），机器人：${target.bot}。手动选择的帖子不受热门门槛限制。`;$('#send-dialog').showModal();};
  $('#cancel-send').onclick=()=>$('#send-dialog').close();$('#confirm-send').onclick=()=>action(async()=>{const result=await api('POST','send',sendPayload);$('#send-dialog').close();notice(result.state==='submitted'?'已提交目标群，推送记录已保存。':'发送结果未知，请先检查群聊与历史记录，不要立即重复发送。',result.state!=='submitted');await loadHistory();await refresh();});
  $('#history-prev').onclick=()=>action(async()=>{historyOffset=Math.max(0,historyOffset-25);await loadHistory();});$('#history-next').onclick=()=>action(async()=>{historyOffset+=25;await loadHistory();});
  document.querySelectorAll('nav a').forEach(a=>a.onclick=()=>{document.querySelectorAll('nav a').forEach(x=>x.classList.remove('active'));a.classList.add('active');});
  window.addEventListener('beforeunload',e=>{if(dirty){e.preventDefault();e.returnValue='';}});
  action(async()=>{await refresh(false);await Promise.all([loadTargets(),loadHistory()]);});
})();
