from flask import Flask, request, jsonify, render_template_string, session, Response
import os
import re
import uuid
import html
import math
import base64
import time
import requests
from groq import Groq

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-key-change-this-later")
app.config["SESSION_COOKIE_SAMESITE"] = "None"
app.config["SESSION_COOKIE_SECURE"] = True
# Photos are resized in the browser before upload, so payloads are small.
# This is only a safety cap to reject anything abnormally large.
app.config["MAX_CONTENT_LENGTH"] = 12 * 1024 * 1024  # 12 MB

_groq_client = None
all_conversations = {}
session_images = {}
notified_sessions = set()
chat_activity = {}


# ---------------------------------------------------------------------------
#  Business facts  (single source of truth - the bot AND the calculator use it)
# ---------------------------------------------------------------------------
BIZ = {
    "name": "West Handyman",
    "area": "Fulham & West London",
    "base": "Dawes Road Hub, 20 Dawes Rd, London SW6 7EN",
    "phone_display": "020 7118 9989",
    "phone_tel": "+442071189989",
    "wa": "+442071189989",
    "email": "contact@westhandyman.com",
    "hours": "Mon–Sun, 8am–10pm",
    "since": 2010,
    "rating": "4.7",
    "reviews_count": 31,
    "site": "https://westhandyman.com",
    "facebook": "https://www.facebook.com/WestHandymanUK/",
    "tiktok": "https://www.tiktok.com/@westhandyman",
    "youtube": "https://www.youtube.com/@westhandyman4540",
    "telegram": "https://t.me/WHMLT",
    "google_reviews": "https://g.page/west-handyman?share",
}

# Published rates (ex VAT), billed in 30-minute blocks, minimum booking 1 hour.
RATE_PER_30 = {1: 49, 2: 69}     # £ per half hour, by crew size
MIN_MINUTES = 60

# Estimator library. crew = number of handymen; lo/hi = typical minutes on site.
# Kept deliberately honest and slightly conservative so quotes rarely undershoot.
SERVICES = [
    {"id": "tv_small",   "label": "TV wall mount — up to 43\", solid/brick wall", "crew": 1, "lo": 60,  "hi": 60},
    {"id": "tv_large",   "label": "TV wall mount — 50\"+ or plasterboard/cavity", "crew": 1, "lo": 60,  "hi": 120},
    {"id": "mirror_sm",  "label": "Mirror hanging — up to 1m",                    "crew": 1, "lo": 60,  "hi": 60},
    {"id": "mirror_lg",  "label": "Mirror hanging — over 1m / heavy",             "crew": 2, "lo": 60,  "hi": 90},
    {"id": "pictures",   "label": "Pictures & art — a set (up to ~6 pieces)",     "crew": 1, "lo": 60,  "hi": 120},
    {"id": "shelves",    "label": "Shelves — per run / a few brackets",           "crew": 1, "lo": 60,  "hi": 90},
    {"id": "flatpack_sm","label": "Flat-pack — small unit (drawers, bedside)",    "crew": 1, "lo": 60,  "hi": 120},
    {"id": "flatpack_wd","label": "Flat-pack — wardrobe / large item",            "crew": 2, "lo": 120, "hi": 180},
    {"id": "curtains",   "label": "Curtain pole / blinds",                        "crew": 1, "lo": 60,  "hi": 60},
    {"id": "lighting",   "label": "Ceiling light / fixture (ladder work)",        "crew": 2, "lo": 60,  "hi": 90},
    {"id": "general",    "label": "General odd jobs / property maintenance",      "crew": 1, "lo": 60,  "hi": 120},
]
SERVICE_BY_ID = {s["id"]: s for s in SERVICES}


def _round_up_30(minutes):
    """Round minutes up to the next 30-minute billing block, floored at the minimum."""
    minutes = max(minutes, MIN_MINUTES)
    return int(math.ceil(minutes / 30.0) * 30)


