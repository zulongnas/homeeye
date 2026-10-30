/* 对讲采集：AudioWorklet 版重采样器（跑在音频线程，不占主线程）。
 *
 * 为什么不用 createScriptProcessor：那个 API 已废弃，回调排在主线程的事件循环里，
 * 页面一忙（切画面、轮询回包）就丢块 —— 听起来就是对讲忽快忽慢、偶尔爆音。
 *
 * 输入是浏览器给的 Float32（44.1k/48k），输出 8kHz PCM16 小端，按块 postMessage
 * 给主线程攒够 120ms 再 POST。相位在小数部分跨块保留，块接缝才不会周期性咔哒。
 *
 * 输出留空：这个节点接了 destination（为了被驱动），
 * 把麦克风原样吐回去就是啸叫。
 */
class TalkResample extends AudioWorkletProcessor {
  constructor() {
    super();
    this.ratio = sampleRate / 8000;   // sampleRate 是音频线程全局，等价 ctx.sampleRate
    this.frac = 0;
  }

  process(inputs) {
    const ch = (inputs[0] || [])[0];
    if (!ch || !ch.length) return true;
    const n = ch.length;
    let pos = this.frac;
    const out = [];
    while (pos + 1 < n) {
      const i0 = Math.floor(pos), f = pos - i0;
      let v = ch[i0] * (1 - f) + ch[i0 + 1] * f;
      if (v > 1) v = 1; else if (v < -1) v = -1;
      out.push(v * 32767);
      pos += this.ratio;
    }
    this.frac = Math.max(0, pos - n);
    if (!out.length) return true;
    const i16 = new Int16Array(out.length);
    for (let k = 0; k < out.length; k++) i16[k] = out[k];
    const buf = i16.buffer;
    this.port.postMessage(buf, [buf]);   // 转移所有权，不复制
    return true;
  }
}

registerProcessor("homeeye-talk-resample", TalkResample);
