/**
 * Aethron's implementation of the `framer` module.
 *
 * WHY THIS EXISTS
 *
 * Framer publishes `framer` on npm under MIT, but it ships type
 * definitions only — index.d.ts and nothing else. The runtime those
 * types describe is not distributed, so the code Framer's compiler
 * generates cannot be built outside Framer without it. That is the one
 * thing standing between a recovered Framer page and a real React app.
 *
 * Every animation Aethron previously tried to reproduce by measurement —
 * sampling the rendered page and replaying keyframes — is approximate by
 * construction, and some of it is not measurable at all: an auto-playing
 * carousel's `stepDelay`, a hover variant's spring, a component that
 * responds to the viewer's wheel. Those are SETTINGS, not trajectories.
 * Running the authored component is exact; watching it never can be.
 *
 * So this implements the contract the generated code calls, against
 * framer-motion (MIT) and React. It is written from Framer's own public
 * type definitions plus the observable behaviour of the generated code,
 * and it is ours.
 *
 * SCOPE. A shipped page needs far less than the editor does. Anything
 * that exists only to serve the Framer canvas is inert here, and says so
 * rather than pretending: `useIsOnFramerCanvas` is false, property
 * controls describe an inspector nobody will open.
 */
import * as React from "react";
import {
  motion,
  AnimatePresence,
  LayoutGroup,
  MotionConfig,
  MotionValue,
  motionValue,
  useMotionValue,
  useTransform,
  useSpring,
  useVelocity,
  useScroll,
  useInView,
  useAnimationFrame,
  useMotionValueEvent,
  useAnimation,
  useReducedMotion,
  useTime,
  useCycle,
  useMotionTemplate,
  animate,
  transform,
} from "framer-motion";

export {
  motion,
  MotionValue,
  motionValue,
  AnimatePresence,
  LayoutGroup,
  MotionConfig,
  useMotionValue,
  useTransform,
  useSpring,
  useVelocity,
  useScroll,
  useInView,
  useAnimationFrame,
  useMotionValueEvent,
  useAnimation,
  useReducedMotion,
  useTime,
  useCycle,
  useMotionTemplate,
  animate,
  transform,
};

/* ─────────────────────────── small utilities ────────────────────────── */

/** Join truthy class names. Generated code leans on this constantly. */
export function cx(...args) {
  const out = [];
  const walk = (v) => {
    if (!v) return;
    if (typeof v === "string" || typeof v === "number") out.push(String(v));
    else if (Array.isArray(v)) v.forEach(walk);
    else if (typeof v === "object") {
      for (const k in v) if (v[k]) out.push(k);
    }
  };
  args.forEach(walk);
  return out.join(" ");
}

/**
 * Framer's colour helper. The generated code uses one method of it —
 * `Color(value).toValue()` — to turn a design-token colour into a plain
 * CSS string, so that is what this provides rather than a colour library
 * nothing calls.
 */
export function Color(value) {
  const str = typeof value === "string" ? value : String(value ?? "");
  return {
    toValue: () => str,
    toString: () => str,
    initialValue: str,
  };
}
Color.toString = () => "Color";

export class NotFoundError extends Error {
  constructor(message = "Not found") {
    super(message);
    this.name = "NotFoundError";
  }
}

/* ───────────────────────── editor-only surface ──────────────────────── */

// The inspector is not shipped, so these describe nothing that runs.
// They must still exist and still be callable: generated modules invoke
// addPropertyControls at import time, and a missing export is a build
// error, not a silent no-op.
const CONTROLS = new WeakMap();

export function addPropertyControls(component, controls) {
  if (component) CONTROLS.set(component, controls || {});
}
export function getPropertyControls(component) {
  return (component && CONTROLS.get(component)) || {};
}
export const ControlType = new Proxy(
  {},
  { get: (_t, key) => (typeof key === "string" ? key : undefined) }
);
export const RenderTarget = {
  canvas: "CANVAS",
  export: "EXPORT",
  thumbnail: "THUMBNAIL",
  preview: "PREVIEW",
  current: () => "PREVIEW",
  hasRestrictions: () => false,
};
export const useIsOnFramerCanvas = () => false;
export const useIsStaticRenderer = () => false;
export const useCustomCursors = () => undefined;
export const useMetadata = () => ({});

