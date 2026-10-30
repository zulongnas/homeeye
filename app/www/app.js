/* ============================================================================
 * HomeEye · 前端（原生 JS，无框架、无构建）
 * 设计要点：
 *  - 所有请求都走 BASE 拼相对路径，这样无论飞牛网关带不带 /app/<id> 前缀都能用；
 *  - 实时画面用 <video> + mpegts.js + MediaSource 播放 MPEG-TS 流（对齐 minvr
 *    的稳定方案：低延迟、低带宽、断线可重连），切走视图立刻销毁播放器断流；
 *  - 全屏优先用 Fullscreen API；被 iframe 拒绝时退化成「铺满应用窗口」，
 *    两种情况的视觉几何完全一致（见 style.css 里 .stage:fullscreen / .is-fs）。
 * ==========================================================================*/
(function () {
  "use strict";

  /* ------------------------------------------------------------ 基础 */
  var BASE = (function () {
    var m = location.pathname.match(/^(\/app\/[^\/]+)/);
    if (m) return m[1] + "/";
    if (/\/app\/?$/.test(location.pathname)) return "/app/homeeye/";
    return location.pathname.replace(/[^\/]*$/, "");
  })();

  function url(p) {
    return BASE + String(p || "").replace(/^\//, "");
  }

  var state = {
    cams: [],
    settings: null,
    storage: null,
    version: "",
    grid: 4,
    fit: "contain",
    focusId: null,
    picks: [],        // 每一格放哪台摄像头（空串 = 该格留空）
    muted: true,      // 是否静音（默认静音：浏览器不允许无交互自动出声）
    view: "live",
    ptz: { available: false, reason: "" },   // 云台侧车是否可用
    ptzOpen: "",                              // 当前展开方向盘的摄像头 id
    play: { cam: null, day: null, segs: [], seg: null, playhead: 0, dayStart: 0,
            auto: true, offsetBase: 0 }
  };

  function $(id) { return document.getElementById(id); }
  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }

  var toastTimer = null;
  function toast(msg, kind) {
    var t = $("toast");
    t.textContent = msg;
    t.className = "toast" + (kind ? " " + kind : "");
    t.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { t.hidden = true; }, kind === "err" ? 5200 : 2600);
  }

  function api(path, opt) {
    opt = opt || {};
    var init = { method: opt.method || "GET", headers: {} };
    if (opt.body !== undefined) {
      init.headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(opt.body);
    }
    return fetch(url(path), init).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (j) {
        if (r.status === 401) {
          // 闸门拦下：弹登录框。所有轮询都走这里，所以一次拦截就够。
          needAuth(j.auth);
          var e4 = new Error("需要登录");
          e4.needAuth = true;
          throw e4;
        }
        if (!r.ok || j.ok === false) {
          var e = new Error(j.error || ("请求失败 " + r.status));
          // need_session：云台/对讲要复用取流那条 P2P 会话，
          // 会话不在时前端要先把这路画面打开、等它连上，再重试命令。
          e.needSession = !!j.need_session;
          throw e;
        }
        return j;
      });
    });
  }

  function fmtBytes(n) {
    if (!n && n !== 0) return "—";
    var u = ["B", "KB", "MB", "GB", "TB"], i = 0;
    while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
    return (i === 0 ? n : n.toFixed(n >= 100 ? 0 : 1)) + " " + u[i];
  }
  function pad(n) { return n < 10 ? "0" + n : "" + n; }
  function hhmmss(ts) {
    var d = new Date(ts * 1000);
    return pad(d.getHours()) + ":" + pad(d.getMinutes()) + ":" + pad(d.getSeconds());
  }
  function ymd(d) {
    return d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate());
  }

  /* ------------------------------------------------------------ 视图切换 */
  function setView(v) {
    state.view = v;
    Array.prototype.forEach.call(document.querySelectorAll(".tab"), function (b) {
      b.classList.toggle("active", b.getAttribute("data-view") === v);
    });
    Array.prototype.forEach.call(document.querySelectorAll(".view"), function (s) {
      s.classList.toggle("active", s.id === "view-" + v);
    });
    if (v === "live") mountTiles();
    else unmountTiles();
    if (v === "play") {
      /* 每次进回放页先允许"自动挑一台有录像的"：摄像头列表第一台往往是离线/
         没在录的那台，一进来就"该摄像头无录像"，看着像回放坏了。 */
      state.play.auto = true;
      refreshDays();
    }
    if (v === "setting") {
      renderCams(); fillSettings();
      // 设备侧配置面：进设置页才去问一次云（后端 20 秒内用缓存，不重复敲）
      motionFillSelect(); loadMotion(false);
      if (state.markSetNav) state.markSetNav();
    }
  }

  Array.prototype.forEach.call(document.querySelectorAll(".tab"), function (b) {
    b.addEventListener("click", function () { setView(b.getAttribute("data-view")); });
  });

  /* ------------------------------------------------------------ 实时画面 */
  var tiles = {};   // camId -> {video, player, tile}

  function destroyPlayer(entry) {
    if (!entry) return;
    var p = entry.player;
    if (p) {
      try { p.destroy(); } catch (e) {}
      entry.player = null;
    }
    if (entry.video) {
      try { entry.video.removeAttribute("src"); entry.video.load(); } catch (e) {}
      entry.video = null;
    }
  }

  function unmountTiles() {
    Object.keys(tiles).forEach(function (k) {
      destroyPlayer(tiles[k]);
    });
    tiles = {};
  }

  /* 录像状态只做标签的增量更新，**不重建 tile**。
     重建 tile 会掐断这一格的 MJPEG 流（画面黑一下再重连），
     也会把正在按的对讲按钮换掉。断电的摄像头录像进程会反复重启，
     running 每秒抖一次 —— 早先把它放进 tile 签名，抖一次重建一次全屏。 */
  function updateRecBadges() {
    state.cams.forEach(function (c) {
      var t = tiles[c.id];
      if (!t || !t.tile) return;
      var has = !!t.tile.querySelector(".tile-meta");
      var want = !!(c.rec && c.rec.running);
      if (want && !has) {
        t.tile.appendChild(el("div", "tile-meta", "● 录像中"));
      } else if (!want && has) {
        var m = t.tile.querySelector(".tile-meta");
        if (m && m.parentNode) m.parentNode.removeChild(m);
      }
    });
  }

  function gridCount() {
    var n = state.grid;
    return Math.max(1, Math.min(n, Math.max(1, state.cams.length)));
  }

  function camById(id) {
    for (var i = 0; i < state.cams.length; i++) {
      if (String(state.cams[i].id) === String(id)) return state.cams[i];
    }
    return null;
  }

  function savePicks() {
    try { localStorage.setItem("nw.picks", JSON.stringify(state.picks)); } catch (e) {}
  }

  /* 维护"哪一格放哪台摄像头"。
     摄像头数量 > 格子数时（3 台选 2 格、单画面看某一台）必须由用户指定，
     否则永远只能看列表里靠前的那几路。规则：
       - 已经不存在/已删除的摄像头 → 清成空
       - 同一台不能同时占两格 → 后面的清成空
       - 格子变多时用还没上墙的摄像头补满，没有就留空 */
  function ensurePicks(n) {
    var ids = state.cams.map(function (c) { return String(c.id); });
    var p = (state.picks || []).slice();
    p = p.map(function (x) { return ids.indexOf(String(x)) >= 0 ? String(x) : ""; });
    var used = {};
    p = p.map(function (x) {
      if (x && used[x]) return "";
      if (x) used[x] = 1;
      return x;
    });
    while (p.length < n) {
      var nx = "";
      for (var i = 0; i < ids.length; i++) {
        if (!used[ids[i]]) { nx = ids[i]; used[nx] = 1; break; }
      }
      p.push(nx);
    }
    // 只补不截：从 4 格切到 1 格时别把后 3 格的选择丢掉，切回来还能还原
    state.picks = p;
    return state.picks;
  }

  /* 上方的"画面 N"选择条：只有摄像头比格子多、或者单画面时才有意义 */
  function renderPickBar(slotCount) {
    var bar = $("pickBar");
    if (!bar) return;
    if (!state.cams.length || state.cams.length === 1) {
      // 只有一台摄像头时没什么好选的，别占地方
      bar.hidden = true;
      bar.innerHTML = "";
      return;
    }
    bar.hidden = false;
    var picks = ensurePicks(slotCount);
    bar.innerHTML = "";
    var multi = slotCount > 1;
    for (var i = 0; i < slotCount; i++) {
      (function (slot) {
        var wrap = el("label", "pick-item");
        wrap.appendChild(el("span", "pick-label", multi ? "画面" + (slot + 1) : "画面"));

        var sel = el("select", "select pick-sel");
        var o0 = el("option", null, "（空）");
        o0.value = "";
        sel.appendChild(o0);
        state.cams.forEach(function (c) {
          var o = el("option", null, c.name || c.id);
          o.value = c.id;
          sel.appendChild(o);
        });
        sel.value = picks[slot] || "";

        sel.addEventListener("change", function () {
          var v = sel.value;
          var old = state.picks[slot] || "";
          // 选中的那台已经在别的格 → 两台对调，不会出现两格同一路
          for (var k = 0; k < state.picks.length; k++) {
            if (k !== slot && String(state.picks[k]) === String(v)) {
              state.picks[k] = old;
              break;
            }
          }
          state.picks[slot] = v;
          savePicks();
          state.focusId = null;
          mountTiles();
        });

        wrap.appendChild(sel);
        bar.appendChild(wrap);
      })(i);
    }
  }

  function blankTile(single) {
    var tile = el("div", "tile blank" + (single ? " single" : ""));
    tile.appendChild(el("div", "tile-blank", "空画面"));
    tile.appendChild(el("div", "tile-blank-sub", "用上方「画面」下拉选择摄像头"));
    return tile;
  }

  function mountTiles() {
    var grid = $("grid");
    var hint = $("emptyHint");
    unmountTiles();
    grid.innerHTML = "";
    var t0 = $("emptyHint");

    if (!state.cams.length) {
      t0.hidden = false;
      grid.style.display = "none";
      $("btnFullscreen").hidden = true;
      if ($("pickBar")) { $("pickBar").hidden = true; }
      return;
    }
    t0.hidden = true;
    grid.style.display = "";

    var slots = gridCount();
    renderPickBar(slots);

    var n = state.focusId ? 1 : slots;
    var picks = ensurePicks(slots);
    if (state.focusId) picks = [String(state.focusId)];

    grid.className = "grid n" + n;
    for (var i = 0; i < n; i++) {
      var cam = camById(picks[i]);
      grid.appendChild(cam ? makeTile(cam, n === 1) : blankTile(n === 1));
    }
    $("btnFullscreen").hidden = false;

    if (n === 1) {
      var only = grid.firstChild;
      if (only) only.classList.add("single");
    }
    refreshSoundBtn();
  }

  // ---- 云台（PTZ）--------------------------------------------------------
  // 摄像头的一条云台命令只转一小段（实测约 10°）就自己停，所以按住时必须
  // 连着发。每条命令带 0.9 秒租约：后端到点还没收到新的就补一条停止，
  // 浏览器掉线也不会让云台一直转到底。
  var PTZ_HOLD = 0.9;   // 单条命令的租约（秒）
  var PTZ_TICK = 300;   // 按住时的重发间隔（毫秒）
  function ptzSend(camId, op, hold) {
    return api("api/ptz/move", {
      method: "POST",
      body: { cam: camId, op: op, hold: hold }
    });
  }

  /* go2rtc 模式下，云台命令必须复用**取流那条** P2P 会话（摄像头并发上限只有
     3 条，不能另开）。后端在会话不存在时会自动建一条（借 go2rtc 的 preload），
     但建会话要 1~3 秒，所以这里给用户一个"正在建立"的反馈，
     并且失败后把这路画面拉到前台再重试一次。 */
  function focusCam(camId) {
    if (state.view !== "live") setView("live");
    if (String(state.focusId) !== String(camId)) {
      state.focusId = camId;
      mountTiles();
    }
  }

  function waitSession(camId, timeoutMs) {
    var t0 = Date.now();
    return new Promise(function (resolve) {
      (function poll() {
        api("api/ptz/status").then(function (j) {
          var st = ((j.cams || {})[camId]) || {};
          if (st.state === "connected") { resolve(true); return; }
          if (Date.now() - t0 > timeoutMs) { resolve(false); return; }
          setTimeout(poll, 700);
        }).catch(function () {
          if (Date.now() - t0 > timeoutMs) { resolve(false); return; }
          setTimeout(poll, 1200);
        });
      })();
    });
  }

  // 发一条云台命令；后端没能建起会话时，把这路画面拉起来再重试。
  function ptzMove(camId, op, hold, retried) {
    return ptzSend(camId, op, hold).catch(function (e) {
      if (!e.needSession || retried || !op) throw e;
      toast("正在建立这路摄像头的连接…");
      focusCam(camId);
      return waitSession(camId, 30000).then(function (ok) {
        if (!ok) throw new Error("这路摄像头连不上，请确认设备在线");
        return ptzMove(camId, op, hold, true);
      });
    });
  }

  function makePtzPad(cam) {
    var pad = el("div", "ptz-pad");
    var grid = el("div", "ptz-grid");
    var timer = null, cur = 0;

    function stop() {
      if (timer) { clearInterval(timer); timer = null; }
      if (cur) {
        cur = 0;
        // 停止命令不重试：没有会话时本来就没在转
        ptzSend(cam.id, 0).catch(function () {});
      }
    }
    function begin(op) {
      if (cur === op) return;
      stop();
      cur = op;
      ptzMove(cam.id, op, PTZ_HOLD).catch(function (e) {
        stop();
        toast(e.message, "err");
      });
      timer = setInterval(function () {
        if (!cur) return;
        ptzMove(cam.id, cur, PTZ_HOLD).catch(function () {});
      }, PTZ_TICK);
    }

    [["3", "▲", "上"], ["2", "◀", "左"], ["0", "●", "停止"],
     ["1", "▶", "右"], ["4", "▼", "下"]].forEach(function (d) {
      var op = parseInt(d[0], 10);
      var b = el("button", "ptz-btn" + (op === 0 ? " center" : ""), d[1]);
      b.title = d[2];
      if (op === 0) {
        b.addEventListener("click", function (e) { e.stopPropagation(); stop(); });
      } else {
        b.addEventListener("pointerdown", function (e) {
          e.preventDefault(); e.stopPropagation();
          b.classList.add("on");
          begin(op);
        });
        ["pointerup", "pointerleave", "pointercancel"].forEach(function (ev) {
          b.addEventListener(ev, function (e) {
            e.stopPropagation();
            b.classList.remove("on");
            stop();
          });
        });
      }
      grid.appendChild(b);
    });

    pad.appendChild(grid);
    pad.addEventListener("click", function (e) { e.stopPropagation(); });
    pad.addEventListener("dblclick", function (e) { e.stopPropagation(); });
    return pad;
  }

  // ---- 对讲（上行语音）---------------------------------------------------
  // 浏览器把麦克风采成 8kHz/16bit 单声道裸 PCM 交给后端，后端转 G.711 A-law
  // 后按 40ms 一包写进摄像头的上行音频通道（miss channel3）。
  // 这里刻意不做任何编码，出问题的可能面就只有"麦克风 / 网络 / 摄像头喇叭"。
  var talk = {
    cam: "", ctx: null, stream: null, src: null, node: null, sink: null,
    capture: "", buf: [], bufLen: 0, inFlight: false, sent: 0, started: 0
  };

  function talkFlush(force) {
    if (!talk.cam || talk.inFlight || !talk.bufLen) return;
    if (!force && talk.bufLen < 1920) return;      // 攒到 120ms 再发，别打成碎片
    var bytes = new Uint8Array(talk.bufLen), off = 0;
    for (var i = 0; i < talk.buf.length; i++) {
      bytes.set(talk.buf[i], off);
      off += talk.buf[i].length;
    }
    var camId = talk.cam;
    talk.buf = []; talk.bufLen = 0;
    talk.inFlight = true;
    fetch(url("api/talk/push?cam=" + encodeURIComponent(camId)), {
      method: "POST",
      headers: { "Content-Type": "application/octet-stream" },
      body: bytes
    }).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (j) {
        if (!r.ok || j.ok === false) throw new Error(j.error || ("推音失败 " + r.status));
        talk.sent += bytes.length;
      });
    }).catch(function (e) {
      toast(e.message, "err");
      talkEnd();
    }).then(function () {
      talk.inFlight = false;
      talkFlush(false);
    });
  }

  // 采集路径优先用 AudioWorklet：重采样跑在音频线程上，主线程卡顿不会让
  // PCM 断供（对讲久了会掉帧、爆音）。旧浏览器没有 audioWorklet 或 addModule
  // 失败时，退回已废弃但还能用的 createScriptProcessor，并在诊断里写明用的哪条。
  function talkPump(ctx, stream) {
    var srcNode = ctx.createMediaStreamSource(stream);
    talk.src = srcNode;
    if (ctx.audioWorklet && ctx.audioWorklet.addModule) {
      talk.capture = "AudioWorklet 模块加载中…";
      ctx.audioWorklet.addModule(url("talk-worklet.js?v=1"))
        .then(function () {
          // 建立过程中可能已经松手结束
          if (!talk.cam || talk.ctx !== ctx) { return; }
          return talkWorklet(ctx, srcNode);
        })
        .catch(function (e) {
          if (!talk.cam || talk.ctx !== ctx) return;
          talkScript(ctx, srcNode);
          toast("音频线程不可用，已退回主线程采集：" + (e && e.message ? e.message : e), "err");
        });
      return;
    }
    talkScript(ctx, srcNode);
  }

  function talkWorklet(ctx, srcNode) {
    var node = new AudioWorkletNode(ctx, "homeeye-talk-resample");
    node.port.onmessage = function (e) {
      if (!talk.cam) return;
      var u8 = new Uint8Array(e.data);
      talk.buf.push(u8);
      talk.bufLen += u8.length;
      talkFlush(false);
    };
    // 输出保持静音：WorkletNode 不接到 destination 就不会被驱动，
    // 而直接把麦克风送回去就是啸叫。
    var sink = ctx.createGain();
    sink.gain.value = 0;
    srcNode.connect(node);
    node.connect(sink);
    sink.connect(ctx.destination);
    talk.node = node;
    talk.sink = sink;
    talk.capture = "AudioWorklet（音频线程）";
    renderTalkDiag();
  }

  function talkScript(ctx, srcNode) {
    var node = ctx.createScriptProcessor(2048, 1, 1);
    var ratio = ctx.sampleRate / 8000;
    var frac = 0;

    node.onaudioprocess = function (ev) {
      if (!talk.cam) return;
      var inp = ev.inputBuffer.getChannelData(0);
      var n = inp.length, pos = frac, out = [];
      while (pos + 1 < n) {
        var i0 = Math.floor(pos), f = pos - i0;
        var v = inp[i0] * (1 - f) + inp[i0 + 1] * f;
        if (v > 1) v = 1; else if (v < -1) v = -1;
        out.push(v * 32767);
        pos += ratio;
      }
      frac = Math.max(0, pos - n);

      if (!out.length) return;
      var i16 = new Int16Array(out.length);
      for (var k = 0; k < out.length; k++) i16[k] = out[k];
      var u8 = new Uint8Array(i16.buffer);
      talk.buf.push(u8);
      talk.bufLen += u8.length;
      talkFlush(false);
    };

    srcNode.connect(node);
    node.connect(ctx.destination);   // 必须接到 destination 才会被驱动
    talk.node = node;
    talk.capture = "ScriptProcessor（主线程，已废弃但可用）";
    renderTalkDiag();
  }

  function talkBegin(cam, btn) {
    if (talk.cam) return;
    if (!window.isSecureContext) {
      // 浏览器的硬性限制：只有 HTTPS / localhost 才允许调麦克风。
      // 直接给出可点的地址，省得用户猜哪个入口才对；设置页那张卡把三条入口摊开讲。
      var https = talkHttpsUrl();
      renderTalkDiag();
      toast("麦克风只在 HTTPS 下可用，请改用 " + https, "err");
      return;
    }
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      toast("这个浏览器不支持麦克风采集", "err");
      return;
    }

    talk.cam = cam.id;
    talk.sent = 0;
    talk.started = Date.now();
    talk.capture = "";
    talk.buf = []; talk.bufLen = 0;
    if (btn) btn.classList.add("on");

    api("api/talk/start", { method: "POST", body: { cam: cam.id } })
      .catch(function (e) {
        // 对讲同样要复用取流会话；没有会话就把这路画面拉起来再试
        if (!e.needSession) throw e;
        toast("正在建立这路摄像头的连接…");
        focusCam(cam.id);
        return waitSession(cam.id, 30000).then(function (ok) {
          if (!ok) throw new Error("这路摄像头连不上，请确认设备在线");
          return api("api/talk/start", { method: "POST", body: { cam: cam.id } });
        });
      })
      .then(function () {
        if (!talk.cam) return;                    // 握手期间已经松手
        return navigator.mediaDevices.getUserMedia({
          audio: {
            channelCount: 1,
            echoCancellation: true,
            noiseSuppression: true,
            autoGainControl: true
          }
        });
      })
      .then(function (stream) {
        if (!stream) return;
        if (!talk.cam) {
          stream.getTracks().forEach(function (t) { t.stop(); });
          return;
        }
        talk.stream = stream;
        var AC = window.AudioContext || window.webkitAudioContext;
        try { talk.ctx = new AC({ sampleRate: 8000 }); } catch (e) { talk.ctx = new AC(); }
        if (talk.ctx.state === "suspended") talk.ctx.resume();
        talkPump(talk.ctx, stream);
        toast("对讲中…（再点一下 🎤 结束）", "ok");
      })
      .catch(function (e) {
        toast(talkErrText(e), "err");
        talkEnd();
      });
  }

  // talkHttpsUrl 猜一个可用的 HTTPS 地址。
  // HomeEye 除了主端口还会额外开一个 8443 的 HTTPS 端口，专门给麦克风用
  // （浏览器只在安全上下文下才放行 getUserMedia）。
  function talkHttpsUrl() {
    var host = location.hostname || "127.0.0.1";
    return "https://" + host + ":8443/";
  }

  function talkErrText(e) {
    var n = String((e && e.name) || "");
    if (n === "NotAllowedError") {
      // 飞牛 App 的内嵌 WebView 会把权限请求直接拒掉，且不给理由，
      // 表现和「用户点了拒绝」一样。给出可操作的替代方案。
      return "麦克风被拒绝。飞牛 App 内置浏览器可能不支持采集，" +
             "请用手机自带浏览器或电脑浏览器打开 " + talkHttpsUrl();
    }
    if (n === "NotFoundError") return "没检测到麦克风";
    if (n === "NotReadableError") return "麦克风被别的程序占用了";
    if (n === "SecurityError") return "浏览器安全策略拦截了麦克风，请改用 " + talkHttpsUrl();
    return "对讲失败：" + (e && e.message ? e.message : e);
  }

  function talkEnd() {
    var camId = talk.cam;
    if (!camId) return;
    talk.cam = "";
    if (talk.node) {
      // WorkletNode 的回调端口要显式解绑，否则 processor 不会被回收
      try { if (talk.node.port) talk.node.port.onmessage = null; } catch (e) { }
      try { talk.node.disconnect(); } catch (e) { }
      talk.node = null;
    }
    if (talk.sink) { try { talk.sink.disconnect(); } catch (e) { } talk.sink = null; }
    if (talk.src) { try { talk.src.disconnect(); } catch (e) { } talk.src = null; }
    if (talk.stream) {
      talk.stream.getTracks().forEach(function (t) { t.stop(); });
      talk.stream = null;
    }
    if (talk.ctx) {
      try { talk.ctx.close(); } catch (e) { }
      talk.ctx = null;
    }
    talk.buf = []; talk.bufLen = 0;
    try { api("api/talk/stop", { method: "POST", body: { cam: camId } }); } catch (e) { }
    document.querySelectorAll(".tile-talk.on").forEach(function (b) { b.classList.remove("on"); });
    renderTalkDiag();
  }

  function makeTile(cam, single) {
    var tile = el("div", "tile" + (state.fit === "cover" ? " cover" : ""));
    tile.setAttribute("data-cam", cam.id);

    var name = el("div", "tile-name");
    var dot = el("i", "dot " + (cam.live && cam.live.alive ? "ok" : "bad"));
    name.appendChild(dot);
    name.appendChild(el("span", null, cam.name || cam.id));
    tile.appendChild(name);

    if (cam.rec && cam.rec.running) {
      tile.appendChild(el("div", "tile-meta", "● 录像中"));
    }

    var video = document.createElement("video");
    // live.autoplay 是设置项：设置页勾了"不要自动播放"就不该一进来就抢播
    video.autoplay = ((state.settings || {}).live || {}).autoplay !== false;
    video.muted = state.muted;
    video.playsInline = true;
    video.alt = cam.name || cam.id;
    attachStream(video, tile, cam);
    tile.appendChild(video);
    tiles[cam.id] = { video: video, player: null, tile: tile };

    // 每格单独的声音按钮：两路同时出声会混在一起，所以要能只开这一路
    var snd = el("button", "tile-sound" + (video.muted ? " off" : ""),
                 video.muted ? "🔇" : "🔊");
    snd.title = video.muted ? "打开这一路的声音" : "关闭这一路的声音";
    snd.addEventListener("click", function (e) {
      e.stopPropagation();          // 别触发"点画面放大"
      soundOnly(cam.id, video.muted);
    });
    tile.appendChild(snd);
    tile._snd = snd;

    // 云台：小米摄像头（按 did 判定——迁移到 go2rtc 后 type 会变成 rtsp，
    // 但 did 还在，云台/对讲照样能用）+ 云台后端就绪时才出现。
    // 注意这里刻意不重建 tile（重建会让视频重连），只往这一格里塞/收方向盘。
    if (cam.did && state.ptz && state.ptz.available) {
      var ptzBtn = el("button", "tile-ptz", "✥");
      ptzBtn.title = "云台控制";
      ptzBtn.addEventListener("click", function (e) {
        e.stopPropagation();
        var old = tile.querySelector(".ptz-pad");
        if (old) { old.parentNode.removeChild(old); return; }
        tile.appendChild(makePtzPad(cam));
        // 先把 P2P 会话建起来（按需建要 1~3 秒），免得第一下按下去卡在
        // "正在建立连接"上。op=0 只是空命令，摄像头不会动。
        ptzSend(cam.id, 0).catch(function () {});
      });
      tile.appendChild(ptzBtn);

      // 对讲：点击开始、再点结束（持续通话模式）。talk.cam 在 talkBegin
      // 里是同步设置的，所以握手慢也不会出现"点两次开了两条"的竞态。
      var mic = el("button", "tile-talk", "🎤");
      mic.title = "对讲（点击开始，再点结束）";
      mic.addEventListener("click", function (e) {
        e.stopPropagation();
        if (talk.cam === cam.id) talkEnd();
        else talkBegin(cam, mic);
      });
      /* 这一格如果是在对讲进行中被重绘出来的，新按钮要把"正在说话"的
         状态补上，不然用户会以为没生效。 */
      if (talk.cam === cam.id) mic.classList.add("on");
      tile.appendChild(mic);
    }

    tile.addEventListener("click", function (e) {
      if (e.target === video && state.grid > 1 && !state.focusId) {
        state.focusId = cam.id;
        mountTiles();
      } else if (state.focusId) {
        state.focusId = null;
        mountTiles();
      }
    });
    tile.addEventListener("dblclick", function (e) {
      e.preventDefault();
      if (!state.focusId) { state.focusId = cam.id; mountTiles(); }
      setTimeout(enterFullscreen, 30);
    });
    return tile;
  }

  function attachStream(video, tile, cam) {
    if (!window.mpegts) {
      showTileErr(tile, cam, "mpegts.js 未加载");
      return;
    }
    if (!window.mpegts.isSupported()) {
      showTileErr(tile, cam, "当前浏览器不支持 MSE，请换 Chrome/Edge/Firefox");
      return;
    }
    var player = mpegts.createPlayer({
      type: "mpegts",        // MPEG-TS 直播流
      isLive: true,
      url: url("live/" + cam.id + ".ts"),
      enableStashBuffer: false,   // 直播尽量低延迟
      liveBufferLatencyChasing: true,
      lazyLoad: false
    }, {
      // 不开 worker：mpegts.js 的 worker 是从 blob 里 new 出来的，在部分
      // Chromium 环境下 worker 内的 fetch 会被拦掉，表现为立刻抛 NetworkError
      // 且**服务端根本收不到请求**（订阅数一直是 0）。主线程解封装足够用。
      enableWorker: false,
      liveBufferLatencyMaxLatency: 2.0,
      liveBufferLatencyMinRemain: 0.3,
      enableAutoCleanupSourceBuffer: true
    });
    tiles[cam.id] = { video: video, player: player, tile: tile };
    player.attachMediaElement(video);

    player.on(mpegts.Events.ERROR, function (type, detail) {
      if (state.view !== "live") return;
      // 把错误类型/细节显示出来，方便定位（以前只显示"拉流失败"，什么都看不出来）
      var msg = "拉流失败，自动重试中…";
      try {
        if (type !== undefined) {
          msg = "错误 " + type;
          if (detail) {
            msg += "：" + (detail.msg || detail.reason || detail.code ||
                           (detail.info && detail.info.msg) || "");
          }
        }
      } catch (e) {}
      var old = tile.querySelector(".tile-err");
      if (old) old.remove();
      showTileErr(tile, cam, msg);
    });
    player.on(mpegts.Events.MEDIA_INFO, function (info) {
      var err = tile.querySelector(".tile-err");
      if (err) err.remove();
    });
    player.load();
    video.play().catch(function () {});
  }

  function showTileErr(tile, cam, msg) {
    if (tile.querySelector(".tile-err")) return;
    var box = el("div", "tile-err");
    box.appendChild(el("b", null, (cam.name || cam.id) + " 无画面"));
    // 服务端已经知道原因（设备离线 / 连不上）就直接说原因，别只说"请检查地址"
    var srvErr = cam && cam.live && cam.live.error ? String(cam.live.error) : "";
    box.appendChild(el("span", null, srvErr || msg || "请检查地址/账号密码/网络"));
    box.appendChild(el("code", null, cam.url_masked || ""));
    tile.appendChild(box);
  }

  /* ------------------------------------------------------------ 声音
     浏览器策略：没有用户交互时带声音的自动播放会被拦（画面会直接黑着不动），
     所以页面加载后默认静音，由用户点一下喇叭再开声。
     多路画面各自有喇叭：只开其中一路，避免两个房间的声音混在一起。 */
  function tileVideo(k) {
    var t = tiles[k];
    if (!t) return null;
    return t.video || (t.tile && t.tile.querySelector("video")) || null;
  }

  function refreshSoundBtn() {
    var b = $("btnSound");
    var anyOn = Object.keys(tiles).some(function (k) {
      var v = tileVideo(k);
      return v && !v.muted;
    });
    state.muted = !anyOn;
    if (b) b.textContent = state.muted ? "🔇 静音" : "🔊 有声";
    Object.keys(tiles).forEach(function (k) {
      var t = tiles[k];
      if (!t.tile || !t.tile._snd) return;
      var v = tileVideo(k);
      var off = !v || v.muted;
      t.tile._snd.textContent = off ? "🔇" : "🔊";
      t.tile._snd.classList.toggle("off", off);
      t.tile._snd.title = off ? "打开这一路的声音" : "关闭这一路的声音";
    });
  }

  function soundOnly(camId, on) {
    Object.keys(tiles).forEach(function (k) {
      var v = tileVideo(k);
      if (!v) return;
      if (on) v.muted = (String(k) !== String(camId));
      else if (String(k) === String(camId)) v.muted = true;
      if (!v.muted) v.play().catch(function () {});
    });
    refreshSoundBtn();
  }

  $("btnSound").addEventListener("click", function () {
    state.muted = !state.muted;
    Object.keys(tiles).forEach(function (k) {
      var v = tileVideo(k);
      if (!v) return;
      v.muted = state.muted;
      if (!v.muted) v.play().catch(function () {});
    });
    var p = $("player");
    if (p) p.muted = state.muted;
    refreshSoundBtn();
  });

  /* 分屏按钮 */
  Array.prototype.forEach.call(document.querySelectorAll("#gridPick button"), function (b) {
    b.addEventListener("click", function () {
      Array.prototype.forEach.call(document.querySelectorAll("#gridPick button"), function (x) {
        x.classList.remove("on");
      });
      b.classList.add("on");
      state.grid = parseInt(b.getAttribute("data-n"), 10) || 4;
      state.focusId = null;
      localStorage.setItem("nw.grid", state.grid);
      mountTiles();
    });
  });

  $("fitToggle").addEventListener("change", function () {
    state.fit = this.checked ? "contain" : "cover";
    localStorage.setItem("nw.fit", state.fit);
  });

  /* ------------------------------------------------------------ 全屏 */
  function fsSupported() {
    var d = document;
    return !!(d.fullscreenEnabled || d.webkitFullscreenEnabled);
  }
  function inIframe() {
    try { return window.top !== window.self; } catch (e) { return true; }
  }
  function fsElement(target) {
    var d = document;
    return d.fullscreenElement || d.webkitFullscreenElement || null;
  }
  function enterFullscreen() {
    var target = state.view === "play" ? document.querySelector(".player-wrap") : $("stage");
    if (!target) return;
    if (fsElement()) return;
    var req = target.requestFullscreen || target.webkitRequestFullscreen;
    var done = false;
    var timer = setTimeout(function () {
      if (done || fsElement()) return;
      done = true;
      fakeFs(target);
      if (inIframe()) toast("飞牛桌面窗口不允许真全屏，已铺满应用窗口；想看满整个屏幕请在浏览器新标签页打开本应用");
    }, 320);

    if (req && fsSupported()) {
      var p = null;
      try { p = req.call(target, { navigationUI: "hide" }); } catch (e) { p = null; }
      if (p && typeof p.then === "function") {
        p.then(function () { clearTimeout(timer); }).catch(function () {
          clearTimeout(timer);
          if (done) return;
          done = true;
          fakeFs(target);
          if (inIframe()) toast("已铺满应用窗口（桌面 iframe 不允许真全屏，新标签页打开可全屏）");
        });
        return;
      }
      if (p === undefined) { clearTimeout(timer); return; }
    }
    clearTimeout(timer);
    done = true;
    fakeFs(target);
  }
  function exitFullscreen() {
    var d = document;
    var target = document.querySelector(".is-fs");
    if (fsElement()) {
      var ex = d.exitFullscreen || d.webkitExitFullscreen;
      try { ex && ex.call(d); } catch (e) {}
    }
    if (target) target.classList.remove("is-fs");
    document.body.classList.remove("fs-on");
    if (state.view === "live") setTimeout(mountTiles, 60);
  }
  function fakeFs(target) {
    if (target.classList.contains("is-fs")) {
      target.classList.remove("is-fs");
      document.body.classList.remove("fs-on");
    } else {
      target.classList.add("is-fs");
      document.body.classList.add("fs-on");
    }
  }
  $("btnFullscreen").addEventListener("click", function () {
    if (fsElement() || document.querySelector(".is-fs")) exitFullscreen();
    else enterFullscreen();
  });
  $("btnPlayerFs").addEventListener("click", function () {
    if (fsElement() || document.querySelector(".is-fs")) exitFullscreen();
    else enterFullscreen();
  });
  document.addEventListener("fullscreenchange", function () {
    document.body.classList.toggle("fs-on", !!fsElement());
    if (state.view === "live") setTimeout(mountTiles, 80);
  });
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape" && document.querySelector(".is-fs")) exitFullscreen();
  }, true);
  document.querySelector(".player-wrap").addEventListener("dblclick", function (e) {
    if (e.target.id === "player") enterFullscreen();
  });

  /* ------------------------------------------------------------ 回放 */
  function refreshDays() {
    var sel = $("playCam");
    var cams = state.cams;
    if (!cams.length) {
      sel.innerHTML = "";
      $("playerTip").textContent = "还没有摄像头";
      return;
    }
    var keep = state.play.cam;
    sel.innerHTML = "";
    cams.forEach(function (c) {
      var o = el("option", null, c.name || c.id);
      o.value = c.id;
      sel.appendChild(o);
    });
    var want = keep && cams.some(function (c) { return String(c.id) === String(keep); })
      ? keep : cams[0].id;
    sel.value = want;
    if (String(state.play.cam) !== String(want) || !state.play.day) {
      state.play.cam = want;
      loadDays(want);
    }
  }

  /* 找第一台"确实有录像"的摄像头（默认选中的那台没数据时用） */
  function autoPickCamWithData(exclude) {
    var others = state.cams.filter(function (c) {
      return String(c.id) !== String(exclude);
    });
    var i = 0;
    function step() {
      if (i >= others.length) return Promise.resolve(null);
      var c = others[i++];
      return api("api/recordings?cam=" + encodeURIComponent(c.id)).then(function (j) {
        if ((j.days || []).length) return c.id;
        return step();
      }).catch(function () { return step(); });
    }
    return step();
  }

  function loadDays(camId) {
    var box = $("dayList");
    box.innerHTML = "";
    api("api/recordings?cam=" + encodeURIComponent(camId)).then(function (j) {
      var days = j.days || [];
      if (!days.length) {
        box.appendChild(el("span", "hint", "这台摄像头还没有录像"));
        state.play.segs = [];
        renderTimeline([]);
        $("playerTip").textContent = "该摄像头暂无录像";
        if (state.play.auto) {
          state.play.auto = false;
          autoPickCamWithData(camId).then(function (alt) {
            if (!alt) return;
            state.play.cam = alt;
            state.play.day = null;
            $("playCam").value = alt;
            loadDays(alt);
          });
        }
        return;
      }
      days.slice(0, 21).forEach(function (d) {
        var b = el("button", "day" + (d === state.play.day ? " on" : ""), d.slice(5));
        b.setAttribute("data-day", d);
        b.title = d;
        b.addEventListener("click", function () { pickDay(d); });
        box.appendChild(b);
      });
      if (!state.play.day || days.indexOf(state.play.day) < 0) {
        pickDay(days[0]);
      } else {
        pickDay(state.play.day, true);
      }
    }).catch(function (e) { toast(e.message, "err"); });
  }

  function pickDay(day, keepPos) {
    state.play.day = day;
    Array.prototype.forEach.call(document.querySelectorAll("#dayList .day"), function (b) {
      b.classList.toggle("on", b.getAttribute("data-day") === day);
    });
    var parts = day.split("-");
    var d0 = new Date(+parts[0], +parts[1] - 1, +parts[2], 0, 0, 0);
    state.play.dayStart = Math.floor(d0.getTime() / 1000);
    var t1 = state.play.dayStart + 86400;
    $("playerTip").textContent = "加载中…";
    $("playerMask").hidden = false;
    api("api/playlist?cam=" + encodeURIComponent(state.play.cam) + "&from=" + state.play.dayStart + "&to=" + t1)
      .then(function (j) {
        state.play.segs = j.segments || [];
        renderTimeline(state.play.segs);
        if (!state.play.segs.length) {
          $("playerTip").textContent = "这一天没有录像";
          return;
        }
        if (!keepPos) {
          // 默认跳到当天最新一段：用户点回放十有八九是想看"刚才/最近"的画面，
          // 落在最早一段（凌晨/早上）得手动拖半天才找得到当前。
          var last = state.play.segs[state.play.segs.length - 1];
          jumpTo(Math.min(state.play.dayStart + 86399, last.end - 1));
          $("playerTip").textContent =
            "今天共 " + state.play.segs.length + " 段录像（点时间轴任意位置回看）";
        }
      }).catch(function (e) { toast(e.message, "err"); });
  }

  function renderTimeline(segs) {
    var ruler = $("tlRuler");
    ruler.innerHTML = "";
    for (var h = 0; h <= 24; h += 3) {
      var s = el("span", null, h + ":00");
      s.style.left = (h / 24 * 100) + "%";
      ruler.appendChild(s);
    }
    var blocks = $("tlBlocks");
    blocks.innerHTML = "";
    var d0 = state.play.dayStart;
    segs.forEach(function (sg) {
      var left = (sg.start - d0) / 86400 * 100;
      var w = Math.max(0.12, (sg.end - sg.start) / 86400 * 100);
      var b = el("div", "tl-blk");
      b.style.left = left + "%";
      b.style.width = w + "%";
      b.title = hhmmss(sg.start) + " – " + hhmmss(sg.end);
      blocks.appendChild(b);
    });
  }

  $("tlTrack").addEventListener("click", function (e) {
    if (!state.play.segs.length) return;
    var r = this.getBoundingClientRect();
    var frac = Math.min(0.9999, Math.max(0, (e.clientX - r.left) / r.width));
    jumpTo(state.play.dayStart + Math.floor(frac * 86400));
  });

  function jumpTo(ts) {
    var segs = state.play.segs;
    if (!segs.length) return;
    var seg = null;
    for (var i = 0; i < segs.length; i++) {
      if (segs[i].start <= ts && ts < segs[i].end) { seg = segs[i]; break; }
    }
    if (!seg) {
      for (var k = 0; k < segs.length; k++) {
        if (segs[k].start > ts) { seg = segs[k]; break; }
      }
      seg = seg || segs[segs.length - 1];
    }
    playSeg(seg, Math.max(0, ts - seg.start));
  }

  /* 浏览器能不能直接解 H.265/HEVC（小米等新机器多数是 H.265） */
  var _hevc = null;
  function canHevc() {
    if (_hevc !== null) return _hevc;
    try {
      var p = document.createElement("video").canPlayType('video/mp4; codecs="hvc1"');
      _hevc = (p === "probably" || p === "maybe");
    } catch (e) {
      _hevc = false;
    }
    return _hevc;
  }

  function playSeg(seg, offset) {
    var v = $("player");
    state.play.seg = seg;
    $("playerMask").hidden = true;
    var segLen = Math.max(1, (seg.end || seg.start + 60) - seg.start);

    /* 优先直接播原文件：不耗 CPU、能随便拖进度条。
       只有浏览器解不了 HEVC 时，才让服务端边转边发（起播慢 2~3 秒，
       且不能拖进度条，但点了时间轴会按新起点重新请求，体感一样）。 */
    var direct = canHevc();
    state.play.offsetBase = 0;
    if (direct) {
      v.src = url("rec/" + seg.file);
    } else {
      state.play.offsetBase = Math.max(0, offset || 0);
      v.src = url("rech264/" + seg.file +
                  "?start=" + Math.floor(state.play.offsetBase) +
                  "&dur=" + Math.ceil(Math.max(1, segLen - state.play.offsetBase)));
      $("playerTip").textContent = "浏览器不支持 H.265，回放已实时转 H.264（约 2~3 秒起播）";
    }

    v.addEventListener("loadedmetadata", function once() {
      v.removeEventListener("loadedmetadata", once);
      if (direct && offset > 0.4 && offset < (v.duration || 1e9) - 0.3) {
        try { v.currentTime = offset; } catch (e) {}
      }
      // 回放是用户主动点进来的，优先带声音播；被浏览器策略拦下就退回静音，
      // 播放器本身有音量控件，点一下就能开声。
      v.muted = false;
      v.play().catch(function () {
        v.muted = true;
        v.play().catch(function () {});
      });
    });
    v.addEventListener("error", function once2() {
      v.removeEventListener("error", once2);
      if (!direct) return;
      // 原文件解不了（多半是 HEVC 但 canPlayType 谎报支持）→ 兜底走转码
      if (!state.play.seg) return;
      state.play.offsetBase = Math.max(0, offset || 0);
      v.src = url("rech264/" + seg.file +
                  "?start=" + Math.floor(state.play.offsetBase) +
                  "&dur=" + Math.ceil(Math.max(1, segLen - state.play.offsetBase)));
      v.play().catch(function () {});
    });
  }

  $("player").addEventListener("ended", function () {
    var segs = state.play.segs, cur = state.play.seg;
    if (!cur) return;
    var i = segs.indexOf(cur);
    if (i >= 0 && i + 1 < segs.length) playSeg(segs[i + 1], 0);
  });

  $("player").addEventListener("timeupdate", function () {
    var seg = state.play.seg;
    if (!seg) return;
    var head = $("tlHead");
    var playhead = seg.start + (state.play.offsetBase || 0) + (this.currentTime || 0);
    state.play.playhead = playhead;
    var left = (playhead - state.play.dayStart) / 86400 * 100;
    head.style.display = "block";
    head.style.left = Math.min(99.9, Math.max(0, left)) + "%";
    $("playClock").textContent = hhmmss(playhead);
  });

  $("playCam").addEventListener("change", function () {
    state.play.cam = this.value;
    state.play.day = null;
    state.play.segs = [];
    state.play.auto = false;   // 用户手动选了，就别再自动跳走
    loadDays(this.value);
  });

  /* ------------------------------------------------------------ 设置页 */
  function renderCams() {
    var box = $("camList");
    box.innerHTML = "";
    if (!state.cams.length) {
      box.appendChild(el("p", "hint", "还没有摄像头。"));
      return;
    }
    state.cams.forEach(function (cam) {
      var item = el("div", "cam-item");
      var main = el("div", "ci-main");
      var nm = el("div", "ci-name");
      nm.appendChild(el("span", null, cam.name || cam.id));
      if (cam.record) nm.appendChild(el("span", "tag rec", "录像"));
      if (!cam.enabled) nm.appendChild(el("span", "tag off", "已停用"));
      main.appendChild(nm);
      main.appendChild(el("div", "ci-url", cam.url_masked || ""));
      item.appendChild(main);

      var acts = el("div", "ci-acts");
      var bSnap = el("button", "btn small", "截图");
      bSnap.addEventListener("click", function () { openSnapshot(cam); });
      var bEdit = el("button", "btn small", "编辑");
      bEdit.addEventListener("click", function () { openCamModal(cam); });
      var bDel = el("button", "btn small danger", "删除");
      bDel.addEventListener("click", function () { delCam(cam); });
      acts.appendChild(bSnap); acts.appendChild(bEdit); acts.appendChild(bDel);
      item.appendChild(acts);
      box.appendChild(item);
    });

    var u = state.storage || {};
    var box2 = $("usageBox");
    box2.innerHTML = "";
    var pct = u.disk_total ? Math.round(u.disk_used / u.disk_total * 100) : 0;
    box2.innerHTML =
      "录像占用 <b>" + fmtBytes(u.total_bytes || 0) + "</b><br>" +
      "所在磁盘：剩余 <b>" + fmtBytes(u.disk_free || 0) + "</b> / 共 " + fmtBytes(u.disk_total || 0) +
      "<div class='bar'><i style='width:" + pct + "%'></i></div>" +
      "<span class='hint mono'>" + (u.root || "") + "</span>";
  }

  // 提示行元素可能因为界面精简被移除，统一走这个安全写入口
  function hint(id, text) {
    var n = $(id);
    if (n) n.textContent = text;
  }

  /* 表单读写小工具：元素不存在时静默跳过，避免某个字段漏写就整页报错 */
  function sv(id, v) { var n = $(id); if (n) n.value = (v == null ? "" : v); }
  function sc(id, v) { var n = $(id); if (n) n.checked = !!v; }
  function gv(id) { var n = $(id); return n ? n.value : ""; }
  function gc(id) { var n = $(id); return n ? n.checked : false; }
  function gn(id, d) { var v = parseInt(gv(id), 10); return isNaN(v) ? d : v; }
  function gf(id, d) { var v = parseFloat(gv(id)); return isNaN(v) ? d : v; }

  /* live.transcode 从布尔升级成三档（auto/always/never）。
     后端 RULES 也会归一，但老配置直接读进来时前端仍可能拿到 true/false ——
     这里翻成"照旧"的档位，别让升级把转码行为悄悄改掉。 */
  function tmodeVal(v) {
    if (v === true) return "always";
    if (v === false) return "never";
    var s = String(v == null ? "" : v).toLowerCase();
    return (s === "auto" || s === "always" || s === "never") ? s : "auto";
  }

  /* 只读的小清单（每路一行：名字 + 实际状态 + 一句话原因）。
     用 health-row 那套样式，跟链路健康长得一致，避免再造一个类。 */
  function renderRows(boxId, items) {
    var box = $(boxId);
    if (!box) return;
    box.textContent = "";
    if (!items.length) {
      box.appendChild(el("div", "h-detail", "—"));
      return;
    }
    items.forEach(function (it) {
      var row = el("div", "health-row state-" + (it.state || "idle"));
      row.appendChild(el("span", "h-name", it.name));
      if (it.badge) row.appendChild(el("span", "badge state-" + (it.state || "idle"), it.badge));
      row.appendChild(el("span", "h-detail", it.detail || ""));
      box.appendChild(row);
    });
  }

  function renderLiveVideoMode() {
    renderRows("liveVideoMode", (state.cams || []).map(function (c) {
      var lv = c.live || {};
      if (!lv.video) {
        return { name: c.name || c.id, detail: "还没起过这一路的取流（打开实时画面后才会知道它是直拷还是转码）" };
      }
      return {
        name: c.name || c.id,
        state: lv.video === "copy" ? "ok" : "idle",
        badge: lv.video === "copy" ? "不转码，原码流直拷" : "转成 H.264",
        detail: (lv.codec ? ("源 " + lv.codec + " · ") : "") + (lv.video_note || "")
      };
    }));
  }

  function renderRecSourceNotes() {
    renderRows("recSourceNotes", (state.cams || []).map(function (c) {
      var rec = c.rec || {};
      return {
        name: c.name || c.id,
        state: rec.running ? "ok" : "idle",
        badge: rec.running ? "录制中" : "未在录",
        detail: (rec.source_note || "") + (rec.url ? ("｜ 在录 " + rec.url) : "")
      };
    }));
  }

  // 对讲这一栏不做「开关」，做「诊断」：能不能采麦克风是浏览器按页面来源判的，
  // 应用改不了。所以把当前来源和三条通用入口逐条摊开，让用户自己看出该换哪个地址。
  function renderTalkDiag() {
    var host = location.hostname || "127.0.0.1";
    var secure = !!window.isSecureContext;
    var loop = /^(127\.[0-9.]+|localhost|\[::1\])$/.test(host);
    var port = location.port || (location.protocol === "https:" ? "443" : "80");
    var rows = [];

    rows.push({
      name: "现在这个页面",
      state: secure ? "ok" : "broken",
      badge: secure ? "允许采麦克风" : "不允许采麦克风",
      detail: location.origin + " —— 浏览器只看页面来源是不是安全上下文，" +
              "跟声音用什么协议传出去没有关系"
    });
    rows.push({
      name: "入口一 · 本机回环",
      state: loop ? (secure ? "ok" : "offline") : "offline",
      badge: loop ? "正在用这条" : "只在能打开本机的设备上可用",
      detail: "http://127.0.0.1:" + port + "/ —— 浏览器把 localhost 算作安全上下文，" +
              "不需要任何证书。代价是只能在装了浏览器的那台机器上打开，手机和别的电脑用不了"
    });
    rows.push({
      name: "入口二 · 内置 HTTPS",
      state: "offline",
      badge: "自签证书",
      detail: talkHttpsUrl() + " —— HomeEye 自己起的 HTTPS 端口，只在直连这台机器时通。" +
              "第一次要在浏览器里点「高级 → 继续前往」并信任，手机系统浏览器对自签证书的" +
              "接受度不一，桌面浏览器最稳；经反向代理进来时这个端口一般没被转发，走入口三"
    });
    rows.push({
      name: "入口三 · 真证书域名",
      state: location.protocol === "https:" ? "ok" : "offline",
      badge: location.protocol === "https:" ? "正在用这条" : "面向外人与上架的唯一通法",
      detail: "由反向代理（fnOS 的证书申请，或 Caddy / nginx + Let's Encrypt）终止 TLS，" +
              "再把 HomeEye 转发出去。只有这条能让别人装完就用麦克风 —— " +
              "不依赖任何内网穿透或组网工具，也不会有「不信任的证书」提示"
    });
    rows.push({
      name: "App 内嵌 WebView",
      state: "offline",
      badge: "常见被拒",
      detail: "飞牛 App 之类内嵌 WebView 会把麦克风权限请求直接拒掉且不给理由，" +
              "表现和「点了拒绝」一样。换手机自带浏览器打开上面的地址即可"
    });
    rows.push({
      name: "本次采集路径",
      state: talk.capture ? "ok" : "offline",
      badge: talk.capture ? "已建立" : "还没开始过对讲",
      detail: talk.capture ||
              "AudioWorklet 在音频线程重采样；浏览器不支持时自动退回主线程的 ScriptProcessor"
    });

    renderRows("talkDiag", rows);
    var chain = $("talkChain");
    if (chain) {
      var audOn = (((state.settings || {}).live) || {}).audio !== false;
      chain.textContent =
        "上行：麦克风 → " + (talk.capture || "AudioWorklet") + " 重采样成 8kHz/16bit 单声道 " +
        "→ 约 120ms 一块 POST 给后端 → go2rtc 转 G.711 A-law 写进设备喇叭通道。" +
        "下行：设备声音跟着实时画面那一路进来，当前 live.audio 为" +
        (audOn ? "开（听得到对方）" : "关 —— 听不到对方，链路只剩半边，到「实时画面」里打开");
    }
  }

  /* ---- 设置项诚实化：不顶事的格子要灰掉并说清为什么 ------------------ */
  // meta 由后端给（nw_config.UNHONORED），前端不再自己记一遍 —— 免得两边漂移。
  // 组名匹配整组，"组.键" 匹配单项。
  var HONEST_IDS = {
    "motion": ["setPreSec", "setPostSec",
               "setMinFrames", "setMaxEvent", "setDetectPersons"],
    "storage.keep_events_days": ["setKeepEvents"],
    "notify.on_motion": ["setOnMotion"]
  };
  var HONEST_WHY = {
    "motion": "motionLocalWhy",
    "storage.keep_events_days": "keepEventsWhy",
    "notify.on_motion": ""
  };

  function applyHonesty(meta) {
    Object.keys(HONEST_IDS).forEach(function (tag) {
      var group = tag.indexOf(".") < 0 ? tag : tag.split(".")[0];
      var info = (meta || {})[group];
      var hit = false;
      if (info) {
        if (tag.indexOf(".") < 0) hit = true;
        else hit = (info.keys || []).indexOf(tag.split(".")[1]) >= 0;
      }
      HONEST_IDS[tag].forEach(function (id) {
        var n = $(id);
        if (!n) return;
        n.disabled = hit;
        n.parentNode.style.opacity = hit ? "0.45" : "";
        if (hit) n.title = info.why;
      });
      var whyId = HONEST_WHY[tag];
      if (!whyId) return;
      var w = $(whyId);
      if (w) {
        w.textContent = hit ? ("现在改了没反应：" + info.why) : "";
        w.style.display = hit ? "" : "none";
      }
      // 不在整张卡片上盖"未实现"：侦测这一面已经能真的读写摄像头，
      // 只有下面"HomeEye 侧的画面分析参数"那一节是占位，说在那一节里就够了。
    });
  }

  function renderStorageState(st) {
    var n = $("setStorageState");
    if (!n) return;
    if (!st) { n.textContent = "存储状态：—"; return; }
    if (!st.ok) {
      n.className = "hint bad";
      n.textContent = "存储有问题：" + (st.error || "录像目录不可写") +
        " —— 这会静默丢录像，先修这里";
      return;
    }
    var free = st.free_gb == null ? "?" : (st.free_gb + " GB");
    n.className = "hint good";
    n.textContent = "可写 · 盘剩 " + free +
      (st.budget_gb ? (" · 预算上限 " + st.budget_gb + " GB") : " · 未设容量上限（建议设）");
  }

  /* ---- 录像目录：从"看得见的挂载点"里选，而不是让人猜路径 ---------------- */
  var rootsLoaded = false;

  function loadRoots() {
    if (rootsLoaded) return;
    var sel = $("setDirPick");
    if (!sel) return;
    api("api/storage/roots").then(function (j) {
      rootsLoaded = true;
      var list = j.roots || [], cur = j.current || "";
      sel.textContent = "";
      var head = el("option", "", "（保持当前：" + (cur || "应用数据目录") + "）");
      head.value = "__keep__";
      sel.appendChild(head);
      list.forEach(function (r) {
        var o = el("option", "", r.path + " ｜ 剩 " + r.free_gb + " / 共 " +
          r.total_gb + " GB" + (r.writable ? "" : "（写不进去）") +
          (r.risky ? "（系统目录，别选）" : ""));
        o.value = r.path;
        o.disabled = !r.writable;
        sel.appendChild(o);
      });
      var hint = $("setRootsHint");
      if (hint) {
        hint.textContent = "容器里看得见 " + list.length + " 个挂载点。" +
          "没有你要的那块盘？那是 compose 还没把它挂进来（飞牛要用真实 /volN 路径，不是 /fs 显示层）。";
      }
    }).catch(function () {
      var hint = $("setRootsHint");
      if (hint) hint.textContent = "读不到挂载点列表（可能未登录），下面手填路径。";
    });
  }

  function pickDirFromRoots() {
    var sel = $("setDirPick"), input = $("setDir");
    if (!sel || !input) return;
    sel.addEventListener("change", function () {
      if (sel.value === "__keep__") return;
      input.value = sel.value;
      var box = input.closest(".field");
      if (box) box.style.outline = "1px solid var(--ok)";
    });
  }
  pickDirFromRoots();

  function fillSettings() {
    var s = state.settings || {};
    var st = s.storage || {}, lv = s.live || {}, mo = s.motion || {}, nt = s.notify || {};

    // 录像存储
    sv("setDir", st.dir);
    sv("setRecMode", st.record_mode || "continuous");
    sv("setSegLen", st.segment_minutes);
    sv("setRetention", st.retention_days);
    sv("setBudget", st.budget_gb);
    sv("setKeepEvents", st.keep_events_days);
    sc("setRecAudio", st.record_audio);
    sv("setRecSource", st.source || "PLAYED");
    var ed = $("setEffDir");
    if (ed) ed.textContent = "生效目录：" + (st.effective_dir || "—");
    renderStorageState(s.storage_status);
    renderRecSourceNotes();
    applyHonesty(s.meta);
    loadRoots();

    // 云台
    var pz = s.ptz || {};
    sc("setPtzOn", pz.enabled);
    sv("setPtzUser", pz.user);
    sv("setPtzToken", "");            // token 不回显，留空表示不修改
    if (pz.token_set) $("setPtzToken").placeholder = "已设置（留空 = 不修改）";
    sv("setPtzStep", pz.step_seconds == null ? 0.45 : pz.step_seconds);
    sv("setPtzIdle", pz.idle_seconds == null ? 45 : pz.idle_seconds);
    sv("setPtzG2r", pz.g2r_addr || "");
    sv("setPtzG2rUser", pz.g2r_user || "");
    var gp = $("setPtzG2rPass");
    if (gp) {
      gp.value = "";
      gp.placeholder = pz.g2r_pass_set ? "已设置（留空 = 不修改）" : "留空 = 不修改";
    }
    sv("setPtzMode", pz.mode || "auto");
    sc("setPtzAudio", pz.media_audio);
    var ps = $("ptzState");
    if (ps) {
      var av = state.ptz || {};
      ps.textContent = "状态：" + (av.available ? "已就绪" : (av.reason || "未启用"));
    }

    // 账号安全
    var au = s.auth || {};
    sv("setAuthUser", au.username || "admin");
    sv("setAuthHours", au.session_hours == null ? 24 : au.session_hours);
    sc("setAuthOn", au.enabled !== false);
    var as = $("authState");
    if (as) {
      as.textContent = au.configured
        ? "状态：已设密码（用户 " + (au.username || "admin") + "），"
          + (au.enabled === false ? "但闸门已关闭" : "闸门生效中")
        : "状态：未设置密码 —— 现在任何能连到本机的设备都能直接看画面与录像，请尽快设置";
    }

    // 自愈看门狗
    var wd = s.watchdog || {};
    sc("setWdOn", wd.enabled !== false);
    sc("setWdRepair", wd.repair !== false);
    sc("setWdDrift", wd.drift !== false);
    sv("setWdInterval", wd.interval == null ? 60 : wd.interval);
    sv("setWdStale", wd.stale_seconds == null ? 180 : wd.stale_seconds);
    sv("setWdCooldown", wd.repair_cooldown == null ? 900 : wd.repair_cooldown);
    sv("setWdMaxTries", wd.max_attempts == null ? 3 : wd.max_attempts);

    // HomeEye 侧的画面分析参数（占位，见 applyHonesty）；设备侧的开关走 /api/motion
    sv("setPreSec", mo.pre_seconds);
    sv("setPostSec", mo.post_seconds);
    sv("setMinFrames", mo.min_frames);
    sv("setMaxEvent", mo.max_event_seconds);
    sc("setDetectPersons", mo.detect_persons);

    // 通知告警
    sc("setNotifyOn", nt.enabled);
    sv("setSmtpHost", nt.smtp_host);
    sv("setSmtpPort", nt.smtp_port);
    sv("setSmtpUser", nt.smtp_user);
    var pw = $("setSmtpPass");
    if (pw) {
      pw.value = "";
      pw.placeholder = nt.smtp_pass_set ? "已设置（留空不修改）" : "留空表示不修改";
    }
    sv("setToAddrs", nt.to_addrs);
    sc("setOnMotion", nt.on_motion);
    sc("setOnFault", nt.on_fault);
    sv("setFaultCd", nt.fault_cooldown_s);
    sc("setDndOn", nt.dnd_enabled);
    sv("setDndStart", nt.dnd_start || "22:00");
    sv("setDndEnd", nt.dnd_end || "08:00");

    // 实时画面
    sv("setLiveWidth", lv.width);
    sv("setLiveFps", lv.fps);
    sv("setLiveQuality", lv.quality);
    sv("setIdleStop", lv.idle_stop);
    sv("setXmQuality", lv.default_quality || "HIGH");
    sc("setAutoplay", lv.autoplay);
    sv("setLiveTranscode", tmodeVal(lv.transcode));
    sc("setLiveAudio", lv.audio !== false);
    renderLiveVideoMode();
    renderTalkDiag();

    $("aboutVer").textContent = "version " + state.version;
  }

  /* -------------------------------- 设备侧侦测配置面（读摄像头，也写摄像头） */
  // 格子不硬编码：字段、档位、范围、分组全来自这台设备自己的 MIoT spec。
  // 以前这张卡上画的是"灵敏度 1~100 滑块"，而设备实际只有低/高两档 ——
  // 让人拖着爽、改了个设备根本不认的数，那就是骗人。
  var motionUI = { entry: null, base: {} };

  function motionCamList() {
    var out = [];
    (state.cams || []).forEach(function (c) {
      if (c.enabled !== false && c.did) out.push(c);
    });
    return out;
  }

  function motionFillSelect() {
    var sel = $("motionCam"), box = $("motionForm");
    if (!sel) return;
    var list = motionCamList(), keep = sel.value;
    sel.textContent = "";
    if (!list.length) {
      var o = el("option", "", "（列表里没有米家摄像头）");
      o.value = "";
      sel.appendChild(o);
      sel.disabled = true;
      if (box) box.innerHTML = "";
      motionUI.entry = null;
      hint("motionState", "设备配置：这一面只读写米家摄像头，当前没有可读写设备。");
      return;
    }
    sel.disabled = false;
    list.forEach(function (c) {
      var op = el("option", "", (c.name || c.id) + "（" + c.id + "）");
      op.value = String(c.id);
      sel.appendChild(op);
    });
    var ids = list.map(function (c) { return String(c.id); });
    sel.value = ids.indexOf(keep) >= 0 ? keep : ids[0];
  }

  function loadMotion(fresh) {
    var sel = $("motionCam");
    if (!sel || !sel.value) return;
    hint("motionState", "设备配置：读取中…（要走一趟米家云，最多十几秒）");
    api("api/motion?cam=" + encodeURIComponent(sel.value) + (fresh ? "&fresh=1" : ""))
      .then(function (j) {
        var e = (j.cams || [])[0];
        if (!e) { hint("motionState", "设备配置：没读到这一路。"); return; }
        renderMotion(e);
      })
      .catch(function (err) {
        motionUI.entry = null;
        var box = $("motionForm");
        if (box) box.innerHTML = "";
        hint("motionState", "设备配置：读取失败 —— " + err.message);
      });
  }

  // 输入框里的显示形态：改动检测只比这个，下发的形态再换算成设备要的
  function displayOf(inp, sch) {
    if (sch.kind === "bool") return inp.checked ? "1" : "0";
    return String(inp.value == null ? "" : inp.value);
  }
  function deviceOf(inp, sch) {
    if (sch.kind === "bool") return inp.checked;
    if (sch.kind === "choice" || sch.kind === "int") {
      var n = parseInt(inp.value, 10);
      return isNaN(n) ? inp.value : n;
    }
    return inp.value;
  }
  function displayOfValue(sch, val) {
    if (val == null) return "";
    if (sch.kind === "bool") return val ? "1" : "0";
    if (sch.kind === "time") return String(val).slice(0, 5);
    return String(val);
  }

  function motionField(key, sch, val, text, err, offline) {
    var f = el("div", "field");
    var label = sch.label + (sch.unit ? "（" + sch.unit + "）" : "");
    if (!sch.writable) {
      // 设备只上报、改不了：给一行读数。放个填了也没用的格子才是骗人。
      f.appendChild(el("label", "", label));
      var shown = val == null ? "" : String(val) + (sch.unit ? " " + sch.unit : "");
      if (text) shown = text + (shown ? " ｜ " + shown : "");
      f.appendChild(el("div", "readout", shown || "读不到"));
      if (err) f.appendChild(el("p", "hint h-bad", err));
      if (sch.hint) f.appendChild(el("p", "hint", sch.hint));
      return f;
    }
    var input;
    if (sch.kind === "bool") {
      var lab = el("label", "check");
      input = el("input");
      input.type = "checkbox";
      input.checked = val === true;
      lab.appendChild(input);
      lab.appendChild(el("span", "", label));
      f.appendChild(lab);
    } else {
      f.appendChild(el("label", "", label));
      if (sch.kind === "choice") {
        input = el("select");
        (sch.options || []).forEach(function (o) {
          var op = el("option", "", o.t);
          op.value = String(o.v);
          input.appendChild(op);
        });
      } else if (sch.kind === "int") {
        input = el("input");
        input.type = "number";
        input.step = "1";
        if (sch.range && sch.range.length === 2) {
          input.min = sch.range[0];
          input.max = sch.range[1];
        }
      } else {
        input = el("input");
        input.type = "text";
        input.placeholder = "HH:MM";
        input.spellcheck = false;
      }
      input.value = (sch.kind === "choice" && (val == null)) ? "" : displayOfValue(sch, val);
      f.appendChild(input);
    }
    input.id = "mIn_" + key;
    input.disabled = !!offline;
    motionUI.base[key] = displayOf(input, sch);
    if (sch.hint) f.appendChild(el("p", "hint", sch.hint));
    if (err) f.appendChild(el("p", "hint h-bad", err));
    return f;
  }

  function renderMotion(entry) {
    var box = $("motionForm");
    if (!box) return;
    box.innerHTML = "";
    motionUI.entry = entry;
    motionUI.base = {};
    var vals = entry.values || {}, errs = entry.errors || {};
    var offline = entry.online === false;
    var line = (entry.online === true ? "在线" : offline ? "离线" : "在线状态未知") +
      " ｜ 本页读取于 " + hhmmss(Date.now() / 1000);
    if (entry.error) line += " ｜ " + entry.error;
    hint("motionState", "设备配置（" + (entry.name || entry.cam) + "）：" + line);

    var secs = entry.sections || [];
    if (!secs.length) {
      box.appendChild(el("p", "hint", entry.error || "这台设备没报回可显示的配置项。"));
      return;
    }
    secs.forEach(function (sec) {
      box.appendChild(el("div", "set-sec", sec.title));
      sec.keys.forEach(function (key) {
        var sch = (entry.keys || {})[key];
        if (!sch) return;
        box.appendChild(motionField(key, sch, vals[key], vals[key + "_text"],
                                    errs[key], offline));
      });
    });
    var sd = entry.sd || {};
    if (sd.note) box.appendChild(el("p", "hint" + (sd.blocking ? " h-bad" : ""), sd.note));
  }

  function saveMotion() {
    var entry = motionUI.entry;
    if (!entry || !entry.cam) { toast("还没读到设备配置，先点「重新读取设备」", "err"); return; }
    var changes = {};
    Object.keys(motionUI.base).forEach(function (key) {
      var sch = (entry.keys || {})[key];
      var inp = $("mIn_" + key);
      if (!sch || !inp) return;
      if (displayOf(inp, sch) === motionUI.base[key]) return;
      changes[key] = deviceOf(inp, sch);
    });
    if (!Object.keys(changes).length) { toast("没有改动需要下发"); return; }
    var btn = $("btnMotionSave");
    var old = btn.textContent;
    btn.disabled = true;
    btn.textContent = "下发中…";
    api("api/motion", { method: "POST", body: { cam: entry.cam, changes: changes } })
      .then(function (j) {
        var r = j.result || {};
        var bad = r.errors || [], warn = r.notes || [];
        if (bad.length) toast("有格子没改成：" + bad.join("；"), "err");
        else if (warn.length) toast(warn.join("；"), "err");
        else toast("已下发并回读校验（" + (r.applied || []).length + " 项生效）");
        if (r.state) renderMotion(r.state); else loadMotion(true);
      })
      .catch(function (e) {
        toast("下发失败：" + e.message, "err");
        loadMotion(true);
      })
      .then(function () { btn.disabled = false; btn.textContent = old; });
  }

  (function bindMotion() {
    var sel = $("motionCam");
    if (!sel) return;
    sel.addEventListener("change", function () { loadMotion(false); });
    $("btnMotionReload").addEventListener("click", function () { loadMotion(true); });
    $("btnMotionSave").addEventListener("click", saveMotion);
  })();

  // 测试发送邮件（先用当前表单值保存一次，再发，避免测的还是旧配置）
  (function bindNotifyTest() {
    var b = $("btnNotifyTest");
    if (!b) return;
    b.addEventListener("click", function () {
      b.disabled = true;
      var old = b.textContent;
      b.textContent = "发送中…";
      // 先把表单保存，保证测试用的是页面上填的值
      api("api/settings", { method: "PUT", body: {
        notify: {
          enabled: true,
          smtp_host: gv("setSmtpHost").trim(),
          smtp_port: Math.max(1, gn("setSmtpPort", 465)),
          smtp_ssl: true,
          smtp_user: gv("setSmtpUser").trim(),
          smtp_pass: gv("setSmtpPass"),
          from_addr: gv("setSmtpUser").trim(),
          to_addrs: gv("setToAddrs").trim()
        }
      }}).then(function () {
        return api("api/notify/test", { method: "POST" });
      }).then(function (j) {
        if (j.ok) toast("测试邮件已发送，请查收", "ok");
        else toast("发送失败：" + (j.error || "未知原因"), "err");
      }).catch(function (e) {
        toast("发送失败：" + e.message, "err");
      }).then(function () {
        b.disabled = false;
        b.textContent = old;
        return refresh();
      });
    });
  })();

  // 放弃修改：用内存里最后一次拉到的设置回填表单
  (function bindReloadSettings() {
    var b = $("btnReloadSettings");
    if (!b) return;
    b.addEventListener("click", function () {
      fillSettings();
      toast("已恢复为已保存的设置", "ok");
    });
  })();

  // 一键从本机 go2rtc 配置导入米家凭据（go2rtc 用的是同一套 miss 协议）
  (function () {
    var b = $("btnPtzImport");
    if (!b) return;
    b.addEventListener("click", function () {
      b.disabled = true;
      api("api/ptz/import", { method: "POST" }).then(function (j) {
        toast(j.message || "已导入", "ok");
        return refresh();
      }).then(function () {
        fillSettings();
      }).catch(function (e) {
        toast(e.message, "err");
      }).then(function () {
        b.disabled = false;
      });
    });
  })();

  // 把小米摄像头改成走 go2rtc 取流。
  // 背景：小米摄像头的 P2P 会话上限只有 3 条，SDK 取流 + 云台侧车 + go2rtc
  // 刚好占满，任何一方重连就会互相踢掉（cs2: EOF）。改走 go2rtc 后全场只占 1 条。
  (function () {
    var b = $("btnPtzMigrate");
    if (!b) return;
    b.addEventListener("click", function () {
      b.disabled = true;
      var msg = $("ptzMigrateMsg");
      if (msg) msg.textContent = "正在迁移…";
      api("api/ptz/migrate", { method: "POST" }).then(function (j) {
        var lines = [];
        (j.changed || []).forEach(function (c) {
          lines.push(c.name + " → " + c.url);
        });
        (j.skipped || []).forEach(function (s) {
          lines.push("跳过 " + s.id + "：" + s.reason);
        });
        if (msg) msg.textContent = lines.join("；");
        var n = (j.changed || []).length;
        toast(j.message || "迁移完成", n ? "ok" : "err");
        return refresh();
      }).then(function () {
        fillSettings();
      }).catch(function (e) {
        if (msg) msg.textContent = "";
        toast(e.message, "err");
      }).then(function () {
        b.disabled = false;
      });
    });
  })();

  (function () {
    var b = $("btnTalkBeep");
    if (!b) return;
    b.addEventListener("click", function () {
      // 按 did 判定（迁移到 go2rtc 后 type 会变成 rtsp，但 did 还在）
      var cams = (state.cams || []).filter(function (c) { return !!c.did; });
      if (!cams.length) { toast("没有小米摄像头", "err"); return; }
      b.disabled = true;
      api("api/talk/beep", { method: "POST", body: { cam: cams[0].id } })
        .then(function (j) {
          toast("正在播放提示音（" + ((j.result || {}).seconds || 3) + " 秒），留意摄像头那边", "ok");
        })
        .catch(function (e) { toast(e.message, "err"); })
        .then(function () { b.disabled = false; });
    });
  })();

  $("btnSaveSettings").addEventListener("click", function () {
    var body = {
      storage: {
        dir: gv("setDir").trim(),
        record_mode: gv("setRecMode") || "continuous",
        segment_minutes: Math.max(1, gn("setSegLen", 5)),
        retention_days: Math.max(0, gn("setRetention", 15)),
        budget_gb: Math.max(0, gf("setBudget", 0)),
        record_audio: gc("setRecAudio"),
        source: gv("setRecSource") || "PLAYED"
      },
      live: {
        width: Math.max(0, gn("setLiveWidth", 1280)),
        fps: Math.max(0, gn("setLiveFps", 10)),
        quality: Math.min(31, Math.max(1, gn("setLiveQuality", 23))),
        transcode: gv("setLiveTranscode") || "auto",
        idle_stop: Math.max(3, gn("setIdleStop", 12)),
        default_quality: gv("setXmQuality") || "HIGH",
        autoplay: gc("setAutoplay"),
        audio: gc("setLiveAudio")
      },
      // HomeEye 侧的画面分析参数：整组没有消费者（后端 meta 会说明为什么灰着）。
      // 提交只是为了不改时别丢原值；真正生效的侦测开关在 /api/motion 那面改摄像头。
      motion: {
        detect_persons: gc("setDetectPersons"),
        pre_seconds: Math.max(0, gn("setPreSec", 3)),
        post_seconds: Math.max(0, gn("setPostSec", 10)),
        min_frames: Math.max(1, gn("setMinFrames", 3)),
        max_event_seconds: Math.max(10, gn("setMaxEvent", 120))
      },
      notify: {
        enabled: gc("setNotifyOn"),
        smtp_host: gv("setSmtpHost").trim(),
        smtp_port: Math.max(1, gn("setSmtpPort", 465)),
        smtp_ssl: true,
        smtp_user: gv("setSmtpUser").trim(),
        smtp_pass: gv("setSmtpPass"),
        from_addr: gv("setSmtpUser").trim(),
        to_addrs: gv("setToAddrs").trim(),
        on_motion: gc("setOnMotion"),
        on_fault: gc("setOnFault"),
        fault_cooldown_s: Math.max(30, gn("setFaultCd", 300)),
        dnd_enabled: gc("setDndOn"),
        dnd_start: gv("setDndStart") || "22:00",
        dnd_end: gv("setDndEnd") || "08:00"
      },
      ptz: {
        enabled: gc("setPtzOn"),
        mode: gv("setPtzMode") || "auto",
        g2r_addr: gv("setPtzG2r").trim(),
        g2r_user: gv("setPtzG2rUser").trim(),
        g2r_pass: gv("setPtzG2rPass"),
        user: gv("setPtzUser").trim(),
        token: gv("setPtzToken").trim(),
        step_seconds: Math.min(2, Math.max(0.1, gf("setPtzStep", 0.45))),
        idle_seconds: Math.min(600, Math.max(10, gf("setPtzIdle", 45))),
        media_audio: gc("setPtzAudio")
      },
      auth: {
        enabled: gc("setAuthOn"),
        username: gv("setAuthUser").trim() || "admin",
        session_hours: Math.min(720, Math.max(1, gn("setAuthHours", 24)))
      },
      watchdog: {
        enabled: gc("setWdOn"),
        repair: gc("setWdRepair"),
        drift: gc("setWdDrift"),
        interval: Math.max(15, gn("setWdInterval", 60)),
        stale_seconds: Math.max(90, gn("setWdStale", 180)),
        repair_cooldown: Math.max(60, gn("setWdCooldown", 900)),
        max_attempts: Math.min(10, Math.max(1, gn("setWdMaxTries", 3)))
      }
    };
    // 密码留空 = 不修改，后端也按这个语义处理
    if (!body.notify.smtp_pass) delete body.notify.smtp_pass;
    if (!body.ptz.token) delete body.ptz.token;
    if (!body.ptz.g2r_pass) delete body.ptz.g2r_pass;

    api("api/settings", { method: "PUT", body: body }).then(function (j) {
      // notes 是后端钳过的清单：改了什么要当场说，不能让用户下次打开
      // 看到别的数字，再去猜是不是程序偷偷动了他的设置
      var notes = (j && j.notes) || [];
      toast(notes.length
        ? ("设置已保存（" + notes.length + " 项和填的不一样，已按说明调整：" + notes.join("；") + "）")
        : "设置已保存", notes.length ? "warn" : "ok");
      return refresh().then(function () { fillSettings(); });
    }).catch(function (e) { toast(e.message, "err"); });
  });

  /* ------------------------------------------------------------ 摄像头弹窗 */
  var editing = null;
  var camType = "rtsp";

  // 每种类型的说明与字段可见性：选哪个类型就只露出该类型需要的字段
  var TYPE_HINT = {
    xiaomi: "小米摄像头走米家官方 Miloco 协议（P2P），无需摄像头开 RTSP；扫码授权后从账号里勾选要接入的设备即可。",
    rtsp: "通用 RTSP：乔安 / 海康 / 大华 / TP-Link 等。需先在摄像头后台开启 RTSP，再把完整地址填进来。",
    onvif: "ONVIF：自动扫描局域网内支持 ONVIF 的设备，选中后自动带出 RTSP 地址，只需填一次账号密码。",
    url: "其他流地址：HTTP-FLV / HLS / RTMP / 本地文件等任意 ffmpeg 能解的地址。"
  };

  function showCamStep(step) {
    var isType = (step === "type");
    var t = $("camStepType"), f = $("camStepForm"), b = $("btnCamBack");
    if (t) t.hidden = !isType;
    if (f) f.hidden = isType;
    if (b) b.hidden = isType;
    // 第一步还没选定类型，「测试连接 / 保存」都没有意义，先藏起来
    var tb = $("btnTestCam"), sb = $("btnCamSave");
    if (tb) tb.hidden = isType || camType === "xiaomi";   // 小米走 P2P，ffprobe 测不了
    if (sb) sb.hidden = isType;
  }

  function applyCamType(t) {
    camType = t;
    var h = $("camTypeHint");
    if (h) h.textContent = TYPE_HINT[t] || "";

    var isXiaomi = (t === "xiaomi");
    var isRtsp = (t === "rtsp");
    var isOnvif = (t === "onvif");
    var isUrl = (t === "url");

    function vis(id, on) { var n = $(id); if (n) n.hidden = !on; }
    vis("fieldUrl", isRtsp || isOnvif || isUrl);
    vis("fieldUser", isRtsp || isOnvif);
    vis("fieldPass", isRtsp || isOnvif);
    vis("fieldDid", isXiaomi);
    vis("fieldQuality", isXiaomi);
    vis("fieldOnvif", isOnvif);
    vis("rowTransport", isRtsp || isOnvif);

    var inp = $("camUrl");
    if (inp) {
      inp.placeholder = isRtsp
        ? "rtsp://admin:密码@192.168.1.64:554/Streaming/Channels/101"
        : (isOnvif ? "点「扫描局域网」自动填充，也可手工填写"
                   : "http://192.168.1.10:8080/live.flv 或 /mnt/video.mp4");
    }
    // 小米靠 P2P 取流，不能用 ffprobe 探测
    var tb = $("btnTestCam");
    if (tb) tb.hidden = isXiaomi;
  }

  function openCamModal(cam) {
    editing = cam || null;
    $("camModalTitle").textContent = cam ? "编辑摄像头" : "添加摄像头";
    var t = cam ? ((cam.type || "rtsp").toLowerCase()) : "rtsp";
    if (TYPE_HINT[t] === undefined) t = "rtsp";
    applyCamType(t);
    $("camName").value = cam ? (cam.name || "") : "";
    $("camUrl").value = cam ? (cam.url || "") : "";
    $("camDid").value = cam ? (cam.did || "") : "";
    $("camTransport").value = cam ? (cam.transport || "tcp") : "tcp";
    $("camQuality").value = cam ? ((cam.quality || "HIGH").toUpperCase()) : "HIGH";
    $("camRecord").checked = cam ? !!cam.record : true;
    $("camEnabled").checked = cam ? !!cam.enabled : true;
    hint("camTestHint", "填好后可以先点「测试连接」确认能出流。");
    $("camModal").hidden = false;
    // 编辑摄像头直接进表单；新增则先进类型选择
    showCamStep(cam ? "form" : "type");
    if (cam) setTimeout(function () { $("camName").focus(); }, 40);
  }
  function closeCamModal() { $("camModal").hidden = true; editing = null; }

  ["btnAddCam", "btnAddCam2", "btnAddCamSet"].forEach(function (id) {
    var n = $(id);
    if (n) n.addEventListener("click", function () { openCamModal(null); });
  });

  // 第 1 步：点类型卡片 -> 进第 2 步（小米例外，必须扫码授权）
  (function bindTypeGrid() {
    var grid = $("camTypeGrid");
    if (!grid) return;
    grid.addEventListener("click", function (e) {
      var card = e.target;
      while (card && card !== grid && !card.classList.contains("type-card")) {
        card = card.parentNode;
      }
      if (!card || card === grid) return;
      var t = card.getAttribute("data-ctype") || "rtsp";
      if (t === "xiaomi") {
        $("camModal").hidden = true;
        openXmModal();
        return;
      }
      applyCamType(t);
      showCamStep("form");
      setTimeout(function () { $("camName").focus(); }, 40);
    });
  })();

  (function bindCamBack() {
    var b = $("btnCamBack");
    if (b) b.addEventListener("click", function () { showCamStep("type"); });
  })();

  // ONVIF：扫描局域网设备
  (function bindOnvifScan() {
    var b = $("btnOnvifScan");
    if (!b) return;
    b.addEventListener("click", function () {
      var sel = $("camOnvif");
      if (!sel) return;
      sel.innerHTML = "";
      var opt = document.createElement("option");
      opt.value = "";
      opt.textContent = "扫描中…";
      sel.appendChild(opt);
      b.disabled = true;
      api("api/onvif/scan", { method: "POST" })
        .then(function (j) {
          var devs = j.devices || [];
          sel.innerHTML = "";
          if (!devs.length) {
            var o = document.createElement("option");
            o.value = ""; o.textContent = "（没扫到设备）";
            sel.appendChild(o);
            toast("没扫到 ONVIF 设备", "err");
            return;
          }
          devs.forEach(function (d) {
            var o = document.createElement("option");
            o.value = d.url || "";
            o.textContent = (d.name || d.host) + "  " + (d.url || "");
            o.dataset.host = d.host || "";
            sel.appendChild(o);
          });
          toast("扫到 " + devs.length + " 台设备", "ok");
        })
        .catch(function (e) { toast("扫描失败：" + e.message, "err"); })
        .then(function () { b.disabled = false; });
    });
  })();

  // 选中 ONVIF 设备后自动把 RTSP 地址带进地址框
  (function bindOnvifPick() {
    var sel = $("camOnvif");
    if (!sel) return;
    sel.addEventListener("change", function () {
      var inp = $("camUrl");
      if (inp) inp.value = sel.value || "";
    });
  })();
  $("camModalClose").addEventListener("click", closeCamModal);
  $("btnCamCancel").addEventListener("click", closeCamModal);
  $("camModal").addEventListener("click", function (e) {
    if (e.target === this) closeCamModal();
  });

  $("btnTestCam").addEventListener("click", function () {
    var camUrl = $("camUrl").value.trim();
    if (!camUrl) { toast("先填流地址", "err"); return; }
    if (!editing) {
      toast("请先保存摄像头，再测试连接", "err");
      return;
    }
    hint("camTestHint", "测试中（最多 12 秒）…");
    api("api/cameras/" + editing.id + "/test", { method: "POST" })
      .then(function (j) {
        var i = j.info || {};
        hint("camTestHint", "连接成功：" + (i.codec || "?") + " " +
          (i.width || "?") + "×" + (i.height || "?") + " @" + (i.fps || "?") + "fps");
        toast("连接成功", "ok");
      }).catch(function (e) {
        hint("camTestHint", "失败：" + e.message);
        toast("测试失败", "err");
      });
  });

  $("btnCamSave").addEventListener("click", function () {
    var body = {
      name: $("camName").value.trim() || "未命名",
      type: camType,
      transport: $("camTransport").value,
      record: $("camRecord").checked,
      enabled: $("camEnabled").checked
    };
    if (camType === "xiaomi") {
      body.did = $("camDid").value.trim();
      body.channel = 0;
      body.quality = $("camQuality").value;
      if (!body.did) { toast("请填写小米摄像头的 DID", "err"); return; }
    } else {
      var raw = $("camUrl").value.trim();
      var u = $("camUser").value.trim();
      var p = $("camPass").value;
      // 地址里没带账号密码、但单独填了的话，自动拼进 URL
      if ((u || p) && raw.indexOf("@") < 0) {
        var m = raw.match(/^([a-zA-Z0-9+.-]+:\/\/)(.*)$/);
        if (m) {
          raw = m[1] + encodeURIComponent(u) + ":" + encodeURIComponent(p) + "@" + m[2];
        }
      }
      body.url = raw;
      if (!body.url) { toast("流地址不能为空", "err"); return; }
    }
    if (camType === "onvif") {
      var sel = $("camOnvif");
      var opt = sel && sel.options[sel.selectedIndex];
      if (opt && opt.dataset) {
        body.onvif_host = opt.dataset.host || "";
      }
    }
    var p = editing
      ? api("api/cameras/" + editing.id, { method: "PUT", body: body })
      : api("api/cameras", { method: "POST", body: body });
    p.then(function () {
      closeCamModal();
      toast("已保存", "ok");
      return refresh();
    }).catch(function (e) { toast(e.message, "err"); });
  });

  function delCam(cam) {
    if (!confirm("删除摄像头「" + (cam.name || cam.id) + "」？\n录像文件不会被删除。")) return;
    api("api/cameras/" + cam.id, { method: "DELETE" }).then(function () {
      toast("已删除", "ok");
      return refresh();
    }).catch(function (e) { toast(e.message, "err"); });
  }

  function openSnapshot(cam) {
    window.open(url("api/cameras/" + cam.id + "/snapshot?t=" + Date.now()), "_blank");
  }

  /* ------------------------------------------------------------ 登录闸门 */
  // 后端在没有会话时对所有 /api、/live、/rec 返回 401。轮询会反复撞上，
  // 所以这里用一把门闩：弹窗只显示一次，登录成功后自动恢复。
  var authNeeded = false;

  function needAuth(info) {
    if (authNeeded) return;
    authNeeded = true;
    showLogin(info);
  }

  function showLogin(info) {
    info = info || {};
    $("loginModal").hidden = false;
    $("loginError").hidden = true;
    // 没设过密码 = 首次认领，登录框变成"设置密码"，一次点击就把这台锁上
    if (info.configured === false) {
      $("loginTitle").textContent = "设置 HomeEye 登录密码";
      $("loginHint").textContent =
        "还没有设置密码。这里填一次即成为本机管理员账号 —— 服务监听在局域网，" +
        "不设密码的话任何能连到 8088 的设备都可以看画面、拖录像。";
      $("btnLogin").textContent = "设置并登录";
    } else {
      $("loginTitle").textContent = "HomeEye 登录";
      $("loginHint").textContent = "这台 HomeEye 已开启登录闸门。";
      $("btnLogin").textContent = "登录";
    }
    var u = $("loginUser");
    if (info.username) u.value = info.username;
    setTimeout(function () { (u.value ? $("loginPass") : u).focus(); }, 60);
  }

  function afterLogin() {
    authNeeded = false;
    $("loginModal").hidden = true;
    $("loginPass").value = "";
    $("loginError").hidden = true;
    state.tileSig = "";
    refresh().then(function () { if (state.view === "live") mountTiles(); });
  }

  function loginFail(msg) {
    var e = $("loginError");
    e.textContent = msg;
    e.hidden = false;
  }

  $("btnLogin").addEventListener("click", function () {
    var btn = this;
    var first = gv("loginUser") && $("loginTitle").textContent.indexOf("设置") >= 0;
    btn.disabled = true;
    var pass = $("loginPass").value;
    var job;
    if (first) {
      job = api("api/password", { method: "POST", body: { password: pass } })
        .then(function () { toast("密码已设置", "ok"); });
    } else {
      job = api("api/login", { method: "POST",
                               body: { username: gv("loginUser"), password: pass } });
    }
    job.then(afterLogin).catch(function (e) {
      loginFail(e.needAuth ? "密码至少 6 位" : e.message);
    }).then(function () { btn.disabled = false; });
  });

  // 回车提交
  ["loginUser", "loginPass"].forEach(function (id) {
    var n = $(id);
    if (!n) return;
    n.addEventListener("keydown", function (ev) {
      if (ev.key === "Enter") $("btnLogin").click();
    });
  });

  function renderHealth(h) {
    var box = $("healthBox"), sum = $("healthSummary");
    if (!box || !sum) return;
    var cn = { ok: "正常", offline: "离线（多半断电）", broken: "链路异常", idle: "空闲" };
    var cams = h.cams || {};
    var ids = Object.keys(cams).sort();
    box.textContent = "";
    ids.forEach(function (id) {
      var d = cams[id] || {};
      var row = el("div", "health-row state-" + (d.state || "idle"));
      row.appendChild(el("span", "h-name", d.name || id));
      row.appendChild(el("span", "badge state-" + (d.state || "idle"),
                         cn[d.state] || d.state || "—"));
      var bits = [];
      if (d.reason) bits.push(d.reason);
      if (d.cam_ip) bits.push("设备 " + d.cam_ip);
      if (d.bytes_rate != null) bits.push("码率 " + fmtBytes(d.bytes_rate) + "/s");
      if (d.segment_age != null) bits.push("最近落盘 " + Math.round(d.segment_age / 60) + " 分钟前");
      if (!d.recording) bits.push("未参与录像");
      row.appendChild(el("span", "h-detail", bits.join(" · ")));
      box.appendChild(row);
    });
    if (!ids.length) box.textContent = "还没有摄像头。";
    // 运行环境自检（后端只在登录态返回）：装完出问题时，用户能直接看到
    // 是缺 ffmpeg、Python 太旧，还是小米 SDK 的 Python 依赖没装
    var evn = h.env;
    if (evn) {
      var ff = evn.ffmpeg || {};
      var xi = evn.xiaomi || {};
      var envRow = el("div", "health-row state-idle");
      envRow.appendChild(el("span", "h-name", "运行环境"));
      envRow.appendChild(el("span", "badge state-idle", "自检"));
      envRow.appendChild(el("span", "h-detail",
        "ffmpeg " + (ff.ok ? (ff.version || "已找到") : "缺失") +
        " · Python " + (evn.python || "?") +
        " · 小米 SDK " + (xi.ok ? "就绪" : "不可用")));
      box.appendChild(envRow);
      (evn.warnings || []).forEach(function (w) {
        var wr = el("div", "health-row state-broken");
        wr.appendChild(el("span", "h-name", "缺少依赖"));
        wr.appendChild(el("span", "badge state-broken", "注意"));
        wr.appendChild(el("span", "h-detail", w));
        box.appendChild(wr);
      });
      (evn.notes || []).forEach(function (n) {
        var nr = el("div", "health-row state-idle");
        nr.appendChild(el("span", "h-name", "说明"));
        nr.appendChild(el("span", "badge state-idle", "可选"));
        nr.appendChild(el("span", "h-detail", n));
        box.appendChild(nr);
      });
    }
    var st = h.status || "ok";
    sum.textContent = "总体：" + (st === "ok" ? "链路正常"
      : st === "degraded" ? "有设备离线（不告警，等它自己回来）"
      : "有链路故障，需要处理") +
      " · 正常 " + ((h.counts || {}).ok || 0) +
      " / 离线 " + ((h.counts || {}).offline || 0) +
      " / 异常 " + ((h.counts || {}).broken || 0) +
      (h.at ? " · 判定于 " + h.at : "");
    var ev = $("healthEvents");
    if (ev) {
      var list = h.events || [];
      ev.textContent = "";
      if (!list.length) ev.appendChild(el("div", "", "暂无状态变化"));
      list.slice(0, 20).forEach(function (x) {
        var tail = x.reason ? ("（" + x.reason + "）") : "";
        ev.appendChild(el("div", "",
          x.at + "  " + (x.name || x.cam) + "：" +
          (cn[x.from] || x.from) + " → " + (cn[x.to] || x.to) + tail));
      });
    }
    var chip = $("chipHealth");
    if (chip) {
      var txt = chip.querySelector("span");
      chip.className = "chip dot-chip" + (st === "ok" ? " on" : st === "broken" ? " err" : "");
      txt.textContent = st === "ok" ? "链路正常"
        : st === "degraded" ? "离线 " + ((h.counts || {}).offline || 0)
        : "链路异常";
    }
  }

  function loadHealth() {
    return api("api/health").then(function (j) {
      renderHealth(j.health || {});
    }).catch(function () { /* 未登录/离线时不打扰 */ });
  }

  $("btnHealthRefresh").addEventListener("click", function () {
    this.disabled = true;
    var old = this.textContent;
    this.textContent = "判定中…";
    api("api/health?fresh=1").then(function (j) {
      renderHealth(j.health || {});
      toast("已重新判定", "ok");
    }).catch(function (e) { toast(e.message, "err"); })
      .then(function () {
        $("btnHealthRefresh").disabled = false;
        $("btnHealthRefresh").textContent = old;
      });
  });

  // ---- 看门狗：心跳循环 + 自愈流水 ----
  function renderWatchdog(w) {
    var sum = $("watchdogSummary"), stb = $("watchdogState"), act = $("watchdogActions");
    if (!sum || !w) return;
    var h = w.hearts || {}, s = w.stats || {};
    var ago = function (v) { return v == null ? "—" : Math.round(v) + " 秒前"; };
    var beat = w.beat_age, lim = (w.interval || 60) * 3;
    var alive = beat != null && beat <= lim;
    sum.textContent = !w.enabled ? "看门狗已关闭：不会再自己补线程、纠源或重建会话。"
      : (alive ? "在转圈（上次 " + ago(beat) + "，每 " + Math.round(w.interval || 60) + " 秒一轮）"
               : "已经 " + ago(beat) + " 没转了 —— 看门狗自己卡住，重启容器才会恢复");
    stb.textContent = "巡检 " + (s.supervise || 0) + " 轮 · 补线程 " + (s.restart_threads || 0) +
      " 次 · 修复 " + (s.repairs || 0) + " 次（纠源 " + (s.drift_fixed || 0) +
      "）· 写坏回退 " + (s.reverted || 0) + " 次" +
      " ｜ 心跳：巡检 " + ago(h.health_age) + " · 录像 " + ago(h.record_watch_age) +
      " · 清理 " + ago(h.record_clean_age);
    var left = w.cooldown_left || {}, stuck = w.stuck || {};
    var extra = [];
    Object.keys(left).forEach(function (k) { extra.push(k + " 冷却中 " + left[k] + " 秒"); });
    Object.keys(stuck).forEach(function (k) { extra.push(k + " 已试 " + stuck[k] + " 次"); });
    if (extra.length) stb.textContent += " ｜ " + extra.join(" · ");
    if (act) {
      var list = w.actions || [];
      act.textContent = "";
      if (!list.length) act.appendChild(el("div", "", "暂无自愈动作"));
      list.forEach(function (x) {
        act.appendChild(el("div", x.ok ? "" : "h-bad",
          x.at + "  [" + x.kind + "] " + (x.cam || "-") + "：" + x.detail));
      });
    }
  }

  function loadWatchdog() {
    return api("api/watchdog").then(function (j) { renderWatchdog(j.watchdog || {}); })
      .catch(function () { /* 未登录/离线时不打扰 */ });
  }

  $("btnWatchdogNow").addEventListener("click", function () {
    var b = this;
    b.disabled = true;
    api("api/watchdog").then(function (j) { renderWatchdog(j.watchdog || {}); toast("已刷新看门狗状态", "ok"); })
      .catch(function (e) { toast(e.message, "err"); })
      .then(function () { b.disabled = false; });
  });

  $("btnWatchdogRepair").addEventListener("click", function () {
    var b = this, old = b.textContent;
    b.disabled = true;
    b.textContent = "修复中…";
    api("api/watchdog", { method: "POST" }).then(function (j) {
      renderWatchdog(j.watchdog || {});
      var rs = j.results || [];
      var did = rs.filter(function (x) { return x.action && x.action !== "none"; });
      toast(did.length ? ("已动手：" + did.map(function (x) { return x.cam + " " + x.action; }).join("，"))
                      : ("没有需要动手的（在流的会话不会被拆，离线的不算故障）"), "ok");
    }).catch(function (e) { toast(e.message, "err"); })
      .then(function () { b.disabled = false; b.textContent = old; });
  });

  $("btnSetPassword").addEventListener("click", function () {
    var nwv = gv("setPassNew");
    if (!nwv) { toast("请先输入新密码", "err"); return; }
    api("api/password", { method: "POST",
                          body: { password: nwv, old_password: gv("setPassOld") } })
      .then(function () {
        $("setPassNew").value = "";
        $("setPassOld").value = "";
        toast("密码已更新，其他设备需要重新登录", "ok");
        return refresh();
      }).catch(function (e) {
        toast(e.needAuth ? "需要登录后才能改密码" : e.message, "err");
      });
  });

  $("btnLogout").addEventListener("click", function () {
    api("api/logout", { method: "POST" }).then(function () {
      authNeeded = false;
      state.tileSig = "";
      unmountTiles();
      needAuth({ configured: true });
    }).catch(function (e) { toast(e.message, "err"); });
  });

  /* ------------------------------------------------------------ 刷新 */
  function refresh() {
    return api("api/state").then(function (j) {
      state.cams = j.cameras || [];
      state.settings = j.settings || null;
      state.storage = j.storage || null;
      state.version = j.version || "";
      state.ptz = j.ptz || { available: false, reason: "" };
      var recN = state.cams.filter(function (c) { return c.rec && c.rec.running; }).length;
      var chip = $("chipRec");
      var txt = chip.querySelector("span");
      if (recN > 0) {
        chip.className = "chip dot-chip on";
        txt.textContent = "录像中 " + recN;
      } else if (state.cams.length) {
        chip.className = "chip dot-chip err";
        txt.textContent = "未在录像";
      } else {
        chip.className = "chip dot-chip";
        txt.textContent = "待机";
      }
      $("chipStorage").textContent = "占用 " + fmtBytes((state.storage || {}).total_bytes || 0);
      if (state.view === "live") {
        /* 只有"画面构成"真的变了才重建 tile。
           否则 8 秒一次的轮询会把所有 MJPEG 流反复掐断重连 —— 看起来就是画面
           每隔几秒黑一下。 */
        var sig = JSON.stringify([
          state.grid, state.focusId || "", state.fit,
          state.cams.map(function (c) {
            /* 🔴 这里**不能**带 rec.running。早先带了，结果一台摄像头断电
               （录像进程不停重启）就让 running 每秒抖一次 → 签名变化 →
               整个 grid 重建 → 所有 MJPEG 流被掐断重连（画面隔几秒黑一下），
               正在进行的对讲按钮也被换掉。录像标签改成增量更新。 */
            return [c.id, c.name, c.record ? 1 : 0];
          })
        ]);
        if (sig !== state.tileSig) {
          state.tileSig = sig;
          mountTiles();
        }
        updateRecBadges();
      }
      // 注意不要在这里调 fillSettings()：8 秒一次的轮询会把用户正在输入的表单冲掉
      if (state.view === "setting") renderCams();
      loadHealth();
      loadWatchdog();   // 顶栏芯片要看链路状态，任何视图都得刷
      return j;
    }).catch(function (e) {
      // 401 已经在 api() 里弹了登录框，不必再刷日志
      if (!e.needAuth) console.warn(e);
    });
  }

  /* ------------------------------------------------------------ 小米扫码 */
  var xmPollTimer = null;

  bindXmPickControls();

  function openXmModal() {
    $("xmModal").hidden = false;
    $("xmStepAuth").hidden = false;
    $("xmStepPick").hidden = true;
    $("xmQr").removeAttribute("src");
    $("xmQrMask").hidden = true;
    $("xmHint").textContent = "正在获取二维码…";
    // 打开弹窗即自动出码，不再需要用户多点一次「生成二维码」
    startXmQr();
  }
  function closeXmModal() {
    $("xmModal").hidden = true;
    if (xmPollTimer) { clearInterval(xmPollTimer); xmPollTimer = null; }
  }

  var btnXm = $("btnAddXiaomi");
  if (btnXm) btnXm.addEventListener("click", openXmModal);
  $("xmModalClose").addEventListener("click", closeXmModal);
  $("btnXmCancel").addEventListener("click", closeXmModal);
  $("xmModal").addEventListener("click", function (e) {
    if (e.target === this) closeXmModal();
  });

  function startXmQr() {
    $("xmQrMask").hidden = false;
    $("xmQrMask").querySelector("span").textContent = "生成中…";
    $("xmHint").textContent = "正在生成二维码…";
    api("api/xiaomi/qr", { method: "POST" }).then(function (j) {
      if (!j.qr_url) { throw new Error(j.error || "生成失败"); }
      // 用后端把 qr_url 转成图片
      $("xmQr").src = url("api/xiaomi/qr_image?t=" + Date.now());
      $("xmQrMask").hidden = true;
      $("xmHint").textContent = "请用「米家」App 扫码授权";
      // 开始轮询授权状态
      if (xmPollTimer) clearInterval(xmPollTimer);
      xmPollTimer = setInterval(pollXmAuth, 3000);
    }).catch(function (e) {
      $("xmQrMask").hidden = true;
      $("xmHint").textContent = "失败：" + e.message;
      toast(e.message, "err");
    });
  }
  $("btnXmQr").addEventListener("click", startXmQr);

  function pollXmAuth() {
    api("api/xiaomi/status").then(function (j) {
      if (j.state === "authorized") {
        if (xmPollTimer) { clearInterval(xmPollTimer); xmPollTimer = null; }
        toast("授权成功", "ok");
        $("xmStepAuth").hidden = true;
        $("xmStepPick").hidden = false;
        loadXmDevices();
      } else if (j.state === "error") {
        if (xmPollTimer) { clearInterval(xmPollTimer); xmPollTimer = null; }
        $("xmHint").textContent = j.error || "登录失败";
        toast(j.error || "登录失败", "err");
      }
    }).catch(function () {});
  }

  function loadXmDevices() {
      $("xmPickHint").textContent = "加载中…";
    api("api/xiaomi/devices").then(function (j) {
      var box = $("xmDevList");
      box.innerHTML = "";
      var devs = j.devices || [];
      if (!devs.length) {
        $("xmPickHint").textContent = "账号下没有摄像头设备";
        return;
      }
      var online = devs.filter(function (d) { return d.online; }).length;
      $("xmPickHint").textContent = ("共 " + devs.length + " 台，在线 " + online + " 台");
      devs.forEach(function (d) {
        var item = el("div", "cam-item");
        var cb = document.createElement("input");
        cb.type = "checkbox";
        cb.className = "xm-cb";
        cb.checked = !!d.online;          // 默认勾上在线的
        cb.disabled = !d.online;          // 离线的不让勾
        cb.dataset.did = d.did || "";
        cb.dataset.name = d.name || d.did || "";
        cb.dataset.model = d.model || "";
        // 小米 SDK 的 channel 是 0-based 的单通道索引，单通道摄像头只能传 0
        cb.dataset.channel = "0";
        var main = el("div", "ci-main");
        var nm = el("div", "ci-name");
        nm.appendChild(el("span", null, d.name || d.did));
        if (!d.online) nm.appendChild(el("span", "tag off", "离线"));
        main.appendChild(nm);
        main.appendChild(el("div", "ci-url", d.model || ""));
        item.appendChild(cb);
        item.appendChild(main);
        item.addEventListener("click", function (e) {
          if (e.target !== cb) { cb.checked = !cb.checked; }
        });
        box.appendChild(item);
      });
    }).catch(function (e) {
      $("xmPickHint").textContent = e.message;
      toast(e.message, "err");
    });
  }

  function addXmSelected() {
    var boxes = document.querySelectorAll("#xmDevList input.xm-cb:checked:not([disabled])");
    if (!boxes.length) { toast("请先勾选要添加的摄像头", "err"); return; }
    var names = [];
    var chain = Promise.resolve();
    var list = Array.prototype.slice.call(boxes);
    list.forEach(function (cb) {
      chain = chain.then(function () {
        var payload = {
          type: "xiaomi",
          name: cb.dataset.name,
          did: cb.dataset.did,
          channel: 0,
          quality: "HIGH",
          record: true,
          enabled: true
        };
        return api("api/cameras", { method: "POST", body: payload })
          .then(function () { names.push(cb.dataset.name); });
      });
    });
    chain.then(function () {
      toast("已添加 " + names.length + " 台： " + names.join("、"), "ok");
      closeXmModal();
      return refresh();
    }).catch(function (e) { toast(e.message, "err"); });
  }

  function bindXmPickControls() {
    var btnAdd = $("btnXmAdd");
    if (btnAdd) btnAdd.addEventListener("click", addXmSelected);
    var btnAll = $("btnXmSelAll");
    if (btnAll) btnAll.addEventListener("click", function () {
      var boxes = document.querySelectorAll("#xmDevList input.xm-cb:not([disabled])");
      var allOn = true;
      Array.prototype.forEach.call(boxes, function (cb) { if (!cb.checked) allOn = false; });
      Array.prototype.forEach.call(boxes, function (cb) { cb.checked = !allOn; });
    });
  }

  function addXmCam(d) {
    api("api/cameras", { method: "POST", body: {
      type: "xiaomi", name: d.name, did: d.did, channel: 0,
      quality: "HIGH", record: true, enabled: true
    }}).then(function () {
      toast("已添加 " + d.name, "ok");
      closeXmModal();
      return refresh();
    }).catch(function (e) { toast(e.message, "err"); });
  }

  /* ---------------------------------------------------- 密码框明文开关 */
  // 一个通用装饰器而不是逐个硬编码：登录框、设置页（go2rtc 密码 / token / SMTP
  // 密码 / 改密两处）、摄像头弹窗一共 7 个密码框，漏一个就是不一致。
  var PW_EYE_SVG =
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" ' +
    'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
    '<path d="M1 12s4-7 11-7 11 7 11 7-4 7-11 7S1 12 1 12z"/>' +
    '<circle cx="12" cy="12" r="3"/>' +
    '<line class="pw-slash" x1="3" y1="21" x2="21" y2="3"/>' +
    '</svg>';

  function decoratePasswords() {
    var list = document.querySelectorAll('input[type=password]');
    Array.prototype.forEach.call(list, function (inp) {
      if (inp.parentNode.className === "pw-wrap") return;
      var wrap = document.createElement("span");
      wrap.className = "pw-wrap";
      inp.parentNode.insertBefore(wrap, inp);
      wrap.appendChild(inp);
      var btn = document.createElement("button");
      btn.type = "button";
      btn.className = "pw-eye";
      btn.setAttribute("aria-pressed", "false");
      btn.title = "显示密码";
      btn.innerHTML = PW_EYE_SVG;
      wrap.appendChild(btn);
      btn.addEventListener("click", function () {
        var shown = inp.type === "password";
        inp.type = shown ? "text" : "password";
        btn.setAttribute("aria-pressed", shown ? "true" : "false");
        btn.title = shown ? "隐藏密码" : "显示密码";
        // 改 type 会让光标弹回头部，接着打字就错位
        inp.focus();
        try { inp.setSelectionRange(inp.value.length, inp.value.length); } catch (e) {}
      });
    });
  }

  /* ---------------------------------------------------- 设置页左侧导航 */
  // 标题从卡片自己的 h3 派生，不再维护第二份清单：加一张卡片不会漏一个导航项。
  // 滚动容器是 .set-main（它 position:relative，所以 card.offsetTop 就是
  // 「滚动到这儿」的目标值，不用换算 offsetParent 链）。
  function buildSettingsNav() {
    var main = $("setMain"), nav = $("setNav");
    if (!main || !nav) return;
    var cards = Array.prototype.slice.call(main.querySelectorAll(".cards > .card"));
    nav.innerHTML = "";
    cards.forEach(function (card, i) {
      if (!card.id) card.id = "setCard" + (i + 1);
      var h = card.querySelector("h3");
      // h3 里还挂着 .card-sub 那行灰字，导航只要主标题
      var label = String((h && (h.firstChild || {}).nodeValue) ||
                         (h ? h.textContent : "") || ("设置项 " + (i + 1))).trim();
      var b = document.createElement("button");
      b.type = "button";
      b.textContent = label;
      b.title = label;
      b.addEventListener("click", function () {
        main.scrollTo({ top: card.offsetTop - 4, behavior: "smooth" });
      });
      nav.appendChild(b);
    });
    var items = Array.prototype.slice.call(nav.children);
    function mark() {
      var y = main.scrollTop + 60, best = 0;
      cards.forEach(function (card, i) { if (card.offsetTop <= y) best = i; });
      items.forEach(function (b, i) { b.classList.toggle("on", i === best); });
    }
    var raf = 0;
    main.addEventListener("scroll", function () {
      if (raf) return;
      raf = requestAnimationFrame(function () { raf = 0; mark(); });
    });
    state.markSetNav = mark;   // 视图隐藏时 offsetTop 全是 0，切进设置页要重算一次
    mark();
  }

  /* ------------------------------------------------------------ 启动 */
  state.grid = parseInt(localStorage.getItem("nw.grid") || "4", 10) || 4;
  state.fit = localStorage.getItem("nw.fit") || "contain";
  try {
    var savedPicks = JSON.parse(localStorage.getItem("nw.picks") || "[]");
    state.picks = Object.prototype.toString.call(savedPicks) === "[object Array]" ? savedPicks : [];
  } catch (e) {
    state.picks = [];
  }
  $("fitToggle").checked = state.fit !== "cover";
  Array.prototype.forEach.call(document.querySelectorAll("#gridPick button"), function (b) {
    b.classList.toggle("on", parseInt(b.getAttribute("data-n"), 10) === state.grid);
  });

  decoratePasswords();
  buildSettingsNav();
  refresh();
  setInterval(refresh, 8000);

  document.addEventListener("visibilitychange", function () {
    if (document.hidden) unmountTiles();
    else if (state.view === "live") mountTiles();
  });
})();
