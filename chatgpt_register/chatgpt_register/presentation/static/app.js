const $ = id => document.getElementById(id);

function setNotice(text, tone = '') {
  $('notice').textContent = text;
  $('notice').className = tone;
}

function taskCard(task) {
  const item = document.createElement('article');
  item.className = `task ${task.status || 'pending'}`;
  const title = document.createElement('strong');
  title.textContent = task.email || '未知邮箱';
  const meta = document.createElement('span');
  const stage = task.stage || '等待';
  meta.textContent = `${task.status || 'pending'} · ${stage} · 尝试 ${task.attempts || 0}`;
  const flags = document.createElement('small');
  flags.textContent = `${task.created ? '账号已创建' : '尚未创建'} · ${task.has_session ? 'Token 已获取' : '无 Token'}${task.error_class ? ` · ${task.error_class}` : ''}`;
  item.append(title, meta, flags);
  return item;
}

async function refresh() {
  try {
    const state = await fetch('/api/state', {cache: 'no-store'}).then(r => r.json());
    $('start').disabled = state.running;
    $('stop').disabled = !state.running;
    $('success').textContent = state.summary.success || 0;
    $('partial').textContent = state.summary.partial || 0;
    $('failed').textContent = state.summary.failed || 0;
    const tasks = $('tasks');
    tasks.replaceChildren();
    if (!state.tasks.length) {
      const empty = document.createElement('p');
      empty.className = 'empty'; empty.textContent = '尚未开始任务'; tasks.append(empty);
    } else {
      state.tasks.forEach(task => tasks.append(taskCard(task)));
    }
    if (state.error) setNotice(`运行异常：${state.error}`, 'error');
    else if (!state.running && state.finished_at) setNotice(`任务结束 · ${state.run_id || '未生成运行记录'}`, 'success');
  } catch (_) {
    setNotice('无法读取服务状态', 'error');
  }
}

$('start').addEventListener('click', async () => {
  setNotice('正在提交…');
  const payload = {
    accounts: $('accounts').value,
    proxy: $('proxy').value,
    proxy_template: $('proxyTemplate').value,
    country_code: $('country').value,
    impersonate: $('impersonate').value,
    concurrency: Number($('concurrency').value),
    retry_max: Number($('retry').value),
  };
  const response = await fetch('/api/run', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload)});
  const data = await response.json();
  if (!response.ok) setNotice(data.detail || '启动失败', 'error');
  else setNotice(`已接受 ${data.count} 个账号`, 'success');
  refresh();
});

$('stop').addEventListener('click', async () => {
  await fetch('/api/stop', {method: 'POST'});
  setNotice('正在停止…');
  refresh();
});

fetch('/health').then(r => r.json()).then(data => {
  $('health').textContent = data.mail_token_configured ? '服务就绪' : '缺少邮箱令牌';
  $('health').classList.add(data.mail_token_configured ? 'ok' : 'bad');
}).catch(() => {$('health').textContent = '服务离线'; $('health').classList.add('bad');});

refresh();
setInterval(refresh, 1000);