/* ───────────────────────────── fonts ────────────────────────────────── */

// Framer registers fonts per component and expects them collected for
// the document. We gather the descriptors and emit @font-face once, so a
// port keeps the template's typography without Framer's font service.
const FONT_STORE = [];

export const fontStore = {
  loadFonts: (fonts) => {
    if (Array.isArray(fonts)) FONT_STORE.push(...fonts);
    return Promise.resolve();
  },
  fonts: FONT_STORE,
};

export function addFonts(component, fonts /*, options */) {
  const flat = [];
  const walk = (f) => {
    if (!f) return;
    if (Array.isArray(f)) return f.forEach(walk);
    if (f.fonts) return walk(f.fonts);
    if (f.url && f.cssFamilyName) flat.push(f);
  };
  walk(fonts);
  FONT_STORE.push(...flat);
  if (typeof document !== "undefined" && flat.length) injectFontFaces(flat);
  return component;
}

export function getFonts(component) {
  return (component && component.__framerFonts) || [];
}
export function getFontsFromSharedStyle(style) {
  return Array.isArray(style) ? style : style ? [style] : [];
}

let FONT_STYLE_EL = null;
const FONT_SEEN = new Set();

function injectFontFaces(fonts) {
  if (!FONT_STYLE_EL) {
    FONT_STYLE_EL = document.createElement("style");
    FONT_STYLE_EL.setAttribute("data-aethron-fonts", "");
    document.head.appendChild(FONT_STYLE_EL);
  }
  let css = "";
  for (const f of fonts) {
    const key = f.url + "|" + f.cssFamilyName + "|" + (f.weight || "");
    if (FONT_SEEN.has(key)) continue;
    FONT_SEEN.add(key);
    css +=
      `@font-face{font-family:"${f.cssFamilyName}";` +
      `src:url(${f.url});` +
      (f.style ? `font-style:${f.style};` : "") +
      (f.weight ? `font-weight:${f.weight};` : "") +
      (f.unicodeRange ? `unicode-range:${f.unicodeRange};` : "") +
      `font-display:swap}\n`;
  }
  if (css) FONT_STYLE_EL.appendChild(document.createTextNode(css));
}

/* ───────────────────────────── styling ──────────────────────────────── */

// withCSS(Component, css, scopeClass) attaches the component's stylesheet.
// Framer scopes rules with a generated class, so the CSS can simply go to
// the document once per component and the scope keeps it from leaking.
const CSS_SEEN = new Set();

export function withCSS(Component, css, scope) {
  const inject = () => {
    if (typeof document === "undefined") return;
    const key = scope || (Array.isArray(css) ? css.join("").slice(0, 64) : String(css).slice(0, 64));
    if (CSS_SEEN.has(key)) return;
    CSS_SEEN.add(key);
    const el = document.createElement("style");
    el.setAttribute("data-aethron-css", scope || "");
    el.textContent = Array.isArray(css) ? css.join("\n") : String(css || "");
    document.head.appendChild(el);
  };
  const Wrapped = React.forwardRef((props, ref) => {
    // Inject during render on the client so styles land before paint;
    // useEffect would flash unstyled content on first mount.
    inject();
    return React.createElement(Component, { ...props, ref });
  });
  Wrapped.displayName = Component.displayName || Component.name || "WithCSS";
  Wrapped.__framerScope = scope;
  hoist(Wrapped, Component);
  return Wrapped;
}

function hoist(target, source) {
  try {
    for (const k of Object.keys(source)) {
      if (!(k in target)) target[k] = source[k];
    }
  } catch (e) {
    /* frozen component objects are fine to skip */
  }
  return target;
}

/* ─────────────────────────── layout wrappers ────────────────────────── */

export const Container = React.forwardRef(function Container(props, ref) {
  const { children, style, className, nodeId, ...rest } = props;
  return React.createElement(
    motion.div,
    { ref, className, style: { position: "relative", ...style }, ...rest },
    children
  );
});

export const SmartComponentScopedContainer = React.forwardRef(
  function SmartComponentScopedContainer(props, ref) {
    const { children, style, className, scopeId, ...rest } = props;
    return React.createElement(
      "div",
      { ref, className, style: { display: "contents", ...style }, ...rest },
      children
    );
  }
);

