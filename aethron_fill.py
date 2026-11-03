"""The fill prompt and its parsing, shared by the studio's AI fill and
the background migration job (aethron_jobs). One copy of the rules, so
the two can never write by different standards."""
import json
import re

PROMPT_RULES = """Rules:
1. REWRITE EVERYTHING. This is a rebrand, not a find-and-replace. Every
   heading, paragraph, feature name, testimonial, blog title, changelog
   line, FAQ, dashboard label and micro-copy must describe the OWNER'S
   business. A visitor reading the finished site must not be able to tell
   what the template originally sold.
2. The ONLY entries you may leave as "" are ones that are already true of
   any business: navigation words (Home, Blog, Contact), UI verbs (Submit,
   Close, Next), country, city and browser names, dates, statistics and
   numbers, and legal boilerplate that names no industry. If a string
   mentions the original industry, its products, its jargon or its example
   data, it MUST be rewritten. When in doubt, rewrite it.
2b. SHORT STRINGS ARE NOT EXEMPT. Most of what a visitor reads is short —
   pricing tier names, feature labels, section eyebrows, card titles, job
   titles, chart legends. A two-word label naming the template's industry
   ("Predict your income", "Smart Expense Sorting", "AI-Powered Finance")
   is exactly as wrong as a paragraph and must be rewritten into the
   owner's equivalent. Never answer "" merely because a string is short or
   because you cannot see where it appears; write the label this owner's
   site would use in that slot.
2c. NEVER answer "" for a string containing the template's own brand name.
   That name must not survive anywhere, in any form — including possessive
   forms, tier names ("Brand Pro"), page titles, email addresses and URL
   slugs. In a slug or an address use one lowercase word, not the full
   name with spaces.
3. Map the original's domain onto the owner's, concept for concept, and
   keep it consistent across the whole site. Do not translate word by word:
   "invoice" -> "migration" produces sentences that parse and mean nothing.
   Read what the sentence is FOR, then write that sentence for this owner.
4. If max_bytes is set, the UTF-8 byte length of "new" must be <= it.
   Em-dashes and curly quotes are 3 bytes each. When unsure, write shorter.
   A shorter true sentence beats a longer one that is rejected. When
   max_bytes cannot hold the full brand name, use the SHORT BRAND given in
   the plan — never invent an abbreviation or an acronym of your own.
5. Never use backticks or ${ in any "new" value.
6. Keep the same shape as the original (a 3-word button stays ~3 words; a
   one-line subtitle stays one line). Shape is layout; words are yours.
7. Links: retarget emails, phone numbers and socials per the plan; leave
   internal anchors (#...) alone unless the plan says otherwise.
8. Images: leave "new" empty unless the plan supplies a replacement URL.
Return the complete JSON you were given, nothing else."""


PLAN_POLISH_PROMPT = """The owner of a website-template migration tool \
wrote a rough, incomplete migration plan. Rewrite it into the exact \
format below. Rules: never drop or contradict anything the owner wrote; \
infer sensible specifics for gaps (domain = brand.com, email = \
hello@<domain>, social handles from the brand name) — those are \
placeholders the owner can edit; if the BRAND NAME itself is missing, \
write <FILL: brand name> so the owner sees it. Return ONLY the plan.

Format:
Brand: <name>
Domain: <domain>
What it is: <one or two sentences>
Tone: <short guidance>
Replace every mention of the template's old brand (any casing, with or
without the (R) symbol) with <write the ACTUAL brand name here, not a
placeholder>. Keep all numbers, stats and pricing the same.
Email: <email>
Socials: <links>
Keep internal anchors (#...) unchanged.
<each extra owner instruction, restated clearly on its own line>

Owner's rough plan:
"""

def build_prompt(plan: str, batch: dict) -> str:
    return (f"You are rebranding a website template. PROJECT PLAN from "
            f"the owner:\n\n{plan.strip()}\n\nFill the \"new\" field of "
            f"each entry in this JSON per the plan. {PROMPT_RULES}\n\n"
            + json.dumps(batch, indent=1, ensure_ascii=False))


def extract_json(text: str) -> dict:
    text = re.sub(r"```(?:json)?", "", text)
    s, e = text.find("{"), text.rfind("}")
    if s < 0 or e <= s:
        raise ValueError("no JSON object in model output")
    return json.loads(text[s:e + 1])
