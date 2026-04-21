#!/usr/bin/env python3
"""
Personal Daily News Digest
Fetches RSS headlines, summarizes with Groq AI, renders a browser dashboard,
and sends a condensed briefing via iMessage (Mac) and/or email.

All user-facing configuration lives in  config.json  (see config.sample.json).
"""

import os, sys, json, re, datetime, subprocess, webbrowser, html, time, smtplib, platform
from email.message import EmailMessage
from pathlib import Path
from zoneinfo import ZoneInfo

import feedparser
import requests

# ───────────────────────────────────────────────
#  CONFIG LOADING
# ───────────────────────────────────────────────
HERE        = Path(__file__).parent
CONFIG_PATH = HERE / "config.json"
SAMPLE_PATH = HERE / "config.sample.json"
OUTPUT_FILE = HERE / "dashboard.html"


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        print(
            f"\n✗  config.json not found at {CONFIG_PATH}\n"
            f"   Copy the sample and fill in your values:\n"
            f"     cp {SAMPLE_PATH.name} {CONFIG_PATH.name}\n",
            file=sys.stderr,
        )
        sys.exit(1)
    try:
        with CONFIG_PATH.open() as f:
            cfg = json.load(f)
    except json.JSONDecodeError as e:
        print(f"\n✗  config.json is not valid JSON: {e}\n", file=sys.stderr)
        sys.exit(1)

    # Secrets: allow env vars to fill in blanks (never override non-empty config values)
    if not cfg.get("groq_api_key"):
        cfg["groq_api_key"] = os.environ.get("GROQ_API_KEY", "")
    smtp = cfg.setdefault("smtp", {})
    for k, env in [("user", "SMTP_USER"), ("password", "SMTP_PASSWORD"),
                   ("host", "SMTP_HOST"), ("from", "EMAIL_FROM"), ("to", "EMAIL_TO")]:
        if not smtp.get(k):
            smtp[k] = os.environ.get(env, smtp.get(k, ""))

    return cfg


CFG            = load_config()
NAME           = CFG.get("name", "Your").strip() or "Your"
TITLE          = f"{NAME}'s Daily Digest"
PHONE_NUMBER   = CFG.get("phone_number", "").strip()
GROQ_API_KEY   = CFG.get("groq_api_key", "").strip()
TIMEZONE       = CFG.get("timezone", "UTC")
MODEL          = CFG.get("model", "llama-3.3-70b-versatile")
MAX_ARTICLES   = int(CFG.get("max_articles", 10))
SEND_IMESSAGE  = bool(CFG.get("delivery", {}).get("send_imessage", True))
SEND_EMAIL     = bool(CFG.get("delivery", {}).get("send_email", False))
SMTP_CFG       = CFG.get("smtp", {})
RSS_FEEDS      = CFG.get("rss_feeds", {})
CATEGORY_PROMPTS = CFG.get("category_prompts", {})


# ───────────────────────────────────────────────
#  HELPERS
# ───────────────────────────────────────────────
def log(msg: str):
    ts = datetime.datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def groq_complete(prompt: str, max_tokens: int = 400) -> str:
    if not GROQ_API_KEY:
        return "(Groq API key not set — add it to config.json or export GROQ_API_KEY)"
    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json",
    }
    body = {
        "model": MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": 0.7,
    }
    try:
        r = requests.post(url, headers=headers, json=body, timeout=30)
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:
        return f"(AI summary unavailable: {e})"


# ───────────────────────────────────────────────
#  RSS FETCHING
# ───────────────────────────────────────────────
def fetch_headlines(category: str, feed_urls: list[str]) -> list[dict]:
    seen, articles = set(), []
    for url in feed_urls:
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries[:8]:
                title = getattr(entry, "title", "").strip()
                link  = getattr(entry, "link",  "").strip()
                if title and title not in seen:
                    seen.add(title)
                    articles.append({
                        "title":  title,
                        "link":   link,
                        "source": feed.feed.get("title", url),
                    })
                    if len(articles) >= MAX_ARTICLES:
                        return articles
        except Exception as e:
            log(f"  Feed error ({url[:60]}…): {e}")
    return articles


