(function () {
  "use strict";

  if (window.__INTERLACE_MONEY_AUTOMATION_LOADED__) return;
  window.__INTERLACE_MONEY_AUTOMATION_LOADED__ = true;

  const LOG_PREFIX = "[Interlace Automation]";
  const LOGIN_POLL_INTERVAL_MS = 1500;
  const MAX_LOGIN_ATTEMPTS = 80;
  const MIN_REPORT_INTERVAL_SECONDS = 15;

  let config = null;
  let running = false;
  let clicked = false;
  let attempts = 0;
  let lastSubmittedAuth = "";
  let latestCapturedAuth = "";
  let periodicTimer = null;
  let loginTimer = null;
  let loginObserver = null;
  let loginMutationTimer = null;
  let isLeaderTab = false;
  let urlWatchTimer = null;
  let lastSeenUrl = location.href;
  let wasOnSignInPage = isSignInPath(location.pathname);

  function log(message, ...args) {
    console.log(LOG_PREFIX, message, ...args);
  }

  function panelLog(level, message, details = {}) {
    chrome.runtime.sendMessage({
      type: "ADD_LOG",
      level,
      message,
      details: {
        pageUrl: location.href,
        ...details
      }
    }).catch(() => undefined);
  }

  function sendProbeConfig() {
    if (!config) return;
    window.postMessage({
      type: "INTERLACE_MONEY_PROBE_CONFIG",
      enabled: isLeaderTab && config.automationEnabled !== false && Boolean(config.autoProbe),
      url: config.probeUrl || "",
      intervalSeconds: Number(config.probeIntervalSeconds || 30)
    }, window.location.origin);
  }

  function sleep(ms) {
    return new Promise((resolve) => setTimeout(resolve, ms));
  }

  function isVisible(el) {
    if (!el) return false;
    const style = window.getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    return style.visibility !== "hidden"
      && style.display !== "none"
      && rect.width > 0
      && rect.height > 0;
  }

  function textOf(el) {
    return [
      el.getAttribute("placeholder"),
      el.getAttribute("aria-label"),
      el.getAttribute("name"),
      el.getAttribute("id"),
      el.getAttribute("autocomplete")
    ].filter(Boolean).join(" ").toLowerCase();
  }

  function getUsernameInput() {
    const inputs = Array.from(document.querySelectorAll("input"))
      .filter((input) => input.type !== "hidden" && input.type !== "password" && !input.disabled && isVisible(input));

    const preferred = inputs.find((input) => {
      const text = textOf(input);
      return /用户名|账号|账户|手机号|手机|邮箱|邮件|email|e-mail|phone|mobile|user|username|account|login/.test(text);
    });

    return preferred || inputs.find((input) => ["text", "email", "tel", "search", ""].includes(input.type)) || null;
  }

  function getPasswordInput() {
    return Array.from(document.querySelectorAll('input[type="password"]'))
      .find((input) => !input.disabled && isVisible(input)) || null;
  }

  function getLoginButton() {
    const candidates = Array.from(document.querySelectorAll('button, [role="button"], input[type="submit"], input[type="button"]'))
      .filter((el) => !el.disabled && isVisible(el));

    return candidates.find((el) => {
      const text = `${el.textContent || ""} ${el.value || ""} ${el.getAttribute("aria-label") || ""}`.trim();
      return /登录|登入|登陆|继续|下一步|login|log in|sign in|continue|next/i.test(text);
    }) || candidates.find((el) => {
      const type = String(el.getAttribute("type") || "").toLowerCase();
      return type === "submit";
    }) || null;
  }

  function isButtonReady(btn) {
    if (!btn) return false;
    return !btn.disabled
      && !String(btn.className || "").includes("disabled")
      && btn.getAttribute("aria-disabled") !== "true";
  }

  function setNativeValue(el, value) {
    const proto = el instanceof HTMLTextAreaElement
      ? HTMLTextAreaElement.prototype
      : HTMLInputElement.prototype;
    const desc = Object.getOwnPropertyDescriptor(proto, "value");
    if (desc && desc.set) desc.set.call(el, value);
    else el.value = value;
  }

  async function fillField(el, value) {
    if (!el || value === undefined || value === null) return false;
    el.scrollIntoView({ block: "center", inline: "nearest" });
    await sleep(80);
    el.focus();
    setNativeValue(el, String(value));
    el.dispatchEvent(new KeyboardEvent("keydown", { bubbles: true }));
    el.dispatchEvent(new InputEvent("beforeinput", { bubbles: true, inputType: "insertText", data: String(value) }));
    el.dispatchEvent(new InputEvent("input", { bubbles: true, inputType: "insertText", data: String(value) }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
    el.dispatchEvent(new KeyboardEvent("keyup", { bubbles: true }));
    await sleep(80);
    el.blur();
    return true;
  }

  function isSignInPath(pathname) {
    return /(^|\/)sign[-_]?in(\/|$)|(^|\/)login(\/|$)|(^|\/)auth(\/|$)/i.test(String(pathname || ""));
  }

  function isSignInPage() {
    return isSignInPath(location.pathname);
  }

  function looksLikeLoginPage() {
    if (/sign[-_/]?in|login|auth/i.test(location.pathname)) return true;
    return Boolean(getPasswordInput()) && Boolean(getUsernameInput());
  }

  async function attemptLogin() {
    if (!isLeaderTab || !config || config.automationEnabled === false || !config.autoLogin || running || clicked) return;
    if (!config.username || !config.password) return;
    if (!looksLikeLoginPage()) return;

    attempts += 1;
    if (attempts > MAX_LOGIN_ATTEMPTS) return;

    running = true;
    try {
      const usernameInput = getUsernameInput();
      const passwordInput = getPasswordInput();

      if (!usernameInput && !passwordInput) {
        panelLog("warn", "自动登录跳过：未找到用户名或密码输入框", {
          hasUsernameInput: Boolean(usernameInput),
          hasPasswordInput: Boolean(passwordInput),
          attempt: attempts
        });
        return;
      }

      if (usernameInput && usernameInput.value !== String(config.username)) {
        await fillField(usernameInput, config.username);
      }
      if (passwordInput && passwordInput.value !== String(config.password)) {
        await fillField(passwordInput, config.password);
      }
      await sleep(400);

      const usernameOk = !usernameInput || usernameInput.value === String(config.username);
      const passwordOk = !passwordInput || passwordInput.value === String(config.password);
      panelLog(usernameOk && passwordOk ? "info" : "warn", "自动登录填充检查", {
        attempt: attempts,
        stage: usernameInput && !passwordInput ? "username" : "password",
        usernameLength: usernameInput ? usernameInput.value.length : 0,
        passwordLength: passwordInput ? passwordInput.value.length : 0,
        usernameOk,
        passwordOk
      });

      if (!usernameOk || !passwordOk) {
        clicked = false;
        return;
      }

      const btn = getLoginButton();
      if (isButtonReady(btn)) {
        clicked = true;
        const isUsernameStage = Boolean(usernameInput && !passwordInput);
        log("login form ready, clicking submit");
        panelLog("info", isUsernameStage ? "自动登录：提交用户名步骤" : "自动登录：点击登录按钮", {
          attempt: attempts,
          stage: isUsernameStage ? "username" : "password",
          buttonText: (btn.textContent || btn.value || "").trim()
        });
        btn.click();
        setTimeout(() => {
          if (isUsernameStage) {
            clicked = false;
            scheduleLoginAttempt(100);
            return;
          }
          if (!looksLikeLoginPage()) return;
          const currentUsernameInput = getUsernameInput();
          const currentPasswordInput = getPasswordInput();
          const usernameLength = currentUsernameInput ? currentUsernameInput.value.length : 0;
          const passwordLength = currentPasswordInput ? currentPasswordInput.value.length : 0;

          clicked = false;
          panelLog("warn", "自动登录后仍在登录页，准备重试", {
            attempt: attempts,
            usernameLength,
            passwordLength
          });
        }, isUsernameStage ? 2000 : 5000);
      } else {
        panelLog("warn", "自动登录跳过：登录按钮不可点击", {
          attempt: attempts,
          hasButton: Boolean(btn),
          buttonText: btn ? (btn.textContent || btn.value || "").trim() : ""
        });
      }
    } finally {
      running = false;
    }
  }

  async function getConfig() {
    const response = await chrome.runtime.sendMessage({ type: "GET_CONFIG" });
    if (!response || !response.ok) {
      throw new Error(response && response.error || "failed to load config");
    }
    config = response.config;
  }

  function startLoginLoop() {
    if (loginTimer) window.clearInterval(loginTimer);
    if (loginObserver) loginObserver.disconnect();
    if (loginMutationTimer) window.clearTimeout(loginMutationTimer);
    loginTimer = null;
    loginObserver = null;
    loginMutationTimer = null;
    if (!isLeaderTab || !config || config.automationEnabled === false) return;

    loginTimer = window.setInterval(() => {
      if (attempts > MAX_LOGIN_ATTEMPTS) {
        // Never give up permanently: if we are still stranded on the sign-in
        // page (session expired again), reset the budget and keep re-logging in
        // so the logged-in state is maintained automatically.
        if (isSignInPage()) {
          attempts = 0;
          clicked = false;
          panelLog("warn", "自动登录：仍在登录页，重置重试计数继续尝试", { attempts });
        } else {
          return;
        }
      }
      attemptLogin().catch((error) => log("login attempt failed", error));
    }, LOGIN_POLL_INTERVAL_MS);

    window.setTimeout(() => {
      attemptLogin().catch((error) => log("initial login attempt failed", error));
    }, 1200);

    if (document.documentElement) {
      loginObserver = new MutationObserver(() => scheduleLoginAttempt(150));
      loginObserver.observe(document.documentElement, { childList: true, subtree: true });
    }
  }

  function handlePossibleUrlChange() {
    if (location.href === lastSeenUrl) return;
    lastSeenUrl = location.href;

    const onSignIn = isSignInPage();
    // Detect a fresh transition INTO the sign-in page (typically the app's own
    // 401 interceptor doing a client-side redirect, so scripts are not
    // re-injected). Reset the retry budget and re-kick the login loop.
    if (onSignIn && !wasOnSignInPage) {
      attempts = 0;
      clicked = false;
      running = false;
      panelLog("warn", "检测到跳转至登录页，开始自动重新登录", { url: location.href });
      if (isLeaderTab && config && config.automationEnabled !== false) {
        startLoginLoop();
      }
    }
    wasOnSignInPage = onSignIn;
  }

  function startUrlWatch() {
    if (urlWatchTimer) return;
    urlWatchTimer = window.setInterval(handlePossibleUrlChange, 1000);
    window.addEventListener("popstate", handlePossibleUrlChange);
    window.addEventListener("hashchange", handlePossibleUrlChange);
  }

  function scheduleLoginAttempt(delay = 150) {
    if (!isLeaderTab || !config || config.automationEnabled === false) return;
    if (loginMutationTimer) window.clearTimeout(loginMutationTimer);
    loginMutationTimer = window.setTimeout(() => {
      loginMutationTimer = null;
      attemptLogin().catch((error) => log("scheduled login attempt failed", error));
    }, delay);
  }

  function stopLoginLoop() {
    if (loginTimer) window.clearInterval(loginTimer);
    if (loginObserver) loginObserver.disconnect();
    if (loginMutationTimer) window.clearTimeout(loginMutationTimer);
    loginTimer = null;
    loginObserver = null;
    loginMutationTimer = null;
  }

  function submitCapturedToken(payload, options = {}) {
    if (!payload || !payload.authorization) return;
    latestCapturedAuth = payload.authorization;
    if (!isLeaderTab || !config || config.automationEnabled === false) return;

    if (!options.force && payload.authorization === lastSubmittedAuth) return;
    lastSubmittedAuth = payload.authorization;

    chrome.runtime.sendMessage({
      type: "SUBMIT_TOKEN",
      authorization: payload.authorization,
      url: payload.url || "",
      via: payload.via || "",
      mode: options.mode || payload.mode || "capture",
      force: Boolean(options.force)
    }).then((response) => {
      if (!response || !response.ok) {
        log("token submit failed", response && response.error);
        return;
      }
      log("token submit result", response.result);
    }).catch((error) => log("token submit message failed", error));
  }

  window.addEventListener("message", (event) => {
    if (event.source !== window) return;
    if (event.origin !== window.location.origin) return;
    if (!event.data) return;

    if (event.data.type === "INTERLACE_MONEY_ACCESS_TOKEN") {
      submitCapturedToken(event.data, {
        force: Boolean(event.data.force),
        mode: event.data.mode || "capture"
      });
      return;
    }

    if (event.data.type === "INTERLACE_MONEY_PROBE_EVENT") {
      const status = Number(event.data.status || 0);
      const kind = event.data.kind || "result";
      const level = kind === "error" || (status === 401 && kind !== "reauth-suppressed")
        ? "error"
        : kind === "skipped" || kind === "reauth-suppressed" ? "warn" : "info";
      const message = kind === "reauth-suppressed"
        ? "登录态探活仍为 401，已阻止重复跳转"
        : status === 401
        ? "登录态探活返回 401，正在跳转登录页"
        : kind === "error"
          ? "登录态探活请求异常"
          : kind === "skipped"
            ? "登录态探活等待 accessToken"
            : `登录态探活正常：HTTP ${status}`;
      panelLog(level, message, {
        status,
        mode: "probe",
        url: event.data.url || "",
        error: event.data.error || ""
      });
    }
  });

  function requestLatestToken() {
    window.postMessage({
      type: "INTERLACE_MONEY_GET_LATEST_TOKEN"
    }, window.location.origin);
  }

  function getReportIntervalMs() {
    const seconds = Number(config && config.reportIntervalSeconds || 60);
    return Math.max(MIN_REPORT_INTERVAL_SECONDS, Number.isFinite(seconds) ? seconds : 60) * 1000;
  }

  function startPeriodicReportLoop() {
    if (periodicTimer) window.clearInterval(periodicTimer);
    periodicTimer = null;
    if (!isLeaderTab || !config || config.automationEnabled === false) {
      sendProbeConfig();
      return;
    }

    periodicTimer = window.setInterval(() => {
      if (!isLeaderTab || !config || config.automationEnabled === false || !config.autoSubmit) return;

      if (latestCapturedAuth) {
        submitCapturedToken({
          authorization: latestCapturedAuth,
          url: location.href,
          via: "periodic-cache",
          mode: "periodic"
        }, {
          force: true,
          mode: "periodic"
        });
        return;
      }

      requestLatestToken();
    }, getReportIntervalMs());

    requestLatestToken();
  }

  chrome.storage.onChanged.addListener((changes, areaName) => {
    if (areaName !== "local" || !config) return;
    for (const [key, change] of Object.entries(changes)) {
      config[key] = change.newValue;
    }
    clicked = false;
    attempts = 0;
    if (changes.automationEnabled) {
      if (config.automationEnabled === false) {
        stopLoginLoop();
        if (periodicTimer) window.clearInterval(periodicTimer);
        periodicTimer = null;
      } else if (isLeaderTab) {
        startLoginLoop();
        startPeriodicReportLoop();
      }
      sendProbeConfig();
      return;
    }
    if (changes.reportIntervalSeconds || changes.autoSubmit) {
      startPeriodicReportLoop();
    }
    if (changes.autoProbe || changes.probeUrl || changes.probeIntervalSeconds) {
      sendProbeConfig();
    }
  });

  function applyLeaderStatus(leader) {
    const next = Boolean(leader);
    if (isLeaderTab === next) {
      sendProbeConfig();
      return;
    }

    isLeaderTab = next;
    clicked = false;
    attempts = 0;
    if (!isLeaderTab) {
      stopLoginLoop();
      if (periodicTimer) window.clearInterval(periodicTimer);
      periodicTimer = null;
      sendProbeConfig();
      log("standby: another Interlace tab is the leader");
      return;
    }

    log("this tab is now the automation leader");
    if (config && config.automationEnabled !== false) {
      startLoginLoop();
      startPeriodicReportLoop();
    }
    sendProbeConfig();
  }

  chrome.runtime.onMessage.addListener((message) => {
    if (!message || message.type !== "SET_LEADER_STATUS") return;
    applyLeaderStatus(Boolean(message.isLeader));
  });

  async function claimLeaderStatus() {
    const response = await chrome.runtime.sendMessage({ type: "CLAIM_LEADER_TAB" });
    if (!response || !response.ok) throw new Error(response && response.error || "failed to claim leader tab");
    applyLeaderStatus(Boolean(response.isLeader));
  }

  getConfig().then(async () => {
    log("ready", { path: location.pathname, autoLogin: config.autoLogin, autoSubmit: config.autoSubmit });
    startUrlWatch();
    await claimLeaderStatus();
  }).catch((error) => log("init failed", error));
})();
