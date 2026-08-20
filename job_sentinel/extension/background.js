importScripts("shared.js");
const SUCCESS_HINT = /(success|submit|apply|complete|result|finish|done|注册成功|提交成功|投递成功|申请成功|网申成功|简历提交成功|报名成功)/i;
// 覆盖「网申/投递成功页」的招聘相关域名。命中 + URL 含成功提示词 → 自动捕获本次投递。
// 刻意包含所有主流校招/社招平台（牛客/BOSS/猎聘/51job/海投/应届生/各大厂校招官网/ATS），
// 这样浏览器扩展成为「安全渠道倍增器」：在你已登录的浏览器里跑，用你自己的会话，
// 服务端不存任何账号密码/ cookie，也不做事服端硬爬（避免反爬封禁与 ToS 风险）。
const RECRUIT_HOST = /(zhaopin|job|career|campus|hr|recruit|apply|talent|校招|网申|sourcing|nowcoder|bosszhipin|zhipin|liepin|51job|yingjiesheng|haitou|mokahr|moka|beisen|ncss|iguopin|school)/i;

// 半自动：检测到网申提交成功页（且来自招聘相关域名）→ 弹通知
chrome.tabs.onUpdated.addListener(function (tabId, changeInfo, tab) {
  if (changeInfo.status !== "complete") return;
  const url = tab.url || "";
  if (!SUCCESS_HINT.test(url)) return;
  try {
    const h = new URL(url).hostname;
    if (!RECRUIT_HOST.test(h) && !RECRUIT_HOST.test(url)) return;
  } catch (e) { return; }
  chrome.notifications.create("apply-" + tabId, {
    type: "basic",
    title: "Hermes 网申捕获",
    message: "检测到网申提交成功页，点击记录本次投递到岗位哨兵。",
  });
});

// 点通知 → 提取当前页 → 上报（一次点击完成记录，不用开 popup）
chrome.notifications.onClicked.addListener(async function (id) {
  const tabId = parseInt(id.replace("apply-", ""), 10);
  if (!tabId) { chrome.notifications.clear(id); return; }
  let ex = null;
  try { ex = await chrome.tabs.sendMessage(tabId, { type: "extract" }); } catch (e) {}
  const cfg = await new Promise(r =>
    chrome.storage.local.get({ server: "http://127.0.0.1:9866", token: "hermes-ext-2026" }, r));
  if (ex) {
    const company = guessCompany(ex.title, ex.url) || guessCompany(ex.text.slice(0, 200), ex.url);
    const title = guessTitle(ex.text, ex.title, ex.candidates);
    if (company || title) {
      try {
        const r = await fetch(cfg.server + "/api/extension/apply", {
          method: "POST",
          headers: { "Content-Type": "application/json", "Authorization": "Bearer " + cfg.token },
          body: JSON.stringify({ company, title, url: ex.url }),
        });
        const d = await r.json();
        chrome.notifications.create("done-" + tabId, {
          type: "basic",
          title: "已记录",
          message: (d.company || company) + (d.matched ? " · 匹配库内" : " · 新建"),
        });
      } catch (e) {
        chrome.notifications.create("err-" + tabId, {
          type: "basic", title: "记录失败", message: "无法连接 Hermes",
        });
      }
    }
  }
  chrome.notifications.clear(id);
});