def summarise_category(category: str, articles: list[dict]) -> str:
    if not articles:
        return "No headlines available right now."
    headline_list = "\n".join(f"- {a['title']}" for a in articles[:MAX_ARTICLES])
    persona = CATEGORY_PROMPTS.get(category, "Summarise these headlines in 4 sentences.")
    prompt  = f"{persona}\n\nHeadlines:\n{headline_list}"
    return groq_complete(prompt, max_tokens=300)


def reading_time(text: str) -> str:
    words = len(re.findall(r"\w+", text))
    mins  = max(1, round(words / 200))
    return f"~{mins} min read"


# ───────────────────────────────────────────────
#  MARKET DATA (with 7-day history for sparklines)
# ───────────────────────────────────────────────
def fetch_market_data() -> dict:
    markets = {}
    headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}

    # Yahoo Finance — indices
    for symbol, label in [("^NSEI", "Nifty 50"), ("^GSPC", "S&P 500")]:
        try:
            url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1d&range=7d"
            r = requests.get(url, headers=headers, timeout=12)
            r.raise_for_status()
            result = r.json()["chart"]["result"][0]
            meta   = result["meta"]
            closes = [c for c in result["indicators"]["quote"][0].get("close", []) if c is not None]
            price  = meta["regularMarketPrice"]
            prev   = meta.get("previousClose") or meta.get("chartPreviousClose", price)
            pct    = ((price - prev) / prev) * 100 if prev else 0
            markets[label] = {
                "price":      price,
                "change_pct": pct,
                "currency":   meta.get("currency", ""),
                "history":    closes[-7:] if closes else [],
            }
        except Exception as e:
            log(f"  Market error ({symbol}): {e}")
            markets[label] = {"price": None, "change_pct": 0, "currency": "", "history": []}

    # CoinGecko — current prices
    try:
        url = ("https://api.coingecko.com/api/v3/simple/price"
               "?ids=bitcoin,ethereum&vs_currencies=usd&include_24hr_change=true")
        r = requests.get(url, timeout=12)
        r.raise_for_status()
        data = r.json()
        for coin_id, label in [("bitcoin", "Bitcoin"), ("ethereum", "Ethereum")]:
            markets[label] = {
                "price":      data[coin_id]["usd"],
                "change_pct": data[coin_id].get("usd_24h_change", 0),
                "currency":   "USD",
                "history":    [],
            }
    except Exception as e:
        log(f"  Crypto error: {e}")
        for coin in ("Bitcoin", "Ethereum"):
            markets[coin] = {"price": None, "change_pct": 0, "currency": "USD", "history": []}

    # CoinGecko — 7-day sparkline history
    for coin_id, label in [("bitcoin", "Bitcoin"), ("ethereum", "Ethereum")]:
        try:
            url = (f"https://api.coingecko.com/api/v3/coins/{coin_id}/market_chart"
                   f"?vs_currency=usd&days=7&interval=daily")
            r = requests.get(url, timeout=12)
            r.raise_for_status()
            prices = [p[1] for p in r.json().get("prices", [])]
            markets[label]["history"] = prices[-7:]
        except Exception as e:
            log(f"  Crypto sparkline error ({coin_id}): {e}")

    return markets


def format_price(label: str, info: dict) -> str:
    if info["price"] is None:
        return f"{label}: N/A"
    p, pct = info["price"], info["change_pct"]
    arrow  = "▲" if pct >= 0 else "▼"
    if label in ("Bitcoin", "Ethereum"):
        return f"{label}: ${p:,.0f} {arrow}{abs(pct):.2f}%"
    return f"{label}: {p:,.2f} {arrow}{abs(pct):.2f}%"


