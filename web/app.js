/* ABQ-Run 前端逻辑 */
'use strict';

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));

let S = null;                 // 最新状态快照
let selectedId = null;        // 当前选中作业
let logKind = 'out';          // 日志页签
let logTimer = null;
let probData = null;          // 当前作业的问题扫描结果
let probFilter = 'all';       // 问题筛选 all|error|warning
let probSig = '';             // 结果指纹，没变化就不重绘（避免刷新打断滚动）
let probStatusTag = '';       // 已扫过的问题对应的 "作业:状态"，状态一变就重扫
const picked = new Map();     // 添加对话框里已勾选的文件
let fsPath = '';

/* ---------------- 工具 ---------------- */
const esc = (s) => String(s == null ? '' : s)
  .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
  .replace(/"/g, '&quot;');

function fmtDur(sec) {
  if (sec == null || !isFinite(sec)) return '—';
  sec = Math.max(0, Math.round(sec));
  if (sec < 60) return sec + 's';
  const m = Math.floor(sec / 60), s = sec % 60;
  if (m < 60) return m + 'm' + String(s).padStart(2, '0') + 's';
  const h = Math.floor(m / 60);
  return h + 'h' + String(m % 60).padStart(2, '0') + 'm';
}

function fmtSize(n) {
  if (!n) return '';
  if (n < 1024) return n + 'B';
  if (n < 1048576) return (n / 1024).toFixed(0) + 'K';
  return (n / 1048576).toFixed(1) + 'M';
}

function shortDir(p) {
  if (!p) return '';
  const parts = p.split(/[\\/]/).filter(Boolean);
  if (parts.length <= 2) return p;
  return '…\\' + parts.slice(-2).join('\\');
}

let toastTimer = null;
function toast(msg, bad) {
  const el = $('#toast');
  el.textContent = msg;
  el.className = 'toast on' + (bad ? ' bad' : '');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.className = 'toast' + (bad ? ' bad' : ''); }, 2600);
}

async function api(path, body) {
  const opt = body
    ? { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }
    : {};
  const r = await fetch(path, opt);
  return r.json();
}

/* ---------------- 主题 ---------------- */
const THEMES = ['engine', 'darkplus', 'darkmodern', 'light', 'hc'];

function setTheme(t) {
  if (THEMES.indexOf(t) < 0) t = 'engine';
  const root = document.documentElement;
  if (t === 'engine') root.removeAttribute('data-theme');
  else root.setAttribute('data-theme', t);
  const sel = $('#themeSel');
  if (sel) sel.value = t;
  try { localStorage.setItem('abqrun.theme', t); } catch (e) { /* 无痕模式 */ }
}

