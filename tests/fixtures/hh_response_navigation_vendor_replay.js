// Minimal deterministic fixture based on the observed HH vendors bundle timer path.
(() => {
  const zu = {};
  function Wu(e) {
    const t = zu[e];
    if (t) return t;
    const n = window.document;
    let r = window[e];
    if (n && "function" == typeof n.createElement) try {
      const t = n.createElement("iframe");
      t.hidden = !0;
      n.head.appendChild(t);
      const o = t.contentWindow;
      o && o[e] && (r = o[e]);
      n.head.removeChild(t);
    } catch (e) {}
    return zu[e] = r.bind(window);
  }
  class Ol {
    constructor() {
      this.nodeMap = new WeakMap();
      this.loop = !0;
      this.periodicallyClear();
    }
    periodicallyClear() {
      !function(...e) { Wu("requestAnimationFrame")(...e); }(() => {
        window.__replayTimerCallbackRan = true;
        this.clear();
        this.loop = false;
        this.loop && this.periodicallyClear();
      });
    }
    clear() { this.nodeMap = new WeakMap(); }
  }
  window.__replayTimerCallbackRan = false;
  new Ol();
})();
