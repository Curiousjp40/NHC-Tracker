import os
import json
import datetime
import urllib.request
import feedparser
import re
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

seen = set(json.loads(SEEN_FILE.read_text())) if SEEN_FILE.exists() else set()
first_run = not SEEN_FILE.exists()

new_entries = []
all_entries = []

for feed_name, url in FEEDS.items():
    feed = feedparser.parse(url)
    for entry in feed.entries:
        entry_id = entry.get("id") or entry.get("link") or entry.get("title")
        parsed = {
            "feed":      feed_name,
            "title":     entry.get("title", "No title"),
            "summary":   entry.get("summary", ""),
            "link":      entry.get("link", ""),
            "published": entry.get("published", ""),
        }
        all_entries.append(parsed)
        if entry_id and entry_id not in seen:
            new_entries.append(parsed)
            seen.add(entry_id)

SEEN_FILE.write_text(json.dumps(list(seen)))

if first_run:
    print("First run — seeded seen entries. Will email on next new advisory.")
    exit(0)

SKIP_KEYWORDS = [
    "no tropical cyclones at this time",
    "formation not expected",
    "there are no tropical",
    "tropical weather outlook",
    "graphical tropical weather",
]

TRIGGER_KEYWORDS = [
    "tropical storm", "hurricane", "typhoon", "subtropical storm",
    "watch", "warning", "landfall", "dissipat", "remnant",
    "weakens", "strengthen", "intensif", "upgrade", "downgrade",
    "state of emergency", "evacuation", "airport closure",
    "discontinu", "public advisory", "forecast advisory",
    "special advisory", "intermediate advisory",
]

def is_relevant(entry):
    text = (entry["title"] + " " + entry["summary"]).lower()
    if any(skip in text for skip in SKIP_KEYWORDS):
        return False
    return any(trigger in text for trigger in TRIGGER_KEYWORDS)

new_entries = [e for e in new_entries if is_relevant(e)]

if not new_entries:
    print("No new advisories.")
    exit(0)

def get_storm_status(entries):
    storms = {}
    for e in entries:
        title = e["title"]
        summary = e["summary"].lower()
        text = (title + " " + summary).lower()
        if any(skip in text for skip in SKIP_KEYWORDS):
            continue
        if not any(t in text for t in ["tropical storm", "hurricane", "typhoon", "subtropical storm", "advisory"]):
            continue
        match = re.search(r'(?:tropical storm|hurricane|typhoon|subtropical storm)\s+(\w+)', text)
        if match:
            name = match.group(1).capitalize()
        elif "summary for" in title.lower():
            match2 = re.search(r'summary for (.+?)\s*\(', title, re.IGNORECASE)
            name = match2.group(1).strip() if match2 else title
        else:
            continue
        if name not in storms:
            storms[name] = {
                "name": name,
                "feed": e["feed"],
                "latest_title": title,
                "latest_summary": e["summary"][:600],
                "latest_published": e["published"],
                "link": e["link"],
                "watches": [],
                "warnings": [],
                "status_flags": [],
            }
        if "hurricane warning" in text:
            storms[name]["warnings"].append("Hurricane Warning")
        if "tropical storm warning" in text:
            storms[name]["warnings"].append("Tropical Storm Warning")
        if "hurricane watch" in text:
            storms[name]["watches"].append("Hurricane Watch")
        if "tropical storm watch" in text:
            storms[name]["watches"].append("Tropical Storm Watch")
        if "landfall" in text:
            storms[name]["status_flags"].append("Landfall occurred")
        if any(w in text for w in ["dissipat", "remnant", "weakening"]):
            storms[name]["status_flags"].append("Weakening/dissipating")
        if any(w in text for w in ["strengthen", "intensif"]):
            storms[name]["status_flags"].append("Strengthening")
        if "discontinu" in text:
            storms[name]["status_flags"].append("NHC discontinuing advisories")
    return storms

storms = get_storm_status(all_entries)

