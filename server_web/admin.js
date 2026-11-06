"use strict";

const $ = (id) => document.getElementById(id);
const statusLabels = {
  listening: "监听中",
  delivering: "正在回调",
  received: "已收码",
  delivered: "已回调",
  replaced: "已切换",
  cancelled: "已停止",
  timeout: "等待超时",
  failed: "收信失败",
  delivery_failed: "回调失败",
};
const errors = {
  unauthorized: "管理令牌不正确",
  invalid_request: "请求无效或内容过大",
  mailbox_not_imported: "这个邮箱还没有导入",
  mailbox_store_unavailable: "暂时无法读取邮箱库",
  watch_changed: "当前任务已切换，请重试",
};

let token = "";
let session = 0;
let accounts = [];
let selected = "";
let watch = null;
let watchRequest = 0;
let watchBusy = false;
let timer;

function message(id, value = "") {
  const node = $(id);
  node.textContent = value;
  if (node.classList.contains("error")) node.hidden = !value;
}

async function api(path, data) {
  const activeSession = session;
  const headers = { Authorization: "Bearer " + token };
  const options = { headers, cache: "no-store" };
  if (data !== undefined) {
    options.method = "POST";
    headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(data);
  }
  const response = await fetch("/admin/api/" + path, options);
  const body = await response.json();
  if (activeSession !== session) throw new DOMException("Session changed", "AbortError");
  if (!response.ok) {
    if (response.status === 401 && !$("dashboard").hidden) {
      lock();
      message("login-error", "请用当前管理令牌重新登录");
    }
    throw new Error(errors[body.error] || body.error || "请求失败，请重试");
  }
  return body;
}

function showError(id, error) {
  if (error.name !== "AbortError") message(id, error.message);
}

function lock() {
  session++;
  token = "";
  accounts = [];
  selected = "";
  watch = null;
  watchBusy = false;
  watchRequest++;
  clearTimeout(timer);
  for (const id of ["import-dialog", "settings-dialog"]) $(id).close();
  for (const id of ["login-form", "import-form", "settings-form"]) $(id).reset();
  $("search").value = "";
  message("watch-note");
  renderAccounts();
  renderWatch();
  $("dashboard").hidden = true;
  $("admin-actions").hidden = true;
  $("login-form").hidden = false;
}

function renderAccounts() {
  $("count").textContent = String(accounts.length);
  const list = $("account-list");
  const search = $("search").value.trim().toLowerCase();
  const rows = document.createDocumentFragment();
  for (const [index, account] of accounts.entries()) {
    if (!account.email.toLowerCase().includes(search)) continue;
    const button = document.createElement("button");
    const number = document.createElement("span");
    const address = document.createElement("span");
    button.type = "button";
    button.setAttribute("aria-pressed", String(account.email === selected));
    button.title = account.email;
    number.className = "mail-number";
    number.textContent = "#" + String(index + 1).padStart(3, "0");
    address.className = "mail-address";
    address.textContent = account.email;
    button.append(number, address);
    button.addEventListener("click", () => {
      selected = account.email;
      for (const row of list.querySelectorAll("button")) {
        row.setAttribute("aria-pressed", String(row === button));
      }
      renderWatch();
    });
    rows.append(button);
  }
  if (!rows.childElementCount) {
    const empty = document.createElement("p");
    empty.className = "empty muted";
    empty.textContent = accounts.length ? "没有匹配的邮箱" : "还没有邮箱，请先导入";
    rows.append(empty);
  }
  list.replaceChildren(rows);
}

function renderWatch() {
  const status = watch?.status;
  const live = status === "listening" || status === "delivering";
  const code = watch?.code || "";
  $("current-email").textContent = watch?.email || "还没有监听任务";
  $("watch-status").textContent = statusLabels[status] || "未监听";
  $("watch-status").classList.toggle("live", live);
  const displayed = code || "— — —";
  if ($("code").textContent !== displayed) $("code").textContent = displayed;
  $("copy-code").disabled = !code || watchBusy;
  $("selection").textContent = selected ? "已选 " + selected : "从邮箱列表选择一个邮箱。";
  $("start").textContent = live && selected && selected.toLowerCase() !== watch.email.toLowerCase()
    ? "切换监听" : "开始监听";
  $("start").disabled = !selected || watchBusy || (live && watch.email.toLowerCase() === selected.toLowerCase());
  $("stop").disabled = !live || watchBusy;
}

async function loadAccounts() {
  const data = await api("accounts");
  accounts = data.accounts;
  if (!accounts.some((account) => account.email === selected)) selected = accounts[0]?.email || "";
  renderAccounts();
  renderWatch();
}

async function loadWatch() {
  const request = ++watchRequest;
  const data = await api("watch");
  if (request !== watchRequest) return;
  watch = data.watch;
  renderWatch();
}

