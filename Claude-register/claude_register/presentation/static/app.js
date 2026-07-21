"use strict";

const $ = (id) => document.getElementById(id);
const TASK_PAGE_SIZE = 100;
const RESULT_CARD_LIMIT = 50;

const STAGE_LABELS = {
  arkose: "Arkose",
  send: "发信",
  mail: "抓邮件",
  verify: "验证",
  onboarding: "初始化",
  kyc: "KYC",
  "": "-",
};

const STATUS_LABELS = {
  success: "成功",
  failed: "失败",
  running: "进行中",
  pending: "待处理",
  partial: "部分完成",
};

const MODE_COPY = {
  register: {
    title: "注册新号",
    note: "完成 magic link 验证后继续初始化账号。",
    start: "开始注册",
    running: "注册中",
    started: "已启动注册",
    accounts: "待注册账号",
  },
  session: {
    title: "提 Session",
    note: "已注册账号只完成登录验证并提取会话，不运行 Onboarding。",
    start: "开始提 Session",
    running: "提取 Session 中",
    started: "已启动 Session 提取",
    accounts: "已注册账号",
  },
};

const ACCOUNT_PLACEHOLDERS = {
  mailcom: "name@mail.com----password----display_name\nother@mail.com----password----display_name",
  imap: "user@domain.com----app_password----display_name",
  microsoft: "user@outlook.com----password----client_id----refresh_token",
};

const FORMAT_HINTS = {
  mailcom: "通过 mail.xcaigc.com 的 mailcom provider 取信。",
  imap: "通过 mail.xcaigc.com 的 IMAP provider 取信；请使用邮箱密码或应用专用密码。",
  microsoft: "通过 mail.xcaigc.com 的 Microsoft provider 取信；需要 client_id 和 refresh_token。",
};

const RESULT_TARGETS = {
  pass: "res_pass",
  required: "res_required",
  unknown: "res_unknown",
  dead: "res_dead",
};

const RESULT_DOWNLOADS = {
  pass: "kyc_pass.txt",
  required: "kyc_required.txt",
  unknown: "kyc_unknown.txt",
  dead: "kyc_dead.txt",
};

const state = {
  tasks: [],
  summary: {},
  flowMode: "register",
  taskFilter: "all",
  searchQuery: "",
  currentPage: 1,
  pageSize: TASK_PAGE_SIZE,
  runId: sessionStorage.getItem("activeRunId") || "",
  pendingRunId: "",
  startPending: false,
  startPendingAt: 0,
  stopPending: false,
  eventSource: null,
  liveTicker: null,
  activeDetailTaskId: "",
  taskIndexes: new Map(),
  rowElements: new Map(),
  rowSignatures: new Map(),
};

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>'"]/g, (character) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    "'": "&#39;",
    '"': "&quot;",
  })[character]);
}

function numberValue(id, fallback) {
  const value = Number($(id).value);
  return Number.isFinite(value) ? value : fallback;
}

function accountCount(text) {
  return String(text || "")
    .split(/\r?\n/)
    .filter((line) => line.trim() && !line.trim().startsWith("#"))
    .length;
}

function showToast(message, kind = "info") {
  const toast = document.createElement("div");
  toast.className = `toast${kind === "error" ? " is-error" : ""}`;
  toast.textContent = message;
  $("toast_region").append(toast);
  window.setTimeout(() => toast.remove(), 3600);
}

function showError(message) {
  $("error_message").textContent = String(message || "发生未知错误");
  $("error_banner").hidden = false;
}

function clearError() {
  $("error_banner").hidden = true;
  $("error_message").textContent = "";
}

function markLastUpdate() {
  $("last_update").textContent = `更新于 ${new Date().toLocaleTimeString("zh-CN", { hour12: false })}`;
}

function stampTaskSnapshot(task) {
  if (!task || typeof task !== "object") return task;
  const now = Date.now();
  task._clientUpdatedAtMs = now;
  task._clientElapsedBase = Number(task.elapsed || 0);
  task._clientStageElapsedBaseMs = Number(task.stage_elapsed_ms || 0);
  return task;
}

function stampTasks(tasks) {
  return Array.isArray(tasks) ? tasks.map((task) => stampTaskSnapshot(task)) : [];
}

function liveElapsed(task) {
  const base = Number(task?._clientElapsedBase ?? task?.elapsed ?? 0);
  if (task?.status !== "running") return base;
  const updatedAt = Number(task._clientUpdatedAtMs || Date.now());
  return Math.max(0, Math.round((base + (Date.now() - updatedAt) / 1000) * 10) / 10);
}

