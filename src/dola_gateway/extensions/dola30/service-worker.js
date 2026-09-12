const DEBUGGER_VERSION = "1.3";
const DOLA_HOST = "dola.com";
const DURATION_RESPONSE_PARTS = [
  "dola.com/samantha/skill/pack",
  "dola.com/alice/slot/action_bar_v3/get_item_conf"
];
const fetchPatterns = DURATION_RESPONSE_PARTS.map((part) => ({
  urlPattern: `*${part}*`,
  requestStage: "Response"
}));
const attachedTabs = new Set();

importScripts("duration-patch.js");

chrome.runtime.onInstalled.addListener(attachExistingTabs);
chrome.runtime.onStartup.addListener(attachExistingTabs);

chrome.tabs.onActivated.addListener(async ({ tabId }) => {
  const tab = await safeGetTab(tabId);
  if (tab && shouldAttachToTab(tab.url)) {
    ensureAttached(tabId);
  }
});

chrome.tabs.onUpdated.addListener((tabId, changeInfo, tab) => {
  if (shouldAttachToTab(changeInfo.url || tab.url)) {
    ensureAttached(tabId);
  }
});

chrome.tabs.onRemoved.addListener((tabId) => attachedTabs.delete(tabId));

chrome.action.onClicked.addListener((tab) => {
  if (tab && tab.id) {
    ensureAttached(tab.id);
  }
});

chrome.debugger.onDetach.addListener((source) => {
  if (source.tabId) {
    attachedTabs.delete(source.tabId);
    setBadge(source.tabId, "");
  }
});

chrome.debugger.onEvent.addListener((source, method, params) => {
  if (method === "Fetch.requestPaused" && source.tabId && params) {
    handlePausedResponse(source.tabId, params);
  }
});

async function attachExistingTabs() {
  const tabs = await chrome.tabs.query({});
  for (const tab of tabs) {
    if (tab.id && shouldAttachToTab(tab.url)) {
      ensureAttached(tab.id);
    }
  }
}

async function safeGetTab(tabId) {
  try {
    return await chrome.tabs.get(tabId);
  } catch {
    return null;
  }
}

function shouldAttachToTab(url) {
  try {
    const parsed = new URL(url);
    return ["http:", "https:"].includes(parsed.protocol)
      && (parsed.hostname === DOLA_HOST || parsed.hostname.endsWith(`.${DOLA_HOST}`));
  } catch {
    return false;
  }
}

async function ensureAttached(tabId) {
  if (attachedTabs.has(tabId)) {
    return;
  }
  try {
    await attachDebugger(tabId);
    await sendCommand(tabId, "Fetch.enable", { patterns: fetchPatterns });
    attachedTabs.add(tabId);
    setBadge(tabId, "ON");
  } catch (error) {
    console.warn("Dola duration extension attach failed:", error.message || error);
    setBadge(tabId, "");
  }
}

function attachDebugger(tabId) {
  return new Promise((resolve, reject) => {
    chrome.debugger.attach({ tabId }, DEBUGGER_VERSION, () => {
      const error = chrome.runtime.lastError;
      error ? reject(new Error(error.message)) : resolve();
    });
  });
}

function sendCommand(tabId, method, params = {}) {
  return new Promise((resolve, reject) => {
    chrome.debugger.sendCommand({ tabId }, method, params, (result) => {
      const error = chrome.runtime.lastError;
      error ? reject(new Error(error.message)) : resolve(result);
    });
  });
}

async function handlePausedResponse(tabId, event) {
  const requestId = event.requestId;
  const url = event.request?.url || "";
  try {
    if (!DURATION_RESPONSE_PARTS.some((part) => url.includes(part))) {
      await continueRequest(tabId, requestId);
      return;
    }
    const response = await sendCommand(tabId, "Fetch.getResponseBody", { requestId });
    const body = response.base64Encoded
      ? fromBase64Utf8(response.body)
      : response.body;
    const patchedBody = DolaDurationPatch.patchDurationPayload(body);
    await sendCommand(tabId, "Fetch.fulfillRequest", {
      requestId,
      responseCode: event.responseStatusCode || 200,
      responsePhrase: event.responseStatusText || "OK",
      responseHeaders: responseHeaders(event.responseHeaders || [], patchedBody),
      body: toBase64Utf8(patchedBody)
    });
  } catch (error) {
    console.warn("Dola duration response patch failed:", error.message || error);
    await continueRequest(tabId, requestId).catch(() => {});
  }
}

function continueRequest(tabId, requestId) {
  return sendCommand(tabId, "Fetch.continueRequest", { requestId });
}

function responseHeaders(headers, body) {
  const result = headers.filter(({ name }) => ![
    "content-encoding", "content-length"
  ].includes(String(name).toLowerCase()));
  result.push({ name: "content-length", value: String(new TextEncoder().encode(body).length) });
  return result;
}

function toBase64Utf8(text) {
  const bytes = new TextEncoder().encode(text);
  let binary = "";
  for (const byte of bytes) {
    binary += String.fromCharCode(byte);
  }
  return btoa(binary);
}

function fromBase64Utf8(text) {
  const binary = atob(text);
  const bytes = Uint8Array.from(binary, (character) => character.charCodeAt(0));
  return new TextDecoder().decode(bytes);
}

function setBadge(tabId, text) {
  chrome.action.setBadgeText({ tabId, text });
  if (text) {
    chrome.action.setBadgeBackgroundColor({ tabId, color: "#2563eb" });
  }
}