async function poll(activeSession) {
  try {
    if (!watchBusy) await loadWatch();
  } catch (error) {
    if (activeSession === session) showError("watch-note", error);
  } finally {
    if (activeSession === session) timer = setTimeout(() => poll(activeSession), 500);
  }
}

$("login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = event.currentTarget.querySelector("button");
  const activeSession = ++session;
  token = $("admin-token").value.trim();
  button.disabled = true;
  message("login-error");
  try {
    await Promise.all([loadAccounts(), loadWatch()]);
    $("admin-token").value = "";
    $("login-form").hidden = true;
    $("admin-actions").hidden = false;
    $("dashboard").hidden = false;
    timer = setTimeout(() => poll(activeSession), 500);
  } catch (error) {
    lock();
    showError("login-error", error);
  } finally {
    button.disabled = false;
  }
});

$("logout").addEventListener("click", () => {
  lock();
  message("login-error");
  $("admin-token").focus();
});
$("search").addEventListener("input", renderAccounts);

async function changeWatch(path, data) {
  if (watchBusy) return;
  const activeSession = session;
  watchBusy = true;
  watchRequest++;
  renderWatch();
  message("watch-note");
  try {
    await api(path, data);
    watch = null;
    renderWatch();
    await loadWatch();
  } catch (error) {
    if (activeSession === session) showError("watch-note", error);
  } finally {
    if (activeSession === session) {
      watchBusy = false;
      renderWatch();
    }
  }
}

$("start").addEventListener("click", () => changeWatch("watches", { email: selected }));
$("stop").addEventListener("click", () => changeWatch("stop", { watch_id: watch?.watch_id }));
$("copy-code").addEventListener("click", async () => {
  try {
    await navigator.clipboard.writeText(watch.code);
    message("watch-note", "验证码已复制");
  } catch {
    message("watch-note", "无法自动复制，请选中验证码后复制");
  }
});

$("open-import").addEventListener("click", () => {
  message("import-error");
  $("import-dialog").showModal();
  $("import-text").focus();
});
$("close-import").addEventListener("click", () => $("import-dialog").close());
$("import-dialog").addEventListener("close", () => $("import-form").reset());
$("import-file").addEventListener("change", async (event) => {
  const file = event.currentTarget.files[0];
  if (!file) return;
  message("import-error");
  if (file.size > 8 * 1024 * 1024) {
    message("import-error", "请选择小于 8 MB 的 TXT 文件，或分批导入");
    return;
  }
  const activeSession = session;
  try {
    const content = await file.text();
    if (activeSession === session && $("import-dialog").open && $("import-file").files[0] === file) {
      $("import-text").value = content;
    }
  } catch {
    message("import-error", "无法读取文件");
  }
});
$("import-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  $("submit-import").disabled = true;
  message("import-error");
  try {
    const data = await api("import", { text: $("import-text").value });
    $("import-dialog").close();
    await Promise.all([loadAccounts(), loadWatch()]);
    message("watch-note", "已导入 " + data.added + " 个邮箱，更新 " + data.updated + " 个");
  } catch (error) {
    showError($("import-dialog").open ? "import-error" : "watch-note", error);
  } finally {
    $("submit-import").disabled = false;
  }
});

function showSettings(settings) {
  $("protocol").value = settings.protocol;
  $("interval").value = settings.interval;
  $("timeout").value = settings.timeout;
  $("webhook-url").value = settings.webhook_url;
  $("webhook-secret").value = "";
  $("webhook-secret").placeholder = settings.webhook_secret_set ? "已设置，留空保留" : "至少 32 个字符";
  $("webhook-status").textContent = settings.webhook_ready ? "已配置" : "未配置";
}

$("open-settings").addEventListener("click", async () => {
  message("settings-error");
  $("submit-settings").disabled = true;
  $("settings-dialog").showModal();
  try {
    showSettings(await api("settings"));
    $("submit-settings").disabled = false;
  } catch (error) {
    showError("settings-error", error);
  }
});
$("close-settings").addEventListener("click", () => $("settings-dialog").close());
$("settings-dialog").addEventListener("close", () => $("settings-form").reset());
$("settings-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  $("submit-settings").disabled = true;
  message("settings-error");
  try {
    const data = await api("settings", {
      protocol: $("protocol").value,
      interval: Number($("interval").value),
      timeout: Number($("timeout").value),
      webhook_url: $("webhook-url").value.trim(),
      webhook_secret: $("webhook-secret").value,
    });
    showSettings(data);
    $("settings-dialog").close();
    message("watch-note", "设置已保存");
  } catch (error) {
    showError("settings-error", error);
  } finally {
    $("submit-settings").disabled = false;
  }
});
