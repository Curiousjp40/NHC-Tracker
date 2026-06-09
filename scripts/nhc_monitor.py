"""
nhc_monitor.py
Polls NHC RSS feeds and emails ryan.pohlman@simon.com on new advisories.
Tracks seen entries in seen_entries.json to avoid duplicate emails.
"""

import os
import json
import smtplib
import feedparser
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

# ── NHC RSS Feeds ────────────────────────────────────────────────────────────
FEEDS = {
    "Atlantic Basin":       "https://www.nhc.noaa.gov/index-at.xml",
    "Eastern Pacific":      "https://www.nhc.noaa.gov/index-ep.xml",
    "Central Pacific":      "https://www.nhc.noaa.gov/index-cp.xml",
    "Atlantic Outlook":     "https://www.nhc.noaa.gov/xml/TWOAT.xml",
    "East Pacific Outlook": "https://www.nhc.noaa.gov/xml/TWOEP.xml",
}

SEEN_FILE = Path("seen_entries.json")
smtp_user = os.environ["SMTP_USER"]
smtp_pass = os.environ["SMTP_PASS"]
to_email  = os.environ["TO_EMAIL"]

# ── Load previously seen entry IDs ───────────────────────────────────────────
if SEEN_FILE.exists():
    seen = set(json.loads(SEEN_FILE.read_text()))
else:
    seen = set()

new_entries = []

# ── Poll each feed ────────────────────────────────────────────────────────────
for feed_name, url in FEEDS.items():
    feed = feedparser.parse(url)
    for entry in feed.entries:
        entry_id = entry.get("id") or entry.get("link") or entry.get("title")
        if entry_id and entry_id not in seen:
            new_entries.append({
                "feed":      feed_name,
                "title":     entry.get("title", "No title"),
                "summary":   entry.get("summary", ""),
                "link":      entry.get("link", ""),
                "published": entry.get("published", ""),
            })
            seen.add(entry_id)

# ── Save updated seen list ────────────────────────────────────────────────────
SEEN_FILE.write_text(json.dumps(list(seen)))

if not new_entries:
    print("No new NHC advisories.")
    exit(0)

# ── Build email ───────────────────────────────────────────────────────────────
subject = f"🌀 NHC Alert: {len(new_entries)} New Advisory{'s' if len(new_entries) > 1 else ''}"

rows = ""
for e in new_entries:
    rows += f"""
    <tr>
      <td colspan="2" style="padding: 10px 12px; background: #1a5276; color: white; font-weight: bold;">
        {e['feed']}
      </td>
    </tr>
    <tr>
      <td style="padding: 6px 12px; font-weight: bold; width: 110px;">Advisory</td>
      <td style="padding: 6px 12px;">{e['title']}</td>
    </tr>
    <tr style="background:#f2f3f4;">
      <td style="padding: 6px 12px; font-weight: bold;">Published</td>
      <td style="padding: 6px 12px;">{e['published']}</td>
    </tr>
    <tr>
      <td style="padding: 6px 12px; font-weight: bold; vertical-align:top;">Summary</td>
      <td style="padding: 6px 12px; background:#eaf4fb; border-left: 4px solid #1a5276;">{e['summary'][:500]}</td>
    </tr>
    <tr>
      <td style="padding: 6px 12px;"></td>
      <td style="padding: 6px 12px;"><a href="{e['link']}" style="color:#1a5276;">View full advisory →</a></td>
    </tr>
    <tr><td colspan="2" style="padding:4px;"></td></tr>
    """

html_body = f"""
<html>
<body style="font-family: Arial, sans-serif; color: #222; max-width: 650px;">
  <h2 style="color: #1a5276;">⛈️ NHC New Advisory Alert</h2>
  <p>{len(new_entries)} new update(s) posted to nhc.noaa.gov</p>
  <table style="border-collapse: collapse; width: 100%;">
    {rows}
  </table>
  <hr style="border:none; border-top:1px solid #ddd; margin-top:20px;">
  <p style="font-size:11px; color:#888;">Auto-generated NHC monitor • Checks every 15 minutes</p>
</body>
</html>
"""

plain_body = "\n\n".join(
    f"[{e['feed']}]\n{e['title']}\n{e['published']}\n{e['summary'][:300]}\n{e['link']}"
    for e in new_entries
)

msg = MIMEMultipart("alternative")
msg["Subject"] = subject
msg["From"]    = smtp_user
msg["To"]      = to_email
msg.attach(MIMEText(plain_body, "plain"))
msg.attach(MIMEText(html_body, "html"))

with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
    server.login(smtp_user, smtp_pass)
    server.sendmail(smtp_user, to_email, msg.as_string())

print(f"✅ Sent alert for {len(new_entries)} new NHC entries to {to_email}")