def price_for(service):
    """Return (crew, minutes_lo, minutes_hi, price_lo, price_hi) ex VAT for a service dict."""
    crew = service["crew"]
    rate = RATE_PER_30[crew]
    lo = _round_up_30(service["lo"])
    hi = _round_up_30(service["hi"])
    price_lo = (lo // 30) * rate
    price_hi = (hi // 30) * rate
    return crew, lo, hi, price_lo, price_hi


def rate_card_text():
    """Compact rate summary injected into the bot prompt so it quotes accurately."""
    lines = [
        "RATES (ex VAT, billed in 30-min blocks, minimum 1 hour, materials extra):",
        f"- One handyman: £{RATE_PER_30[1]} per 30 min  (so £{RATE_PER_30[1]*2}/hour).",
        f"- Two handymen: £{RATE_PER_30[2]} per 30 min  (so £{RATE_PER_30[2]*2}/hour).",
        "Typical jobs and rough on-site time (use these to give a ballpark):",
    ]
    for s in SERVICES:
        crew, lo, hi, plo, phi = price_for(s)
        if plo == phi:
            money = f"~£{plo}"
        else:
            money = f"~£{plo}–£{phi}"
        span = f"{lo//60}h" if lo == hi else f"{lo//60}–{hi//60}h"
        lines.append(f"- {s['label']}: {crew} handyman, {span}, {money} ex VAT.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
#  Groq client (lazy)
# ---------------------------------------------------------------------------
def client_chat(**kwargs):
    global _groq_client
    if _groq_client is None:
        _groq_client = Groq(api_key=os.environ.get("GROQ_API_KEY"))
    return _groq_client.chat.completions.create(**kwargs)


# Groq chat model. llama-3.3-70b-versatile was retired for free/developer tier
# (June 2026); openai/gpt-oss-120b is Groq's recommended production replacement.
# Change this one line to swap the model across the whole site.
MODEL = os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b")


# ---------------------------------------------------------------------------
#  Email (Resend over HTTPS - Render's free tier blocks SMTP)
# ---------------------------------------------------------------------------
RESEND_API_KEY = os.environ.get("RESEND_API_KEY")
NOTIFY_TO = os.environ.get("NOTIFY_TO", "contact@westhandyman.com")
RESEND_FROM = os.environ.get("RESEND_FROM", "West Handyman Website <leads@frontdesk.org.uk>")

MAX_IMAGES_PER_SESSION = 6
ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp"}
MAX_IMAGE_BYTES = 6 * 1024 * 1024


def _decode_image_data_url(data_url):
    if not isinstance(data_url, str) or not data_url.startswith("data:"):
        return None
    try:
        header, b64 = data_url.split(",", 1)
    except ValueError:
        return None
    if ";base64" not in header:
        return None
    content_type = header[len("data:"):].split(";", 1)[0].lower()
    if content_type not in ALLOWED_IMAGE_TYPES:
        return None
    try:
        raw = base64.b64decode(b64, validate=True)
    except Exception:
        return None
    if not raw or len(raw) > MAX_IMAGE_BYTES:
        return None
    ext = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}[content_type]
    return {
        "filename": f"job-photo-{uuid.uuid4().hex[:8]}.{ext}",
        "content_type": content_type,
        "b64": base64.b64encode(raw).decode("ascii"),
    }


# ---------------------------------------------------------------------------
#  Contact extraction (server-side - the lead trigger never relies on the AI)
# ---------------------------------------------------------------------------
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
PHONE_RE = re.compile(r"(?<!\d)(?:\+44|0)\d[\d\s\-\.]{8,11}(?!\d)")
POSTCODE_RE = re.compile(r"\b[A-Za-z]{1,2}\d[A-Za-z\d]?\s*\d[A-Za-z]{2}\b")


def _customer_text(conversation):
    return " ".join(m["content"] for m in conversation if m.get("role") == "user")


def find_email(conversation):
    match = EMAIL_RE.search(_customer_text(conversation))
    return match.group(0) if match else None


def find_phone(conversation):
    text = _customer_text(conversation)
    for candidate in PHONE_RE.findall(text):
        digits = re.sub(r"\D", "", candidate)
        if digits.startswith("00"):
            continue
        if digits.startswith("44"):
            digits = "0" + digits[2:]
        if len(digits) == 11 and digits.startswith("0"):
            return f"{digits[:5]} {digits[5:]}"
    return None


def find_postcode(conversation):
    match = POSTCODE_RE.search(_customer_text(conversation))
    if not match:
        return None
    raw = re.sub(r"\s+", "", match.group(0)).upper()
    return raw[:-3] + " " + raw[-3:]


def has_contact_info(conversation):
    return bool(find_email(conversation) or find_phone(conversation))


CLOSING_RE = re.compile(
    r"\b(no longer interested|not interested|no thanks|no thank you|"
    r"that'?s all|that'?s it|that'?s everything|nothing else|all good|"
    r"that'?s great thank|thanks that'?s|goodbye|bye for now|no more|"
    r"i'?m good|im good)\b",
    re.I,
)


def _looks_like_closing(text):
    return bool(CLOSING_RE.search(text or ""))


def _transcript(conversation):
    lines = []
    for msg in conversation:
        if msg["role"] == "user":
            lines.append(f"Customer: {msg['content']}")
        elif msg["role"] == "assistant":
            lines.append(f"Assistant: {msg['content']}")
    return "\n\n".join(lines)


# ---------------------------------------------------------------------------
#  Lead summary (model tidies the chat into scannable fields)
# ---------------------------------------------------------------------------
LEAD_SUMMARY_PROMPT = """You are turning a website chat into a clean job lead for a
London handyman business owner. Read the conversation and output EXACTLY these
labelled lines and nothing else. Fill each from what the customer actually said;
write "Not specified" if they didn't. Keep each line short.

Name:
Job / items (what needs doing):
Size / weight (e.g. under or over 1m, over 25kg, TV size):
Wall or access (brick/plasterboard, floor, ladder needed):
Crew likely needed (1 or 2 handymen - infer from the job):
Rough estimate given (in GBP £ ex VAT, if one was quoted):
Preferred day / timing:
Urgency (1-5 where 1=no rush, 5=urgent - infer):
Area / postcode:
Other notes:"""


def summarise_lead(conversation):
    try:
        resp = client_chat(
            model=MODEL,
            messages=[
                {"role": "system", "content": LEAD_SUMMARY_PROMPT},
                {"role": "user", "content": _transcript(conversation)},
            ],
            max_tokens=280,
            temperature=0.2,
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        print(f"Lead summary failed: {e}")
        return None


def _post_resend(subject, text, html_body=None, attachments=None):
    if not RESEND_API_KEY:
        print("RESEND_API_KEY not set, skipping email")
        return
    payload = {"from": RESEND_FROM, "to": [NOTIFY_TO], "subject": subject, "text": text}
    if html_body:
        payload["html"] = html_body
    if attachments:
        payload["attachments"] = [
            {"filename": a["filename"], "content": a["b64"]} for a in attachments
        ]
    try:
        response = requests.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {RESEND_API_KEY}"},
            json=payload,
            timeout=15,
        )
        if response.status_code >= 300:
            print(f"Resend error: {response.status_code} {response.text}")
    except Exception as e:
        print(f"Failed to send email: {e}")


def _parse_summary(structured):
    out = {}
    if not structured:
        return out
    for line in structured.splitlines():
        if ":" in line:
            key, val = line.split(":", 1)
            out[key.strip().lower()] = val.strip()
    return out


def _lead_fields(conversation):
    s = _parse_summary(summarise_lead(conversation))

    def pick(*keys):
        for k in keys:
            v = s.get(k)
            if v and v.lower() not in ("not specified", "not provided", "n/a", "none", "-"):
                return v
        return None

    return {
        "Name": pick("name"),
        "Phone": find_phone(conversation),
        "Email": find_email(conversation),
        "Postcode": find_postcode(conversation),
        "Area": pick("area / postcode", "area", "location"),
        "Job / items": pick("job / items (what needs doing)", "job / items", "job"),
        "Size / weight": pick("size / weight (e.g. under or over 1m, over 25kg, tv size)", "size / weight", "size"),
        "Wall / access": pick("wall or access (brick/plasterboard, floor, ladder needed)", "wall or access", "wall / access"),
        "Crew needed": pick("crew likely needed (1 or 2 handymen - infer from the job)", "crew likely needed", "crew"),
        "Estimate given": pick("rough estimate given (in gbp £ ex vat, if one was quoted)", "rough estimate given", "estimate"),
        "Preferred timing": pick("preferred day / timing", "preferred timing", "timing"),
        "Urgency": pick("urgency (1-5 where 1=no rush, 5=urgent - infer)", "urgency"),
        "Notes": pick("other notes", "notes"),
    }


def _row(label, value):
    if not value:
        return ""
    return (
        '<tr>'
        f'<td style="padding:10px 16px;border-bottom:1px solid #eef2f7;color:#7c8aa0;'
        f'font-size:13px;white-space:nowrap;vertical-align:top;width:140px">{html.escape(label)}</td>'
        f'<td style="padding:10px 16px;border-bottom:1px solid #eef2f7;color:#0f1b2d;'
        f'font-size:14px;font-weight:600">{html.escape(str(value))}</td>'
        '</tr>'
    )


def _transcript_html(conversation):
    rows = []
    for msg in conversation:
        if msg["role"] == "user":
            who, color, bg = "Customer", "#0f1b2d", "#eef4fb"
        elif msg["role"] == "assistant":
            who, color, bg = "West Handyman assistant", "#0a7fb0", "#ffffff"
        else:
            continue
        text = html.escape(msg["content"]).replace("\n", "<br>")
        rows.append(
            f'<div style="margin:0 0 12px">'
            f'<div style="font-size:11px;letter-spacing:.05em;text-transform:uppercase;'
            f'color:{color};font-weight:700;margin-bottom:4px">{who}</div>'
            f'<div style="background:{bg};border:1px solid #e3ebf3;border-radius:10px;'
            f'padding:11px 14px;font-size:14px;color:#2a3648;line-height:1.5">{text}</div>'
            f'</div>'
        )
    return "".join(rows)


def _urgency_badge(urgency_str):
    if not urgency_str:
        return ""
    m = re.search(r"[1-5]", str(urgency_str))
    if not m:
        return ""
    score = int(m.group(0))
    colours = {
        1: ("#e8f5e9", "#2e7d32", "1 — No rush"),
        2: ("#f1f8e9", "#558b2f", "2 — Low"),
        3: ("#fff8e1", "#f57f17", "3 — Moderate"),
        4: ("#fff3e0", "#e65100", "4 — Fairly urgent"),
        5: ("#ffebee", "#b71c1c", "5 — URGENT — reply ASAP"),
    }
    bg, fg, label = colours.get(score, ("#f5f5f5", "#555", str(score)))
    return (
        f'<div style="margin:0 0 20px">'
        f'<div style="font-size:11px;letter-spacing:.08em;text-transform:uppercase;'
        f'color:#8a97a8;font-weight:700;margin-bottom:6px">Urgency</div>'
        f'<span style="display:inline-block;background:{bg};color:{fg};border:1px solid {fg};'
        f'border-radius:999px;padding:5px 14px;font-size:13px;font-weight:700">{label}</span></div>'
    )


def _lead_email_html(fields, conversation, image_count):
    urgency_val = fields.pop("Urgency", None)
    rows = "".join(_row(k, v) for k, v in fields.items())
    photos_line = ""
    if image_count:
        photos_line = (
            '<p style="margin:0 0 20px;font-size:14px;color:#0f1b2d">'
            f'\U0001F4CE <strong>{image_count} photo(s)</strong> attached to this email.</p>'
        )
    urgency_html = _urgency_badge(urgency_val)
    return (
        '<!DOCTYPE html><html><body style="margin:0;background:#eef3f8;padding:24px;'
        'font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,Helvetica,Arial,sans-serif">'
        '<div style="max-width:620px;margin:0 auto;background:#fff;border-radius:14px;'
        'overflow:hidden;box-shadow:0 2px 12px rgba(15,27,45,.08)">'
        '<div style="background:linear-gradient(135deg,#0a1526,#0d2136);padding:24px 28px">'
        '<div style="color:#25c3f0;font-size:12px;letter-spacing:.18em;text-transform:uppercase;'
        'font-weight:700">West Handyman</div>'
        '<div style="color:#fff;font-size:21px;font-weight:700;margin-top:5px">'
        'New job enquiry from your website</div></div>'
        '<div style="padding:26px 28px">'
        '<p style="margin:0 0 20px;font-size:14px;color:#65728a">'
        'Details captured by the website assistant:</p>'
        f'{urgency_html}{photos_line}'
        '<table style="width:100%;border-collapse:collapse;border:1px solid #eef2f7;'
        f'border-radius:8px;overflow:hidden;margin-bottom:28px">{rows}</table>'
        '<div style="font-size:12px;letter-spacing:.05em;text-transform:uppercase;'
        'color:#8a97a8;font-weight:700;margin-bottom:14px">Full conversation</div>'
        f'{_transcript_html(conversation)}'
        '</div>'
        '<div style="background:#f6f9fc;padding:16px 28px;border-top:1px solid #eef2f7;'
        'font-size:12px;color:#9aa7b8">Sent automatically by the West Handyman website assistant. '
        'Fulham &middot; SW6</div>'
        '</div></body></html>'
    )


def send_lead_email(conversation, images=None):
    images = images or []
    fields = _lead_fields(conversation)
    transcript = _transcript(conversation)

    text_lines = ["NEW LEAD - West Handyman", "========================"]
    for k, v in fields.items():
        if v:
            text_lines.append(f"{k}: {v}")
    if images:
        text_lines.append(f"Photos attached: {len(images)}")
    text_lines += ["========================", "", "Full conversation:", "", transcript]
    text_body = "\n".join(text_lines)

    html_body = _lead_email_html(fields, conversation, len(images))

    urgency_raw = fields.get("Urgency", "")
    urgency_m = re.search(r"[1-5]", str(urgency_raw)) if urgency_raw else None
    urgency_score = int(urgency_m.group(0)) if urgency_m else 0
    urgent_prefix = "🔴 URGENT — " if urgency_score >= 5 else ("🟠 " if urgency_score >= 4 else "")

    contact = fields.get("Phone") or fields.get("Email") or "no number yet"
    bits = [b for b in (fields.get("Name"), fields.get("Area") or fields.get("Postcode")) if b]
    subject = urgent_prefix + "New lead - " + (" \u00b7 ".join(bits + [contact]) if bits else contact)
    _post_resend(subject, text_body, html_body=html_body, attachments=images)


def send_photo_followup(conversation, images):
    if not images:
        return
    phone = find_phone(conversation) or "Not provided"
    email = find_email(conversation) or "Not provided"
    postcode = find_postcode(conversation) or "Not provided"
    text_body = (
        "ADDITIONAL PHOTO(S) - West Handyman\n"
        "This relates to a lead you've already been emailed about.\n"
        f"Phone: {phone}\nEmail: {email}\nPostcode: {postcode}\n"
        f"Photos attached: {len(images)}\n"
    )
    html_body = (
        '<!DOCTYPE html><html><body style="margin:0;background:#eef3f8;padding:24px;'
        'font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,Helvetica,Arial,sans-serif">'
        '<div style="max-width:620px;margin:0 auto;background:#fff;border-radius:14px;overflow:hidden;'
        'box-shadow:0 2px 12px rgba(15,27,45,.08)">'
        '<div style="background:linear-gradient(135deg,#0a1526,#0d2136);padding:22px 28px">'
        '<div style="color:#25c3f0;font-size:12px;letter-spacing:.18em;text-transform:uppercase;font-weight:700">West Handyman</div>'
        '<div style="color:#fff;font-size:19px;font-weight:700;margin-top:5px">Extra photo(s) for a lead</div></div>'
        '<div style="padding:24px 28px">'
        '<p style="margin:0 0 18px;font-size:14px;color:#65728a">Relates to a lead you were already emailed about. '
        f'<strong>{len(images)} new photo(s)</strong> attached below.</p>'
        '<table style="width:100%;border-collapse:collapse;border:1px solid #eef2f7;border-radius:8px;'
        f'overflow:hidden">{_row("Phone", phone)}{_row("Email", email)}{_row("Postcode", postcode)}</table>'
        '</div></div></body></html>'
    )
    _post_resend(f"Photo added - lead: {phone}", text_body, html_body=html_body, attachments=images)


# ---------------------------------------------------------------------------
#  System prompt  (the brain of the bot)
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = f"""
You are the booking assistant for West Handyman, a trusted handyman team in
Fulham and West London, established {BIZ['since']} with a {BIZ['rating']}-star Google
rating. You are the first point of contact for new enquiries on the website.

WHAT WEST HANDYMAN DOES:
Specialists in wall-mounting: TVs (including on plasterboard/cavity walls),
mirrors, pictures and art, shelves and light fixtures. Also flat-pack furniture
assembly (wardrobes, units, sofas), curtain poles and blinds, and general
property maintenance — door shaving, door handle fixing, ceiling lights, bathtub
and shower resealing, light-bulb changes, lifting and moving items, and more.
No job too big or too small.

HOW WE WORK (mention naturally when relevant, don't dump it all at once):
- Covering Fulham, Chelsea, Putney, Hammersmith, Kensington, Wandsworth and
  across West & South-West London. Base: {BIZ['base']}.
- Open {BIZ['hours']}.
- We arrive on time, wear shoe covers, bring cordless vacuums and clean up after.
- Professional tools — powerful drills, titanium-coated bits — so we handle hard
  walls and awkward jobs cleanly.
- Payment on completion by card; interest-free instalments via Clearpay. No parking fees.

{rate_card_text()}

HOW TO QUOTE:
When someone describes a job, give a friendly ballpark from the rates above, and
ALWAYS say it's an estimate ex VAT, plus materials, with a 1-hour minimum. Explain
briefly why (e.g. "plasterboard walls need special fixings, so we usually allow a
bit longer"). If a job is vague or unusual, say a photo would let us quote properly,
and point them to the paperclip to attach one. Never invent prices outside the rates.

YOUR TONE:
Talk like a friendly, straight-talking London tradesperson texting a customer.
Short, warm, helpful. One question at a time. No long paragraphs, no bullet-point
lists, no corporate phrases like "I'd be happy to assist you". A little personality
is good.

CONVERSATION FLOW — work through these naturally, one at a time:
1. What's the job? (TV, mirror, shelves, flat-pack, curtains, repair, etc.)
2. Key details that change the quote: size/weight (over or under 1m, over 25kg),
   TV size, wall type (brick vs plasterboard), and whether ladder/height is involved.
3. Give a rough estimate using the rates. Offer a photo via the paperclip if it helps.
4. Which day / how soon? (today, this week, no rush)
5. Their name.
6. Their area or postcode (to confirm we cover it — we do across West/SW London).
7. Best phone number or email — then repeat it back to confirm you've got it right.
8. Once you have contact details, tell them the enquiry is booked in and the team
   will confirm a slot shortly, and mention they can also WhatsApp {BIZ['phone_display']}
   for the fastest response.

IMPORTANT:
Only add the hidden tag [[READY]] once you have covered the job, the sizing/wall
details, given a ballpark, asked timing, and captured name + area + confirmed
contact details. The customer NEVER sees [[READY]]. Put it on its own line at the
very end of your final wrap-up message only.
"""


# ---------------------------------------------------------------------------
#  Shared front-end pieces
# ---------------------------------------------------------------------------
REVIEWS = [
    ("Jules M.", 5, "Punctual, precise and genuinely proud of the work. Tidied up perfectly. Incredible rates — will use again."),
    ("Lita Z.", 5, "Moved into West Kensington and needed a big heavy mirror up. Easy booking, arrived on time, level and secure, spotless finish."),
    ("V. Richards", 5, "Blinds swapped, pictures and a mirror onto very hard walls, a light fitting replaced. Shoe covers on without asking. Tremendously pleased."),
    ("Emer M.", 5, "Called at 8pm, a handyman was out within the hour and fixed our broken lock quickly. Will definitely use again."),
    ("Rubinder G.", 5, "Clear on pricing, kept me informed, arrived on time and left everything clean. Will book again."),
    ("Nikolas M.", 5, "Great, professional service from start to finish. Highly recommend."),
]

SOCIAL_SVGS = {
    "facebook": '<path d="M22 12a10 10 0 1 0-11.6 9.9v-7H7.9V12h2.5V9.8c0-2.5 1.5-3.9 3.8-3.9 1.1 0 2.2.2 2.2.2v2.5h-1.2c-1.2 0-1.6.8-1.6 1.6V12h2.7l-.4 2.9h-2.3v7A10 10 0 0 0 22 12z"/>',
    "tiktok": '<path d="M16.6 5.8a4.3 4.3 0 0 1-1-2.8h-3.3v12.3a2.4 2.4 0 1 1-2.4-2.4c.2 0 .5 0 .7.1V9.6a5.7 5.7 0 0 0-.7 0 5.7 5.7 0 1 0 5.7 5.7V9.2a7.5 7.5 0 0 0 4.4 1.4V7.3a4.3 4.3 0 0 1-3.4-1.5z"/>',
    "youtube": '<path d="M23 12s0-3.2-.4-4.7a2.5 2.5 0 0 0-1.8-1.8C19.3 5 12 5 12 5s-7.3 0-8.8.5A2.5 2.5 0 0 0 1.4 7.3 26 26 0 0 0 1 12a26 26 0 0 0 .4 4.7 2.5 2.5 0 0 0 1.8 1.8C4.7 19 12 19 12 19s7.3 0 8.8-.5a2.5 2.5 0 0 0 1.8-1.8C23 15.2 23 12 23 12zM9.8 15V9l5.2 3z"/>',
}

BASE_STYLE = """
<meta name="google-site-verification" content="GKaUBvuOcWao-SIScAyudggQO9z1T4oMHUBuD8cSWrA" />
<link rel="icon" type="image/png" href="/static/images/favicon.png">
<meta name="theme-color" content="#0a1526">
<meta property="og:type" content="website">
<meta property="og:site_name" content="West Handyman">
<meta property="og:title" content="West Handyman — Fulham & West London Handyman">
<meta property="og:description" content="TV & mirror mounting, shelves, flat-pack and property maintenance across Fulham and West London. Instant quote, photos welcome. 4.7★ on Google.">
<meta property="og:image" content="/static/images/favicon.png">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;500;600;700&family=Inter:wght@400;500;600;700;800&family=Space+Mono:wght@700&display=swap" rel="stylesheet">
<style>
  :root{
    --bg:#081221; --bg2:#0a1526; --panel:#0e1c31; --panel2:#112743;
    --ink:#eaf2fb; --mut:#93a6c0; --line:rgba(37,195,240,.16);
    --cyan:#25c3f0; --cyan-dk:#0a7fb0; --blue:#2f7be6; 
    --paper:#f4f8fc; --paper-ink:#0f1b2d;
  }
  *{box-sizing:border-box} html{scroll-behavior:smooth}
  body{margin:0;background:var(--bg);color:var(--ink);font-family:Inter,-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;line-height:1.55;-webkit-font-smoothing:antialiased;overflow-x:hidden}
  a{color:var(--cyan)} img,video{max-width:100%;display:block}
  h1,h2,h3,.dsp{font-family:'Space Grotesk',Inter,sans-serif}
  .mono{font-family:'Space Mono',monospace}
  .wrap{max-width:1180px;margin:0 auto;width:100%;padding:0 24px}.narrow{max-width:840px}
  section{padding:74px 0}
  .eyebrow{font-size:12px;letter-spacing:.24em;text-transform:uppercase;color:var(--cyan);font-weight:700}
  .sec-h{font-size:clamp(26px,3.6vw,40px);margin:12px 0 8px;letter-spacing:-.4px;line-height:1.05}
  .sec-sub{color:var(--mut);max-width:640px;margin:0 0 34px;font-size:16px}

  /* nav */
  nav{position:sticky;top:0;z-index:60;display:flex;align-items:center;justify-content:space-between;gap:18px;padding:10px 24px;background:rgba(8,18,33,.86);backdrop-filter:blur(14px);border-bottom:1px solid var(--line)}
  .brand{display:flex;align-items:center;gap:10px;text-decoration:none}
  .brand img{height:38px;width:auto}
  .links{display:flex;align-items:center;gap:22px;flex-wrap:wrap}
  .links a{color:#dbe7f6;text-decoration:none;font-size:13.5px;font-weight:600;letter-spacing:.01em}
  .links a:hover{color:var(--cyan)}
  .links a.navsoc{width:34px;height:34px;border-radius:9px;display:grid;place-items:center;border:1px solid var(--line);background:rgba(255,255,255,.03)}
  .links a.navsoc:hover{border-color:var(--cyan)}
  .navcta{background:linear-gradient(135deg,var(--cyan-dk),var(--cyan));padding:10px 16px;border-radius:10px;color:#04121e!important;font-weight:800!important;box-shadow:0 8px 22px rgba(37,195,240,.28)}

  /* hero */
  .hero{position:relative;overflow:hidden;padding:66px 0 56px;background:
     radial-gradient(880px 520px at 82% 0%,rgba(37,195,240,.16),transparent 58%),
     radial-gradient(720px 520px at 0% 100%,rgba(47,123,230,.12),transparent 55%),
     linear-gradient(180deg,#0a1526,#081221)}
  .hero:before{content:"";position:absolute;inset:0;background-image:linear-gradient(rgba(37,195,240,.05) 1px,transparent 1px),linear-gradient(90deg,rgba(37,195,240,.05) 1px,transparent 1px);background-size:44px 44px;mask:radial-gradient(700px 500px at 75% 10%,#000,transparent 75%)}
  .hero-inner{position:relative;z-index:1;display:grid;grid-template-columns:minmax(0,1fr) 330px;gap:48px;align-items:center}
  h1{font-size:clamp(34px,5.4vw,60px);line-height:1.02;margin:16px 0 18px;letter-spacing:-1px}
  h1 .g{color:var(--cyan)}
  .hero p.lede{font-size:18px;color:#cfdcec;max-width:540px;margin:0 0 24px}
  .btns{display:flex;gap:12px;flex-wrap:wrap;align-items:center}
  .btn{display:inline-flex;align-items:center;gap:9px;justify-content:center;border:0;border-radius:11px;background:linear-gradient(135deg,var(--cyan-dk),var(--cyan));color:#04121e;text-decoration:none;font-weight:800;padding:14px 22px;box-shadow:0 14px 34px rgba(37,195,240,.26);font-size:15px;cursor:pointer;font-family:inherit}
  .btn svg{width:18px;height:18px;fill:currentColor}
  .btn.ghost{background:rgba(255,255,255,.04);border:1px solid var(--line);box-shadow:none;color:#eaf2fb}
  .btn.wa{background:linear-gradient(135deg,#128c3e,#25d366);color:#fff}
  .trust{display:flex;gap:18px;flex-wrap:wrap;margin-top:24px;align-items:center;color:var(--mut);font-size:13.5px;font-weight:600}
  .trust b{color:#eaf2fb}
  .stars{color:#ffc531;letter-spacing:1px}

  /* live open/closed status */
  .livebar{display:inline-flex;align-items:center;gap:9px;background:rgba(10,22,38,.72);border:1px solid var(--line);border-radius:999px;padding:7px 15px 7px 13px;font-size:13.5px;font-weight:600;color:#eaf2fb;margin-bottom:2px}
  .livebar .dot{width:9px;height:9px;border-radius:50%;background:#25d366;box-shadow:0 0 0 0 rgba(37,211,102,.55);animation:pulse 1.8s infinite;flex:none}
  .livebar.closed .dot{background:#f0a52c;animation:none}
  .livebar b{font-weight:800}
  .livebar .sub{color:var(--mut);font-weight:500}

  /* hero phone with reel */
  .phone{position:relative;justify-self:center;width:300px;max-width:100%;border-radius:34px;padding:10px;background:linear-gradient(160deg,#16304f,#0b1626);border:1px solid var(--line);box-shadow:0 40px 90px rgba(0,0,0,.55),inset 0 0 0 1px rgba(255,255,255,.03)}
  .phone video{width:100%;aspect-ratio:9/19.5;object-fit:cover;border-radius:26px;background:#000;display:block}
  .phone .notch{position:absolute;top:20px;left:50%;translate:-50% 0;width:96px;height:20px;background:#050e1a;border-radius:0 0 14px 14px;z-index:2}
  .phone .live{position:absolute;left:20px;bottom:20px;z-index:3;display:inline-flex;align-items:center;gap:7px;background:rgba(4,14,26,.72);border:1px solid var(--line);border-radius:999px;padding:6px 12px;font-size:11.5px;font-weight:700;color:#eaf2fb}
  .phone .live i{width:8px;height:8px;border-radius:50%;background:#25d366;box-shadow:0 0 0 0 rgba(37,211,102,.6);animation:pulse 1.8s infinite}
  @keyframes pulse{0%{box-shadow:0 0 0 0 rgba(37,211,102,.55)}70%{box-shadow:0 0 0 9px rgba(37,211,102,0)}100%{box-shadow:0 0 0 0 rgba(37,211,102,0)}}

  /* logo strip / marquee of services */
  .strip{border-top:1px solid var(--line);border-bottom:1px solid var(--line);background:rgba(255,255,255,.015);overflow:hidden}
  .strip .row{display:flex;gap:40px;padding:16px 0;white-space:nowrap;animation:slide 26s linear infinite;width:max-content}
  .strip span{font-family:'Space Grotesk';font-weight:600;color:#7f93ad;font-size:15px;letter-spacing:.02em;display:inline-flex;align-items:center;gap:12px}
  .strip span:before{content:"";width:6px;height:6px;border-radius:50%;background:var(--cyan)}
  @keyframes slide{to{transform:translateX(-50%)}}

  /* services grid */
  .cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(250px,1fr));gap:16px}
  .card{background:linear-gradient(180deg,var(--panel),var(--bg2));border:1px solid var(--line);border-radius:16px;padding:24px;transition:transform .25s,border-color .25s;position:relative;overflow:hidden}
  .card:hover{transform:translateY(-4px);border-color:rgba(37,195,240,.42)}
  .card .ic{width:44px;height:44px;border-radius:11px;display:grid;place-items:center;background:rgba(37,195,240,.1);border:1px solid var(--line);margin-bottom:14px}
  .card .ic svg{width:23px;height:23px;stroke:var(--cyan);fill:none;stroke-width:1.8}
  .card h3{margin:0 0 6px;font-size:18px;color:#fff}.card p{margin:0;color:var(--mut);font-size:14px}
  .card .from{margin-top:14px;font-size:13px;color:#bcccdf;font-weight:600}
  .card .from b{font-family:'Space Mono';color:var(--cyan)}

  /* quote calculator (signature) */
  .quote{background:linear-gradient(135deg,#0c1c31,#0a1626);border:1px solid var(--line);border-radius:22px;padding:0;overflow:hidden;display:grid;grid-template-columns:1.05fr .95fr}
  .quote .left{padding:34px 34px 30px}
  .quote .right{padding:34px;background:radial-gradient(500px 400px at 100% 0,rgba(37,195,240,.1),transparent 60%);border-left:1px solid var(--line);display:flex;flex-direction:column;justify-content:center}
  .quote label{display:block;font-size:12px;letter-spacing:.06em;text-transform:uppercase;color:#8fa3bd;font-weight:700;margin:0 0 8px}
  .quote select,.quote input[type=range]{width:100%}
  .quote select{background:#0a1830;color:#eaf2fb;border:1px solid var(--line);border-radius:11px;padding:14px 14px;font-size:15px;font-family:inherit;font-weight:600;-webkit-appearance:none;appearance:none;background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='8' fill='none' stroke='%2325c3f0' stroke-width='2'%3E%3Cpath d='M1 1l5 5 5-5'/%3E%3C/svg%3E");background-repeat:no-repeat;background-position:right 16px center}
  .quote select:focus{outline:none;border-color:var(--cyan)}
  .qrow{margin-bottom:20px}
  .crew-toggle{display:flex;gap:8px}
  .crew-toggle button{flex:1;background:#0a1830;color:#bcccdf;border:1px solid var(--line);border-radius:11px;padding:12px;font-weight:700;font-size:14px;cursor:pointer;font-family:inherit;transition:.15s}
  .crew-toggle button.on{background:linear-gradient(135deg,var(--cyan-dk),var(--cyan));color:#04121e;border-color:transparent}
  .q-price{font-family:'Space Mono';font-size:clamp(40px,6vw,58px);line-height:1;color:#fff;letter-spacing:-1px}
  .q-price .cur{color:var(--cyan)}
  .q-label{font-size:13px;color:#8fa3bd;font-weight:700;text-transform:uppercase;letter-spacing:.08em;margin-bottom:10px}
  .q-meta{color:#bcccdf;font-size:14px;margin:14px 0 4px;line-height:1.6}
  .q-meta b{color:#eaf2fb}
  .q-note{color:#7f93ad;font-size:12.5px;margin-top:14px;line-height:1.5}
  .q-cta{margin-top:22px}

  /* reels */
  .reels{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:16px}
  .reel{position:relative;background:#000;border:1px solid var(--line);border-radius:18px;overflow:hidden;box-shadow:0 18px 44px rgba(0,0,0,.4);cursor:pointer}
  .reel video{width:100%;aspect-ratio:9/16;object-fit:cover;background:#000;display:block}
  .reel .play{position:absolute;inset:0;display:grid;place-items:center;background:linear-gradient(transparent 55%,rgba(4,10,20,.55));transition:opacity .2s}
  .reel .play svg{width:52px;height:52px;fill:#fff;filter:drop-shadow(0 6px 14px rgba(0,0,0,.5));opacity:.95}
  .reel.playing .play{opacity:0;pointer-events:none}
  .reel .cap{position:absolute;left:12px;bottom:12px;z-index:2;font-size:12px;font-weight:700;color:#fff;background:rgba(4,14,26,.6);border:1px solid var(--line);padding:5px 10px;border-radius:999px}
  .reel.playing .cap{opacity:0}

  /* why us */
  .why{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:16px}
  .why .w{background:var(--panel);border:1px solid var(--line);border-radius:16px;padding:26px}
  .why .n{font-family:'Space Mono';color:var(--cyan);font-size:14px;font-weight:700;letter-spacing:.1em}
  .why h3{margin:12px 0 8px;font-size:19px;color:#fff}.why p{margin:0;color:var(--mut);font-size:14px}

  /* reviews */
  .rev-top{display:grid;grid-template-columns:auto 1fr;gap:18px;align-items:center;margin-bottom:26px;flex-wrap:wrap}
  .score{background:var(--panel);border:1px solid var(--line);border-radius:18px;padding:22px 26px;text-align:center;min-width:150px}
  .score b{font-family:'Space Grotesk';font-size:46px;line-height:1;color:#fff;display:block}
  .score .st{color:#ffc531;font-size:17px;letter-spacing:2px;margin:6px 0}
  .score span{color:var(--mut);font-size:13px;font-weight:600}
  .reviews-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(268px,1fr));gap:14px}
  .review{background:var(--panel);border:1px solid var(--line);border-radius:16px;padding:22px}
  .review .st{color:#ffc531;font-size:13px;letter-spacing:1px;margin-bottom:10px}
  .review p{margin:0 0 14px;color:#d2ddec;font-size:14.5px;line-height:1.55}
  .review .who{color:var(--cyan);font-size:13px;font-weight:700}

  /* coverage */
  .cov{display:grid;grid-template-columns:1.05fr .95fr;gap:24px;align-items:stretch}
  .cov-map{border-radius:20px;overflow:hidden;border:1px solid var(--line);box-shadow:0 18px 50px rgba(0,0,0,.45);min-height:380px}
  #covmap{height:100%;min-height:380px;width:100%;background:#0b1626}
  .leaflet-popup-content-wrapper,.leaflet-popup-tip{background:#0e1c31;color:#eaf2fb}
  .leaflet-container{font-family:Inter,sans-serif}
  .cov-list{display:flex;flex-direction:column;justify-content:center}
  .cov-pill{display:inline-flex;align-items:center;gap:8px;align-self:flex-start;font-weight:700;font-size:13px;color:#04121e;background:linear-gradient(135deg,var(--cyan-dk),var(--cyan));padding:9px 15px;border-radius:999px;margin-bottom:18px}
  .areas{display:flex;gap:8px;flex-wrap:wrap}
  .chip{font-size:13px;font-weight:600;border:1px solid var(--line);border-radius:999px;padding:8px 13px;color:#dbe7f6;background:rgba(255,255,255,.02)}
  .cov-note{margin-top:18px;color:var(--mut);font-size:14px}
  .pc-check{background:var(--panel);border:1px solid var(--line);border-radius:16px;padding:20px;margin-bottom:18px}
  .pc-check label{display:block;font-size:12.5px;font-weight:700;color:#8fa3bd;text-transform:uppercase;letter-spacing:.07em;margin-bottom:11px}
  .pc-row{display:flex;gap:8px}
  .pc-row input{flex:1;min-width:0;background:#0a1830;border:1px solid var(--line);border-radius:11px;padding:13px 14px;color:#eaf2fb;font-family:inherit;font-size:15px;text-transform:uppercase;letter-spacing:.04em}
  .pc-row input::placeholder{color:#5f7690;text-transform:none;letter-spacing:0}
  .pc-row input:focus{outline:none;border-color:var(--cyan)}
  .pc-row button{background:linear-gradient(135deg,var(--cyan-dk),var(--cyan));color:#04121e;border:0;border-radius:11px;padding:0 22px;font-weight:800;font-size:14px;cursor:pointer;font-family:inherit;white-space:nowrap}
  .pc-out{margin-top:14px;font-size:14px;line-height:1.55;display:none}
  .pc-out.show{display:block}
  .pc-out.yes{color:#c2f0d6}.pc-out.maybe{color:#ffe3ab}
  .pc-out b{color:#fff}
  .pc-out a.wa-link{color:var(--cyan);font-weight:700;text-decoration:none;display:inline-block;margin-top:4px}
  .pc-out a.wa-link:hover{text-decoration:underline}

  /* cta band */
  .ctaband{background:linear-gradient(135deg,#0c2036,#0a1626);border:1px solid var(--line);border-radius:24px;padding:44px;text-align:center;position:relative;overflow:hidden}
  .ctaband:before{content:"";position:absolute;inset:0;background:radial-gradient(500px 300px at 50% 0,rgba(37,195,240,.14),transparent 60%)}
  .ctaband > *{position:relative}
  .ctaband h2{font-size:clamp(26px,4vw,40px);margin:0 0 10px;color:#fff}
  .ctaband p{color:var(--mut);margin:0 0 22px}

  /* generic prose / contact */
  .contact-box{background:var(--panel);border:1px solid var(--line);border-radius:20px;padding:28px}.contact-box p{margin:10px 0;color:#d2ddec}
  .prose{color:#c6d4e6;max-width:780px}.prose h3{color:#fff;font-size:19px;margin:26px 0 8px}.prose p{margin:0 0 12px;font-size:15px;line-height:1.7}

  footer{padding:48px 24px 34px;text-align:center;color:var(--mut);border-top:1px solid var(--line);background:#060f1c}
  footer img{height:40px;margin:0 auto 14px}
  .fsoc{display:flex;gap:10px;justify-content:center;margin:16px 0}
  .fsoc a{width:38px;height:38px;border-radius:10px;display:grid;place-items:center;border:1px solid var(--line);background:rgba(255,255,255,.03)}
  .fsoc a:hover{border-color:var(--cyan)} .fsoc svg{width:18px;height:18px;fill:#cfe;opacity:.85}

  .wa-float{position:fixed;left:20px;bottom:22px;z-index:99998;width:56px;height:56px;border-radius:50%;background:#25d366;display:grid;place-items:center;box-shadow:0 12px 30px rgba(0,0,0,.4);transition:transform .2s}
  .wa-float:hover{transform:scale(1.07)}.wa-float svg{width:30px;height:30px;fill:#fff}

  .reveal{opacity:0;transform:translateY(18px);transition:opacity .7s ease,transform .7s ease}.reveal.in{opacity:1;transform:none}

  @media(max-width:900px){
    .hero-inner{grid-template-columns:1fr;gap:22px}
    .phone{width:188px;order:0;margin:2px auto 0}
    .quote{grid-template-columns:1fr}.quote .right{border-left:0;border-top:1px solid var(--line)}
    .cov{grid-template-columns:1fr}
    .links a:not(.navcta){display:none}
  }
  @media(max-width:560px){
    section{padding:44px 0}
    .hero{padding:32px 0 38px}
    .wrap{padding:0 18px}
    .phone{width:154px;padding:6px;border-radius:26px}
    .phone .notch{width:64px;height:14px;top:14px}
    .phone .live{left:11px;bottom:11px;padding:5px 9px;font-size:10.5px}
    .reels{grid-template-columns:1fr 1fr;gap:10px}
    .reel{border-radius:14px}
    .reel .cap{font-size:10.5px;left:8px;bottom:8px;padding:4px 8px}
    .reel .play svg{width:38px;height:38px}
    .strip .row{gap:26px}
    .cov-map,#covmap{min-height:260px}
  }
</style>
"""


def nav_html():
    fb = SOCIAL_SVGS["facebook"]
    tk = SOCIAL_SVGS["tiktok"]
    return f"""
<nav>
  <a class="brand" href="/"><img src="/static/images/logo.svg" alt="West Handyman"></a>
  <div class="links">
    <a href="/#services">Services</a>
    <a href="/#quote">Instant quote</a>
    <a href="/#reels">Our work</a>
    <a href="/#reviews">Reviews</a>
    <a href="/rates">Rates</a>
    <a href="/contact">Contact</a>
    <a class="navsoc" href="{BIZ['facebook']}" target="_blank" rel="noopener" aria-label="Facebook"><svg viewBox="0 0 24 24" fill="#25c3f0">{fb}</svg></a>
    <a class="navsoc" href="{BIZ['tiktok']}" target="_blank" rel="noopener" aria-label="TikTok"><svg viewBox="0 0 24 24" fill="#25c3f0">{tk}</svg></a>
    <a class="navcta" href="https://wa.me/{BIZ['wa'].lstrip('+')}" target="_blank" rel="noopener">Book on WhatsApp</a>
  </div>
</nav>
"""


def footer_html():
    fb, tk, yt = SOCIAL_SVGS["facebook"], SOCIAL_SVGS["tiktok"], SOCIAL_SVGS["youtube"]
    return f"""
<footer>
  <div class="wrap">
    <img src="/static/images/logo.svg" alt="West Handyman">
    <div class="fsoc">
      <a href="{BIZ['facebook']}" target="_blank" rel="noopener" aria-label="Facebook"><svg viewBox="0 0 24 24">{fb}</svg></a>
      <a href="{BIZ['tiktok']}" target="_blank" rel="noopener" aria-label="TikTok"><svg viewBox="0 0 24 24">{tk}</svg></a>
      <a href="{BIZ['youtube']}" target="_blank" rel="noopener" aria-label="YouTube"><svg viewBox="0 0 24 24">{yt}</svg></a>
    </div>
    <p style="margin:6px 0">{BIZ['base']} &middot; {BIZ['hours']}</p>
    <p style="margin:6px 0"><a href="tel:{BIZ['phone_tel']}">{BIZ['phone_display']}</a> &middot; <a href="mailto:{BIZ['email']}">{BIZ['email']}</a></p>
    <p style="margin:14px 0 0;font-size:12.5px;color:#6f829c">&copy; {time.strftime('%Y')} West Handyman &middot; Serving Fulham &amp; West London since {BIZ['since']} &middot; <a href="/privacy">Privacy</a></p>
  </div>
</footer>
"""


WA_FLOAT = f"""
<a class="wa-float" href="https://wa.me/{BIZ['wa'].lstrip('+')}" target="_blank" rel="noopener" aria-label="WhatsApp">
  <svg viewBox="0 0 32 32"><path d="M16 3C9 3 3.5 8.5 3.5 15.5c0 2.3.6 4.5 1.8 6.4L3 29l7.3-2.2c1.8 1 3.8 1.5 5.7 1.5 7 0 12.5-5.5 12.5-12.5S23 3 16 3zm0 22.8c-1.8 0-3.5-.5-5-1.4l-.4-.2-4.3 1.3 1.3-4.2-.3-.4a10 10 0 0 1-1.6-5.4C5.7 9.8 10.3 5.3 16 5.3s10.3 4.5 10.3 10.2S21.7 25.8 16 25.8zm5.7-7.6c-.3-.2-1.8-.9-2.1-1-.3-.1-.5-.2-.7.2s-.8 1-1 1.2c-.2.2-.4.2-.7.1-.3-.2-1.3-.5-2.5-1.5-.9-.8-1.5-1.8-1.7-2.1-.2-.3 0-.5.1-.7l.5-.6c.2-.2.2-.3.3-.5.1-.2 0-.4 0-.6l-1-2.3c-.2-.6-.5-.5-.7-.5h-.6c-.2 0-.5.1-.8.4-.3.3-1.1 1-1.1 2.6s1.1 3 1.3 3.3c.2.2 2.3 3.5 5.5 4.9.8.3 1.4.5 1.8.7.8.2 1.4.2 2 .1.6-.1 1.8-.7 2-1.4.3-.7.3-1.3.2-1.4-.1-.2-.3-.2-.6-.4z"/></svg>
</a>
"""

REVEAL_JS = """
<script>
// always open at the top on a fresh load; let #anchors (e.g. Reviews link) still work
if('scrollRestoration' in history){history.scrollRestoration='manual';}
window.addEventListener('load',function(){if(!location.hash){window.scrollTo(0,0);}});
(function(){
  var io=new IntersectionObserver(function(es){es.forEach(function(e){if(e.isIntersecting){e.target.classList.add('in');io.unobserve(e.target)}})},{threshold:.12});
  document.querySelectorAll('.reveal').forEach(function(el){io.observe(el)});
  // reels: tap to play/pause with sound; only one at a time
  document.querySelectorAll('.reel').forEach(function(r){
    var v=r.querySelector('video');
    r.addEventListener('click',function(){
      if(v.paused){document.querySelectorAll('.reel video').forEach(function(o){if(o!==v){o.pause();o.closest('.reel').classList.remove('playing')}});v.muted=false;v.play();r.classList.add('playing')}
      else{v.pause();r.classList.remove('playing')}
    });
    v.addEventListener('ended',function(){r.classList.remove('playing')});
  });
  // live open/closed status — real hours 8am–10pm, every day
  (function(){
    var bar=document.getElementById('livebar'), txt=document.getElementById('livetxt');
    if(!bar||!txt) return;
    var OPEN=8, CLOSE=22;
    function upd(){
      var n=new Date(), h=n.getHours()+n.getMinutes()/60;
      bar.classList.remove('closed');
      if(h>=OPEN && h<CLOSE){
        txt.innerHTML='<b>Open now</b> <span class="sub">· replies in minutes</span>';
      } else {
        txt.innerHTML='<b>Online 24/7</b> <span class="sub">· instant quotes anytime</span>';
      }
    }
    upd(); setInterval(upd,60000);
  })();
})();
</script>
"""


def services_cards():
    icons = {
        "tv": '<rect x="2" y="4" width="20" height="13" rx="2"/><path d="M8 21h8M12 17v4"/>',
        "mirror": '<rect x="6" y="2" width="12" height="20" rx="6"/><path d="M9 6c1.2-1 4-1 6 0"/>',
        "art": '<rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="9" cy="9" r="1.6"/><path d="M21 16l-5-5-9 9"/>',
        "shelf": '<path d="M3 7h18M3 14h18M6 7v-2M18 7v-2M8 14v-2M16 14v-2"/>',
        "flat": '<path d="M4 20V9l8-5 8 5v11M9 20v-6h6v6"/>',
        "maint": '<path d="M14.7 6.3a4 4 0 0 0-5.4 5.4L3 18l3 3 6.3-6.3a4 4 0 0 0 5.4-5.4l-2.3 2.3-2-2 2.3-2.3z"/>',
    }
    rows = [
        ("tv", "TV wall mounting", "Any size, any wall — including plasterboard and cavity. Cables tidied, perfectly level.", "tv_small"),
        ("mirror", "Mirror hanging", "Heavy or oversized mirrors fixed safe and secure, dead level, every time.", "mirror_sm"),
        ("art", "Pictures & art", "Single frames or a full gallery wall — measured, placed and balanced.", "pictures"),
        ("shelf", "Shelves & storage", "Floating shelves and runs put up straight and solid on any wall.", "shelves"),
        ("flat", "Flat-pack assembly", "Wardrobes, units, sofas — built properly and cleared away after.", "flatpack_sm"),
        ("maint", "Property maintenance", "Door handles, curtain poles, ceiling lights, resealing and odd jobs.", "general"),
    ]
    out = []
    for icon, title, desc, sid in rows:
        crew, lo, hi, plo, phi = price_for(SERVICE_BY_ID[sid])
        frm = f"from <b>£{plo}</b> ex VAT"
        out.append(
            f'<div class="card reveal"><div class="ic"><svg viewBox="0 0 24 24" stroke-linecap="round" stroke-linejoin="round">{icons[icon]}</svg></div>'
            f'<h3>{title}</h3><p>{desc}</p><div class="from">{frm}</div></div>'
        )
    return '<div class="cards">' + "".join(out) + "</div>"


def quote_options_json():
    import json
    data = []
    for s in SERVICES:
        crew, lo, hi, plo, phi = price_for(s)
        data.append({"id": s["id"], "label": s["label"], "crew": crew,
                     "lo": lo, "hi": hi, "plo": plo, "phi": phi})
    return json.dumps(data)


def quote_section():
    opts = "".join(f'<option value="{s["id"]}">{html.escape(s["label"])}</option>' for s in SERVICES)
    return f"""
<section id="quote">
  <div class="wrap">
    <div class="eyebrow reveal">Instant quote</div>
    <h2 class="sec-h reveal">Ballpark your job in ten seconds</h2>
    <p class="sec-sub reveal">Real rates, no waiting for a callback. Pick what you need and see roughly what it costs — then book the slot.</p>
    <div class="quote reveal">
      <div class="left">
        <div class="qrow">
          <label for="q-service">What needs doing?</label>
          <select id="q-service">{opts}</select>
        </div>
        <div class="qrow">
          <label>How big / heavy is it?</label>
          <div class="crew-toggle" id="q-size">
            <button data-size="std" class="on">Standard</button>
            <button data-size="big">Large / heavy / awkward</button>
          </div>
        </div>
        <p class="q-note" id="q-hint">Under 1m and under 25kg is a one-person job. Bigger, heavier or ladder work usually needs two.</p>
      </div>
      <div class="right">
        <div class="q-label">Estimated price</div>
        <div class="q-price" id="q-out"><span class="cur">£</span>98</div>
        <div class="q-meta" id="q-detail"><b>1 handyman</b> · about 1 hour</div>
        <div class="q-note">Estimate only, ex VAT. Materials (fixings, plugs) extra. 1-hour minimum, billed per 30 mins. A photo gets you an exact price.</div>
        <div class="q-cta">
          <a class="btn wa" href="https://wa.me/{BIZ['wa'].lstrip('+')}" target="_blank" rel="noopener">
            <svg viewBox="0 0 32 32"><path d="M16 3C9 3 3.5 8.5 3.5 15.5c0 2.3.6 4.5 1.8 6.4L3 29l7.3-2.2c1.8 1 3.8 1.5 5.7 1.5 7 0 12.5-5.5 12.5-12.5S23 3 16 3z"/></svg>
            Book this on WhatsApp
          </a>
        </div>
      </div>
    </div>
  </div>
</section>
<script>
(function(){{
  var SERVICES={quote_options_json()};
  var byId={{}};SERVICES.forEach(function(s){{byId[s.id]=s}});
  var sel=document.getElementById('q-service'),out=document.getElementById('q-out'),
      detail=document.getElementById('q-detail'),hint=document.getElementById('q-hint'),
      sizeWrap=document.getElementById('q-size'),size='std';
  sizeWrap.querySelectorAll('button').forEach(function(b){{
    b.addEventListener('click',function(){{sizeWrap.querySelectorAll('button').forEach(function(x){{x.classList.remove('on')}});b.classList.add('on');size=b.dataset.size;render()}});
  }});
  sel.addEventListener('change',render);
  function fmt(n){{return n.toLocaleString('en-GB')}}
  function render(){{
    var s=byId[sel.value];
    // "big" nudges toward the upper (2h / 2-crew) end of the band
    var crew=s.crew, plo=s.plo, phi=s.phi, lo=s.lo, hi=s.hi, price, mins;
    if(size==='big'){{price=phi;mins=hi; if(s.crew===1 && s.plo===s.phi){{price=Math.round(s.phi*1.4);}} }}
    else{{price=plo;mins=lo}}
    var priceTxt = (size==='big' && s.plo!==s.phi)?('£'+fmt(plo)+'–£'+fmt(phi)):('£'+fmt(price));
    out.innerHTML='<span class="cur">'+priceTxt.charAt(0)+'</span>'+priceTxt.slice(1);
    var hrs=(mins/60); var hrTxt=(hrs%1===0)?hrs+' hour'+(hrs>1?'s':''):hrs+' hours';
    detail.innerHTML='<b>'+crew+(crew>1?' handymen':' handyman')+'</b> · about '+hrTxt;
    hint.textContent = size==='big'
      ? 'Larger or heavier jobs often need two handymen or a bit longer — this is the upper end.'
      : 'Under 1m and under 25kg is usually a one-person, one-hour job.';
  }}
  render();
}})();
</script>
"""


def reels_section():
    caps = ["On the tools", "Real jobs", "Clean finishes", "Behind the scenes"]
    reels = []
    for i in range(1, 5):
        reels.append(
            f'<div class="reel reveal">'
            f'<span class="cap">{caps[i-1]}</span>'
            f'<video src="/static/videos/reel{i}.mp4" poster="/static/videos/poster{i}.jpg" preload="metadata" playsinline loop muted></video>'
            f'<div class="play"><svg viewBox="0 0 24 24"><path d="M8 5v14l11-7z"/></svg></div>'
            f'</div>'
        )
    return f"""
<section id="reels">
  <div class="wrap">
    <div class="eyebrow reveal">Our work</div>
    <h2 class="sec-h reveal">Straight from the jobs</h2>
    <p class="sec-sub reveal">A few clips from our TikTok — tap any to play. Follow <a href="{BIZ['tiktok']}" target="_blank" rel="noopener">@westhandyman</a> for more.</p>
    <div class="reels">{''.join(reels)}</div>
  </div>
</section>
"""


def reviews_section():
    cards = []
    for who, st, txt in REVIEWS:
        stars = "★" * st
        cards.append(
            f'<div class="review reveal"><div class="st">{stars}</div>'
            f'<p>{html.escape(txt)}</p><div class="who">{html.escape(who)}</div></div>'
        )
    return f"""
<section id="reviews">
  <div class="wrap">
    <div class="eyebrow reveal">Reviews</div>
    <h2 class="sec-h reveal">Rated {BIZ['rating']} by our neighbours</h2>
    <p class="sec-sub reveal">Every review comes straight from our <a href="{BIZ['google_reviews']}" target="_blank" rel="noopener">Google Business page</a>.</p>
    <div class="rev-top reveal">
      <div class="score"><b>{BIZ['rating']}</b><div class="st">★★★★★</div><span>{BIZ['reviews_count']} Google reviews</span></div>
      <div style="color:#c6d4e6;font-size:15px;max-width:520px">On time, shoe covers on, tidy up after — the little things people keep coming back for. Here's a sample of what West London says about us.</div>
    </div>
    <div class="reviews-grid">{''.join(cards)}</div>
  </div>
</section>
"""


def coverage_section():
    areas = ["Fulham", "Chelsea", "Putney", "Hammersmith", "Kensington", "Wandsworth",
             "Parsons Green", "West Kensington", "Battersea", "Earl's Court", "Barnes", "Shepherd's Bush"]
    chips = "".join(f'<span class="chip">{a}</span>' for a in areas)
    return f"""
<section id="coverage">
  <div class="wrap">
    <div class="eyebrow reveal">Coverage</div>
    <h2 class="sec-h reveal">Based in SW6, all over West London</h2>
    <p class="sec-sub reveal">Our hub is on Dawes Road in Fulham, so we're quick to reach most of West and South-West London.</p>
    <div class="cov reveal">
      <div class="cov-map"><div id="covmap"></div></div>
      <div class="cov-list">
        <div class="pc-check">
          <label>Check we cover your postcode</label>
          <div class="pc-row">
            <input id="pc-in" placeholder="e.g. SW6 7EN" autocomplete="postal-code" inputmode="text" maxlength="8"/>
            <button id="pc-btn" type="button">Check</button>
          </div>
          <div id="pc-out" class="pc-out"></div>
        </div>
        <span class="cov-pill">📍 {BIZ['base']}</span>
        <div class="areas">{chips}</div>
      </div>
    </div>
  </div>
</section>
<script>
(function(){{
  var WA="{BIZ['wa'].lstrip('+')}";
  var CORE=['SW6','SW10','SW5','SW3','SW7','W6','W14','W12','W4','W8','SW11','SW13','SW15','SW18'];
  var NEAR=['NW','WC','EC','SE','TW','KT','SM','CR','UB','HA'];
  var input=document.getElementById('pc-in');
  var out=document.getElementById('pc-out');
  if(!input) return;
  function districtOf(pc){{
    pc=(pc||'').toUpperCase().replace(/\\s+/g,'');
    if(pc.length<5) return null;                 // full UK postcode is 5-7 chars
    var outward=pc.slice(0,pc.length-3);         // inward code is always 3 chars
    if(!/^[A-Z]{{1,2}}[0-9][A-Z0-9]?$/.test(outward)) return null;
    return outward;
  }}
  function areaOf(d){{ var m=d.match(/^[A-Z]+/); return m?m[0]:''; }}
  function link(t){{ return " <a class='wa-link' href='https://wa.me/"+WA+"' target='_blank' rel='noopener'>"+t+" &rarr;</a>"; }}
  function check(){{
    var d=districtOf(input.value);
    out.className='pc-out show';
    if(!d){{ out.classList.add('maybe'); out.innerHTML="Pop your full postcode in and we'll check — e.g. <b>SW6 7EN</b>."; return; }}
    var area=areaOf(d);
    if(CORE.indexOf(d)>-1){{
      out.classList.add('yes');
      out.innerHTML="✅ <b>"+d+" &mdash; you're right in our patch.</b> We're on jobs round here most days."+link("Book a slot on WhatsApp");
    }} else if(area==='SW'||area==='W'){{
      out.classList.add('yes');
      out.innerHTML="✅ <b>"+d+" &mdash; yes, we cover you.</b> Well inside our West / South-West London area."+link("Grab a time on WhatsApp");
    }} else if(NEAR.indexOf(area)>-1){{
      out.classList.add('yes');
      out.innerHTML="✅ <b>"+d+" &mdash; we can usually reach you.</b> Send the job over and we'll confirm a slot."+link("Ask on WhatsApp");
    }} else {{
      out.classList.add('maybe');
      out.innerHTML="🤔 <b>"+d+" is a bit outside our usual patch.</b> Message us anyway &mdash; if we can make it work, we will."+link("Check with us");
    }}
  }}
  document.getElementById('pc-btn').addEventListener('click',check);
  input.addEventListener('keydown',function(e){{ if(e.key==='Enter'){{ e.preventDefault(); check(); }} }});
}})();
</script>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script>
(function(){{
  function init(){{
    if(typeof L==='undefined'){{return setTimeout(init,200)}}
    var map=L.map('covmap',{{scrollWheelZoom:false,attributionControl:false}}).setView([51.478,-0.20],12.4);
    L.tileLayer('https://{{s}}.basemaps.cartocdn.com/dark_all/{{z}}/{{x}}/{{y}}{{r}}.png',{{maxZoom:19}}).addTo(map);
    var hub=[51.4826,-0.2013];
    L.circle(hub,{{radius:3800,color:'#25c3f0',weight:1.5,fillColor:'#25c3f0',fillOpacity:.12}}).addTo(map);
    L.marker(hub).addTo(map).bindPopup('<b>West Handyman</b><br>Dawes Road Hub, SW6 7EN');
  }}
  init();
}})();
</script>
"""


def cta_band():
    return f"""
<section>
  <div class="wrap">
    <div class="ctaband reveal">
      <h2>Got a job in mind?</h2>
      <p>Ask the assistant for a price, send a photo, or WhatsApp us — we reply fast, {BIZ['hours']}.</p>
      <div class="btns" style="justify-content:center">
        <button class="btn" onclick="if(window.WHM)WHM.open()">💬 Ask for a price</button>
        <a class="btn wa" href="https://wa.me/{BIZ['wa'].lstrip('+')}" target="_blank" rel="noopener">WhatsApp {BIZ['phone_display']}</a>
        <a class="btn ghost" href="tel:{BIZ['phone_tel']}">Call us</a>
      </div>
    </div>
  </div>
</section>
"""


# ---------------------------------------------------------------------------
#  Chat widget (floating bubble + panel) - embeddable on any page
# ---------------------------------------------------------------------------
WIDGET = f"""
<div id="whm-launcher" onclick="WHM.toggle()" aria-label="Chat with West Handyman">
  <svg class="wl-chat" viewBox="0 0 24 24" fill="#04121e"><path d="M12 3C6.5 3 2 6.8 2 11.5c0 2.3 1.1 4.4 2.9 5.9L4 21l4-1.6c1.2.4 2.6.6 4 .6 5.5 0 10-3.8 10-8.5S17.5 3 12 3z"/></svg>
  <svg class="wl-close" viewBox="0 0 24 24" fill="#04121e" style="display:none"><path d="M6 6l12 12M18 6L6 18" stroke="#04121e" stroke-width="2.4" stroke-linecap="round"/></svg>
</div>
<div id="whm-panel" role="dialog" aria-label="West Handyman assistant">
  <div class="whm-head">
    <div class="whm-ava"><img src="/static/images/logo.svg" alt=""></div>
    <div class="whm-h-txt">
      <b>West Handyman</b>
      <span><i></i> Usually replies in seconds</span>
    </div>
    <button class="whm-min" onclick="WHM.toggle()" aria-label="Close">–</button>
  </div>
  <div id="whm-box" class="whm-box"></div>
  <div class="whm-quick" id="whm-quick"></div>
  <div class="whm-input">
    <label class="whm-clip" title="Send a photo">
      <input type="file" accept="image/*" onchange="WHM.files(this)" hidden>
      <svg viewBox="0 0 24 24" fill="none" stroke="#8fa3bd" stroke-width="1.8" stroke-linecap="round"><path d="M21 12.5l-8.5 8.5a5 5 0 0 1-7-7l9-9a3.3 3.3 0 0 1 4.7 4.7l-9 9a1.6 1.6 0 0 1-2.3-2.3l8.5-8.5"/></svg>
    </label>
    <input type="text" id="whm-in" placeholder="Describe your job…" autocomplete="off"
           onkeydown="if(event.key==='Enter')WHM.send()">
    <input type="text" id="whm-hp" tabindex="-1" autocomplete="off" style="position:absolute;left:-9999px" aria-hidden="true">
    <button class="whm-send" onclick="WHM.send()" aria-label="Send">
      <svg viewBox="0 0 24 24" fill="#04121e"><path d="M3 11l18-8-8 18-2-7-8-3z"/></svg>
    </button>
  </div>
</div>
<style>
  #whm-launcher{{position:fixed;right:20px;bottom:22px;z-index:99999;width:60px;height:60px;border-radius:50%;background:linear-gradient(135deg,var(--cyan-dk),var(--cyan));display:grid;place-items:center;cursor:pointer;box-shadow:0 14px 34px rgba(37,195,240,.4);transition:transform .2s}}
  #whm-launcher:hover{{transform:scale(1.06)}} #whm-launcher svg{{width:28px;height:28px}}
  #whm-panel{{position:fixed;right:20px;bottom:92px;z-index:99999;width:378px;max-width:calc(100vw - 32px);height:600px;max-height:calc(100vh - 120px);background:#0b1626;border:1px solid var(--line);border-radius:20px;overflow:hidden;display:none;flex-direction:column;box-shadow:0 30px 80px rgba(0,0,0,.55)}}
  #whm-panel.open{{display:flex;animation:whmpop .22s ease}}
  @keyframes whmpop{{from{{opacity:0;transform:translateY(12px)}}to{{opacity:1;transform:none}}}}
  .whm-head{{display:flex;align-items:center;gap:11px;padding:14px 14px;background:linear-gradient(135deg,#0c1c31,#0a1626);border-bottom:1px solid var(--line)}}
  .whm-ava{{width:40px;height:40px;border-radius:11px;background:#04121e;display:grid;place-items:center;border:1px solid var(--line);overflow:hidden}}
  .whm-ava img{{width:32px}}
  .whm-h-txt{{display:flex;flex-direction:column;line-height:1.3}} .whm-h-txt b{{color:#fff;font-size:15px;font-family:'Space Grotesk'}}
  .whm-h-txt span{{color:#8fa3bd;font-size:12px;display:flex;align-items:center;gap:6px}}
  .whm-h-txt i{{width:7px;height:7px;border-radius:50%;background:#25d366}}
  .whm-min{{margin-left:auto;background:none;border:0;color:#8fa3bd;font-size:24px;cursor:pointer;line-height:1;padding:0 6px}}
  .whm-box{{flex:1;overflow-y:auto;padding:16px;display:flex;flex-direction:column;gap:10px;background:radial-gradient(500px 400px at 100% 0,rgba(37,195,240,.05),transparent 60%)}}
  .whm-msg{{max-width:82%;padding:11px 14px;border-radius:15px;font-size:14.5px;line-height:1.5;white-space:pre-wrap;word-wrap:break-word}}
  .whm-msg.bot{{align-self:flex-start;background:#12233c;color:#e7f0fb;border:1px solid var(--line);border-bottom-left-radius:5px}}
  .whm-msg.user{{align-self:flex-end;background:linear-gradient(135deg,var(--cyan-dk),var(--cyan));color:#04121e;font-weight:600;border-bottom-right-radius:5px}}
  .whm-msg.photo{{padding:5px}} .whm-msg.photo img{{border-radius:10px;max-width:180px}}
  .whm-typing{{align-self:flex-start;background:#12233c;border:1px solid var(--line);border-radius:15px;border-bottom-left-radius:5px;padding:13px 16px;display:flex;gap:4px}}
  .whm-typing span{{width:7px;height:7px;border-radius:50%;background:#5b7a9c;animation:whmb 1.2s infinite}}
  .whm-typing span:nth-child(2){{animation-delay:.2s}} .whm-typing span:nth-child(3){{animation-delay:.4s}}
  @keyframes whmb{{0%,60%,100%{{opacity:.3;transform:translateY(0)}}30%{{opacity:1;transform:translateY(-4px)}}}}
  .whm-quick{{display:flex;gap:8px;padding:0 14px 10px;flex-wrap:wrap}}
  .whm-quick button{{background:rgba(37,195,240,.08);border:1px solid var(--line);color:#bcccdf;font-size:12.5px;font-weight:600;padding:8px 12px;border-radius:999px;cursor:pointer;font-family:inherit;transition:.15s}}
  .whm-quick button:hover{{background:rgba(37,195,240,.16);color:#fff}}
  .whm-input{{display:flex;align-items:center;gap:8px;padding:12px;border-top:1px solid var(--line);background:#0a1626;position:relative}}
  .whm-clip{{cursor:pointer;display:grid;place-items:center;width:36px;height:36px;border-radius:9px;flex:none}} .whm-clip:hover{{background:rgba(255,255,255,.04)}} .whm-clip svg{{width:20px;height:20px}}
  #whm-in{{flex:1;background:#0e1c31;border:1px solid var(--line);border-radius:11px;padding:12px 14px;color:#eaf2fb;font-size:14.5px;font-family:inherit}}
  #whm-in:focus{{outline:none;border-color:var(--cyan)}}
  .whm-send{{width:40px;height:40px;border-radius:11px;border:0;background:linear-gradient(135deg,var(--cyan-dk),var(--cyan));display:grid;place-items:center;cursor:pointer;flex:none}} .whm-send svg{{width:20px;height:20px}}
</style>
<script>
window.WHM=(function(){{
  var box,panel,launcher,started=false,busy=false;
  var QUICKS=["Mount my TV","Hang a heavy mirror","Put up shelves","How much for flat-pack?"];
  function el(id){{return document.getElementById(id)}}
  function open(){{panel.classList.add('open');el('whm-launcher').querySelector('.wl-chat').style.display='none';el('whm-launcher').querySelector('.wl-close').style.display='block';if(!started){{started=true;greet()}};setTimeout(function(){{el('whm-in').focus()}},250)}}
  function close(){{panel.classList.remove('open');el('whm-launcher').querySelector('.wl-chat').style.display='block';el('whm-launcher').querySelector('.wl-close').style.display='none'}}
  function toggle(){{panel.classList.contains('open')?close():open()}}
  function add(text,who){{var d=document.createElement('div');d.className='whm-msg '+who;d.textContent=text;box.appendChild(d);box.scrollTop=box.scrollHeight;return d}}
  function addImg(src){{var d=document.createElement('div');d.className='whm-msg user photo';var i=new Image();i.src=src;d.appendChild(i);box.appendChild(d);box.scrollTop=box.scrollHeight}}
  function typing(){{var d=document.createElement('div');d.className='whm-typing';d.id='whm-typ';d.innerHTML='<span></span><span></span><span></span>';box.appendChild(d);box.scrollTop=box.scrollHeight}}
  function untyp(){{var t=el('whm-typ');if(t)t.remove()}}
  function quicks(){{var q=el('whm-quick');q.innerHTML='';QUICKS.forEach(function(t){{var b=document.createElement('button');b.textContent=t;b.onclick=function(){{el('whm-in').value=t;send()}};q.appendChild(b)}})}}
  function greet(){{typing();setTimeout(function(){{untyp();add("Hiya 👋 I'm the West Handyman assistant. Tell me what needs doing — TV, mirror, shelves, flat-pack, a repair — and I'll give you a price and get you booked in.",'bot');quicks()}},600)}}
  async function send(){{
    var inp=el('whm-in'),m=inp.value.trim();if(!m||busy)return;
    el('whm-quick').innerHTML='';add(m,'user');inp.value='';busy=true;typing();
    try{{
      var r=await fetch('/chat',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{message:m,website:el('whm-hp').value||''}}),credentials:'same-origin'}});
      var d=await r.json();untyp();add(d.reply,'bot');
    }}catch(e){{untyp();add("Sorry, that didn't send — mind trying again? Or WhatsApp us on {BIZ['phone_display']}.",'bot')}}
    busy=false;
  }}
  function resize(file){{return new Promise(function(res,rej){{var rd=new FileReader();rd.onload=function(){{var im=new Image();im.onload=function(){{var mx=1600,w=im.naturalWidth,h=im.naturalHeight;if(Math.max(w,h)>mx){{if(w>=h){{h=Math.round(h*mx/w);w=mx}}else{{w=Math.round(w*mx/h);h=mx}}}}var c=document.createElement('canvas');c.width=w;c.height=h;var x=c.getContext('2d');x.fillStyle='#fff';x.fillRect(0,0,w,h);x.drawImage(im,0,0,w,h);res(c.toDataURL('image/jpeg',.82))}};im.onerror=rej;im.src=rd.result}};rd.onerror=rej;rd.readAsDataURL(file)}})}}
  async function files(input){{
    var fs=Array.from(input.files||[]);input.value='';if(!started){{open()}}
    for(const f of fs){{if(!f.type.startsWith('image/')){{add('Please pick a photo file (JPG or PNG).','bot');continue}}
      try{{var url=await resize(f);addImg(url);typing();
        var r=await fetch('/upload',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{image:url}}),credentials:'same-origin'}});
        var d=await r.json();untyp();add(d.reply,'bot');
      }}catch(e){{untyp();add('Could not upload that one — try another photo?','bot')}}
    }}
  }}
  function init(){{box=el('whm-box');panel=el('whm-panel');launcher=el('whm-launcher')}}
  document.addEventListener('DOMContentLoaded',init);
  return {{toggle:toggle,open:open,close:close,send:send,files:files}};
}})();
</script>
"""


# ---------------------------------------------------------------------------
#  Pages
# ---------------------------------------------------------------------------
def page(body, title="West Handyman — Fulham & West London Handyman"):
    return (
        f'<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">'
        f'<meta name="viewport" content="width=device-width,initial-scale=1">'
        f'<title>{title}</title>{BASE_STYLE}'
        f'<noscript><style>.reveal{{opacity:1!important;transform:none!important}}</style></noscript>'
        f'</head><body>'
        f'{nav_html()}{body}{footer_html()}{WA_FLOAT}{WIDGET}{REVEAL_JS}'
        f'</body></html>'
    )


def home_body():
    fb, tk = SOCIAL_SVGS["facebook"], SOCIAL_SVGS["tiktok"]
    strip_items = ["TV mounting", "Mirror hanging", "Picture & art walls", "Shelving",
                   "Flat-pack assembly", "Curtain poles", "Ceiling lights", "Resealing",
                   "Door handles", "Property maintenance"]
    strip = "".join(f"<span>{s}</span>" for s in strip_items)
    hero = f"""
<div class="hero">
  <div class="wrap hero-inner">
    <div>
      <div class="eyebrow">Fulham &amp; West London · since {BIZ['since']}</div>
      <div class="livebar" id="livebar"><span class="dot"></span><span id="livetxt">Checking hours…</span></div>
      <h1>Everything, <span class="g">up on the wall.</span><br>Done clean, done right.</h1>
      <p class="lede">TVs, mirrors, art, shelves, flat-pack and property fixes across West London — by a team that turns up on time, wears shoe covers and hoovers up after. Get a price in seconds.</p>
      <div class="btns">
        <button class="btn" onclick="WHM.open()">💬 Get an instant price</button>
        <a class="btn wa" href="https://wa.me/{BIZ['wa'].lstrip('+')}" target="_blank" rel="noopener">WhatsApp us</a>
      </div>
      <div class="trust">
        <span class="stars">★★★★★</span><span><b>{BIZ['rating']}</b> on Google ({BIZ['reviews_count']} reviews)</span>
        <span>·</span><span><b>{BIZ['hours']}</b></span>
        <span>·</span><span>No parking fees</span>
      </div>
    </div>
    <div class="phone">
      <div class="notch"></div>
      <video src="/static/videos/reel1.mp4" poster="/static/videos/poster1.jpg" autoplay muted loop playsinline></video>
      <span class="live"><i></i> On the tools</span>
    </div>
  </div>
</div>
<div class="strip"><div class="row">{strip}{strip}</div></div>
"""
    services = f"""
<section id="services">
  <div class="wrap">
    <div class="eyebrow reveal">What we do</div>
    <h2 class="sec-h reveal">One team for the whole to-do list</h2>
    <p class="sec-sub reveal">From a single picture hook to a full flat of fixes — no job too big or too small.</p>
    {services_cards()}
  </div>
</section>
"""
    return (hero + services + quote_section() + reels_section()
            + why_section() + reviews_section() + coverage_section() + cta_band())


def why_section():
    items = [
        (".01", "Attitude", "On time, shoe covers on, cordless vacuum out. The little things that leave your place better than we found it."),
        (".02", "Experience", f"Serving West London since {BIZ['since']}. We've done a job like yours many times over — that's why people rebook."),
        (".03", "The right tools", "Powerful drills and titanium-coated bits, so hard walls, plasterboard and awkward fixings are no problem."),
    ]
    ws = "".join(
        f'<div class="w reveal"><div class="n">{n}</div><h3>{t}</h3><p>{d}</p></div>'
        for n, t, d in items
    )
    return f"""
<section id="why" style="background:linear-gradient(180deg,#081221,#0a1727)">
  <div class="wrap">
    <div class="eyebrow reveal">Why West Handyman</div>
    <h2 class="sec-h reveal">Booked once, kept on speed-dial</h2>
    <p class="sec-sub reveal">Reliable, tidy and straight with you on price. Here's what that looks like.</p>
    <div class="why">{ws}</div>
  </div>
</section>
"""


SERVICES_DETAIL = [
    ("TV wall mounting", "We mount any TV on any wall — solid, brick or plasterboard/cavity — with the right fixings for the weight. Cables tidied or concealed, screen dead level, tested before we leave."),
    ("Mirror & glass hanging", "Heavy and oversized mirrors fixed securely with concealed, load-rated fixings. Perfectly level, safe on the wall, and no mess left behind."),
    ("Pictures & art walls", "One frame or a whole gallery wall — measured, spaced and balanced so it looks right. Good-quality fixings every time."),
    ("Shelves & storage", "Floating shelves and full runs put up straight and solid, from a single bracket to a whole alcove of storage."),
    ("Flat-pack assembly", "Wardrobes, drawers, units, sofas and more — assembled properly, fixed to the wall where needed, packaging cleared away."),
    ("Curtain poles & blinds", "Poles, tracks and blinds fitted level and secure, ready to hang the same day."),
    ("Ceiling lights & fixtures", "Light fittings and fixtures removed and installed, including ladder and height work with two handymen."),
    ("Property maintenance", "Door shaving, door-handle fixing, bathtub and shower resealing, bulb changes, lifting and moving — the odd-jobs list, sorted in one visit."),
]


def services_page_body():
    rows = "".join(
        f'<div class="card reveal" style="padding:26px"><h3>{html.escape(t)}</h3><p style="margin-top:8px">{html.escape(d)}</p></div>'
        for t, d in SERVICES_DETAIL
    )
    return f"""
<section>
  <div class="wrap narrow">
    <div class="eyebrow reveal">Services</div>
    <h1 class="sec-h reveal" style="font-size:clamp(30px,5vw,46px)">Everything a good handyman should do</h1>
    <p class="sec-sub reveal">Wall to wall, and well beyond it. If it's on your list, ask us — the answer's usually yes.</p>
    <div class="cards" style="grid-template-columns:1fr 1fr">{rows}</div>
    <div style="margin-top:34px" class="btns">
      <button class="btn" onclick="WHM.open()">💬 Price up your job</button>
      <a class="btn wa" href="https://wa.me/{BIZ['wa'].lstrip('+')}" target="_blank" rel="noopener">WhatsApp us</a>
    </div>
  </div>
</section>
{quote_section()}
"""


def rates_page_body():
    rows = []
    for s in SERVICES:
        crew, lo, hi, plo, phi = price_for(s)
        price = f"£{plo}" if plo == phi else f"£{plo}–£{phi}"
        span = f"{lo//60}h" if lo == hi else f"{lo//60}–{hi//60}h"
        rows.append(
            f'<tr><td style="padding:14px 16px;border-bottom:1px solid var(--line);color:#eaf2fb;font-weight:600">{html.escape(s["label"])}</td>'
            f'<td style="padding:14px 16px;border-bottom:1px solid var(--line);color:#bcccdf;white-space:nowrap">{crew} · {span}</td>'
            f'<td style="padding:14px 16px;border-bottom:1px solid var(--line);color:var(--cyan);font-family:\'Space Mono\';white-space:nowrap;text-align:right">{price}</td></tr>'
        )
    tier1 = RATE_PER_30[1]; tier2 = RATE_PER_30[2]
    return f"""
<section>
  <div class="wrap narrow">
    <div class="eyebrow reveal">Rates</div>
    <h1 class="sec-h reveal" style="font-size:clamp(30px,5vw,46px)">Clear, fair, no surprises</h1>
    <p class="sec-sub reveal">Priced by the half hour, billed honestly, materials shown separately. All prices ex VAT.</p>

    <div class="cards reveal" style="grid-template-columns:1fr 1fr;margin-bottom:30px">
      <div class="card" style="padding:28px">
        <div class="eyebrow">One handyman</div>
        <div class="q-price mono" style="font-size:44px;margin:10px 0">£{tier1}<span style="font-size:15px;color:#8fa3bd"> / 30 min</span></div>
        <p style="color:#bcccdf;font-size:14px">Single-person jobs — pictures, mirrors and shelves up to 1m, items up to 25kg, bulb changes, resealing, curtain poles, door handles.</p>
      </div>
      <div class="card" style="padding:28px">
        <div class="eyebrow">Two handymen</div>
        <div class="q-price mono" style="font-size:44px;margin:10px 0">£{tier2}<span style="font-size:15px;color:#8fa3bd"> / 30 min</span></div>
        <p style="color:#bcccdf;font-size:14px">Bigger jobs — over 1m or 25kg, ceiling lights, ladder work, wardrobes and sofas. Faster with two.</p>
      </div>
    </div>

    <div class="card reveal" style="padding:8px 8px 4px;overflow-x:auto">
      <table style="width:100%;border-collapse:collapse">
        <thead><tr>
          <th style="text-align:left;padding:12px 16px;color:#8fa3bd;font-size:12px;letter-spacing:.06em;text-transform:uppercase">Typical job</th>
          <th style="text-align:left;padding:12px 16px;color:#8fa3bd;font-size:12px;letter-spacing:.06em;text-transform:uppercase">Crew · time</th>
          <th style="text-align:right;padding:12px 16px;color:#8fa3bd;font-size:12px;letter-spacing:.06em;text-transform:uppercase">Estimate</th>
        </tr></thead>
        <tbody>{''.join(rows)}</tbody>
      </table>
    </div>

    <p class="cov-note reveal" style="margin-top:18px">Minimum booking 1 hour, then billed per 30 minutes. Materials (fixings, plugs, screws) charged separately. Pay on completion by card — interest-free instalments via Clearpay. No parking fees. Prices are estimates; send a photo for an exact quote.</p>

    <div style="margin-top:26px" class="btns reveal">
      <button class="btn" onclick="WHM.open()">💬 Get my exact price</button>
      <a class="btn wa" href="https://wa.me/{BIZ['wa'].lstrip('+')}" target="_blank" rel="noopener">Book on WhatsApp</a>
    </div>
  </div>
</section>
{quote_section()}
"""


def contact_page_body():
    fb, tk, yt = SOCIAL_SVGS["facebook"], SOCIAL_SVGS["tiktok"], SOCIAL_SVGS["youtube"]
    return f"""
<section>
  <div class="wrap narrow">
    <div class="eyebrow reveal">Contact</div>
    <h1 class="sec-h reveal" style="font-size:clamp(30px,5vw,46px)">Let's get it booked in</h1>
    <p class="sec-sub reveal">Fastest way to reach us is WhatsApp or the chat — a real slot, usually same week.</p>
    <div class="cov reveal">
      <div class="contact-box">
        <p><b style="color:#fff">📞 Phone</b><br><a href="tel:{BIZ['phone_tel']}">{BIZ['phone_display']}</a></p>
        <p><b style="color:#fff">💬 WhatsApp</b><br><a href="https://wa.me/{BIZ['wa'].lstrip('+')}" target="_blank" rel="noopener">Message us on WhatsApp</a></p>
        <p><b style="color:#fff">✉️ Email</b><br><a href="mailto:{BIZ['email']}">{BIZ['email']}</a></p>
        <p><b style="color:#fff">📍 Hub</b><br>{BIZ['base']}</p>
        <p><b style="color:#fff">🕗 Hours</b><br>{BIZ['hours']}</p>
        <div class="fsoc" style="justify-content:flex-start">
          <a href="{BIZ['facebook']}" target="_blank" rel="noopener" aria-label="Facebook"><svg viewBox="0 0 24 24">{fb}</svg></a>
          <a href="{BIZ['tiktok']}" target="_blank" rel="noopener" aria-label="TikTok"><svg viewBox="0 0 24 24">{tk}</svg></a>
          <a href="{BIZ['youtube']}" target="_blank" rel="noopener" aria-label="YouTube"><svg viewBox="0 0 24 24">{yt}</svg></a>
        </div>
      </div>
      <div class="contact-box" style="display:flex;flex-direction:column;justify-content:center;text-align:center">
        <h3 style="font-family:'Space Grotesk';color:#fff;font-size:22px;margin:0 0 8px">Prefer to just ask?</h3>
        <p style="color:#bcccdf">Tell the assistant what you need — it'll price it up, take a photo, and pass your details straight to the team.</p>
        <div style="margin-top:16px"><button class="btn" onclick="WHM.open()">💬 Open the assistant</button></div>
      </div>
    </div>
  </div>
</section>
"""


def privacy_page_body():
    return f"""
<section>
  <div class="wrap narrow prose">
    <div class="eyebrow">Privacy</div>
    <h1 class="sec-h" style="font-size:clamp(28px,4vw,40px)">Privacy policy</h1>
    <p>West Handyman ("we") respects your privacy. This page explains what the website assistant collects and why.</p>
    <h3>What we collect</h3>
    <p>When you chat with our website assistant or send a photo, we collect only what you choose to share — such as your name, phone number, email, postcode, a description of the job and any photos — so we can quote and arrange the work.</p>
    <h3>How we use it</h3>
    <p>Your enquiry is emailed to our team so we can get back to you. We don't sell your data or use it for anything other than responding to your enquiry and carrying out the work.</p>
    <h3>Photos</h3>
    <p>Photos you attach are used only to understand and quote your job, and are shared only within our team.</p>
    <h3>Contact</h3>
    <p>To ask what we hold or request deletion, email <a href="mailto:{BIZ['email']}">{BIZ['email']}</a> or call {BIZ['phone_display']}.</p>
  </div>
</section>
"""


# ---------------------------------------------------------------------------
#  Routes
# ---------------------------------------------------------------------------
def ensure_session():
    if "session_id" not in session:
        session["session_id"] = str(uuid.uuid4())


@app.route("/")
def home():
    ensure_session()
    return render_template_string(page(home_body()))


@app.route("/services")
def services():
    ensure_session()
    return render_template_string(page(services_page_body(), "Services — West Handyman"))


@app.route("/rates")
def rates():
    ensure_session()
    return render_template_string(page(rates_page_body(), "Rates — West Handyman"))


@app.route("/contact")
def contact():
    ensure_session()
    return render_template_string(page(contact_page_body(), "Contact — West Handyman"))


@app.route("/privacy")
@app.route("/privacy-policy")
def privacy():
    ensure_session()
    return render_template_string(page(privacy_page_body(), "Privacy — West Handyman"))


@app.route("/quote", methods=["GET"])
def quote_api():
    """Machine-readable version of the rate estimator (handy for the other site too)."""
    return jsonify({
        "rate_per_30": RATE_PER_30,
        "min_minutes": MIN_MINUTES,
        "services": [
            dict(zip(("id", "label", "crew", "minutes_lo", "minutes_hi", "price_lo", "price_hi"),
                     (s["id"], s["label"]) + price_for(s)[:1] + price_for(s)[1:]))
            for s in SERVICES
        ],
    })


@app.route("/diag")
def diag():
    """Temporary self-check. Visit /diag to see if the chat bot can reach Groq.
    Reveals only whether keys are PRESENT (never the key values)."""
    import groq as _g
    info = {
        "groq_key_present": bool(os.environ.get("GROQ_API_KEY")),
        "resend_key_present": bool(RESEND_API_KEY),
        "groq_sdk_version": getattr(_g, "__version__", "unknown"),
        "model": MODEL,
    }
    try:
        r = client_chat(
            model=MODEL,
            messages=[{"role": "user", "content": "Reply with the single word: ok"}],
            max_tokens=5,
            temperature=0,
            timeout=20,
        )
        info["groq_call"] = "SUCCESS"
        info["groq_reply"] = (r.choices[0].message.content or "").strip()
    except Exception as e:
        info["groq_call"] = "FAILED"
        info["error_type"] = type(e).__name__
        info["error_message"] = str(e)[:400]
    return jsonify(info)


@app.route("/sitemap.xml")
def sitemap():
    pages = ["/", "/services", "/rates", "/contact", "/privacy-policy"]
    base = BIZ["site"]
    urls = "".join(f"<url><loc>{base}{p}</loc></url>" for p in pages)
    xml = f'<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{urls}</urlset>'
    return Response(xml, mimetype="application/xml")


@app.route("/robots.txt")
def robots():
    return Response(f"User-agent: *\nAllow: /\nSitemap: {BIZ['site']}/sitemap.xml", mimetype="text/plain")


@app.route("/chat", methods=["POST"])
def chat_endpoint():
    session_id = session.get("session_id")
    if not session_id:
        session_id = str(uuid.uuid4())
        session["session_id"] = session_id

    if session_id not in all_conversations:
        all_conversations[session_id] = [{"role": "system", "content": SYSTEM_PROMPT}]
    conversation = all_conversations[session_id]

    data = request.get_json(silent=True) or {}

    # Honeypot
    if (data.get("website") or "").strip():
        return jsonify({"reply": "Thanks!"})

    user_message = (data.get("message") or "").strip()
    if not user_message:
        return jsonify({"reply": "Sorry, I didn't catch that — mind typing it again?"})

    now = time.time()
    recent = [t for t in chat_activity.get(session_id, []) if now - t < 60]
    if len(recent) >= 20:
        return jsonify({"reply": "You're sending those very fast — give it a few seconds and try again."})
    if len(conversation) >= 60:
        return jsonify({"reply": f"Thanks for all the detail! Pop your name and number here and the team will pick this up, or WhatsApp {BIZ['phone_display']}."})
    recent.append(now)
    chat_activity[session_id] = recent

    conversation.append({"role": "user", "content": user_message})

    try:
        response = client_chat(
            model=MODEL,
            messages=conversation,
            max_tokens=270,
            temperature=0.6,
            timeout=20,
        )
        ai_reply = response.choices[0].message.content or ""
    except Exception as e:
        print(f"Chat completion failed: {e}")
        conversation.pop()
        return jsonify({"reply": "Sorry, had a brief hiccup there — could you send that again?"})

    # defensive: strip any stray reasoning markers some models emit
    ai_reply = re.sub(r"<think>.*?</think>", "", ai_reply, flags=re.I | re.S).strip()
    lead_ready = bool(re.search(r"\[\[?\s*READY\s*\]?\]", ai_reply, re.I))
    ai_reply = re.sub(r"\[\[?\s*READY\s*\]?\]", "", ai_reply)
    ai_reply = ai_reply.replace("[LEAD_CAPTURED]", "").strip()
    if not ai_reply:
        ai_reply = ("Great — that's everything we need. The team will confirm a slot shortly. "
                    f"For the fastest reply you can also WhatsApp {BIZ['phone_display']}.")

    conversation.append({"role": "assistant", "content": ai_reply})

    if session_id not in notified_sessions and has_contact_info(conversation):
        if lead_ready or _looks_like_closing(user_message) or len(conversation) >= 24:
            notified_sessions.add(session_id)
            send_lead_email(list(conversation), list(session_images.get(session_id, [])))

    return jsonify({"reply": ai_reply})


@app.route("/upload", methods=["POST"])
def upload_endpoint():
    session_id = session.get("session_id")
    if not session_id:
        session_id = str(uuid.uuid4())
        session["session_id"] = session_id

    if session_id not in all_conversations:
        all_conversations[session_id] = [{"role": "system", "content": SYSTEM_PROMPT}]
    conversation = all_conversations[session_id]

    data = request.get_json(silent=True) or {}
    image = _decode_image_data_url(data.get("image", ""))
    if image is None:
        return jsonify({"reply": "Sorry, I couldn't read that image. A JPG or PNG works best."}), 400

    images = session_images.setdefault(session_id, [])
    if len(images) >= MAX_IMAGES_PER_SESSION:
        return jsonify({"reply": "That's plenty of photos, thanks! Pop your name and number here and we'll take a look and price it up."})

    images.append(image)
    conversation.append({"role": "user", "content": "(Customer attached a photo of the job)"})
    reply = ("Got the photo, thanks — that really helps. Add another if you like, or tell me it's "
             "all there and I'll carry on with your quote.")
    conversation.append({"role": "assistant", "content": reply})

    if session_id in notified_sessions:
        send_photo_followup(list(conversation), [image])

    return jsonify({"reply": reply})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5001))
    app.run(host="0.0.0.0", port=port)