function liveStageElapsedMs(task) {
  const base = Number(task?._clientStageElapsedBaseMs ?? task?.stage_elapsed_ms ?? 0);
  if (task?.status !== "running") return base;
  const updatedAt = Number(task._clientUpdatedAtMs || Date.now());
  return Math.max(0, Math.round(base + Date.now() - updatedAt));
}

function hasRunningTasks() {
  return state.tasks.some((task) => task.status === "running");
}

function updateAccessScope() {
  const localHosts = new Set(["localhost", "127.0.0.1", "::1", "[::1]"]);
  const isLocal = localHosts.has(window.location.hostname);
  const element = $("access_scope");
  element.textContent = isLocal ? "本机访问" : "远程访问";
  element.classList.toggle("is-remote", !isLocal);
}

function updateRunButtonPair(kind, disabled, label) {
  [$("btn_" + kind), $("mobile_btn_" + kind)].forEach((button) => {
    button.disabled = disabled;
    button.textContent = label;
  });
}

function setConnectionStatus(status) {
  const element = $("connection_status");
  const labels = {
    online: "实时连接正常",
    connecting: "正在连接",
    offline: "连接已断开",
  };
  element.className = `connection-pill is-${status}`;
  element.lastChild.textContent = labels[status] || labels.connecting;
}

async function fetchJson(url, options) {
  const response = await fetch(url, options);
  let payload = {};
  try {
    payload = await response.json();
  } catch (_error) {
    payload = {};
  }
  if (!response.ok) {
    const detail = typeof payload.detail === "string" ? payload.detail : "";
    throw new Error(payload.error || detail || `请求失败（HTTP ${response.status}）`);
  }
  return payload;
}

async function loadConfig() {
  try {
    const config = await fetchJson("/api/config");
    $("proxy_mode").value = "configured";
    $("proxy_preview").textContent = config.proxy_configured
      ? `服务端已配置：${config.proxy_preview || "已脱敏"}`
      : "服务端未配置代理";
    $("impersonate").value = config.impersonate || "chrome142";
    $("concurrency").value = config.concurrency ?? 2;
    $("retry_max").value = config.retry_max ?? 2;
    $("auto_send").checked = config.auto_send !== false;
    $("mail_fast_path").checked = config.mail_fast_path === true;
    $("send_settle_delay").value = config.send_settle_delay == null ? "" : config.send_settle_delay;
    $("resolve_exit_ip").checked = config.resolve_exit_ip === true;
    $("mail_provider").value = ["mailcom", "imap", "microsoft"].includes(config.mail_provider)
      ? config.mail_provider
      : "mailcom";
    $("mail_poll").value = config.mail_poll_interval ?? 3;
    setFlowMode(config.flow_mode === "session" ? "session" : "register");
    syncProxyMode();
    syncMailProvider();
  } catch (_error) {
    $("status_line").textContent = "本地配置读取失败，仍可手动填写后运行。";
    showError("本地配置读取失败；请检查服务状态，或手动填写后重试。");
    showToast("无法读取本地配置，请确认服务状态。", "error");
  }
}

function syncProxyMode() {
  const override = $("proxy_mode").value === "override";
  $("proxy_template").disabled = !override;
  if (!override) $("proxy_template").value = "";
}

async function restoreCurrentRun() {
  try {
    const current = await fetchJson("/api/current-run");
    if (current.running && current.run_id) {
      if (!state.runId || state.runId === current.run_id) {
        state.runId = current.run_id;
        sessionStorage.setItem("activeRunId", state.runId);
        setFlowMode(current.flow_mode);
        setRunDownloads(state.runId);
      } else {
        sessionStorage.removeItem("activeRunId");
        state.runId = "";
        setRunDownloads("");
      }
      return;
    }
    sessionStorage.removeItem("activeRunId");
    state.runId = "";
    setRunDownloads("");
  } catch (_error) {
    sessionStorage.removeItem("activeRunId");
    state.runId = "";
  }
}

function setFlowMode(mode) {
  state.flowMode = mode === "session" ? "session" : "register";
  const copy = MODE_COPY[state.flowMode];
  document.querySelectorAll("[data-flow-mode]").forEach((button) => {
    const active = button.dataset.flowMode === state.flowMode;
    button.classList.toggle("is-active", active);
    button.setAttribute("aria-selected", active ? "true" : "false");
    button.tabIndex = active ? 0 : -1;
  });
  $("mode_note_title").textContent = copy.title;
  $("mode_note_text").textContent = copy.note;
  $("accounts_label").textContent = copy.accounts;
  if (!state.startPending) {
    $("btn_start").textContent = copy.start;
    $("mobile_btn_start").textContent = copy.start;
  }
}