$('#themeSel').addEventListener('change', (ev) => setTheme(ev.target.value));
setTheme(localStorage.getItem('abqrun.theme') || 'engine');
// 地址栏里可以叠加：#theme:light,set —— 既看主题又开某个面板（截图检查用）
const HASH_FLAGS = location.hash.replace(/^#/, '').split(',').filter(Boolean);
HASH_FLAGS.forEach((f) => {
  if (f.indexOf('theme:') === 0) setTheme(f.slice(6));
});

/* ---------------- 主刷新 ---------------- */
async function refresh() {
  try {
    S = await api('/api/state');
  } catch (e) {
    return;
  }
  renderTop();
  renderTable();
  if (selectedId && !S.jobs.some((j) => j.id === selectedId)) {
    selectedId = null;
    probData = null;
    probSig = '';
    probStatusTag = '';
    $('#logTitle').textContent = '未选择作业';
    $('#logpre').textContent = '选择一行作业查看日志…';
    renderProbBadge();
    if (paneIsProb()) renderProblems();
  }
  // 徽标：选中作业、作业状态变化时补扫一次，不点开页签也知道有几个错。
  // 运行中不再重复扫（probStatusTag 已记住），避免反复读几百 MB 的 .dat
  const sel = selectedId && S.jobs.find((j) => j.id === selectedId);
  if (sel && sel.id + ':' + sel.status !== probStatusTag) fetchProblems();
}

function renderTop() {
  const st = S.stats, s = S.settings;
  $('#stRun').textContent = st.running;
  $('#stQue').textContent = st.queued;
  $('#stCpu').textContent = st.cpus;
  $('#vMaxJobs').textContent = s.max_jobs;
  $('#vMaxCpus').textContent = s.max_cpus > 0 ? s.max_cpus : '不限';
  const bits = [`${s.abaqus_cmd} job=<作业名> input=<文件>.inp cpus=<n> memory="${s.default_memory}" ask_delete=OFF interactive`];
  if (s.default_precision) bits.push(s.default_precision);
  if (Number(s.default_gpus) > 0) bits.push('gpus=' + s.default_gpus);
  if (s.extra_args) bits.push(s.extra_args);
  const tpl = bits.join(' ');

  $('#cmdline').textContent = tpl;
  $('#cmdline').title = tpl;
  const paused = !!s.paused;
  $('#btnPause').textContent = paused ? '继续队列' : '暂停队列';
  $('#btnPause').classList.toggle('primary', paused);
  $('#pauseFlag').style.display = paused ? '' : 'none';
}

function renderTable() {
  const tb = $('#tbody');
  const jobs = S.jobs;
  $('#empty').style.display = jobs.length ? 'none' : '';
  tb.innerHTML = jobs.map((j, i) => rowHtml(j, i)).join('');
}

function rowHtml(j, i) {
  const p = j.progress || {};
  const sel = j.id === selectedId ? ' sel' : '';
  const now = Date.now() / 1000;
  const elapsed = j.status === 'running' && j.started
    ? now - j.started
    : (j.elapsed != null ? j.elapsed : null);

  // 进度条
  let bar = '', pctTxt = '';
  if (j.status === 'running') {
    if (p.pct != null) {
      bar = `<div class="bar"><i style="width:${p.pct.toFixed(1)}%"></i></div>`;
      pctTxt = p.pct.toFixed(1) + '%';
    } else {
      bar = `<div class="bar indet"><i></i></div>`;
      pctTxt = '计算中';
    }
  } else if (j.status === 'done') {
    bar = `<div class="bar done"><i style="width:100%"></i></div>`;
    pctTxt = '100%';
  } else if (j.status === 'failed' || j.status === 'canceled') {
    // 失败/取消的作业停在哪儿也是信息，能算出来就显示
    if (p.pct != null) {
      bar = `<div class="bar ${j.status === 'failed' ? 'fail' : ''}"><i style="width:${p.pct.toFixed(1)}%"></i></div>`;
      pctTxt = p.pct.toFixed(1) + '%';
    } else {
      bar = `<div class="bar ${j.status === 'failed' ? 'fail' : ''}"><i style="width:100%"></i></div>`;
      pctTxt = j.status === 'failed' ? '中断' : '已停';
    }
  } else {
    bar = `<div class="bar"><i style="width:0"></i></div>`;
    pctTxt = j.status === 'queued' ? '等待' : '—';
  }

  const stepTxt = p.step != null
    ? `S${p.step}${p.nsteps ? '/' + p.nsteps : ''}·I${p.inc}`
    : '—';

  // 分析类型：显式 / 隐式 / 频率（来自 INP 里的 step 过程关键字）
  const sv = j.solver || '';
  const typeCls = sv === 'explicit' ? 'explicit' : (sv === 'modal' ? 'modal' : 'implicit');
  const typeTxt = sv === 'explicit' ? '显式' : (sv === 'modal' ? '频率' : (sv ? '隐式' : '—'));
  const nstep = Number(j.nsteps) || 0;
  const procTip = (j.procs || []).map((x, i) => `${i + 1}. ${x || '（未识别过程）'}`).join('\n');
  const sub = (j.status === 'failed' && j.note) ? j.note : shortDir(j.workdir);
  let etaTxt = '—';
  if (j.status === 'running' && p.eta != null) etaTxt = '~' + fmtDur(p.eta);
  else if (j.status === 'queued') etaTxt = j.waitReason || '等待';

  const acts = [];
  if (j.status === 'running') acts.push(`<button class="btn" data-act="cancel">取消</button>`);
  if (j.status === 'queued') {
    acts.push(`<button class="btn" data-act="up" title="上移">↑</button>`);
    acts.push(`<button class="btn" data-act="down" title="下移">↓</button>`);
  }
  if (j.odb) {
    acts.push(`<button class="btn" data-act="odb" title="用 CAE 打开结果文件">ODB</button>`);
  }
  if (j.status === 'failed') {
    acts.push(`<button class="btn" data-act="problems" title="查看错误与警告">问题</button>`);
  }
  if (['done', 'failed', 'canceled', 'interrupted'].indexOf(j.status) >= 0) {
    acts.push(`<button class="btn" data-act="retry" title="重新排队">↻ 重跑</button>`);
  }
  acts.push(`<button class="btn" data-act="folder" title="在资源管理器中打开作业目录">目录</button>`);
  if (j.status !== 'running') acts.push(`<button class="btn" data-act="remove" title="从列表移除">✕</button>`);

  return `<tr class="${j.status}${sel}" data-id="${j.id}">
    <td class="c-seq">${i + 1}</td>
    <td class="c-name"><div class="name-main"><b title="${esc(j.inp)}">${esc(j.name)}</b>
      <small title="${esc(j.status === 'failed' && j.note ? j.note : j.workdir)}">${esc(sub)}</small></div></td>
    <td class="c-status"><span class="badge ${j.status}">${esc(j.statusText)}</span></td>
    <td class="c-type" title="${esc(procTip || 'INP 里没有 *Step')}"><span class="atype ${typeCls}">${typeTxt}</span>${
      nstep ? `<i class="nstep">${nstep} 步</i>` : ''}</td>
    <td class="c-prog"><div class="prog">${bar}<span>${pctTxt}</span></div></td>
    <td class="c-step" title="${esc(p.stepName ? 'Step: ' + p.stepName : '')}">${stepTxt}</td>
    <td class="c-cpu" title="${esc(j.precision ? '精度：' + j.precision : '精度：单精度（默认）')}">${j.cpus}${
      Number(j.gpus) > 0 ? `<i class="tag">+GPU${Number(j.gpus) > 1 ? '×' + j.gpus : ''}</i>` : ''}</td>
    <td class="c-mem">${esc(j.memory)}</td>
    <td class="c-time">${fmtDur(elapsed)}</td>
    <td class="c-eta" title="${esc(j.waitReason || '')}">${etaTxt}</td>
    <td class="c-act"><div class="acts">${acts.join('')}</div></td>
  </tr>`;
}

/* ---------------- 表格交互 ---------------- */
function selectRow(id) {
  if (id !== selectedId) { probData = null; probSig = ''; probStatusTag = ''; }
  selectedId = id;
  $$('#tbody tr').forEach((el) => el.classList.toggle('sel', el.dataset.id === id));
  const job = S && S.jobs.find((x) => x.id === id);
  $('#logTitle').textContent = job ? `${job.name} · ${job.statusText}` : '';
  refreshPane(true);
}

$('#tbody').addEventListener('click', async (ev) => {
  const btn = ev.target.closest('button[data-act]');
  const tr = ev.target.closest('tr[data-id]');
  if (!tr) return;
  const id = tr.dataset.id;
  if (btn) {
    ev.stopPropagation();
    const act = btn.dataset.act;
    if (act === 'folder') {
      const r = await api('/api/open', { id });
      if (!r.ok) toast(r.error || '打开失败', true);
      return;
    }
    if (act === 'problems') {
      selectRow(id);
      switchTab('problems');
      return;
    }
    if (act === 'odb') {
      const r = await api('/api/open-odb', { id });
      toast(r.ok ? 'CAE 正在启动，稍等十几秒…' : (r.error || '打开失败'), !r.ok);
      return;
    }
    const r = await api('/api/action', { id, action: act });
    if (!r.ok) toast(r.error || '操作失败', true);
    if (act === 'remove' && selectedId === id) {
      selectedId = null;
      probData = null;
      probSig = '';
    }
    refresh();
    return;
  }
  selectRow(id);
});

$('#tbody').addEventListener('dblclick', (ev) => {
  const tr = ev.target.closest('tr[data-id]');
  if (!tr || ev.target.closest('button')) return;
  const job = S.jobs.find((x) => x.id === tr.dataset.id);
  if (!job) return;
  $('#editName').textContent = job.inp;
  $('#editCpus').value = job.cpus;
  $('#editMem').value = job.memory;
  $('#editPrec').value = job.precision || '';
  $('#editGpu').value = job.gpus || 0;
  $('#editExtra').value = job.extra || '';
  $('#maskEdit').dataset.id = job.id;
  openMask('#maskEdit');
});

/* ---------------- 日志 ---------------- */
async function fetchLog(force) {
  if (!selectedId) return;
  const kind = logKind;
  const [id] = [selectedId];
  const r = await api(`/api/log?id=${encodeURIComponent(id)}&kind=${kind}&lines=400`);
  if (id !== selectedId || kind !== logKind) return;
  if (!r.ok) {
    $('#logpre').textContent = r.error || '读取失败';
    return;
  }
  if (!r.exists) {
    $('#logpre').textContent = '（文件尚未生成：' + r.path + '）';
    return;
  }
  const html = r.text.split('\n').map((ln) => {
    const e = esc(ln);
    if (/\*\*\*ERROR|ERROR:|error\(/.test(ln)) return `<span class="err">${e}</span>`;
    if (/COMPLETED|completed successfully|Normal Termination/i.test(ln)) return `<span class="ok">${e}</span>`;
    if (/^(Abaqus|Begin |End )/.test(ln)) return `<span class="hd">${e}</span>`;
    return e;
  }).join('\n');
  const pre = $('#logpre');
  pre.innerHTML = html || '（空）';
  if ($('#autoScroll').checked) pre.scrollTop = pre.scrollHeight;
}

/* ---------------- 问题（错误 / 警告） ---------------- */
let probBusy = false;          // 防止大文件扫描还没回来又发一次请求

async function fetchProblems() {
  if (!selectedId) {
    probData = null;
    probSig = '';
    renderProblems();
    return;
  }
  if (probBusy) return;
  probBusy = true;
  const id = selectedId;
  let r;
  try {
    r = await api('/api/problems?id=' + encodeURIComponent(id));
  } catch (e) {
    probBusy = false;
    return;
  }
  probBusy = false;
  if (id !== selectedId) return;
  if (!r.ok) {
    probData = null;
    $('#problist').innerHTML = `<div class="prob-empty">${esc(r.error || '读取失败')}</div>`;
    return;
  }
  probData = r;
  const job = S && S.jobs.find((x) => x.id === id);
  probStatusTag = id + ':' + (job ? job.status : '');
  const sig = `${id}|${r.counts.error}|${r.counts.warning}|${r.total}|`
    + r.items.map((i) => i.level[0] + i.line + 'x' + i.count).join(',');
  renderProbBadge();
  if (sig === probSig) return;   // 内容没变，别重绘（会打断滚动 / 选中）
  probSig = sig;
  renderProblems();
}

function renderProbBadge() {
  const el = $('#probBadge');
  if (!probData) { el.textContent = ''; el.className = 'tabnum'; return; }
  const e = probData.counts.error, w = probData.counts.warning;
  el.textContent = e || w ? `E${e} W${w}` : '';
  el.className = 'tabnum' + (e ? ' has-err' : (w ? ' has-warn' : ''));
}

function renderProblems() {
  const box = $('#problist');
  if (!probData) {
    box.innerHTML = '<div class="prob-empty">先在左表选中一个作业，这里会列出它的 ***ERROR / ***WARNING</div>';
    return;
  }
  const c = probData.counts;
  $('#pcAll').textContent = c.error + c.warning;
  $('#pcErr').textContent = c.error;
  $('#pcWarn').textContent = c.warning;

  const items = probData.items.filter((it) => probFilter === 'all' || it.level === probFilter);
  if (!items.length) {
    box.innerHTML = `<div class="prob-empty">${c.error + c.warning
      ? '当前筛选下没有条目'
      : '没扫到 ***ERROR / ***WARNING<br><span style="opacity:.7">已检查 .dat / .msg / .sta / .log / 输出捕获</span>'}</div>`;
    return;
  }
  const top = box.scrollTop;
  box.innerHTML = items.map((it, i) => `
    <div class="prob-row ${it.level}" data-i="${i}" title="点击复制这条消息">
      <span class="lv">${it.level === 'error' ? '错误' : '警告'}</span>
      <div class="bd">
        <div class="prob-meta"><span>${esc(it.file)}:${it.line}</span>${
          it.count > 1 ? `<span class="x">重复 ${it.count} 次</span>` : ''}</div>
        <div class="prob-tx">${esc(it.text)}</div>
      </div>
    </div>`).join('');
  box.scrollTop = top;
  const cut = probData.total > probData.items.length
    ? `（只列出前 ${probData.items.length} 条）` : '';
  $('#probtip').textContent = `${items.length} 条${cut} · 点一行可复制`;
}

$('#problist').addEventListener('click', async (ev) => {
  const row = ev.target.closest('.prob-row');
  if (!row || !probData) return;
  const items = probData.items.filter((it) => probFilter === 'all' || it.level === probFilter);
  const it = items[Number(row.dataset.i)];
  if (!it) return;
  const txt = `${it.file}:${it.line}  ${it.text}`;
  try {
    await navigator.clipboard.writeText(txt);
    toast('已复制这条消息');
  } catch (e) {
    toast('复制失败，请手动选中文字', true);
  }
});

$$('.fchip').forEach((b) => b.addEventListener('click', () => {
  $$('.fchip').forEach((x) => x.classList.toggle('on', x === b));
  probFilter = b.dataset.f;
  renderProblems();
}));

/* ---------------- 日志 / 问题 页签 ---------------- */
function paneIsProb() { return logKind === 'problems'; }

function refreshPane(force) {
  if (!selectedId) { renderProblems(); return; }
  if (paneIsProb()) fetchProblems();
  else fetchLog(force);
}

function switchTab(kind) {
  const tab = $$('.tab').find((t) => t.dataset.kind === kind);
  if (tab) tab.click();
}

$$('.tab').forEach((tab) => tab.addEventListener('click', () => {
  $$('.tab').forEach((t) => t.classList.toggle('on', t === tab));
  logKind = tab.dataset.kind;
  const prob = paneIsProb();
  $('#probwrap').classList.toggle('on', prob);
  document.body.classList.toggle('prob-on', prob);
  $('#logpre').style.display = prob ? 'none' : '';
  $('#lblAuto').style.display = prob ? 'none' : '';
  $('#btnRefreshLog').textContent = prob ? '重新扫描' : '刷新';
  if (prob) {
    if (!probData) $('#problist').innerHTML = '<div class="prob-empty">扫描中…</div>';
    probSig = '';
    fetchProblems();
  } else {
    $('#logpre').textContent = '加载中…';
    fetchLog(true);
  }
}));
$('#btnRefreshLog').addEventListener('click', () => {
  if (paneIsProb()) { probSig = ''; fetchProblems(); }
  else fetchLog(true);
});

/* ---------------- 定时刷新 ---------------- */
let logDirty = true;
let probTick = 0;

function tickLog() {
  if (!selectedId) return;
  const job = S && S.jobs.find((x) => x.id === selectedId);
  if (!job) return;
  if (paneIsProb()) {
    // 运行中不按 1.5s 的频率重扫（.dat 可能上百 MB），10s 一次；状态变了立刻补一次
    const tag = job.id + ':' + job.status;
    if (job.status === 'running') {
      if (++probTick >= 7) { probTick = 0; fetchProblems(); }
    } else if (tag !== probStatusTag) {
      probStatusTag = tag;
      fetchProblems();
    }
    return;
  }
  const active = job.status === 'running' || job.status === 'queued';
  if (active || logDirty) fetchLog();
}

/* ---------------- 顶部按钮 ---------------- */
$('#btnPause').addEventListener('click', async () => {
  await api('/api/settings', { paused: !S.settings.paused });
  refresh();
});

$('#btnClear').addEventListener('click', async () => {
  const n = S.jobs.filter((j) => !['queued', 'running'].includes(j.status)).length;
  if (!n) { toast('没有已结束的作业'); return; }
  if (!confirm(`从列表移除 ${n} 条已结束的作业？（不会删除结果文件）`)) return;
  await api('/api/clear', {});
  refresh();
});

$$('.stepper button').forEach((b) => b.addEventListener('click', async () => {
  const [key, delta] = b.dataset.step.split(':');
  const cur = Number(S.settings[key]) || 0;
  let v = cur + Number(delta);
  if (key === 'max_jobs') v = Math.max(1, v);
  if (key === 'max_cpus') v = Math.max(0, v);
  await api('/api/settings', { [key]: v });
  refresh();
}));

/* ---------------- 对话框 ---------------- */
function openMask(sel) { $(sel).classList.add('on'); }
function closeMask(el) { el.classList.remove('on'); }
$$('[data-close]').forEach((b) => b.addEventListener('click', () => closeMask(b.closest('.mask'))));
$$('.mask').forEach((m) => m.addEventListener('click', (e) => { if (e.target === m) closeMask(m); }));
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') $$('.mask.on').forEach(closeMask);
});

