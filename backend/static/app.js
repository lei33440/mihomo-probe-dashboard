// 通用工具
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
const fmtTs = (ms) => {
  if (!ms) return "—";
  const d = new Date(ms);
  return d.toLocaleTimeString();
};
const fmtAge = (ms) => {
  if (!ms) return "—";
  const s = Math.floor((Date.now() - ms) / 1000);
  if (s < 60) return s + "s ago";
  if (s < 3600) return Math.floor(s / 60) + "m ago";
  if (s < 86400) return Math.floor(s / 3600) + "h ago";
  return Math.floor(s / 86400) + "d ago";
};
const STATUS = {
  up: { label: "可用", dotClass: "up" },
  down: { label: "不可用", dotClass: "down" },
  unknown: { label: "未知", dotClass: "unknown" },
};

async function api(path, opts = {}) {
  const headers = { "Content-Type": "application/json", ...(opts.headers || {}) };
  if (opts.token) headers["Authorization"] = "Bearer " + opts.token;
  const res = await fetch(path, { ...opts, headers });
  if (res.status === 401) {
    sessionStorage.removeItem("token");
    throw new Error("unauthorized");
  }
  if (!res.ok) {
    const text = await res.text();
    throw new Error(text || res.statusText);
  }
  return res.json();
}

function delayColor(ms) {
  if (ms == null) return "muted";
  if (ms < 150) return "good";
  if (ms < 400) return "warn";
  return "bad";
}