function syncMailProvider() {
  const provider = $("mail_provider").value || "mailcom";
  $("format_hint").textContent = FORMAT_HINTS[provider] || FORMAT_HINTS.mailcom;
  $("accounts_text").placeholder = ACCOUNT_PLACEHOLDERS[provider] || ACCOUNT_PLACEHOLDERS.mailcom;
}

function updateAccountCount() {
  const count = accountCount($("accounts_text").value);
  $("account_count").textContent = `${count} 个账号`;
}

function setRunDownloads(runId) {
  document.querySelectorAll("[data-run-download]").forEach((link) => {
    const filename = link.dataset.runDownload;
    if (!runId || !filename) {
      link.hidden = true;
      link.removeAttribute("href");
      return;
    }
    link.href = `/api/runs/${encodeURIComponent(runId)}/${encodeURIComponent(filename)}`;
    link.hidden = false;
  });
}

function statusLabel(status) {
  return STATUS_LABELS[status] || status || "-";
}

function kycMeta(status) {
  if (status === "not_required" || status === "approved") return { label: "无需 / 已过", tone: "pass" };
  if (status === "pending" || status === "denied") return { label: "需处理", tone: "required" };
  if (status === "dead") return { label: "失效", tone: "dead" };
  if (status) return { label: "未知", tone: "unknown" };
  return { label: "-", tone: "" };
}

function renderSummary(summary) {
  const safe = summary || {};
  $("s_total").textContent = safe.total || 0;
  $("s_running").textContent = safe.running || 0;
  $("s_pending").textContent = safe.pending || 0;
  $("s_success").textContent = safe.success || 0;
  $("s_partial").textContent = safe.partial || 0;
  $("s_failed").textContent = safe.failed || 0;
  $("s_kyc_pass").textContent = safe.kyc_pass || 0;
  $("s_kyc_required").textContent = safe.kyc_required || 0;
  $("s_kyc_unknown").textContent = safe.kyc_unknown || 0;
  $("s_kyc_dead").textContent = safe.kyc_dead || 0;
  $("cnt_pass").textContent = safe.kyc_pass || 0;
  $("cnt_required").textContent = safe.kyc_required || 0;
  $("cnt_unknown").textContent = safe.kyc_unknown || 0;
  $("cnt_dead").textContent = safe.kyc_dead || 0;
}

function filteredTasks() {
  const query = state.searchQuery.trim().toLowerCase();
  const matches = [];
  state.tasks.forEach((task, index) => {
    if (state.taskFilter !== "all" && task.status !== state.taskFilter) return;
    const matchesQuery = !query || [
      task.email, task.display_name, task.worker_id, task.error_class, task.stage,
    ].some((value) => String(value || "").toLowerCase().includes(query));
    if (matchesQuery) matches.push({ task, index });
  });
  return matches;
}

function taskRowMarkup(task, index) {
  const kyc = kycMeta(task.kyc_status);
  const detail = task.error_class || task.display_name || "-";
  return `
    <td class="account-cell">
      <strong title="${escapeHtml(task.email)}">${escapeHtml(task.email)}</strong>
      <small>${escapeHtml(detail)}</small>
    </td>
    <td>${escapeHtml(STAGE_LABELS[task.stage] || task.stage || "-")}</td>
    <td><span class="status-tag ${escapeHtml(task.status)}">${escapeHtml(statusLabel(task.status))}</span></td>
    <td><span class="kyc-tag ${kyc.tone}">${escapeHtml(kyc.label)}</span></td>
    <td class="mono">${escapeHtml(task.worker_id || "-")}</td>
    <td class="mono">${escapeHtml(task.proxy_exit_ip || "-")}</td>
    <td class="mono">${escapeHtml(liveElapsed(task))}s</td>
    <td><button class="row-action" type="button" data-task-index="${index}">详情</button></td>`;
}

function taskRow(task, index, nextElements, nextSignatures) {
  const key = `${index}:${task.email || ""}`;
  const markup = taskRowMarkup(task, index);
  const row = state.rowElements.get(key) || document.createElement("tr");
  if (state.rowSignatures.get(key) !== markup) row.innerHTML = markup;
  nextElements.set(key, row);
  nextSignatures.set(key, markup);
  return row;
}