/* ---------------- 文件选择器（求解器 / odb） ---------------- */
const pk = { ext: '.inp', target: '', onDone: null, path: '' };

function openPicker(opt) {
  pk.ext = opt.ext || '.inp';
  pk.target = opt.target || '';
  pk.onDone = opt.onDone || null;
  pk.path = opt.path || fsPath || '';
  $('#pkTitle').textContent = opt.title || '选择文件';
  $('#pkHint').textContent = '只列出 ' + pk.ext;
  $('#pkPath').value = '';
  $('#pkList').innerHTML = '<div class="fsempty">读取中…</div>';
  openMask('#maskPick');
  loadPkRoots();
  pkBrowse(pk.path);
}

function applyPick(raw) {
  const p = String(raw || '').trim().replace(/^"|"$/g, '');
  if (!p) { toast('先选一个文件', true); return; }
  if (pk.target) {
    const el = $(pk.target);
    if (el) el.value = p;
  }
  const cb = pk.onDone;
  closeMask($('#maskPick'));
  if (cb) cb(p);
}

async function loadPkRoots() {
  const r = await api('/api/roots');
  $('#pkRoots').innerHTML = r.roots.map((x) =>
    `<button class="btn sm" data-path="${esc(x.path)}">${esc(x.name)}</button>`).join('');
  $$('#pkRoots button').forEach((b) => b.addEventListener('click', () => pkBrowse(b.dataset.path)));
}

