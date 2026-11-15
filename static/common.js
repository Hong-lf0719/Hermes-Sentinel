/* ============================================================
   Hermes Sentinel · 共享 JS 工具层（全站唯一引用）
   依赖约定：页面各自在上方声明 let AUTH_TOKEN / TOKEN = "__WEB_TOKEN__"，
   本文件懒读取（let 声明在同一全局词法环境，后加载的脚本可访问）。
   暴露：HermesUI.esc / toast / api / renderNav / funnelChart / barChart / lineChart
   ============================================================ */
window.HermesUI = (function () {
  "use strict";

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, c => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
    }[c]));
  }

  /* 页面顶层 let AUTH_TOKEN / TOKEN —— 惰性读取，兼容两种命名 */
  function _token() {
    try {
      if (typeof AUTH_TOKEN !== "undefined" && AUTH_TOKEN && AUTH_TOKEN !== "__WEB_TOKEN__") return AUTH_TOKEN;
    } catch (e) { /* 未声明则忽略 */ }
    try {
      if (typeof TOKEN !== "undefined" && TOKEN && TOKEN !== "__WEB_TOKEN__") return TOKEN;
    } catch (e) { /* ignore */ }
    return "";
  }

  function toast(msg, isErr) {
    const t = document.createElement("div");
    t.className = "hs-toast" + (isErr ? " err" : "");
    t.textContent = msg;
    document.body.appendChild(t);
    setTimeout(() => t.remove(), 2600);
  }

  /* 统一 API 封装：自动带 Bearer；非 2xx 抛错（附服务端 detail） */
  async function api(path, opts) {
    opts = opts || {};
    const headers = Object.assign({ "Authorization": "Bearer " + _token() }, opts.headers || {});
    if (opts.body && !opts.headers) headers["Content-Type"] = "application/json";
    const resp = await fetch(path, Object.assign({}, opts, { headers }));
    if (!resp.ok) {
      let detail = "";
      try { detail = (await resp.json()).detail || ""; } catch (e) { /* ignore */ }
      throw new Error(detail || ("HTTP " + resp.status));
    }
    const ct = resp.headers.get("content-type") || "";
    return ct.includes("application/json") ? resp.json() : resp.text();
  }

  /* ── 统一导航：active 取 "chat" | "jobs" | "deliveries" | "dashboard" | "traces" | "demo" ── */
  const LINKS = [
    ["chat", "/", "💬 聊天"],
    ["match", "/match", "🧲 匹配中心"],
    ["jobs", "/jobs", "🎯 岗位雷达"],
    ["deliveries", "/deliveries", "📮 投递进度"],
    ["dashboard", "/dashboard", "📊 后台"],
    ["traces", "/traces", "🛰 追踪"],
    ["demo", "/demo", "🎬 演示"],
  ];
  function renderNav(active) {
    if (document.querySelector(".hs-nav")) return; // 幂等
    const nav = document.createElement("nav");
    nav.className = "hs-nav";
    nav.innerHTML =
      '<a class="hs-brand" href="/"><span class="logo">H</span>Hermes Sentinel</a>' +
      '<span class="sp"></span>' +
      LINKS.map(([k, href, label]) =>
        `<a href="${href}"${k === active ? ' class="on"' : ""}>${esc(label)}</a>`).join("") +
      '<span class="sp"></span>';
    document.body.prepend(nav);
  }

  /* ── SVG 图表（零依赖） ─────────────────────────────── */

  /* 聚合漏斗：stages = [{label, value, color?}]，颜色缺省取漏斗语义色 */
  function funnelChart(container, stages) {
    const max = Math.max(1, ...stages.map(s => s.value || 0));
    const rowH = 34, gap = 8, labelW = 92;
    const width = Math.max(container.clientWidth || 600, 420);
    const barW = width - labelW - 64;
    let svg = `<svg viewBox="0 0 ${width} ${stages.length * (rowH + gap) + 6}" preserveAspectRatio="xMidYMid meet">`;
    stages.forEach((s, i) => {
      const y = i * (rowH + gap);
      const w = Math.max(6, Math.round((s.value || 0) / max * barW));
      const color = s.color || `var(--f-${esc(s.label)})`;
      svg += `<text x="0" y="${y + rowH / 2 + 5}" text-anchor="start">${esc(s.label)}</text>`;
      svg += `<rect x="${labelW}" y="${y}" width="${barW}" height="${rowH}" rx="8" fill="rgba(148,163,184,.12)"/>`;
      svg += `<rect x="${labelW}" y="${y}" width="${w}" height="${rowH}" rx="8" fill="${color}" opacity=".85"/>`;
      svg += `<text x="${labelW + barW + 8}" y="${y + rowH / 2 + 5}" fill="#e2e8f0" font-weight="700">${s.value ?? 0}</text>`;
    });
    svg += "</svg>";
    container.innerHTML = svg;
  }

  /* 横向条形排行：items = [{label, value}] */
  function barChart(container, items, unit) {
    const max = Math.max(1, ...items.map(i => i.value || 0));
    const rowH = 26, gap = 6, labelW = Math.min(150, width());
    function width() { return Math.max(container.clientWidth || 600, 420); }
    const w = width(), barW = w - labelW - 58;
    let svg = `<svg viewBox="0 0 ${w} ${items.length * (rowH + gap) + 4}" preserveAspectRatio="xMidYMid meet">`;
    items.forEach((it, i) => {
      const y = i * (rowH + gap);
      const bw = Math.max(4, Math.round((it.value || 0) / max * barW));
      svg += `<text x="0" y="${y + rowH / 2 + 4}" text-anchor="start">${esc(String(it.label).slice(0, 16))}</text>`;
      svg += `<rect x="${labelW}" y="${y + 4}" width="${barW}" height="${rowH - 10}" rx="5" fill="rgba(56,189,248,.25)"/>`;
      svg += `<rect x="${labelW}" y="${y + 4}" width="${bw}" height="${rowH - 10}" rx="5" fill="#38bdf8"/>`;
      svg += `<text x="${labelW + barW + 8}" y="${y + rowH / 2 + 4}" fill="#e2e8f0">${it.value ?? 0}${unit ? esc(unit) : ""}</text>`;
    });
    svg += "</svg>";
    container.innerHTML = svg;
  }

  /* 折线趋势：points = [{label, value}]（如近 7 天消息数），自动等距 */
  function lineChart(container, points, opts) {
    opts = opts || {};
    const w = Math.max(container.clientWidth || 600, 420);
    const h = opts.height || 180, padL = 42, padB = 26, padT = 10;
    const vals = points.map(p => p.value || 0);
    const max = Math.max(1, ...vals);
    const iw = w - padL - 16, ih = h - padT - padB;
    const px = i => points.length <= 1 ? padL + iw / 2 : padL + (i / (points.length - 1)) * iw;
    const py = v => padT + ih - (v / max) * ih;
    let svg = `<svg viewBox="0 0 ${w} ${h}" preserveAspectRatio="xMidYMid meet">`;
    svg += `<line class="axis" x1="${padL}" y1="${padT + ih}" x2="${padL + iw}" y2="${padT + ih}"/>`;
    [0, 0.5, 1].forEach(f => {
      svg += `<line class="axis" x1="${padL}" y1="${padT + ih * f}" x2="${padL + iw}" y2="${padT + ih * f}" stroke-dasharray="3 4" opacity=".5"/>`;
      svg += `<text x="${padL - 6}" y="${padT + ih * f + 4}" text-anchor="end">${Math.round(max * (1 - f))}</text>`;
    });
    const pts = points.map((p, i) => `${px(i)},${py(p.value || 0)}`).join(" ");
    svg += `<polyline points="${pts}" fill="none" stroke="#38bdf8" stroke-width="2.5"/>`;
    points.forEach((p, i) => {
      svg += `<circle cx="${px(i)}" cy="${py(p.value || 0)}" r="3.5" fill="#38bdf8"/>`;
      svg += `<text x="${px(i)}" y="${h - 8}" text-anchor="middle">${esc(p.label)}</text>`;
    });
    svg += "</svg>";
    container.innerHTML = svg;
  }

  return { esc, toast, api, renderNav, funnelChart, barChart, lineChart };
})();
