import os
import json
import urllib.request
import feedparser
from pathlib import Path
from email.utils import parsedate_to_datetime
from zoneinfo import ZoneInfo

def to_et(published_str):
    try:
        dt = parsedate_to_datetime(published_str)
        dt_et = dt.astimezone(ZoneInfo("America/New_York"))
        suffix = "EDT" if dt_et.dst() else "EST"
        return dt_et.strftime(f"%b %d, %Y %I:%M %p {suffix}")
    except Exception:
        return published_str

FEEDS = {
    "Atlantic Basin":  "https://www.nhc.noaa.gov/index-at.xml",
    "Eastern Pacific": "https://www.nhc.noaa.gov/index-ep.xml",
    "Central Pacific": "https://www.nhc.noaa.gov/index-cp.xml",
}

SEEN_FILE  = Path("seen_entries.json")
api_key    = os.environ["SENDGRID_API_KEY"]
from_email = os.environ["SMTP_USER"]
to_email   = os.environ["TO_EMAIL"]

# Load seen entries
seen = set(json.loads(SEEN_FILE.read_text())) if SEEN_FILE.exists() else set()
first_run = not SEEN_FILE.exists()

new_entries = []

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

# Always save seen entries
SEEN_FILE.write_text(json.dumps(list(seen)))

# On first run just seed — don't email
if first_run:
    print("First run — seeded seen entries. Will email on next new advisory.")
    exit(0)

# Only keep entries matching notification triggers
TRIGGER_KEYWORDS = [
    # Initial — storm named or watches/warnings
    "tropical storm", "hurricane", "typhoon", "subtropical storm",
    "watch", "warning",
    # Updates
    "upgrade", "downgrade", "landfall", "weakens", "dissipat", "remnant",
    # Preparedness
    "state of emergency", "evacuation", "airport closure",
    # Final
    "discontinu", "advisory number",
]

SKIP_KEYWORDS = [
    "tropical weather outlook",
    "no tropical cyclones",
    "formation not expected",
    "disturbance",
]

def is_relevant(entry):
    text = (entry["title"] + " " + entry["summary"]).lower()
    if any(skip in text for skip in SKIP_KEYWORDS):
        return False
    return any(trigger in text for trigger in TRIGGER_KEYWORDS)

new_entries = [e for e in new_entries if is_relevant(e)]

if not new_entries:
    print("No relevant storm advisories.")
    exit(0)

subject = f"🌀 NHC Alert: {len(new_entries)} New Advisory{'s' if len(new_entries) > 1 else ''}"

rows = ""
for e in new_entries:
    rows += f"""
    <tr>
      <td colspan="2" style="padding:10px 12px;background:#1a5276;color:white;font-weight:bold;">{e['feed']}</td>
    </tr>
    <tr>
      <td style="padding:6px 12px;font-weight:bold;width:110px;">Advisory</td>
      <td style="padding:6px 12px;">{e['title']}</td>
    </tr>
    <tr style="background:#f2f3f4;">
      <td style="padding:6px 12px;font-weight:bold;">Published</td>
      <td style="padding:6px 12px;">{to_et(e['published'])}</td>
    </tr>
    <tr>
      <td style="padding:6px 12px;font-weight:bold;vertical-align:top;">Summary</td>
      <td style="padding:6px 12px;background:#eaf4fb;border-left:4px solid #1a5276;">{e['summary'][:500]}</td>
    </tr>
    <tr>
      <td></td>
      <td style="padding:6px 12px;"><a href="{e['link']}" style="color:#1a5276;">View full advisory →</a></td>
    </tr>
    """

html_body = f"""
<html><body style="font-family:Arial,sans-serif;color:#222;max-width:650px;">
  <h2 style="color:#1a5276;">⛈️ NHC New Advisory Alert</h2>
  <p>{len(new_entries)} new update(s) posted to nhc.noaa.gov</p>
  <table style="border-collapse:collapse;width:100%;">{rows}</table>
  <hr style="border:none;border-top:1px solid #ddd;margin-top:20px;">
  <p style="font-size:11px;color:#888;">Auto-generated NHC monitor • Checks every 15 minutes</p>
</body></html>
"""

payload = json.dumps({
    "personalizations": [{"to": [{"email": to_email}]}],
    "from": {"email": from_email},
    "subject": subject,
    "content": [{"type": "text/html", "value": html_body}]
}).encode("utf-8")

req = urllib.request.Request(
    "https://api.sendgrid.com/v3/mail/send",
    data=payload,
    headers={
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    },
    method="POST"
)

with urllib.request.urlopen(req) as resp:
    print(f"✅ Email sent to {to_email} — status {resp.status}")
