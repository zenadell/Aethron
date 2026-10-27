// Announcements: a card every copy of Aethron shows once — a title, a few
// words, a button, and an illustration drawn for it by the admin AI in the
// house style (or a hand-drawn template when no admin AI is set).

import { db, Row } from "./db.ts";
import { costOf, openaiReasoning } from "./prices.ts";
import { checkSvg, ILLUSTRATOR_PROMPT, templateFor, TEMPLATES } from "./art.ts";
import { adminAiSettings } from "./ai.ts";
import { Admin, audit, cached, control, forget, setControl } from "./state.ts";
import { clip, http, HttpError, nowIso, nowMs, randomToken } from "./util.ts";

const LEVELS = ["info", "warn", "critical"];

/** Ask the admin AI to draw. Two tries — the second is shown why the first
 *  was refused — then a template, so publishing never waits on a model. */
export async function drawIllustration(args: Row): Promise<Row> {
  const title = String(args.title || "").trim(), body = String(args.body || "").trim();
  const level = LEVELS.includes(String(args.level)) ? String(args.level) : "info";
  const brief = String(args.brief || "").trim();
  if (!title && !body) throw new HttpError(400, "an illustration needs the announcement's title or words");
  const fallback = (why: string) => ({
    svg: TEMPLATES[templateFor(title, body, level)], source: "template", template: templateFor(title, body, level),
    note: why, cost_usd: 0,
  });
  let st;
  try {
    st = await adminAiSettings();
  } catch (e) {
    void e;
    return fallback("A house illustration was used. Set up the admin AI in “AI & models” and it draws a new one for each announcement.");
  }
  const examples = ["feature", "maintenance"].map((k) => `Example (${k}):\n${TEMPLATES[k]}`).join("\n\n");
  const ask = `${examples}\n\nNow draw the illustration for this announcement (${level}):\nTitle: ${title}\nWords: ${body}` +
    (brief ? `\nArt direction from the admin: ${brief}` : "") +
    "\n\nDraw something specific to THIS announcement — not a copy of an example.";
  const msgs: Row[] = [{ role: "system", content: ILLUSTRATOR_PROMPT }, { role: "user", content: ask }];
  let spent = 0, last = "";
  for (let attempt = 0; attempt < 2; attempt++) {
    const req: Row = { model: st.model, messages: msgs };
    if (!openaiReasoning(st.model)) req.temperature = 0.7;
    const t0 = nowMs();
    let r: Response;
    try {
      r = await http(st.base + "/chat/completions", {
        method: "POST", headers: { "Content-Type": "application/json", Authorization: "Bearer " + st.key },
        body: JSON.stringify(req),
      });
    } catch (e) {
      return { ...fallback("A house illustration was used — the admin AI could not be reached: " + (e as Error).message), cost_usd: spent };
    }
    if (!r.ok) {
      const t = await r.text().catch(() => "");
      return { ...fallback(`A house illustration was used — the admin AI answered ${r.status}: ${clip(t, 160)}`), cost_usd: spent };
    }
    const j = await r.json();
    const u = j.usage || {};
    const cost = costOf(st.model, Number(u.prompt_tokens || 0), Number(u.completion_tokens || 0));
    spent += cost;
    db.insert("usage_ledger", { provider: st.provider, model: st.model, purpose: "admin_art",
      input_tokens: Number(u.prompt_tokens || 0), output_tokens: Number(u.completion_tokens || 0),
      cost_usd: Math.round(cost * 1e6) / 1e6, ms: nowMs() - t0, estimated: !j.usage }).catch(() => {});
    const out = String((j.choices && j.choices[0] && j.choices[0].message && j.choices[0].message.content) || "");
    try {
      return { svg: checkSvg(out), source: "ai", model: st.model, cost_usd: Math.round(spent * 1e6) / 1e6 };
    } catch (e) {
      last = (e as Error).message;
      msgs.push({ role: "assistant", content: clip(out, 12000) },
        { role: "user", content: last + ". Fix every point and answer with the corrected SVG only." });
    }
  }
  return { ...fallback("A house illustration was used — the admin AI's drawing was refused twice (" + last + ")"), cost_usd: Math.round(spent * 1e6) / 1e6 };
}

function cleanUrl(v: unknown): string {
  const s = String(v || "").trim();
  if (!s) return "";
  if (!/^https:\/\/[^\s"'<>]+$/i.test(s)) throw new HttpError(400, "the button's address must start with https://");
  return s.slice(0, 500);
}

export async function publish(args: Row, a: Admin): Promise<Row> {
  const title = String(args.title || "").trim().slice(0, 120);
  const body = String(args.body || "").trim().slice(0, 600);
  if (!title) throw new HttpError(400, "an announcement needs a title");
  const level = LEVELS.includes(String(args.level)) ? String(args.level) : "info";
  const ctaUrl = cleanUrl(args.cta_url);
  const ctaLabel = String(args.cta_label || "").trim().slice(0, 40) || (ctaUrl ? "Learn more" : "");
  let art = "", artSource = "none", note = "";
  const want = args.art === undefined || args.art === null || args.art === "" ? "auto" : String(args.art);
  if (want === "auto") {
    const d = await drawIllustration({ title, body, level, brief: args.brief });
    art = d.svg;
    artSource = d.source;
    note = d.note || "";
  } else if (want !== "none") {
    art = checkSvg(want);
    artSource = "given";
  }
  const id = randomToken("an_", 9);
  await db.insert("announcements", {
    id, title, body, level, art, art_source: artSource, cta_label: ctaLabel, cta_url: ctaUrl,
    created_by: a.actor, created_at: nowIso(),
  });
  const patch: Row = { announcement_id: id };
  if (args.banner) Object.assign(patch, { banner: title + (body ? " — " + body : ""), banner_level: level });
  await setControl(patch, a.actor);
  forget("announcement");
  await audit(a, "publish_announcement", id, { title, level, art: artSource, banner: !!args.banner });
  return { id, title, level, art: artSource, note, live: true };
}

export async function end(a: Admin): Promise<Row> {
  const c = await control(true);
  await setControl({ announcement_id: "" }, a.actor);
  forget("announcement");
  await audit(a, "end_announcement", String(c.announcement_id || ""));
  return { ended: c.announcement_id || null };
}

export async function list(limit = 30): Promise<Row[]> {
  const c = await control();
  const rows = await db.select("announcements", {
    select: "id,title,body,level,art_source,cta_label,cta_url,created_by,created_at", order: "created_at.desc",
    limit: Math.min(100, limit || 30),
  });
  return rows.map((r) => ({ ...r, live: r.id === c.announcement_id }));
}

/** The live announcement, without its drawing (every copy reads this each
 *  minute; the drawing is fetched once by id and cached). */
export async function current(id: string): Promise<Row | null> {
  if (!id) return null;
  return cached("announcement:" + id, 30_000, async () => {
    const r = await db.one("announcements", { id: "eq." + id,
      select: "id,title,body,level,art_source,cta_label,cta_url,created_at" });
    if (!r) return null;
    return { id: r.id, title: r.title, body: r.body, level: r.level, has_art: r.art_source !== "none",
      cta_label: r.cta_label || "", cta_url: r.cta_url || "", published_at: r.created_at };
  });
}

export async function artOf(id: string): Promise<string> {
  if (!/^an_[A-Za-z0-9_-]{6,40}$/.test(id || "")) throw new HttpError(400, "no such announcement");
  const r = await db.one("announcements", { id: "eq." + id, select: "art" });
  if (!r || !r.art) throw new HttpError(404, "this announcement has no illustration");
  return checkSvg(r.art); // checked again on the way out, whatever is stored
}