async function pkBrowse(path) {
  const r = await api('/api/fs?ext=' + encodeURIComponent(pk.ext)
    + '&path=' + encodeURIComponent(path || ''));
  if (!r.ok) { toast(r.error || '无法打开目录', true); return; }
  pk.path = r.path;
  $('#pkPath').value = r.path;
  const rows = [];
  if (r.parent && r.parent !== r.path) {
    rows.push(`<div class="fsrow dir" data-path="${esc(r.parent)}"><span class="ico">↑</span><span class="nm">..</span></div>`);
  }
  r.dirs.forEach((d) => {
    rows.push(`<div class="fsrow dir" data-path="${esc(d.path)}"><span class="ico">▸</span><span class="nm">${esc(d.name)}</span></div>`);
  });
  r.files.forEach((f) => {
    rows.push(`<div class="fsrow file" data-path="${esc(f.path)}">
      <span class="ico">◇</span><span class="nm">${esc(f.name)}</span>
      <span class="sz">${fmtSize(f.size)}</span></div>`);
  });
  if (!rows.length) {
    rows.push(`<div class="fsempty">这个目录里没有 ${esc(pk.ext)} 文件</div>`);
  }
  const list = $('#pkList');
  list.innerHTML = rows.join('');
  $$('.fsrow', list).forEach((row) => row.addEventListener('click', () => {
    if (row.classList.contains('dir')) { pkBrowse(row.dataset.path); return; }
    applyPick(row.dataset.path);
  }));
}