function renderTable() {
  const matches = filteredTasks();
  const body = $("tbody");
  const pageCount = Math.max(1, Math.ceil(matches.length / state.pageSize));
  state.currentPage = Math.min(Math.max(1, state.currentPage), pageCount);
  const start = (state.currentPage - 1) * state.pageSize;
  const pageRows = matches.slice(start, start + state.pageSize);
  const visibleStart = matches.length ? start + 1 : 0;
  const visibleEnd = Math.min(start + pageRows.length, matches.length);
  $("visible_count").textContent = `显示 ${visibleStart}-${visibleEnd} / ${matches.length}（总计 ${state.tasks.length}）`;
  $("page_label").textContent = `第 ${state.currentPage} / ${pageCount} 页`;
  $("page_prev").disabled = state.currentPage <= 1;
  $("page_next").disabled = state.currentPage >= pageCount;

  if (!pageRows.length) {
    const message = state.tasks.length ? "没有符合当前筛选条件的任务。" : "暂无任务，先从左侧创建一次运行。";
    body.innerHTML = `<tr><td colspan="8" class="empty-state">${message}</td></tr>`;
    state.rowElements.clear();
    state.rowSignatures.clear();
    return;
  }

  const nextElements = new Map();
  const nextSignatures = new Map();
  const rows = pageRows.map(({ task, index }) => taskRow(task, index, nextElements, nextSignatures));
  body.replaceChildren(...rows);
  state.rowElements = nextElements;
  state.rowSignatures = nextSignatures;
}

function appendDetail(definitionList, label, value, mono = false) {
  const row = document.createElement("div");
  row.className = "detail-row";
  const term = document.createElement("dt");
  term.textContent = label;
  const description = document.createElement("dd");
  if (mono) description.className = "mono";
  description.textContent = String(value || "-");
  row.append(term, description);
  definitionList.append(row);
}

function formatMs(value) {
  const ms = Number(value || 0);
  if (!Number.isFinite(ms) || ms <= 0) return "0 ms";
  return ms >= 1000 ? `${Math.round(ms / 100) / 10}s` : `${Math.round(ms)} ms`;
}

function appendTimingBreakdown(definitionList, task) {
  const stageDurations = task.stage_durations_ms || {};
  const substageDurations = task.substage_durations_ms || {};
  const rows = [
    ["准备预热", substageDurations["send.warm_login"]],
    ["登录方式", substageDurations["send.login_methods"]],
    ["发信请求", substageDurations["send.magic_link"]],
    ["发信阶段合计", stageDurations.send],
    ["抓邮件", stageDurations.mail],
    ["Arkose", stageDurations.arkose],
    ["验证", stageDurations.verify],
    ["初始化", stageDurations.onboarding],
    ["KYC", stageDurations.kyc],
  ].filter(([, value]) => Number(value || 0) > 0);

  if (!rows.length) return;
  rows.forEach(([label, value]) => appendDetail(definitionList, label, formatMs(value), true));
}

function showTaskDetails(task) {
  if (!task) return;
  state.activeDetailTaskId = task.task_id || "";
  const content = $("task_details_content");
  const details = document.createElement("dl");
  details.className = "dialog-content-list";
  appendDetail(details, "账号", task.email, true);
  appendDetail(details, "显示名", task.display_name);
  appendDetail(details, "状态", statusLabel(task.status));
  appendDetail(details, "当前阶段", STAGE_LABELS[task.stage] || task.stage || "-");
  appendDetail(details, "KYC", kycMeta(task.kyc_status).label);
  appendDetail(details, "Worker", task.worker_id, true);
  appendDetail(details, "出口 IP", task.proxy_exit_ip, true);
  appendDetail(details, "尝试次数", task.attempts || 0);
  appendDetail(details, "队列等待", `${task.queue_wait_ms || 0} ms`, true);
  appendDetail(details, "当前阶段耗时", formatMs(liveStageElapsedMs(task)), true);
  appendTimingBreakdown(details, task);
  appendDetail(details, "Session", task.has_session ? "已生成" : "未生成");
  appendDetail(details, "结果写入", task.persistence_status || "pending");
  appendDetail(details, "错误类型", task.error_class || "无");
  content.replaceChildren(details);
  const dialog = $("task_details");
  if (!dialog.open) dialog.showModal();
}