storm_blocks = ""
for name, s in storms.items():
    watches = list(dict.fromkeys(s["watches"]))
    warnings = list(dict.fromkeys(s["warnings"]))
    flags = list(dict.fromkeys(s["status_flags"]))
    watch_html = "".join(f"<li>⚠️ {w}</li>" for w in watches) if watches else "<li>None</li>"
    warning_html = "".join(f"<li>🔴 {w}</li>" for w in warnings) if warnings else "<li>None</li>"
    flag_html = "".join(f"<li>✅ {f}</li>" for f in flags) if flags else "<li>No significant status changes</li>"
    storm_blocks += f"""
    <div style="background:#f8f9fa;border:1px solid #dee2e6;border-radius:6px;padding:15px;margin-bottom:15px;">
      <h3 style="color:#1a5276;margin:0 0 10px 0;">🌀 {s['name']} — {s['feed']}</h3>
      <table style="width:100%;border-collapse:collapse;">
        <tr><td style="padding:4px 8px;font-weight:bold;width:140px;vertical-align:top;">Latest Advisory</td><td style="padding:4px 8px;">{s['latest_title']}</td></tr>
        <tr style="background:#eaf4fb;"><td style="padding:4px 8px;font-weight:bold;">Published</td><td style="padding:4px 8px;">{to_et(s['latest_published'])}</td></tr>
        <tr><td style="padding:4px 8px;font-weight:bold;vertical-align:top;">Watches</td><td style="padding:4px 8px;"><ul style="margin:0;padding-left:18px;">{watch_html}</ul></td></tr>
        <tr style="background:#eaf4fb;"><td style="padding:4px 8px;font-weight:bold;vertical-align:top;">Warnings</td><td style="padding:4px 8px;"><ul style="margin:0;padding-left:18px;">{warning_html}</ul></td></tr>
        <tr><td style="padding:4px 8px;font-weight:bold;vertical-align:top;">Status</td><td style="padding:4px 8px;"><ul style="margin:0;padding-left:18px;">{flag_html}</ul></td></tr>
        <tr style="background:#eaf4fb;"><td style="padding:4px 8px;font-weight:bold;vertical-align:top;">Summary</td><td style="padding:4px 8px;border-left:4px solid #1a5276;">{s['latest_summary']}</td></tr>
        <tr><td></td><td style="padding:4px 8px;"><a href="{s['link']}" style="color:#1a5276;">View full advisory →</a></td></tr>
      </table>
    </div>
    """

new_rows = ""
for e in new_entries:
    new_rows += f"""
    <tr><td colspan="2" style="padding:10px 12px;background:#1a5276;color:white;font-weight:bold;">{e['feed']}</td></tr>
    <tr><td style="padding:6px 12px;font-weight:bold;width:110px;">Advisory</td><td style="padding:6px 12px;">{e['title']}</td></tr>
    <tr style="background:#f2f3f4;"><td style="padding:6px 12px;font-weight:bold;">Published</td><td style="padding:6px 12px;">{to_et(e['published'])}</td></tr>
    <tr><td style="padding:6px 12px;font-weight:bold;vertical-align:top;">Summary</td><td style="padding:6px 12px;background:#eaf4fb;border-left:4px solid #1a5276;">{e['summary'][:400]}</td></tr>
    <tr><td></td><td style="padding:6px 12px;"><a href="{e['link']}" style="color:#1a5276;">View full advisory →</a></td></tr>
    """

subject = f"🌀 NHC Alert: {len(new_entries)} New Advisory{'s' if len(new_entries) > 1 else ''}"

html_body = f"""
<html><body style="font-family:Arial,sans-serif;color:#222;max-width:680px;">
  <h2 style="color:#1a5276;border-bottom:2px solid #1a5276;padding-bottom:6px;">⛈️ NHC Advisory Alert</h2>
  <p style="color:#555;">{len(new_entries)} new update(s) posted to nhc.noaa.gov</p>
  <h3 style="color:#1a5276;margin-top:20px;">📋 Active Storm Status</h3>
  {storm_blocks if storm_blocks else '<p style="color:#888;">No active storms currently tracked.</p>'}
  <h3 style="color:#1a5276;margin-top:20px;">🆕 New Advisories This Check</h3>
  <table style="border-collapse:collapse;width:100%;">{new_rows}</table>
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