$$('[data-pick]').forEach((b) => b.addEventListener('click', () => {
  const cur = ($(b.dataset.target).value || '').trim();
  openPicker({
    ext: b.dataset.pick,
    target: b.dataset.target,
    title: b.dataset.pick.indexOf('.odb') >= 0 ? '选择 .odb' : '选择 abaqus 启动器',
    path: cur ? cur.replace(/[\\/][^\\/]*$/, '') : '',
  });
}));

$('#pkUp').addEventListener('click', () => {
  const p = $('#pkPath').value.replace(/[\\/]+$/, '');
  pkBrowse(p.replace(/[\\/][^\\/]*$/, '') || p);
});
$('#pkGo').addEventListener('click', () => pkBrowse($('#pkPath').value));
$('#pkPath').addEventListener('keydown', (e) => { if (e.key === 'Enter') pkBrowse($('#pkPath').value); });
$('#pkOk').addEventListener('click', () => applyPick($('#pkPath').value));

/* ---------------- 用 CAE 打开 ODB ---------------- */
$('#btnOdb').addEventListener('click', () => {
  const job = S && S.jobs.find((x) => x.id === selectedId);
  openPicker({
    ext: '.odb',
    title: '打开 ODB',
    path: job ? job.workdir : '',
    onDone: async (p) => {
      const r = await api('/api/open-odb', { path: p });
      toast(r.ok ? 'CAE 正在启动，稍等十几秒…' : (r.error || '打开失败'), !r.ok);
    },
  });
});