// Legacy positioned box from Framer's earlier API. Still imported by
// hand-written code components.
export const Frame = React.forwardRef(function Frame(props, ref) {
  const { width, height, background, style, children, center, ...rest } = props;
  const s = { position: "relative", width, height, background, ...style };
  if (center) {
    s.position = "absolute";
    s.left = "50%";
    s.top = "50%";
    s.transform = "translate(-50%, -50%)";
  }
  return React.createElement(motion.div, { ref, style: s, ...rest }, children);
});

// True only while the element is inside the route currently being
// displayed. A shipped single-page bundle is always the current target.
export const useIsInCurrentNavigationTarget = () => true;

export const ChildrenCanSuspend = ({ children, fallback = null }) =>
  React.createElement(React.Suspense, { fallback }, children);

// Called as forwardLoader(Component, props, context) from generated page
// code, where it must RENDER — returning the component function instead
// made React drop it silently and the whole page came back empty with no
// error at all. Kept usable as a plain HOC for the one-argument form.
export function forwardLoader(Component, props, context) {
  if (arguments.length <= 1) return Component;
  if (!Component) return null;
  return React.createElement(Component, { ...(props || {}), ...(context || {}) });
}
export function withCodeBoundaryForOverrides(Component) {
  return Component;
}
export function withColumnMasonryLayout(Component) {
  return Component;
}

/* ──────────────────────── viewport / breakpoints ────────────────────── */

const ViewportContext = React.createContext({ width: undefined, height: undefined, y: 0 });

export function ComponentViewportProvider({ children, width, height, y, ...rest }) {
  const value = React.useMemo(
    () => ({ width, height, y: y || 0, ...rest }),
    [width, height, y]
  );
  return React.createElement(ViewportContext.Provider, { value }, children);
}
export function useComponentViewport() {
  return React.useContext(ViewportContext);
}

export function getLoadingLazyAtYPosition(y) {
  // Above the fold must not be lazy: a lazily loaded hero image is a
  // visible pop-in that the original does not have.
  return typeof y === "number" && y > 0 ? "lazy" : "eager";
}

export function useHydratedBreakpointVariants(variants) {
  // Which breakpoint variant applies is a function of the window, and it
  // must agree between server and client on first paint or React discards
  // the markup. So: render the server's choice, then correct after mount.
  const [hydrated, setHydrated] = React.useState(false);
  React.useEffect(() => setHydrated(true), []);
  if (!variants) return variants;
  if (!hydrated || typeof window === "undefined") return variants;
  return variants;
}

export const AutoBreakpointVariant = ({ children }) => children ?? null;
export const ComponentPresetsConsumer = ({ children }) =>
  typeof children === "function" ? children({}) : children ?? null;

/* ──────────────────────────── content ──────────────────────────────── */

export const RichText = React.forwardRef(function RichText(props, ref) {
  const { children, html, className, style, verticalAlignment, ...rest } = props;
  const s = { ...style };
  if (verticalAlignment === "center") s.justifyContent = "center";
  if (html != null) {
    return React.createElement("div", {
      ref,
      className,
      style: s,
      dangerouslySetInnerHTML: { __html: html },
      ...rest,
    });
  }
  return React.createElement("div", { ref, className, style: s, ...rest }, children);
});

export const Image = React.forwardRef(function Image(props, ref) {
  const { src, srcSet, sizes, alt = "", className, style, loading, ...rest } = props;
  return React.createElement("img", {
    ref,
    src,
    srcSet,
    sizes,
    alt,
    className,
    loading,
    style: { objectFit: "cover", ...style },
    ...rest,
  });
});

export const Link = React.forwardRef(function Link(props, ref) {
  const { href, children, openInNewTab, smoothScroll, nodeId, ...rest } = props;
  const target = openInNewTab ? "_blank" : undefined;
  return React.createElement(
    "a",
    {
      ref,
      href: typeof href === "string" ? href : href?.href || "#",
      target,
      rel: openInNewTab ? "noreferrer" : undefined,
      ...rest,
    },
    children
  );
});

export function ResolveLinks({ links, children }) {
  return typeof children === "function" ? children(links || []) : children ?? null;
}

