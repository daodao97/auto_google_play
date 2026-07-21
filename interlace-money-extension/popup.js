const fields = [
  "username",
  "password",
  "source",
  "submitUrl",
  "apiKey",
  "reportIntervalSeconds",
  "autoLogin",
  "autoSubmit",
  "autoProbe",
  "probeUrl",
  "probeIntervalSeconds"
];

function $(id) {
  return document.getElementById(id);
}

function setStatus(message, isError = false) {
  const el = $("status");
  el.textContent = message;
  el.style.color = isError ? "#c62828" : "#2357d5";
}

function renderAutomationState(enabled) {
  const el = $("automationState");
  el.textContent = enabled ? "运行中" : "已停止";
  el.className = `state ${enabled ? "running" : "stopped"}`;
}

function renderTabMonitor(status) {
  const state = $("tabMonitorState");
  const url = $("tabMonitorUrl");
  if (!status.enabled) {
    state.textContent = "标签页监测：已暂停";
    state.className = "monitor-state paused";
  } else if (status.tabPresent && status.leaderTabId) {
    state.textContent = `主标签页：正常（共 ${status.tabCount} 个 Interlace tab）`;
    state.className = "monitor-state ok";
  } else if (status.tabPresent) {
    state.textContent = "主标签页：正在选举";
    state.className = "monitor-state missing";
  } else {
    state.textContent = "标签页监测：未找到，正在自动打开";
    state.className = "monitor-state missing";
  }
  url.textContent = status.leaderUrl || "";
}

async function loadAutomationStatus() {
  const response = await chrome.runtime.sendMessage({ type: "GET_AUTOMATION_STATUS" });
  if (!response || !response.ok) throw new Error(response && response.error || "读取监测状态失败");
  renderAutomationState(response.status.enabled);
  renderTabMonitor(response.status);
}

function formatTime(ts) {
  if (!ts) return "-";
  return new Date(ts).toLocaleString("zh-CN", {
    hour12: false,
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit"
  });
}

function summarizeDetails(details = {}) {
  const parts = [];
  if (details.status) parts.push(`HTTP ${details.status}`);
  if (details.source) parts.push(`source=${details.source}`);
  if (details.mode) parts.push(`mode=${details.mode}`);
  if (details.via) parts.push(`via=${details.via}`);
  if (details.force) parts.push("force=true");
  if (details.tokenPreview) parts.push(`token=${details.tokenPreview}`);
  if (details.error) parts.push(details.error);
  if (details.url) parts.push(details.url);
  return parts.join(" · ");
}

async function loadLogs() {
  const response = await chrome.runtime.sendMessage({ type: "GET_LOGS" });
  if (!response || !response.ok) throw new Error(response && response.error || "读取日志失败");
  renderLastTokenEvent(response.lastTokenEvent);

  const list = $("logsList");
  list.innerHTML = "";
  if (!response.logs || response.logs.length === 0) {
    const empty = document.createElement("li");
    empty.className = "empty";
    empty.textContent = "暂无运行日志。";
    list.appendChild(empty);
    return;
  }

  for (const log of response.logs) {
    const item = document.createElement("li");
    item.className = `log ${log.level || "info"}`;
    const line = document.createElement("div");
    line.className = "log-line";
    const time = document.createElement("span");
    time.className = "log-time";
    time.textContent = formatTime(log.at);
    const message = document.createElement("span");
    message.className = "log-message";
    message.textContent = log.message || "";
    line.append(time, message);
    item.appendChild(line);

    const details = summarizeDetails(log.details);
    if (details) {
      const meta = document.createElement("div");
      meta.className = "log-meta";
      meta.textContent = details;
      item.appendChild(meta);
    }
    list.appendChild(item);
  }
}