/* ---------------- GPU 能力 ---------------- */
let gpuPolling = false;
let gpuInfo = null;           // 最近一次检测结果，用于拦明显填错的 GPU 数

function pollGpu() {
  if (gpuPolling) return;
  gpuPolling = true;
  setTimeout(() => { gpuPolling = false; loadGpu(false); }, 1600);
}

async function loadGpu(refresh) {
  let r;
  try {
    r = await api('/api/gpu' + (refresh ? '?refresh=1' : ''));
  } catch (e) {
    return;
  }
  const d = r.data;
  const cb = $('#setGpu');
  const msg = $('#gpuMsg');
  if (!d) {
    cb.disabled = true;
    msg.className = '';
    msg.textContent = r.state === 'detecting'
      ? '正在检测显卡与 Abaqus 版本…（约十秒）'
      : '还没检测过';
    if (r.state === 'detecting') pollGpu();
    return;
  }
  const n = (d.cards && d.cards.length) || 1;
  $('#gpuN').textContent = n;
  gpuInfo = d;
  if (d.ok) {
    cb.disabled = false;
    msg.className = '';
    msg.innerHTML = `<div class="gpu-line good">可用：<b>${esc(d.reason)}</b>`
      + (d.version ? ` · Abaqus ${esc(d.version)}` : '') + '</div>'
      + (d.advice ? `<div class="gpu-line warn">${esc(d.advice)}</div>` : '')
      + `<div class="gpu-line">${esc(d.note || '')}</div>`;
  } else {
    cb.disabled = true;
    cb.checked = false;
    msg.className = '';
    msg.innerHTML = `<div class="gpu-line bad">不可用：${esc(d.reason)}</div>`
      + (d.note ? `<div class="gpu-line">${esc(d.note)}</div>` : '');
  }
  if (r.state === 'detecting') {
    msg.innerHTML += '<div class="gpu-line">正在重新检测…</div>';
    pollGpu();
  }
}

