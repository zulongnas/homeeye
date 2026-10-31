/* ============================================================================
 * HomeEye · Service Worker（PWA 外壳 + 通知点击深链）
 *
 * 缓存策略只有一条底线：**画面与录像绝不落盘缓存**。
 * 所以判定「动态」的路径一律直通网络，只有应用外壳（html/css/js/图标/清单）才进缓存。
 * 外壳用「网络优先、失败退回缓存」：这样升级发版后用户不会被迫用旧 app.js，
 * 而断网或容器重启期间至少还能打开页面看到「离线」提示。
 *
 * 没有 push 事件处理：真正的 Web Push 要 VAPID/ES256 签名，后端只用 Python
 * 标准库、不引第三方加密包，所以没做（页面里用的是本地通知）。
 * ==========================================================================*/
"use strict";

var SHELL_CACHE = "homeeye-shell-v1";
var SHELL = ["./", "./index.html", "./style.css", "./app.js",
             "./manifest.webmanifest", "./icons/icon-192.png",
             "./icons/icon-512.png"];

// 这些前缀后面跟的是"内容"而不是"外壳"：缓存它们等于把别人家的画面留在手机里
var DYNAMIC = ["/api/", "/live/", "/rec/", "/replay/", "/rech264/",
               "/export/", "/share/", "/talk/", "/snapshot"];

function isDynamic(path) {
  for (var i = 0; i < DYNAMIC.length; i++) {
    if (path.indexOf(DYNAMIC[i]) >= 0) return true;
  }
  return false;
}

self.addEventListener("install", function (ev) {
  ev.waitUntil(caches.open(SHELL_CACHE).then(function (c) {
    // 逐个 add：某个文件缺失（老镜像里没有图标）不该让整次安装失败
    return Promise.all(SHELL.map(function (u) {
      return c.add(u).catch(function () {});
    }));
  }).then(function () { self.skipWaiting(); }));
});

self.addEventListener("activate", function (ev) {
  ev.waitUntil(caches.keys().then(function (keys) {
    return Promise.all(keys.filter(function (k) {
      return k !== SHELL_CACHE;
    }).map(function (k) { return caches.delete(k); }));
  }).then(function () { return self.clients.claim(); }));
});

self.addEventListener("fetch", function (ev) {
  var req = ev.request;
  if (req.method !== "GET") return;
  var u = new URL(req.url);
  if (u.origin !== self.location.origin) return;
  if (isDynamic(u.pathname)) return;

  // 页面导航：先问网络，拿不到再给缓存的那一份
  if (req.mode === "navigate") {
    ev.respondWith(fetch(req).then(function (res) {
      var copy = res.clone();
      caches.open(SHELL_CACHE).then(function (c) { c.put("./index.html", copy); });
      return res;
    }).catch(function () {
      return caches.match("./index.html").then(function (hit) {
        return hit || new Response(
          "<!doctype html><meta charset=utf-8><title>HomeEye 离线</title>" +
          "<body style='background:#0d1117;color:#e6edf3;font:15px/1.7 system-ui;" +
          "padding:24px'><h2>HomeEye 现在连不上</h2>" +
          "<p>容器可能正在重启，或者这个地址从当前网络走不通。<br>" +
          "页面会在服务回来之后自动恢复。</p>",
          { headers: { "Content-Type": "text/html; charset=utf-8" } });
      });
    }));
    return;
  }

  // 静态资源：有缓存就直接用，同时后台补一份新的；没有缓存才走网络
  ev.respondWith(caches.match(req).then(function (hit) {
    var net = fetch(req).then(function (res) {
      if (res && res.ok) {
        var copy = res.clone();
        caches.open(SHELL_CACHE).then(function (c) { c.put(req, copy); });
      }
      return res;
    }).catch(function () { return hit; });
    return hit || net;
  }));
});

// 点通知 → 跳到那一刻的回放。已有标签页就把它拿过来导航，没有就开一个。
self.addEventListener("notificationclick", function (ev) {
  ev.notification.close();
  var link = (ev.notification.data && ev.notification.data.url) || "./";
  ev.waitUntil(self.clients.matchAll({ type: "window", includeUncontrolled: true })
    .then(function (list) {
      for (var i = 0; i < list.length; i++) {
        if (!/^https?:/.test(list[i].url)) continue;
        list[i].postMessage({ he: "goto", url: link });
        return list[i].focus().then(function () { return list[i]; });
      }
      return self.clients.openWindow(link);
    }));
});