function renderLastTokenEvent(event) {
  const el = $("lastTokenEvent");
  if (!event) {
    el.textContent = "最近 token 事件：暂无";
    return;
  }
  const meta = [formatTime(event.at)];
  if (event.reason) meta.push(event.reason);
  if (event.source) meta.push(`source=${event.source}`);
  if (event.tokenPreview) meta.push(`token=${event.tokenPreview}`);
  if (event.captureMeta && event.captureMeta.via) meta.push(`via=${event.captureMeta.via}`);
  el.textContent = `最近 token 事件：${meta.join(" · ")}`;
}

async function loadConfig() {
  const response = await chrome.runtime.sendMessage({ type: "GET_CONFIG" });
  if (!response || !response.ok) throw new Error(response && response.error || "读取配置失败");
  for (const field of fields) {
    const el = $(field);
    if (el.type === "checkbox") el.checked = Boolean(response.config[field]);
    else el.value = response.config[field] ?? "";
  }
  renderAutomationState(response.config.automationEnabled !== false);
}

async function saveConfig() {
  const config = {};
  for (const field of fields) {
    const el = $(field);
    if (el.type === "checkbox") config[field] = el.checked;
    else if (el.type === "number") config[field] = Number(el.value || (field === "probeIntervalSeconds" ? 30 : 60));
    else config[field] = el.value.trim();
  }
  const response = await chrome.runtime.sendMessage({ type: "SAVE_CONFIG", config });
  if (!response || !response.ok) throw new Error(response && response.error || "保存失败");
  setStatus("已保存，当前 Interlace 页面立即生效。");
}

async function openInterlace() {
  await saveConfig();
  const response = await chrome.runtime.sendMessage({ type: "OPEN_INTERLACE" });
  if (!response || !response.ok) throw new Error(response && response.error || "打开页面失败");
  setStatus(response.result && response.result.reused ? "已切换到现有 Interlace 标签页。" : "已打开 Interlace。");
  await loadLogs();
}

async function startAutomation() {
  await saveConfig();
  const response = await chrome.runtime.sendMessage({ type: "START_AUTOMATION" });
  if (!response || !response.ok) throw new Error(response && response.error || "启动失败");
  renderAutomationState(true);
  setStatus(response.result && response.result.created
    ? "自动化已开始，并已打开 Interlace。"
    : "自动化已开始，正在使用现有 Interlace 标签页。");
  await loadLogs();
  await loadAutomationStatus();
}

async function stopAutomation() {
  const response = await chrome.runtime.sendMessage({ type: "STOP_AUTOMATION" });
  if (!response || !response.ok) throw new Error(response && response.error || "停止失败");
  renderAutomationState(false);
  setStatus("自动化已停止。自动登录、上报和探活均已暂停。");
  await loadLogs();
  await loadAutomationStatus();
}

document.addEventListener("DOMContentLoaded", () => {
  loadConfig().catch((error) => setStatus(error.message, true));
  loadAutomationStatus().catch((error) => setStatus(error.message, true));
  loadLogs().catch((error) => setStatus(error.message, true));
  $("save").addEventListener("click", () => saveConfig().catch((error) => setStatus(error.message, true)));
  $("open").addEventListener("click", () => openInterlace().catch((error) => setStatus(error.message, true)));
  $("start").addEventListener("click", () => startAutomation().catch((error) => setStatus(error.message, true)));
  $("stop").addEventListener("click", () => stopAutomation().catch((error) => setStatus(error.message, true)));
  $("refreshLogs").addEventListener("click", () => loadLogs().catch((error) => setStatus(error.message, true)));
  $("clearLogs").addEventListener("click", async () => {
    await chrome.runtime.sendMessage({ type: "CLEAR_LOGS" });
    await loadLogs();
    setStatus("日志已清空。");
  });
  chrome.storage.onChanged.addListener((changes, areaName) => {
    if (areaName === "local" && changes.automationEnabled) {
      renderAutomationState(changes.automationEnabled.newValue !== false);
      loadAutomationStatus().catch((error) => setStatus(error.message, true));
    }
    if (areaName === "local" && changes.submitLogs) loadLogs().catch((error) => setStatus(error.message, true));
  });
  window.setInterval(() => {
    loadAutomationStatus().catch(() => undefined);
  }, 2000);
});