$('#btnReGpu').addEventListener('click', () => {
  $('#gpuMsg').className = '';
  $('#gpuMsg').textContent = '正在检测显卡与 Abaqus 版本…（约十秒）';
  $('#setGpu').disabled = true;
  loadGpu(true);
});

/* ---------------- 添加作业 ---------------- */
$('#btnAdd').addEventListener('click', async () => {
  picked.clear();
  renderPicked();
  $('#pastePaths').value = '';
  $('#addCpus').value = S.settings.default_cpus;
  $('#addMem').value = S.settings.default_memory;
  $('#addPrec').value = S.settings.default_precision || '';
  $('#addGpu').value = S.settings.default_gpus || 0;
  $('#addExtra').value = '';
  openMask('#maskAdd');
  if (!fsPath) { fsPath = (await api('/api/roots')).home || ''; }
  loadRoots();
  browse(fsPath);
});

async function loadRoots() {
  const r = await api('/api/roots');
  $('#roots').innerHTML = r.roots.map((x) =>
    `<button class="btn sm" data-path="${esc(x.path)}">${esc(x.name)}</button>`).join('');
  $$('#roots button').forEach((b) => b.addEventListener('click', () => browse(b.dataset.path)));
}

async function browse(path) {
  const r = await api('/api/fs?path=' + encodeURIComponent(path || ''));
  if (!r.ok) { toast(r.error || '无法打开目录', true); return; }
  fsPath = r.path;
  $('#fsPath').value = r.path;
  const rows = [];
  if (r.parent && r.parent !== r.path) {
    rows.push(`<div class="fsrow dir" data-path="${esc(r.parent)}"><span class="ico">↑</span><span class="nm">..</span></div>`);
  }
  r.dirs.forEach((d) => {
    rows.push(`<div class="fsrow dir" data-path="${esc(d.path)}"><span class="ico">▸</span><span class="nm">${esc(d.name)}</span></div>`);
  });
  r.files.forEach((f) => {
    const on = picked.has(f.path);
    rows.push(`<div class="fsrow file${on ? ' picked' : ''}" data-path="${esc(f.path)}" data-name="${esc(f.name)}">
      <input type="checkbox" ${on ? 'checked' : ''}>
      <span class="ico">◇</span>
      <span class="nm">${esc(f.name)}</span>
      <span class="sz">${fmtSize(f.size)}</span></div>`);
  });
  if (!rows.length) rows.push(`<div class="fsempty">这个目录里没有子目录，也没有 .inp 文件</div>`);
  const list = $('#fslist');
  list.innerHTML = rows.join('');
  $$('.fsrow', list).forEach((row) => {
    row.addEventListener('click', (ev) => {
      if (row.classList.contains('dir')) { browse(row.dataset.path); return; }
      ev.preventDefault();
      const p = row.dataset.path;
      if (picked.has(p)) picked.delete(p); else picked.set(p, row.dataset.name);
      row.classList.toggle('picked', picked.has(p));
      const cb = row.querySelector('input');
      if (cb) cb.checked = picked.has(p);
      renderPicked();
    });
  });
}

function renderPicked() {
  const n = picked.size;
  $('#addHint').textContent = n ? `已选 ${n} 个文件` : '未选择文件';
  $('#picked').innerHTML = Array.from(picked.entries()).map(([p, nm]) =>
    `<code title="${esc(p)}">${esc(nm)} <b data-x="${esc(p)}">✕</b></code>`).join('');
  $$('#picked b').forEach((b) => b.addEventListener('click', () => {
    picked.delete(b.dataset.x);
    renderPicked();
    browse(fsPath);
  }));
}

$('#btnUp').addEventListener('click', () => {
  const p = $('#fsPath').value.replace(/[\\/]+$/, '');
  const parent = p.replace(/[\\/][^\\/]+$/, '');
  browse(parent || p);
});
$('#btnGo').addEventListener('click', () => browse($('#fsPath').value));
$('#fsPath').addEventListener('keydown', (e) => { if (e.key === 'Enter') browse($('#fsPath').value); });