def sparkline_svg(prices: list[float], width: int = 56, height: int = 16) -> str:
    if not prices or len(prices) < 2:
        return ""
    mn, mx = min(prices), max(prices)
    rng = (mx - mn) or 1
    pts = []
    for i, p in enumerate(prices):
        x = (i / (len(prices) - 1)) * width
        y = height - ((p - mn) / rng) * (height - 2) - 1
        pts.append(f"{x:.1f},{y:.1f}")
    trend_up = prices[-1] >= prices[0]
    stroke   = "var(--up)" if trend_up else "var(--down)"
    return (
        f'<svg class="spark" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" aria-hidden="true">'
        f'<polyline points="{" ".join(pts)}" fill="none" '
        f'stroke="{stroke}" stroke-width="1.4" stroke-linejoin="round" '
        f'stroke-linecap="round"/></svg>'
    )


# ───────────────────────────────────────────────
#  HTML GENERATION
# ───────────────────────────────────────────────
def build_html(digest_data: dict) -> str:
    now        = digest_data["generated_at"]
    markets    = digest_data["markets"]
    categories = digest_data["categories"]

    date_str = now.strftime("%A, %B %-d · %Y")
    time_str = now.strftime("%H:%M %Z")

    # Market ticker
    ticker_items = ""
    for label, info in markets.items():
        pct   = info["change_pct"]
        cls   = "up" if pct >= 0 else "down"
        arrow = "▲" if pct >= 0 else "▼"
        if info["price"] is None:
            val = "N/A"
        elif label in ("Bitcoin", "Ethereum"):
            val = f"${info['price']:,.0f}"
        else:
            val = f"{info['price']:,.2f}"
        spark = sparkline_svg(info.get("history", []))
        ticker_items += f"""
        <div class="ticker-item">
          <span class="ticker-label">{html.escape(label)}</span>
          <span class="ticker-price">{val}</span>
          {spark}
          <span class="ticker-change {cls}">{arrow}{abs(pct):.2f}%</span>
        </div>"""

    # Category cards
    category_icons = {
        "World Affairs":    "🌍",
        "War & Conflicts":  "⚔",
        "India News":       "🇮🇳",
        "Fashion":          "✦",
        "Marketing Updates":"📣",
    }
    cat_cards = ""
    cat_list  = list(categories.items())
    for idx, (cat, data) in enumerate(cat_list):
        icon        = category_icons.get(cat, "◆")
        summary_txt = data["summary"]
        summary     = html.escape(summary_txt).replace("\n", "<br>")
        rtime       = reading_time(summary_txt)
        headlines   = ""
        for a in data["articles"][:6]:
            title  = html.escape(a["title"])
            link   = html.escape(a["link"])
            source = html.escape(a.get("source", "")[:40])
            headlines += (
                f'<li><a href="{link}" target="_blank" rel="noopener">{title}</a>'
                f'<span class="src-chip">{source}</span></li>\n'
            )

        extra_class = " full-width" if (idx == len(cat_list) - 1 and len(cat_list) % 2 == 1) else ""
        cat_cards += f"""
    <article class="card{extra_class}" id="cat-{html.escape(cat.lower().replace(' ', '-'))}">
      <header class="card-header">
        <span class="card-icon">{icon}</span>
        <h2>{html.escape(cat)}</h2>
      </header>
      <div class="byline">{rtime} · summarised by {html.escape(MODEL)}</div>
      <p class="summary">{summary}</p>
      <ul class="headlines">{headlines}</ul>
    </article>"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{html.escape(TITLE)}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Playfair+Display:ital,wght@0,400;0,600;0,700;1,400&family=Jost:wght@300;400;500;600&display=swap" rel="stylesheet">
<style>
  :root {{
    --bg:           #f7f1e6;
    --bg2:          #fdf9f0;
    --card:         #ffffff;
    --card-hover:   #fffcf5;
    --border:       #e5d9c4;
    --accent:       #8b1a3a;
    --accent-soft:  #b03a5a;
    --accent-glow:  rgba(139,26,58,.08);
    --gold:         #9e7820;
    --text:         #2a1418;
    --text-muted:   #6b4e3c;
    --text-dim:     #9a7d5f;
    --up:           #2d7a54;
    --down:         #b8432f;
    --rule:         #eadfc9;
    --chip-bg:      #f3e9d4;
  }}
  *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
  html {{ scroll-behavior: smooth; }}
  body {{
    background: var(--bg);
    color: var(--text);
    font-family: 'Jost', sans-serif;
    font-weight: 400;
    line-height: 1.65;
    min-height: 100vh;
  }}

  .masthead {{
    background: linear-gradient(180deg, #fdf9f0 0%, var(--bg) 100%);
    border-bottom: 1px solid var(--border);
    padding: 2.5rem 2rem 0;
    text-align: center;
    position: relative;
  }}
  .masthead::after {{
    content: '';
    display: block;
    height: 2px;
    background: linear-gradient(90deg, transparent, var(--accent), var(--gold), var(--accent), transparent);
    margin-top: 1.5rem;
    opacity: .6;
  }}
  .masthead-eyebrow {{
    font-size: .7rem;
    font-weight: 600;
    letter-spacing: .25em;
    text-transform: uppercase;
    color: var(--accent);
    margin-bottom: .5rem;
  }}
  .masthead-title {{
    font-family: 'Playfair Display', serif;
    font-size: clamp(2.2rem, 5vw, 3.6rem);
    font-weight: 700;
    letter-spacing: -.01em;
    line-height: 1.1;
    color: var(--text);
    margin-bottom: .3rem;
  }}
  .masthead-date {{
    font-size: .85rem;
    color: var(--text-muted);
    letter-spacing: .06em;
    margin-bottom: 1.5rem;
  }}

  .ticker {{
    background: var(--bg2);
    border-top:    1px solid var(--border);
    border-bottom: 1px solid var(--border);
    display: flex;
    justify-content: center;
    flex-wrap: wrap;
    padding: .55rem 1.5rem;
  }}
  .ticker-item {{
    display: flex;
    align-items: center;
    gap: .5rem;
    padding: .25rem 1.2rem;
    border-right: 1px solid var(--rule);
    font-size: .8rem;
  }}
  .ticker-item:last-child {{ border-right: none; }}
  .ticker-label {{
    color: var(--text-muted);
    font-weight: 500;
    letter-spacing: .04em;
    font-size: .72rem;
    text-transform: uppercase;
  }}
  .ticker-price {{
    color: var(--text);
    font-weight: 600;
    font-variant-numeric: tabular-nums;
  }}
  .ticker-change.up   {{ color: var(--up);   font-weight: 500; }}
  .ticker-change.down {{ color: var(--down); font-weight: 500; }}
  .spark {{ display: inline-block; vertical-align: middle; opacity: .85; }}

  .container {{
    max-width: 1280px;
    margin: 0 auto;
    padding: 2.5rem 1.5rem 4rem;
  }}
  .grid {{
    display: grid;
    grid-template-columns: repeat(2, 1fr);
    gap: 1.5rem;
  }}
  .grid .full-width {{ grid-column: 1 / -1; }}

  .card {{
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 4px;
    padding: 1.75rem 1.75rem 1.5rem;
    transition: background .2s, border-color .2s, box-shadow .2s, transform .2s;
    position: relative;
    overflow: hidden;
    box-shadow: 0 1px 3px rgba(74,30,40,.04);
  }}
  .card::before {{
    content: '';
    position: absolute;
    top: 0; left: 0; right: 0;
    height: 2px;
    background: linear-gradient(90deg, var(--accent), transparent);
    opacity: 0;
    transition: opacity .2s;
  }}
  .card:hover {{
    background: var(--card-hover);
    border-color: #d4b590;
    box-shadow: 0 6px 24px rgba(74,30,40,.08), 0 0 0 1px rgba(139,26,58,.06);
  }}
  .card:hover::before {{ opacity: 1; }}

  .card-header {{
    display: flex;
    align-items: center;
    gap: .75rem;
    margin-bottom: .4rem;
    padding-bottom: .75rem;
    border-bottom: 1px solid var(--rule);
  }}
  .card-icon {{
    font-size: 1rem;
    opacity: .8;
  }}
  .card-header h2 {{
    font-family: 'Jost', sans-serif;
    font-size: .72rem;
    font-weight: 600;
    letter-spacing: .2em;
    text-transform: uppercase;
    color: var(--accent);
  }}
  .byline {{
    font-size: .68rem;
    color: var(--text-dim);
    letter-spacing: .08em;
    text-transform: uppercase;
    margin-bottom: 1rem;
  }}

  .summary {{
    font-size: .95rem;
    line-height: 1.7;
    color: var(--text);
    margin-bottom: 1.2rem;
    font-weight: 300;
  }}
  .summary::first-letter {{
    font-family: 'Playfair Display', serif;
    font-size: 3.1rem;
    font-weight: 700;
    float: left;
    line-height: 1;
    padding: .25rem .55rem 0 0;
    color: var(--accent);
  }}

  .headlines {{
    list-style: none;
    border-top: 1px solid var(--rule);
    padding-top: .9rem;
    display: flex;
    flex-direction: column;
    gap: .55rem;
  }}
  .headlines li {{
    font-size: .82rem;
    line-height: 1.45;
    display: flex;
    flex-wrap: wrap;
    align-items: baseline;
    gap: .55rem;
    padding-bottom: .55rem;
    border-bottom: 1px solid var(--rule);
  }}
  .headlines li:last-child {{ border-bottom: none; padding-bottom: 0; }}
  .headlines a {{
    color: var(--text);
    text-decoration: none;
    transition: color .15s;
    flex: 1 1 60%;
  }}
  .headlines a:hover {{
    color: var(--accent);
    text-decoration: underline;
    text-decoration-color: var(--accent);
    text-underline-offset: 3px;
  }}
  .src-chip {{
    display: inline-block;
    background: var(--chip-bg);
    color: var(--text-muted);
    font-size: .62rem;
    font-weight: 500;
    letter-spacing: .1em;
    text-transform: uppercase;
    padding: .18rem .55rem;
    border-radius: 999px;
    white-space: nowrap;
    border: 1px solid rgba(139,26,58,.08);
  }}

  .footer {{
    text-align: center;
    padding: 2rem 1rem;
    border-top: 1px solid var(--rule);
    margin-top: 3rem;
    font-size: .72rem;
    color: var(--text-dim);
    letter-spacing: .06em;
  }}

  @media (max-width: 768px) {{
    .grid {{ grid-template-columns: 1fr; }}
    .masthead {{ padding: 1.5rem 1rem 0; }}
    .ticker-item {{ padding: .25rem .6rem; }}
    .summary::first-letter {{ font-size: 2.6rem; }}
  }}
</style>
</head>
<body>

<header class="masthead">
  <div class="masthead-eyebrow">Morning Edition</div>
  <h1 class="masthead-title">{html.escape(TITLE)}</h1>
  <div class="masthead-date">{date_str} &nbsp;·&nbsp; Generated at {time_str}</div>
</header>

<div class="ticker">
  {ticker_items}
</div>

<main class="container">
  <div class="grid">
    {cat_cards}
  </div>
</main>

<footer class="footer">
  {html.escape(TITLE)} &nbsp;·&nbsp; Powered by Groq &amp; {html.escape(MODEL)} &nbsp;·&nbsp; Data via Yahoo Finance &amp; CoinGecko
</footer>

</body>
</html>"""


