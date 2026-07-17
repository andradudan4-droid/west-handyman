# West Handyman — website + AI booking assistant

Flask site with a Groq-powered chat assistant, an instant quote calculator, the
TikTok reels, and email lead capture via Resend. Same stack as your other builds
(Flask / Groq / Resend / Render / GitHub).

## Run locally

```bash
pip install -r requirements.txt
export GROQ_API_KEY=your_key
python app.py           # http://localhost:5001
```

The site works without any keys (you just won't get AI replies or lead emails).

## Environment variables (set these in Render → Environment)

| Variable         | What it's for                                   | Example |
|------------------|-------------------------------------------------|---------|
| `GROQ_API_KEY`   | Powers the chat assistant                        | `gsk_...` |
| `RESEND_API_KEY` | Sends lead emails over HTTPS                      | `re_...` |
| `NOTIFY_TO`      | Where leads land                                 | `contact@westhandyman.com` |
| `RESEND_FROM`    | Verified sender (your verified frontdesk domain) | `West Handyman <leads@frontdesk.org.uk>` |
| `SECRET_KEY`     | Flask session secret                             | any long random string |

`RESEND_FROM` must use a domain you've verified in Resend (your `frontdesk.org.uk`
works). Only the display name changes per client.

## Deploy on Render

- Build command: `pip install -r requirements.txt`
- Start command: `gunicorn app:app` (recommended) or `python app.py`
- Render sets `PORT` automatically — the app already reads it.

## What's inside

- **Instant quote calculator** — grounded in the real published rates (£49/£69
  per 30 min). Pick a job + size → live estimate. The bot quotes from the *same*
  rate table (`SERVICES` / `RATE_PER_30` in `app.py`), so the site and the
  assistant never disagree.
- **AI assistant** — tuned for handyman jobs: asks the right sizing/wall
  questions, gives a ballpark, takes photos (paperclip), captures the lead and
  emails it to you. Lead trigger is server-side (real phone/email detected), not
  reliant on the AI.
- **Reels** — your four TikTok clips, tap to play with sound. Hero plays one
  muted on loop in a phone frame. Files live in `static/videos/`.
- **Coverage map** (Leaflet, dark theme), reviews (4.7★), rates page, services,
  contact, privacy, sitemap + robots.

## Reusing this for his second business

Almost everything a second site needs to change lives at the top of `app.py`:
the `BIZ` dictionary (name, contact, socials), the `SERVICES` list + `RATE_PER_30`
(the quote engine), the `SYSTEM_PROMPT`, and the brand colour `--cyan` in
`BASE_STYLE`. Swap those, drop in new videos/logo, and you've got the second
site without rebuilding the plumbing.

## Files

```
app.py                     the whole app (pages, bot, calculator, email)
requirements.txt
static/
  images/logo.svg          brand wordmark (recreated as clean SVG)
  images/favicon.png
  videos/reel1..4.mp4       the TikTok clips (hero uses reel1)
  videos/poster1..4.jpg     poster frames
```