function refreshOpenTaskDetails() {
  const dialog = $("task_details");
  if (!dialog.open || !state.activeDetailTaskId) return;
  const index = state.taskIndexes.get(state.activeDetailTaskId);
  if (index === undefined) return;
  showTaskDetails(state.tasks[index]);
}

function resultBucket(task) {
  if (task.kyc_status === "not_required" || task.kyc_status === "approved") return "pass";
  if (task.kyc_status === "pending" || task.kyc_status === "denied") return "required";
  if (task.kyc_status === "dead") return "dead";
  return "unknown";
}

function resultItem(task) {
  if (!["success", "partial"].includes(task.status) || !task.has_session) return null;
  return {
    taskId: task.task_id || task.email || "",
    email: task.email || "",
    status: task.status,
    kycStatus: task.kyc_status || "",
    persistenceStatus: task.persistence_status || "pending",
  };
}

function resultField(label, value) {
  const row = document.createElement("div");
  row.className = "result-field";
  const name = document.createElement("span");
  name.className = "result-label";
  name.textContent = label;
  const text = document.createElement("span");
  text.className = "result-value";
  text.textContent = value || "-";
  row.append(name, text);
  return row;
}

function resultCard(item) {
  const card = document.createElement("div");
  card.className = "result-card";
  card.append(resultField("邮箱", item.email));
  card.append(resultField("Session", "已生成"));
  card.append(resultField(
    "结果写入",
    item.persistenceStatus === "failed" ? "流程已完成，但结果写入失败" : item.persistenceStatus,
  ));
  return card;
}

function resetResults() {
  Object.keys(RESULT_TARGETS).forEach((bucket) => {
    $(RESULT_TARGETS[bucket]).replaceChildren();
  });
}

function renderResults() {
  const buckets = { pass: [], required: [], unknown: [], dead: [] };
  const seen = new Set();
  state.tasks.forEach((task) => {
    const item = resultItem(task);
    if (!item) return;
    const key = item.taskId;
    if (seen.has(key)) return;
    seen.add(key);
    buckets[resultBucket(task)].push(item);
  });

  Object.entries(buckets).forEach(([bucket, items]) => {
    const target = $(RESULT_TARGETS[bucket]);
    const previousTop = target.scrollTop;
    const nearBottom = previousTop + target.clientHeight >= target.scrollHeight - 24;
    const nodes = items.slice(0, RESULT_CARD_LIMIT).map(resultCard);
    target.replaceChildren(...nodes);
    target.scrollTop = nearBottom ? target.scrollHeight : previousTop;
  });
}

function syncRunControls(snapshot, ownsSnapshot) {
  if (!ownsSnapshot) {
    $("progress_bar").style.width = "0%";
    updateRunButtonPair("start", Boolean(snapshot.running) || state.startPending, state.startPending ? "启动中…" : MODE_COPY[state.flowMode].start);
    updateRunButtonPair("stop", true, "停止");
    document.querySelectorAll("[data-flow-mode]").forEach((button) => {
      button.disabled = Boolean(snapshot.running);
    });
    const message = snapshot.running
      ? "已有其他运行正在执行；本页面不会显示其任务。"
      : "就绪。填好账号后即可开始。";
    $("status_line").textContent = message;
    $("mobile_run_status").textContent = message;
    return;
  }
  const summary = snapshot.summary || {};
  const done = (summary.success || 0) + (summary.failed || 0) + (summary.partial || 0);
  const percentage = summary.total ? Math.min(100, (done / summary.total) * 100) : 0;
  $("progress_bar").style.width = `${percentage}%`;

  if (state.pendingRunId && snapshot.run_id === state.pendingRunId) {
    state.startPending = false;
    state.pendingRunId = "";
  }
  if (state.startPending && Date.now() - state.startPendingAt > 10000) {
    state.startPending = false;
    state.pendingRunId = "";
  }
  state.stopPending = state.stopPending && Boolean(snapshot.running);

  const mode = snapshot.flow_mode === "session" ? "session" : state.flowMode;
  const copy = MODE_COPY[mode];
  updateRunButtonPair(
    "start",
    Boolean(snapshot.running) || state.startPending,
    state.startPending ? "启动中…" : copy.start,
  );
  updateRunButtonPair(
    "stop",
    !snapshot.running || state.stopPending,
    state.stopPending ? "停止中…" : "停止",
  );
  document.querySelectorAll("[data-flow-mode]").forEach((button) => {
    button.disabled = Boolean(snapshot.running);
  });

  if (snapshot.running) {
    $("status_line").textContent = `${copy.running} · ${done}/${summary.total || 0} 已完成`;
  } else if (summary.total) {
    sessionStorage.removeItem("activeRunId");
    $("status_line").textContent = `本次完成：${summary.success || 0} 成功，${summary.partial || 0} 部分完成，${summary.failed || 0} 失败。`;
  } else {
    $("status_line").textContent = "就绪。填好账号后即可开始。";
  }
  $("mobile_run_status").textContent = $("status_line").textContent;
}