# ───────────────────────────────────────────────
#  MESSAGING
# ───────────────────────────────────────────────
def build_message_text(digest_data: dict) -> str:
    now     = digest_data["generated_at"]
    markets = digest_data["markets"]
    cats    = digest_data["categories"]

    lines = [f"☀️ {TITLE} · {now.strftime('%a %b %-d')}\n"]

    m_parts = []
    for label, info in markets.items():
        if info["price"] is None:
            continue
        pct   = info["change_pct"]
        arrow = "▲" if pct >= 0 else "▼"
        if label in ("Bitcoin", "Ethereum"):
            m_parts.append(f"{label[:3]} ${info['price']:,.0f} {arrow}{abs(pct):.1f}%")
        else:
            m_parts.append(f"{label}: {info['price']:,.0f} {arrow}{abs(pct):.1f}%")
    if m_parts:
        lines.append("📈 " + "  |  ".join(m_parts))

    lines.append("")
    cat_emojis = {"World Affairs": "🌍", "War & Conflicts": "⚔", "India News": "🇮🇳",
                  "Fashion": "✦", "Marketing Updates": "📣"}
    for cat, data in cats.items():
        first_sentence = re.split(r'(?<=[.!?])\s', data["summary"])[0]
        lines.append(f"{cat_emojis.get(cat, '◆')} {cat}: {first_sentence}")

    return "\n".join(lines)


