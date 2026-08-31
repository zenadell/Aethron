**Yes — there are tools that get very close to (or achieve) exact, high-fidelity extraction of Figma animations and conversion to production code**, especially for the newer **Figma Motion** timeline system and classic prototype/Smart Animate transitions. Pixel-perfect static layouts are already strong in many Figma-to-code tools; the hard part has always been motion fidelity (timing, easing curves, springs, multi-keyframe tracks, nested/child animations, and logic). Several options now address this directly.

### 1. Best direct match: Motion Export (Figma plugin)
This is currently one of the strongest dedicated solutions for your exact need.

- Works with **both classic Figma prototype transitions** (Smart Animate, dissolve, slide, etc.) **and the new Figma Motion timeline** (keyframed multi-stop animations).
- Reads the actual timeline data: every keyframe, per-property tracks, per-keyframe easings, spring settings, durations, and nested/child element animations.
- Exports clean, ready-to-ship code that preserves timing and easing to a high degree of fidelity.
- Supported outputs: **CSS**, React, Vue 3, Vanilla JS, **Framer Motion** (highest fidelity mapping — Figma Motion maps almost 1:1 onto Framer Motion’s keyframe model), and React Spring.
- Also exports GIF / transparent WebM for visual verification.
- Free tier: limited code + video exports; paid for unlimited.
- Plugin page / site: [motionexport.com](https://motionexport.com/) and the corresponding Figma Community plugin (“Motion Export”).

This is designed precisely so you don’t have to rebuild the motion by hand or approximate it with an LLM. Install it, select the animated frame/flow, and export the animation code in your target format. You can then drop the Framer Motion (or CSS/etc.) output into your existing HTML → React/Next.js pipeline.

### 2. Native Figma Motion export (built into Figma)
Figma’s own **Figma Motion** (timeline + keyframe editor, open beta around mid-2026) has first-class code export:

- In Dev Mode, select an animated layer → Motion panel → export as **CSS**, **JSON**, or **React** (Motion / motion.dev code, which is the evolution of Framer Motion).
- Every timing value, easing curve, and keyframe is inspectable and copyable.
- Official guidance from the Motion team confirms the React export produces real, production-ready Motion code you can paste in (install `motion` / `motion/react` or the compatible `framer-motion` package).
- Also supports video exports (MP4, GIF, WebM, Animated SVG) and is MCP-compatible for AI agents.
- Docs: [motion.dev/docs/figma](https://motion.dev/docs/figma) and Figma’s Motion help/docs.

If your “frame animations” are (or can be) built/rebuilt with Figma Motion, this is the cleanest path for 1:1 behavior.

### 3. Other useful options and complementary approaches
- **LottieFiles for Figma** (and related tools like Lottify, Protonix, etc.): Excellent for exporting prototypes / Smart Animate / frames to Lottie JSON or dotLottie. High visual fidelity for many UI animations, interactive state machines supported in some modes, then play with a Lottie player (or convert further). Great when the animation is more complex or vector-heavy. Not always “pure CSS/JS logic” but the behavior can stay extremely close.
- **Figmotion** (and similar motion plugins): Can export to CSS, JSON, Lottie, or Framer Motion objects for handoff.
- **“Export to CSS Animation”** plugin and other community CSS exporters: Simpler cases; results vary and may not perfectly match custom easings/springs.
- Broader Figma-to-code platforms (Anima, Locofy, Builder.io Visual Copilot, etc.): Strong on static pixel-perfect HTML/CSS/React/Next.js/Tailwind and basic interactions/hover states. Animation support is improving but usually not as deep or precise on complex multi-keyframe / Smart Animate timelines as the dedicated motion tools above. Anima in particular has long supported interactive prototypes.
- Advanced / custom routes: Figma’s Plugin API now exposes Motion data (timelines, keyframes, easings, springs). You (or an agent) can build a custom extractor if needed. There are also reverse-engineering approaches (record the prototype → analyze motion → generate Framer Motion), but the dedicated exporters are preferable.

### Practical recommendations for your pipeline
1. Prefer **Figma Motion** (or convert your existing frame animations into it) + native Dev Mode export or **Motion Export** plugin → Framer Motion / CSS / vanilla JS. This gives the highest chance of identical timing, easing, and nested behavior.
2. Install Framer Motion (or `motion/react`) if you go the React route — it is explicitly supported and maps very cleanly.
3. For pure HTML/CSS first stage: use the CSS export, then transform that (or the Framer Motion version) into React/Next.js/etc. while keeping the animation definitions intact.
4. Always visually verify (side-by-side with the Figma prototype or exported video/GIF). No tool is magically 100% perfect on every edge-case interaction or extremely complex Smart Animate, but the ones above get far closer than LLM approximation.
5. Structure in Figma matters: clean layers, Auto Layout where possible, properly set up variants/prototypes/Motion timelines improve extraction quality across the board.

**Motion Export** and native **Figma Motion → code** are the closest things currently available to “extract the exact animation code and transform it while keeping behavior pixel-perfect / logic-identical.” Start there — try the free tier of Motion Export on one of your broken frame animations and compare the output side-by-side with the original prototype.

If you share more details (classic Smart Animate vs. Figma Motion timeline, complexity of the animation, preferred target framework, or a public example file), I can narrow it further or help evaluate specific outputs.