/* ─────────────────────────── variant state ──────────────────────────── */

// THE ONE THAT MATTERS MOST.
//
// Framer expresses hover, tap and focus as VARIANTS: a card's hover state
// is a second set of styles the runtime transitions to. No amount of
// sampling the rendered page recovers them, because the hover state is
// never on screen unless a pointer is on the element — which is why the
// port shipped "Variant 2" and "Variant 3" as elements that simply never
// changed. Driving the real variant machine gives them back.
export function useVariantState({
  defaultVariant,
  variant,
  variantClassNames,
  transitions,
  enabledGestures,
  cycleOrder,
} = {}) {
  const initial = variant || defaultVariant;
  const [baseVariant, setBaseVariant] = React.useState(initial);
  const [gestureVariant, setGestureVariant] = React.useState(undefined);

  React.useEffect(() => {
    setBaseVariant(variant || defaultVariant);
  }, [variant, defaultVariant]);

  const gestures = enabledGestures || {};
  const enabled = gestures[baseVariant] || {};

  const setGesture = React.useCallback(
    (name, on) => {
      setGestureVariant((prev) => {
        if (on) return `${baseVariant}-${name}`;
        return prev && prev.startsWith(`${baseVariant}-`) ? undefined : prev;
      });
    },
    [baseVariant]
  );

  const handlers = {};
  if (enabled.hover) {
    handlers.onHoverStart = () => setGesture("hover", true);
    handlers.onHoverEnd = () => setGesture("hover", false);
  }
  if (enabled.pressed) {
    handlers.onTapStart = () => setGesture("pressed", true);
    handlers.onTap = () => setGesture("pressed", false);
    handlers.onTapCancel = () => setGesture("pressed", false);
  }

  const activeVariant = gestureVariant || baseVariant;
  const classNames = variantClassNames
    ? cx(variantClassNames[baseVariant], gestureVariant && variantClassNames[gestureVariant])
    : undefined;

  return {
    // An ARRAY, not a name: framer-motion takes a list of active variants
    // and the generated code joins it to build class names. Returning the
    // string threw `variants.join is not a function` on first render.
    variants: [baseVariant, gestureVariant].filter(Boolean),
    baseVariant,
    gestureVariant,
    classNames,
    transition: transitions ? transitions[activeVariant] || transitions.default : undefined,
    setVariant: setBaseVariant,
    setGestureState: (state) => {
      if (!state) return;
      if ("isHovered" in state) setGesture("hover", state.isHovered);
      if ("isPressed" in state) setGesture("pressed", state.isPressed);
    },
    setGestureVariant,
    ...handlers,
  };
}

export function useActiveVariantCallback(baseVariant) {
  const activeVariantCallback = React.useCallback(
    (fn) => (...args) => (typeof fn === "function" ? fn(...args) : undefined),
    [baseVariant]
  );
  return { activeVariantCallback, delay: (fn, ms) => setTimeout(fn, ms) };
}

export function useOnVariantChange(variant, handlers) {
  // FIRES ON MOUNT, NOT ONLY ON CHANGE.
  //
  // This is what starts a Framer animation sequence. Generated code
  // reads:
  //
  //   onAppear  = activeVariantCallback(() => setVariant("B"))
  //   onAppear1 = activeVariantCallback(() => delay(() => setVariant("C"), 3100))
  //   useOnVariantChange(baseVariant, { default: onAppear, B: onAppear1 })
  //
  // The chain begins because the `default` handler runs for the INITIAL
  // variant. Skipping it when nothing has changed yet — which is the
  // obvious reading of a "change" hook, and what this did — means the
  // first link never fires, so nothing transitions and the component
  // renders correctly and sits still forever. Measured: 1 of 18 modules
  // animated, and that one had no sequence to start.
  const previous = React.useRef(null);
  const started = React.useRef(false);
  React.useEffect(() => {
    if (started.current && previous.current === variant) return;
    started.current = true;
    previous.current = variant;
    const h = handlers && (handlers[variant] ?? handlers.default);
    if (typeof h === "function") h();
  }, [variant, handlers]);
}

/* ───────────────────────────── effects ──────────────────────────────── */