function resetTaskView() {
  state.taskFilter = "all";
  state.searchQuery = "";
  state.currentPage = 1;
  state.rowElements.clear();
  state.rowSignatures.clear();
  state.taskIndexes.clear();
  $("task_search").value = "";
  $("task_filter").value = "all";
  document.querySelectorAll("[data-summary-filter]").forEach((button) => {
    const active = button.dataset.summaryFilter === "all";
    button.classList.toggle("is-active", active);
    button.setAttribute("aria-pressed", active ? "true" : "false");
  });
}

function render(snapshot) {
  if (!snapshot || !Array.isArray(snapshot.tasks)) return;
  const ownsSnapshot = Boolean(state.runId && snapshot.run_id === state.runId);
  if (!ownsSnapshot) {
    if (state.runId) {
      state.runId = "";
      sessionStorage.removeItem("activeRunId");
      setRunDownloads("");
    }
    state.tasks = [];
    state.taskIndexes.clear();
    state.summary = {};
    resetResults();
    resetTaskView();
    renderSummary(state.summary);
    syncRunControls(snapshot, false);
    renderTable();
    renderResults();
    markLastUpdate();
    return;
  }
  state.tasks = stampTasks(snapshot.tasks);
  state.taskIndexes = new Map(
    state.tasks.map((task, index) => [task.task_id, index]),
  );
  state.summary = snapshot.summary || {};
  renderSummary(state.summary);
  syncRunControls(snapshot, true);
  renderTable();
  renderResults();
  markLastUpdate();
}

function parseProgressEvent(event) {
  try {
    return JSON.parse(event.data);
  } catch (_error) {
    showError("收到的任务状态格式异常；页面会继续等待下一次更新。");
    showToast("收到无法解析的任务状态。", "error");
    return null;
  }
}

function applyTaskUpdated(payload) {
  const task = stampTaskSnapshot(payload?.task);
  if (!task || payload.run_id !== state.runId || !task.task_id) return;
  const index = state.taskIndexes.get(task.task_id);
  if (index === undefined) {
    state.taskIndexes.set(task.task_id, state.tasks.length);
    state.tasks.push(task);
  } else {
    const current = state.tasks[index];
    if (Number(current?.version || 0) >= Number(task.version || 0)) return;
    state.tasks[index] = task;
  }
  renderTable();
  renderResults();
  markLastUpdate();
}

function applySummaryUpdated(payload) {
  if (!payload || payload.run_id !== state.runId) return;
  state.summary = payload.summary || {};
  renderSummary(state.summary);
  syncRunControls(payload, true);
  markLastUpdate();
}

async function startRun() {
  if (state.startPending) return;
  const body = {
    proxy_mode: $("proxy_mode").value,
    proxy_template: $("proxy_mode").value === "override"
      ? $("proxy_template").value.trim()
      : null,
    impersonate: $("impersonate").value.trim(),
    concurrency: numberValue("concurrency", 2),
    retry_max: numberValue("retry_max", 2),
    auto_send: $("auto_send").checked,
    mail_fast_path: $("mail_fast_path").checked,
    resolve_exit_ip: $("resolve_exit_ip").checked,
    flow_mode: state.flowMode,
    mail_provider: $("mail_provider").value,
    mail_poll_interval: numberValue("mail_poll", 3),
    send_settle_delay: $("send_settle_delay").value.trim()
      ? numberValue("send_settle_delay", 4)
      : null,
    accounts_text: $("accounts_text").value,
  };

  state.startPending = true;
  state.startPendingAt = Date.now();
  clearError();
  updateRunButtonPair("start", true, "启动中…");
  try {
    const result = await fetchJson("/api/start", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!result.ok) throw new Error(result.error || "启动失败");
    state.runId = result.run_id || "";
    sessionStorage.setItem("activeRunId", state.runId);
    state.pendingRunId = result.run_id || "";
    state.tasks = [];
    state.summary = {};
    resetResults();
    resetTaskView();
    setRunDownloads(state.runId);
    $("status_line").textContent = `${MODE_COPY[state.flowMode].started}，共 ${result.count} 个账号。`;
    $("mobile_run_status").textContent = $("status_line").textContent;
    const ignored = Number(result.ignored_count || 0);
    if (ignored) {
      const issues = Array.isArray(result.issues) ? result.issues.slice(0, 5) : [];
      const details = issues
        .map((issue) => `第 ${issue.line_number} 行：${issue.message}`)
        .join("；");
      showError(`已忽略 ${ignored} 行无效账号${details ? `。${details}` : ""}`);
    }
    showToast(`任务已启动：${result.count} 个账号${ignored ? `，忽略 ${ignored} 行` : ""}`);
  } catch (error) {
    state.startPending = false;
    state.pendingRunId = "";
    updateRunButtonPair("start", false, MODE_COPY[state.flowMode].start);
    showError(error.message || "启动失败，请检查服务状态。");
    showToast(error.message || "启动失败，请检查服务状态。", "error");
  }
}