def send_imessage(phone: str, message: str) -> bool:
    if platform.system() != "Darwin":
        log("  iMessage requires macOS — skipping.")
        return False
    escaped = message.replace("\\", "\\\\").replace('"', '\\"')
    script = f'''
tell application "Messages"
    set targetService to 1st service whose service type = iMessage
    set targetBuddy to buddy "{phone}" of targetService
    send "{escaped}" to targetBuddy
end tell
'''
    try:
        result = subprocess.run(["osascript", "-e", script],
                                capture_output=True, text=True, timeout=20)
        if result.returncode == 0:
            log("  iMessage sent.")
            return True
        log(f"  iMessage failed: {result.stderr.strip()}")
        return False
    except Exception as e:
        log(f"  iMessage error: {e}")
        return False


def send_email(subject: str, text_body: str, html_body: str) -> bool:
    user = SMTP_CFG.get("user", "")
    pwd  = SMTP_CFG.get("password", "")
    if not user or not pwd:
        log("  Email skipped — smtp.user / smtp.password not set.")
        return False
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"]    = SMTP_CFG.get("from") or user
    msg["To"]      = SMTP_CFG.get("to")   or user
    msg.set_content(text_body)
    msg.add_alternative(html_body, subtype="html")
    try:
        with smtplib.SMTP_SSL(SMTP_CFG.get("host", "smtp.gmail.com"),
                              int(SMTP_CFG.get("port", 465)), timeout=20) as s:
            s.login(user, pwd)
            s.send_message(msg)
        log(f"  Email sent to {msg['To']}.")
        return True
    except Exception as e:
        log(f"  Email error: {e}")
        return False


