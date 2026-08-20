const DEF_SERVER = "http://127.0.0.1:9866", DEF_TOKEN = "hermes-ext-2026";
function loadCfg() { return new Promise(r => chrome.storage.local.get({ server: DEF_SERVER, token: DEF_TOKEN }, r)); }
function saveCfg(c) { return new Promise(r => chrome.storage.local.set(c, r)); }
async function extract() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab) return null;
  try { return await chrome.tabs.sendMessage(tab.id, { type: "extract" }); }
  catch (e) { return { title: tab.title || "", url: tab.url || "", text: "" }; }
}
function setStatus(s, ok) {
  const el = document.getElementById("status");
  el.textContent = s;
  el.className = "status " + (ok ? "ok" : "err");
}
async function main() {
  const cfg = await loadCfg();
  document.getElementById("server").value = cfg.server;
  document.getElementById("token").value = cfg.token;
  const ex = await extract();
  if (!ex) { setStatus("无法获取当前页", false); return; }
  const co = guessCompany(ex.title, ex.url) || guessCompany(ex.text.slice(0, 200), ex.url);
  const ti = guessTitle(ex.text, ex.title, ex.candidates);
  document.getElementById("company").value = co;
  document.getElementById("title").value = ti;
  document.getElementById("src").textContent = ex.url || ex.title;
  window._cur = ex;
}
async function record() {
  const cfg = {
    server: document.getElementById("server").value.trim() || DEF_SERVER,
    token: document.getElementById("token").value.trim() || DEF_TOKEN,
  };
  await saveCfg(cfg);
  const company = document.getElementById("company").value.trim();
  const title = document.getElementById("title").value.trim();
  const url = (window._cur && window._cur.url) || "";
  if (!company && !title) { setStatus("请至少填公司或岗位", false); return; }
  setStatus("上报中…");
  try {
    const r = await fetch(cfg.server + "/api/extension/apply", {
      method: "POST",
      headers: { "Content-Type": "application/json", "Authorization": "Bearer " + cfg.token },
      body: JSON.stringify({ company, title, url }),
    });
    const d = await r.json();
    if (r.ok && d.ok)
      setStatus("已记录：" + d.company + (d.matched ? "（匹配库内岗位）" : "（新建记录）"), true);
    else setStatus("失败：" + (d.error || r.status), false);
  } catch (e) {
    setStatus("连接失败：确认 Hermes 在 " + cfg.server + " 运行", false);
  }
}
async function readList() {
  const cfg = {
    server: document.getElementById("server").value.trim() || DEF_SERVER,
    token: document.getElementById("token").value.trim() || DEF_TOKEN,
  };
  await saveCfg(cfg);
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab) { setStatus("无法获取当前页", false); return; }
  setStatus("正在读取列表…");
  let rows;
  try {
    rows = await chrome.tabs.sendMessage(tab.id, { type: "extract_deliveries" });
  } catch (e) {
    setStatus("提取失败：请先打开招聘平台的「我的投递」页", false);
    return;
  }
  if (!rows || !rows.rows || !rows.rows.length) {
    setStatus("这一页没找到投递记录（确认在「我的投递」列表页）", false);
    return;
  }
  try {
    const r = await fetch(cfg.server + "/api/extension/deliveries", {
      method: "POST",
      headers: { "Content-Type": "application/json", "Authorization": "Bearer " + cfg.token },
      body: JSON.stringify({ rows: rows.rows, url: rows.url || "" }),
    });
    const d = await r.json();
    if (r.ok && d.ok) {
      setStatus(`读取完成：新增 ${(d.created || []).length} 条、更新 ${(d.updated || []).length} 条（共识别 ${d.total} 行）`, true);
    } else setStatus("失败：" + (d.error || r.status), false);
  } catch (e) {
    setStatus("连接失败：确认 Hermes 在 " + cfg.server + " 运行", false);
  }
}

document.addEventListener("DOMContentLoaded", () => {
  main();
  document.getElementById("record").addEventListener("click", record);
  document.getElementById("readList").addEventListener("click", readList);
});