function requestStopConfirmation() {
  $("stop_running_count").textContent = state.summary.running || 0;
  $("stop_pending_count").textContent = state.summary.pending || 0;
  const dialog = $("stop_confirm");
  if (!dialog.open) dialog.showModal();
  $("btn_cancel_stop").focus();
}

async function stopRun() {
  if (state.stopPending) return;
  state.stopPending = true;
  clearError();
  updateRunButtonPair("stop", true, "停止中…");
  try {
    await fetchJson("/api/stop", { method: "POST" });
    $("status_line").textContent = "已发送停止请求，正在等待当前任务退出。";
    $("mobile_run_status").textContent = $("status_line").textContent;
  } catch (error) {
    state.stopPending = false;
    updateRunButtonPair("stop", false, "停止");
    showError(error.message || "停止失败，请稍后重试。");
    showToast(error.message || "停止失败，请稍后重试。", "error");
  }
}

async function copyBucket(bucket) {
  const filename = RESULT_DOWNLOADS[bucket];
  if (!state.runId || !filename) {
    showToast("当前分组暂无可复制内容。", "error");
    return;
  }
  try {
    const response = await fetch(`/api/runs/${encodeURIComponent(state.runId)}/${encodeURIComponent(filename)}`);
    if (!response.ok) throw new Error("暂无可复制内容");
    const text = (await response.text()).trim();
    if (!text) throw new Error("暂无可复制内容");
    let copied = false;
    if (navigator.clipboard?.writeText) {
      try {
        await navigator.clipboard.writeText(`${text}\n`);
        copied = true;
      } catch (_error) {
        copied = false;
      }
    }
    if (!copied) {
      const textarea = document.createElement("textarea");
      textarea.value = `${text}\n`;
      textarea.setAttribute("readonly", "");
      textarea.style.position = "fixed";
      textarea.style.opacity = "0";
      document.body.append(textarea);
      textarea.select();
      copied = document.execCommand("copy");
      textarea.remove();
      if (!copied) throw new Error("复制失败");
    }
    showToast(`已复制 ${text.split("\n").length} 条结果。`);
  } catch (error) {
    if (error?.message === "暂无可复制内容") {
      showToast(error.message, "error");
      return;
    }
    showToast("复制失败，请使用下载文件。", "error");
  }
}

function setTaskFilter(filter) {
  state.taskFilter = filter || "all";
  state.currentPage = 1;
  $("task_filter").value = state.taskFilter;
  document.querySelectorAll("[data-summary-filter]").forEach((button) => {
    const active = button.dataset.summaryFilter === state.taskFilter;
    button.classList.toggle("is-active", active);
    button.setAttribute("aria-pressed", active ? "true" : "false");
  });
  renderTable();
}