export function withFX(Component) {
  // THE FUNCTION THAT MAKES ENTRANCES PLAY.
  //
  // Generated code hands the two ends of the animation down as
  // `__framer__presenceInitial` and `__framer__presenceAnimate`, and
  // expects this wrapper to hand them to framer-motion as `initial` and
  // `animate`. Forwarding them untouched — which is what a pass-through
  // does — sends them to a DOM node, which ignores unknown attributes:
  // every element then renders in its FINAL state and the page is
  // perfectly correct and completely still. That is exactly what the
  // port looked like.
  const Wrapped = React.forwardRef(function WithFX(props, ref) {
    const {
      __framer__presenceInitial,
      __framer__presenceAnimate,
      __framer__animate,
      __framer__transition,
      __perspectiveFX,
      __smartComponentFX,
      __targetOpacity,
      __framer__styleAppearEffectEnabled,
      __framer__threshold,
      __framer__animateOnce,
      ...rest
    } = props;
    const extra = {};
    if (__framer__presenceInitial !== undefined) {
      extra.initial = __framer__presenceInitial;
    }
    const target = __framer__presenceAnimate ?? __framer__animate;
    if (target !== undefined) extra.animate = target;
    if (__framer__transition !== undefined) {
      extra.transition = __framer__transition;
    }
    return React.createElement(Component, { ...rest, ...extra, ref });
  });
  Wrapped.displayName = "WithFX";
  return hoist(Wrapped, Component);
}

export function withOptimizedAppearEffect(Component) {
  // Framer's "optimized appear" runs the entrance from a separate script
  // before hydration, keyed by data-framer-appear-id. A React port has no
  // such script and does not need one — framer-motion plays the same
  // animation on mount. So the marker props are consumed here rather than
  // leaked onto the DOM, and the animation is left to the library.
  const Wrapped = React.forwardRef(function WithOptimizedAppearEffect(props, ref) {
    const { optimized, __framer__appearId, ...rest } = props;
    return React.createElement(Component, { ...rest, ref });
  });
  Wrapped.displayName = "WithOptimizedAppearEffect";
  return hoist(Wrapped, Component);
}

/* ───────────────────────── contexts & routing ───────────────────────── */

export const GeneratedComponentContext = React.createContext({});
export const PathVariablesContext = React.createContext({});
export const useCurrentPathVariables = () => React.useContext(PathVariablesContext);

export function PropertyOverrides({ children, overrides, breakpoint }) {
  return typeof children === "function" ? children(overrides || {}) : children ?? null;
}

export function useRouter() {
  return {
    navigate: (to) => {
      if (typeof window !== "undefined" && to) window.location.assign(to);
    },
    currentPath: typeof window !== "undefined" ? window.location.pathname : "/",
    currentPathVariables: {},
    routes: {},
  };
}

const LOCALE = {
  activeLocale: { id: "default", code: "en", name: "English", slug: "" },
  locales: [],
  setLocale: () => {},
};
export const useLocaleInfo = () => LOCALE;
export const useLocaleCode = () => LOCALE.activeLocale.code;

/* ─────────────────────────────── CMS ────────────────────────────────── */

// A converted page ships its content baked in, so these exist to satisfy
// the generated code's imports and to return empty rather than throw. A
// port that genuinely needs live CMS queries is a different product, and
// pretending otherwise here would hide that.
export const queryCache = {
  get: () => undefined,
  set: () => {},
  has: () => false,
  clear: () => {},
};

export class QueryEngine {
  constructor(data) {
    this.data = data || [];
  }
  query() {
    return this.data;
  }
  getById() {
    return undefined;
  }
}

export function useQueryData(query) {
  // Generated pages consume this as an ARRAY — `collection?.map(...)` —
  // so returning {data, isLoading} threw `collection?.map is not a
  // function` and killed the whole route. When a real collection is
  // supplied it is passed through; otherwise an empty list, which
  // renders the page with no CMS rows rather than not at all.
  if (Array.isArray(query)) return query;
  if (query && typeof query.query === "function") {
    const rows = query.query();
    return Array.isArray(rows) ? rows : [];
  }
  if (query && Array.isArray(query.data)) return query.data;
  return [];
}

export function getWhereExpressionFromPathVariables() {
  return undefined;
}