# ───────────────────────────────────────────────
#  MAIN
# ───────────────────────────────────────────────
def main():
    try:
        tz  = ZoneInfo(TIMEZONE)
        now = datetime.datetime.now(tz)
    except Exception:
        now = datetime.datetime.now()

    print(f"\n{'━'*55}")
    print(f"  {TITLE}")
    print(f"  {now.strftime('%A, %B %-d %Y  ·  %H:%M %Z')}")
    print(f"{'━'*55}\n")

    log("Fetching market data…")
    markets = fetch_market_data()
    for label, info in markets.items():
        log(f"  {format_price(label, info)}")

    categories = {}
    for cat, feeds in RSS_FEEDS.items():
        log(f"Fetching [{cat}]…")
        articles = fetch_headlines(cat, feeds)
        log(f"  {len(articles)} headlines · summarising…")
        categories[cat] = {
            "articles": articles,
            "summary":  summarise_category(cat, articles),
        }
        time.sleep(.3)

    digest_data = {"generated_at": now, "markets": markets, "categories": categories}

    log("Building dashboard.html…")
    html_content = build_html(digest_data)
    OUTPUT_FILE.write_text(html_content, encoding="utf-8")
    log(f"  Saved → {OUTPUT_FILE}")
    webbrowser.open(OUTPUT_FILE.as_uri())

    message_text = build_message_text(digest_data)
    imessage_ok  = False
    if SEND_IMESSAGE and PHONE_NUMBER and PHONE_NUMBER != "+91XXXXXXXXXX":
        log("Sending iMessage…")
        imessage_ok = send_imessage(PHONE_NUMBER, message_text)
    elif SEND_IMESSAGE:
        log("iMessage skipped — phone_number not set in config.json.")

    should_email = SEND_EMAIL or (not imessage_ok and platform.system() != "Darwin")
    if should_email:
        log("Sending email…")
        subject = f"{TITLE} — {now.strftime('%a %b %-d')}"
        send_email(subject, message_text, html_content)

    print(f"\n{'━'*55}")
    print("  Done. Good morning!")
    print(f"{'━'*55}\n")


if __name__ == "__main__":
    main()