function connectProgress() {
  if (state.eventSource) state.eventSource.close();
  setConnectionStatus("connecting");

  const source = new EventSource("/api/progress");
  state.eventSource = source;
  source.onopen = () => setConnectionStatus("online");
  source.onmessage = (event) => {
    const payload = parseProgressEvent(event);
    if (payload) render(payload);
  };
  source.addEventListener("run_started", (event) => {
    const payload = parseProgressEvent(event);
    if (!payload) return;
    if (!state.runId && state.startPending && payload.run_id) {
      state.runId = payload.run_id;
      sessionStorage.setItem("activeRunId", state.runId);
      setRunDownloads(state.runId);
    }
    render(payload);
  });
  source.addEventListener("task_updated", (event) => {
    const payload = parseProgressEvent(event);
    if (payload) applyTaskUpdated(payload);
  });
  source.addEventListener("summary_updated", (event) => {
    const payload = parseProgressEvent(event);
    if (payload) applySummaryUpdated(payload);
  });
  source.addEventListener("run_finished", (event) => {
    const payload = parseProgressEvent(event);
    if (payload) applySummaryUpdated(payload);
  });
  source.addEventListener("heartbeat", () => {
    setConnectionStatus("online");
    markLastUpdate();
  });
  source.onerror = () => {
    setConnectionStatus("offline");
  };
}

function startLiveTicker() {
  if (state.liveTicker) return;
  state.liveTicker = window.setInterval(() => {
    markLastUpdate();
    if (!hasRunningTasks()) return;
    renderTable();
    refreshOpenTaskDetails();
  }, 1000);
}

function bindEvents() {
  document.querySelectorAll("[data-flow-mode]").forEach((button) => {
    button.addEventListener("click", () => setFlowMode(button.dataset.flowMode));
    button.addEventListener("keydown", (event) => {
      if (!["ArrowLeft", "ArrowRight"].includes(event.key)) return;
      event.preventDefault();
      const tabs = Array.from(document.querySelectorAll("[data-flow-mode]"));
      const offset = event.key === "ArrowRight" ? 1 : -1;
      const index = (tabs.indexOf(button) + offset + tabs.length) % tabs.length;
      tabs[index].focus();
      tabs[index].click();
    });
  });
  document.querySelectorAll("[data-summary-filter]").forEach((button) => {
    button.addEventListener("click", () => setTaskFilter(button.dataset.summaryFilter));
  });

  $("mail_provider").addEventListener("change", syncMailProvider);
  $("proxy_mode").addEventListener("change", syncProxyMode);
  $("accounts_text").addEventListener("input", updateAccountCount);
  $("task_search").addEventListener("input", (event) => {
    state.searchQuery = event.target.value;
    state.currentPage = 1;
    renderTable();
  });
  $("task_filter").addEventListener("change", (event) => setTaskFilter(event.target.value));
  $("task_page_size").addEventListener("change", (event) => {
    const pageSize = Number(event.target.value);
    state.pageSize = [50, 100, 200].includes(pageSize) ? pageSize : TASK_PAGE_SIZE;
    state.currentPage = 1;
    renderTable();
  });
  $("page_prev").addEventListener("click", () => {
    state.currentPage = Math.max(1, state.currentPage - 1);
    renderTable();
  });
  $("page_next").addEventListener("click", () => {
    state.currentPage += 1;
    renderTable();
  });
  $("btn_start").addEventListener("click", startRun);
  $("mobile_btn_start").addEventListener("click", startRun);
  $("btn_stop").addEventListener("click", requestStopConfirmation);
  $("mobile_btn_stop").addEventListener("click", requestStopConfirmation);
  $("btn_dismiss_error").addEventListener("click", clearError);

  $("btn_cancel_stop").addEventListener("click", () => $("stop_confirm").close());
  $("btn_confirm_stop").addEventListener("click", () => {
    $("stop_confirm").close();
    stopRun();
  });
  $("stop_confirm").addEventListener("click", (event) => {
    if (event.target === $("stop_confirm")) $("stop_confirm").close();
  });

  $("tbody").addEventListener("click", (event) => {
    const button = event.target.closest("[data-task-index]");
    if (!button) return;
    showTaskDetails(state.tasks[Number(button.dataset.taskIndex)]);
  });

  $("btn_close_details").addEventListener("click", () => $("task_details").close());
  $("task_details").addEventListener("click", (event) => {
    if (event.target === $("task_details")) $("task_details").close();
  });
  $("task_details").addEventListener("close", () => {
    state.activeDetailTaskId = "";
  });

  $("btn_copy_pass").addEventListener("click", () => copyBucket("pass"));
  $("btn_copy_required").addEventListener("click", () => copyBucket("required"));
  $("btn_copy_unknown").addEventListener("click", () => copyBucket("unknown"));
  $("btn_copy_dead").addEventListener("click", () => copyBucket("dead"));
}

bindEvents();
updateAccessScope();
updateAccountCount();
startLiveTicker();

async function bootstrap() {
  await loadConfig();
  await restoreCurrentRun();
  connectProgress();
}

bootstrap();