$('#btnDoAdd').addEventListener('click', async () => {
  const paths = Array.from(picked.keys());
  $('#pastePaths').value.split(/\r?\n/).forEach((x) => {
    const t = x.trim().replace(/^"|"$/g, '');
    if (t && paths.indexOf(t) < 0) paths.push(t);
  });
  if (!paths.length) { toast('先选几个 .inp 文件', true); return; }
  if (gpuInfo && !gpuInfo.ok && Number($('#addGpu').value) > 0) {
    toast('这台机器检测不到可用的 GPU 加速，已按 0 提交', true);
    $('#addGpu').value = 0;
  }
  const btn = $('#btnDoAdd');
  btn.disabled = true;
  const r = await api('/api/add', {
    paths,
    cpus: Number($('#addCpus').value) || S.settings.default_cpus,
    memory: $('#addMem').value.trim() || S.settings.default_memory,
    extra: $('#addExtra').value.trim(),
    precision: $('#addPrec').value,
    gpus: Number($('#addGpu').value) || 0,
  });
  btn.disabled = false;
  if (r.errors && r.errors.length) toast(r.errors.join('；'), true);
  else toast(`已加入 ${r.added.length} 个作业`);
  closeMask($('#maskAdd'));
  refresh();
});

/* ---------------- 编辑 ---------------- */
$('#btnSaveEdit').addEventListener('click', async () => {
  const id = $('#maskEdit').dataset.id;
  if (gpuInfo && !gpuInfo.ok && Number($('#editGpu').value) > 0) {
    toast('这台机器检测不到可用的 GPU 加速，已按 0 保存', true);
    $('#editGpu').value = 0;
  }
  await api('/api/update', {
    id,
    cpus: Number($('#editCpus').value) || 1,
    memory: $('#editMem').value.trim(),
    extra: $('#editExtra').value.trim(),
    precision: $('#editPrec').value,
    gpus: Number($('#editGpu').value) || 0,
  });
  closeMask($('#maskEdit'));
  refresh();
});

/* ---------------- 设置 ---------------- */
$('#btnSet').addEventListener('click', () => {
  const s = S.settings;
  $('#setCmd').value = s.abaqus_cmd;
  $('#setCpus').value = s.default_cpus;
  $('#setMem').value = s.default_memory;
  $('#setMaxJobs').value = s.max_jobs;
  $('#setMaxCpus').value = s.max_cpus;
  $('#setPrec').value = s.default_precision || '';
  $('#setOdbMode').value = s.odb_open_mode || 'cae';
  $('#setExtra').value = s.extra_args || '';
  $('#setClearLck').checked = !!s.clear_lck;
  $('#setBeep').checked = !!s.beep;
  $('#setGpu').checked = Number(s.default_gpus) > 0;
  openMask('#maskSet');
  loadGpu(false);
});

$('#btnSaveSet').addEventListener('click', async () => {
  await api('/api/settings', {
    abaqus_cmd: $('#setCmd').value.trim(),
    default_cpus: Number($('#setCpus').value) || 1,
    default_memory: $('#setMem').value.trim(),
    max_jobs: Number($('#setMaxJobs').value) || 1,
    max_cpus: Number($('#setMaxCpus').value) || 0,
    default_precision: $('#setPrec').value,
    default_gpus: $('#setGpu').checked ? 1 : 0,
    odb_open_mode: $('#setOdbMode').value,
    extra_args: $('#setExtra').value.trim(),
    clear_lck: $('#setClearLck').checked,
    beep: $('#setBeep').checked,
  });
  closeMask($('#maskSet'));
  toast('设置已保存');
  refresh();
});

/* ---------------- 循环 ---------------- */
const flag = (k) => HASH_FLAGS.indexOf(k) >= 0;
refresh().then(() => {
  loadGpu(false);   // 先取一次缓存的 GPU 结论，添加作业时好做拦截
  // 方便直接打开某个面板（也用于截图检查）
  if (flag('add')) $('#btnAdd').click();
  else if (flag('set')) $('#btnSet').click();
  else if (flag('pick')) setTimeout(() => $('#btnOdb').click(), 500);
  else if (flag('log') || flag('prob')) {
    setTimeout(() => {
      const wantProb = flag('prob');
      const row = (wantProb && document.querySelector('#tbody tr.failed'))
        || document.querySelector('#tbody tr');
      if (row) {
        row.click();
        if (wantProb) setTimeout(() => switchTab('problems'), 400);
      }
    }, 600);
  }
});
setInterval(refresh, 1000);
setInterval(tickLog, 1500);
