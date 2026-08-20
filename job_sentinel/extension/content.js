chrome.runtime.onMessage.addListener(function (msg, sender, sendResponse) {
  if (msg && msg.type === "extract") {
    var txt = (document.body ? document.body.innerText : "") || "";
    sendResponse({ title: document.title || "", url: location.href || "",
                   text: txt.slice(0, 4000), candidates: collectTitleCandidates(txt) });
  }
  if (msg && msg.type === "extract_deliveries") {
    sendResponse({ url: location.href || "", rows: extractDeliveryRows() });
  }
});

// 采集页面上"更像岗位名"的结构化线索，交给 background 的 guessTitle 打分挑选
function collectTitleCandidates(text) {
  var cands = [];
  function push(s) {
    if (!s) return;
    s = s.replace(/\s+/g, " ").trim();
    if (s.length >= 2 && s.length <= 60) cands.push(s);
  }
  push(document.title);
  ["h1", "h2"].forEach(function (t) {
    Array.prototype.slice.call(document.querySelectorAll(t)).forEach(function (el) { push(el.innerText); });
  });
  Array.prototype.slice.call(document.querySelectorAll(
    '[class*="title" i], [class*="job" i], [class*="position" i], [class*="post" i], [class*="recruit" i], [class*="role" i]'
  )).forEach(function (el) { push(el.innerText); });
  if (text) {
    var lr = /(应聘职位|申请职位|应聘岗位|申请岗位|职位名称|岗位名称|招聘职位|投递职位|报考职位|应聘方向|意向岗位|求职意向|岗位)[：:\s]*([^\n：:，,。；;]{2,30})/g;
    var m;
    while ((m = lr.exec(text)) !== null) push(m[2]);
  }
  return cands.slice(0, 25);
}

// 从「我的投递」列表页抓出每条投递记录行的纯文本。
// 策略：① 表格型(tr) ② 卡片型(含状态词的容器) ③ 兜底整页按行。
function extractDeliveryRows() {
  var rows = [];
  var statusRe = /(投递|面试|笔试|测评|简历|不合适|录用|筛选|邀|offer|oc|查看|反馈|进度|待)/i;

  // ① 表格行
  document.querySelectorAll("tr").forEach(function (tr) {
    var t = (tr.innerText || "").replace(/\s+/g, " ").trim();
    if (t && statusRe.test(t)) rows.push(t);
  });

  // ② 卡片型（无表格，找含状态词的 10~200 字容器）
  if (rows.length === 0) {
    document.querySelectorAll("div,li,section,article").forEach(function (el) {
      var t = (el.innerText || "").replace(/\s+/g, " ").trim();
      if (t.length > 10 && t.length < 200 && statusRe.test(t)) rows.push(t);
    });
  }

  // ③ 兜底：整页按行
  if (rows.length === 0) {
    (document.body.innerText || "").split(/\n+/).forEach(function (line) {
      var t = line.replace(/\s+/g, " ").trim();
      if (t && statusRe.test(t)) rows.push(t);
    });
  }

  // 去重 + 限制长度
  var seen = {};
  var out = [];
  rows.forEach(function (r) {
    if (!seen[r]) { seen[r] = 1; out.push(r); }
  });
  return out.slice(0, 200);
}
