/* ── the vanilla mount ──────────────────────────────────────────────
   metal-fx ships a React component; Aethron has no React. Their own
   index.ts calls the engine primitives a "power-user surface ... for
   consumers building non-React integrations", so this is the sanctioned
   path, not a workaround. Everything below is MetalFx.tsx's lifecycle
   with the hooks removed — measure, create, glow, rim, observe, destroy.
   Nothing about the effect itself is reimplemented. */
const MFX_GLOW = new Map();     // instance -> {handles, themeRef}
setGlowCallback(function (inst, nowMs) {
  const e = MFX_GLOW.get(inst);
  if (e) updateGlow(e.handles, inst, nowMs, inst.opacityMul, e.themeRef.current);
});

/* Wrap an element that is already in the document. Returns a handle with
   .root (the new wrapper), .pause(bool) and .destroy(). */
function metalWrap(el, o) {
  o = o || {};
  if (!el) return null;
  if (el.__mfx) return el.__mfx;
  if (!isMetalFxSupported()) return null;   // no WebGL2: leave it alone
  ensureStylesInjected();

  const theme = o.theme || 'dark';
  const kind = o.variant === 'circle' ? 'circle' : 'pill';
  const scale = o.scale || 1;
  const mask = o.mask || null;

  const root = document.createElement('div');
  root.className = 'metal-fx-root' + (o.className ? ' ' + o.className : '');
  root.dataset.variant = o.variant || 'button';
  root.dataset.shape = kind;
  root.dataset.theme = theme;
  root.dataset.normalize = o.normalize === false ? 'false' : 'true';
  root.style.setProperty('--mfx-strength',
    String(o.strength == null ? 1 : o.strength));
  root.style.opacity = '0'; root.style.visibility = 'hidden';

  const canvas = document.createElement('canvas');
  canvas.className = 'metal-fx-canvas';
  canvas.style.cssText = 'position:absolute;inset:0;width:100%;height:100%';
  const inner = document.createElement('div');
  inner.className = 'metal-fx-inner';
  inner.setAttribute('aria-hidden', 'true');
  inner.style.cssText = 'position:absolute;inset:3px';
  const glowHost = document.createElement('div');
  glowHost.setAttribute('aria-hidden', 'true');
  glowHost.style.cssText =
    'position:absolute;inset:0;pointer-events:none;z-index:3;border-radius:inherit';
  if (o.glow === false) glowHost.style.display = 'none';
  const rimHost = document.createElement('div');
  rimHost.setAttribute('aria-hidden', 'true');
  rimHost.style.cssText = 'position:absolute;inset:0;pointer-events:none;z-index:4';
  const content = document.createElement('div');
  content.className = 'metal-fx-content';

  el.parentNode.insertBefore(root, el);
  content.appendChild(el);
  root.append(canvas, inner, glowHost, rimHost, content);

  const themeRef = { current: theme };
  setSharedPreset(o.preset || 'chromatic', theme);

  const radiusOf = function (w, h) {
    if (kind === 'circle') return Math.min(w, h) / 2;
    let raw = o.borderRadius;
    if (raw == null) {
      const p = parseFloat(getComputedStyle(el).borderTopLeftRadius);
      raw = Number.isFinite(p) && p > 0 ? p : 20;
    }
    return Math.min(raw, Math.min(w, h) / 2);
  };
  const measure = function () {
    const r = root.getBoundingClientRect();
    const w = Math.max(1, Math.round(r.width)), h = Math.max(1, Math.round(r.height));
    return { cssWidth: w, cssHeight: h, cornerRadius: radiusOf(w, h) };
  };

  const d0 = measure();
  const inst = createInstance({
    hostCanvas: canvas, cssWidth: d0.cssWidth, cssHeight: d0.cssHeight,
    cornerRadius: d0.cornerRadius, kind, paused: !!o.paused,
    shaderScale: o.shaderScale, ringCssPx: o.ringCssPx, scale,
    opacityMul: o.strength == null ? 1 : o.strength,
    glowGain: o.glowGain == null ? 1 : o.glowGain,
    mask: mask,
    onFirstCopy: reveal,
  });
  function reveal() {
    root.style.opacity = '1'; root.style.visibility = 'visible';
    root.style.transition = 'opacity .15s ease-out';
  }
  /* THE TRAP THIS PROJECT HAS ALREADY PAID FOR TWICE: a reveal that hangs
     off a frame callback never fires under starvation, and here that
     would mean the button simply is not there. A timer backs it. */
  setTimeout(reveal, 1200);
  root.style.setProperty('--mfx-radius', d0.cornerRadius + 'px');
  root.style.borderRadius = d0.cornerRadius + 'px';

  /* A masked instance (metal-filled glyphs, a filled badge) has no ring
     band, so the halo is given points inside the mask and the mask
     itself to clip against — their glowMaskData, unchanged. */
  const glowMaskData = function (w, h) {
    if (!mask || o.glowMode === 'ring') return {};
    const dpr = window.devicePixelRatio || 1;
    const c = document.createElement('canvas');
    c.width = Math.max(1, Math.round(w * dpr));
    c.height = Math.max(1, Math.round(h * dpr));
    const g = c.getContext('2d');
    if (!g) return {};
    g.fillStyle = '#fff';
    mask(g, c.width, c.height, dpr);
    const d = g.getImageData(0, 0, c.width, c.height).data;
    const pts = [], step = Math.max(1, Math.round(2 * dpr));
    for (let y = step >> 1; y < c.height; y += step)
      for (let x = step >> 1; x < c.width; x += step)
        if (d[(y * c.width + x) * 4 + 3] > 128) pts.push({ x: x / dpr, y: y / dpr });
    return { samplePoints: pts, maskDataUrl: c.toDataURL('image/png') };
  };

  let handles = null;
  const buildGlow = function (d) {
    if (o.glow === false) return;
    const prev = handles;
    glowHost.innerHTML = '';
    handles = injectGlow(glowHost, Object.assign({
      width: d.cssWidth, height: d.cssHeight,
      cornerRadius: d.cornerRadius, kind, scale,
    }, glowMaskData(d.cssWidth, d.cssHeight)));
    // a rebuilt glow starts invisible; carry the old state so a resize
    // does not read as the halo blinking out
    if (prev) carryGlowState(prev, handles);
    MFX_GLOW.set(inst, { handles, themeRef });
  };
  buildGlow(d0);
  if (o.glow !== false) registerGlowInstance(inst);

  let rim = null;
  const buildRim = function (d) {
    removeRim(rim); rim = null;
    if (!o.innerShadow) return;
    const ro = o.innerShadow === true ? RIM_DEFAULTS
      : Object.assign({}, RIM_DEFAULTS, o.innerShadow);
    rim = injectRim(rimHost, {
      width: d.cssWidth, height: d.cssHeight, cornerRadius: d.cornerRadius,
      kind, ring: inst.ringCssPx,
    }, ro);
  };
  buildRim(d0);

  let raf = 0, bw = d0.cssWidth, bh = d0.cssHeight, br = d0.cornerRadius;
  const ro = new ResizeObserver(function () {
    if (raf) return;
    raf = requestAnimationFrame(function () {
      raf = 0;
      const n = measure();
      if (Math.abs(n.cssWidth - bw) < .5 && Math.abs(n.cssHeight - bh) < .5 &&
          Math.abs(n.cornerRadius - br) < .5) return;
      bw = n.cssWidth; bh = n.cssHeight; br = n.cornerRadius;
      updateInstance(inst, n);
      root.style.setProperty('--mfx-radius', n.cornerRadius + 'px');
      root.style.borderRadius = n.cornerRadius + 'px';
      buildGlow(n); buildRim(n);
    });
  });
  ro.observe(root);

  const unsubGlow = subscribeGlowConfig(function (markupChanged) {
    if (markupChanged) buildGlow(measure());
  });

  let io = null;
  if (typeof IntersectionObserver !== 'undefined') {
    io = new IntersectionObserver(function (es) {
      for (const e of es) setInstanceVisible(inst, e.isIntersecting);
    }, { rootMargin: '64px' });
    io.observe(root);
  }
  attachCursorLight();

  /* Neighbours catch the light. This is the part a <div> cannot fake: the
     engine reads the shader's own pixels and paints a soft copy of them
     onto whatever stands near the button. Dark mode only, by design. */
  let refl = [];
  if (o.reflect && o.reflect.length && theme === 'dark') {
    inst.onAfterFrame = scheduleReflectionPaint;
    refl = o.reflect.filter(Boolean);
    for (const t of refl) addReflectionTarget(t.el || t, inst, root, t.strength == null ? 1 : t.strength);
  }

  const h = {
    root: root, inst: inst, el: el,
    pause: function (p) { updateInstance(inst, { paused: !!p }); },
    destroy: function () {
      detachCursorLight(); removeRim(rim); rim = null;
      ro.disconnect(); if (io) io.disconnect(); unsubGlow();
      if (raf) cancelAnimationFrame(raf);
      for (const t of refl) removeReflectionTarget(t.el || t);
      MFX_GLOW.delete(inst); unregisterGlowInstance(inst); destroyInstance(inst);
      delete el.__mfx;
      if (root.parentNode) { root.parentNode.insertBefore(el, root); root.remove(); }
    },
  };
  el.__mfx = h;
  return h;
}
