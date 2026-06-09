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

def extract_wind_speed(text):
    m = re.search(r'maximum sustained winds?.*?(\d+)\s*mph', text, re.IGNORECASE)
    if m:
        return f"{m.group(1)} mph"
    m = re.search(r'(\d+)\s*mph.*?winds?', text, re.IGNORECASE)
    if m:
        return f"{m.group(1)} mph"
    return None

def extract_location(text):
    m = re.search(r'located.*?near\s+([\d.]+)[°\s]*([NS]).*?([\d.]+)[°\s]*([EW])', text, re.IGNORECASE)
    if m:
        return f"{m.group(1)}°{m.group(2)}, {m.group(3)}°{m.group(4)}"
    return None

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

print(f"DEBUG: {len(new_entries)} new (unseen) entries before filter")
for e in new_entries:
    text = (e["title"] + " " + e["summary"]).lower()
    skipped = any(s in text for s in SKIP_KEYWORDS)
    triggered = any(t in text for t in TRIGGER_KEYWORDS)
    print(f"  [{'SKIP' if skipped else ('PASS' if triggered else 'MISS')}] {e['title']}")

new_entries = [e for e in new_entries if is_relevant(e)]

if not new_entries:
    print("No new advisories.")
    exit(0)

def get_storm_status(entries):
    storms = {}
    for e in entries:
        title = e["title"]
        text = (title + " " + e["summary"]).lower()
        if any(skip in text for skip in SKIP_KEYWORDS):
            continue
        if not any(t in text for t in ["tropical storm", "hurricane", "typhoon", "subtropical storm", "advisory"]):
            continue
        match = re.search(r'(?:tropical storm|hurricane|typhoon|subtropical storm)\s+(\w+)', text)
        if match:
            name = match.group(1).capitalize()
        elif "summary for" in title.lower():
            match2 = re.search(r'summary for (.+?)\s*\(', title, re.IGNORECASE)
            name = match2.group(1).strip() if match2 else None
        else:
            continue
        if not name:
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
        if "...tropical storm warning in effect" in text or "tropical storm warning is in effect" in text:
            storms[name]["warnings"].append("Tropical Storm Warning")
        if "...hurricane warning in effect" in text or "hurricane warning is in effect" in text:
            storms[name]["warnings"].append("Hurricane Warning")
        if "...tropical storm watch in effect" in text or "tropical storm watch is in effect" in text:
            storms[name]["watches"].append("Tropical Storm Watch")
        if "...hurricane watch in effect" in text or "hurricane watch is in effect" in text:
            storms[name]["watches"].append("Hurricane Watch")
        if "made landfall" in text or "center made landfall" in text:
            storms[name]["status_flags"].append("Landfall confirmed by NHC")
        if "discontinu" in text and "advisory" in text:
            storms[name]["status_flags"].append("NHC discontinuing advisories")
        if "is forecast to strengthen" in text or "expected to intensify" in text:
            storms[name]["status_flags"].append("Strengthening forecast")
        if "is forecast to weaken" in text or "expected to weaken" in text:
            storms[name]["status_flags"].append("Weakening forecast")
    return storms

storms = get_storm_status(all_entries)

now_et = datetime.datetime.now(ZoneInfo("America/New_York"))
now_suffix = "EDT" if now_et.dst() else "EST"
now_str = now_et.strftime(f"%b %d, %Y %I:%M %p {now_suffix}")

storm_blocks = ""
for name, s in storms.items():
    watches  = list(dict.fromkeys(s["watches"]))
    warnings = list(dict.fromkeys(s["warnings"]))
    flags    = list(dict.fromkeys(s["status_flags"]))

    wind  = extract_wind_speed(s["latest_summary"])
    loc   = extract_location(s["latest_summary"])

    # Card border color
    if warnings:
        border_color = "#c0392b"
    elif watches:
        border_color = "#e67e22"
    else:
        border_color = "#7f8c8d"

    # Watches/warnings HTML
    def alert_pills(items, bg, color):
        if not items:
            return '<span style="color:#888;font-size:13px;">None</span>'
        return "".join(
            f'<span style="background:{bg};color:{color};padding:3px 8px;border-radius:3px;font-size:13px;margin-right:4px;">{i}</span>'
            for i in items
        )

    warning_html = alert_pills(warnings, "#fde8e8", "#c0392b")
    watch_html   = alert_pills(watches,  "#fff3e0", "#e67e22")

    if flags:
        flag_html = "".join(
            f'<div style="background:#e8f5e9;color:#2e7d32;padding:4px 8px;border-radius:3px;font-size:13px;margin-bottom:3px;">{f}</div>'
            for f in flags
        )
    else:
        flag_html = '<span style="color:#888;font-size:13px;">No significant status changes</span>'

    wind_row = f"""
        <tr>
          <td style="padding:5px 10px;color:#555;font-weight:bold;font-size:13px;white-space:nowrap;">Wind Speed</td>
          <td style="padding:5px 10px;font-size:13px;">{wind}</td>
        </tr>""" if wind else ""

    loc_row = f"""
        <tr style="background:#f9f9f9;">
          <td style="padding:5px 10px;color:#555;font-weight:bold;font-size:13px;white-space:nowrap;">Location</td>
          <td style="padding:5px 10px;font-size:13px;">{loc}</td>
        </tr>""" if loc else ""

    storm_blocks += f"""
    <div style="border-left:5px solid {border_color};border-radius:6px;margin-bottom:18px;overflow:hidden;box-shadow:0 1px 4px rgba(0,0,0,0.08);">
      <div style="background:#1a3a5c;padding:12px 16px;">
        <span style="color:white;font-size:16px;font-weight:bold;">🌀 {s['name']}</span>
        <span style="color:#a8c4e0;font-size:13px;margin-left:10px;">{s['feed']}</span>
      </div>
      <div style="background:white;padding:0;">
        <table style="width:100%;border-collapse:collapse;">
          <tr>
            <td style="padding:5px 10px;color:#555;font-weight:bold;font-size:13px;white-space:nowrap;">Latest Advisory</td>
            <td style="padding:5px 10px;font-size:13px;">{s['latest_title']}</td>
          </tr>
          <tr style="background:#f9f9f9;">
            <td style="padding:5px 10px;color:#555;font-weight:bold;font-size:13px;white-space:nowrap;">Published</td>
            <td style="padding:5px 10px;font-size:13px;">{to_et(s['latest_published'])}</td>
          </tr>
          {wind_row}
          {loc_row}
          <tr{"" if wind or loc else " style=\"background:#f9f9f9;\""}>
            <td style="padding:5px 10px;color:#555;font-weight:bold;font-size:13px;white-space:nowrap;vertical-align:top;">Warnings</td>
            <td style="padding:8px 10px;">{warning_html}</td>
          </tr>
          <tr style="background:#f9f9f9;">
            <td style="padding:5px 10px;color:#555;font-weight:bold;font-size:13px;white-space:nowrap;vertical-align:top;">Watches</td>
            <td style="padding:8px 10px;">{watch_html}</td>
          </tr>
          <tr>
            <td style="padding:5px 10px;color:#555;font-weight:bold;font-size:13px;white-space:nowrap;vertical-align:top;">Status</td>
            <td style="padding:8px 10px;">{flag_html}</td>
          </tr>
          <tr style="background:#f0f4f8;">
            <td style="padding:5px 10px;color:#555;font-weight:bold;font-size:13px;white-space:nowrap;vertical-align:top;">Summary</td>
            <td style="padding:8px 10px;font-size:13px;border-left:3px solid {border_color};">{s['latest_summary']}</td>
          </tr>
          <tr>
            <td colspan="2" style="padding:10px 16px;text-align:right;">
              <a href="{s['link']}" style="background:#1a3a5c;color:white;padding:7px 16px;border-radius:4px;text-decoration:none;font-size:13px;font-weight:bold;">View Full Advisory →</a>
            </td>
          </tr>
        </table>
      </div>
    </div>
    """

new_rows = ""
for e in new_entries:
    new_rows += f"""
    <tr><td colspan="2" style="padding:10px 12px;background:#1a3a5c;color:white;font-weight:bold;">{e['feed']}</td></tr>
    <tr><td style="padding:6px 12px;font-weight:bold;color:#555;width:110px;font-size:13px;">Advisory</td><td style="padding:6px 12px;font-size:13px;">{e['title']}</td></tr>
    <tr style="background:#f9f9f9;"><td style="padding:6px 12px;font-weight:bold;color:#555;font-size:13px;">Published</td><td style="padding:6px 12px;font-size:13px;">{to_et(e['published'])}</td></tr>
    <tr><td style="padding:6px 12px;font-weight:bold;color:#555;font-size:13px;vertical-align:top;">Summary</td><td style="padding:6px 12px;font-size:13px;border-left:3px solid #1a3a5c;background:#f0f4f8;">{e['summary'][:400]}</td></tr>
    <tr><td colspan="2" style="padding:8px 16px;text-align:right;"><a href="{e['link']}" style="background:#1a3a5c;color:white;padding:6px 14px;border-radius:4px;text-decoration:none;font-size:13px;font-weight:bold;">View Full Advisory →</a></td></tr>
    """

subject = f"🌀 NHC Alert: {len(new_entries)} New Advisory{'s' if len(new_entries) > 1 else ''}"

html_body = f"""
<html>
<body style="margin:0;padding:0;background:#f0f2f5;font-family:Arial,sans-serif;color:#222;">
  <div style="max-width:680px;margin:0 auto;padding:20px 0;">

    <!-- NOAA-style banner -->
    <div style="background:#1a3a5c;border-radius:6px 6px 0 0;padding:18px 24px;display:flex;align-items:center;">
      <div>
        <div style="color:#a8c4e0;font-size:11px;letter-spacing:2px;text-transform:uppercase;margin-bottom:4px;">National Hurricane Center</div>
        <div style="color:white;font-size:22px;font-weight:bold;letter-spacing:1px;">⛈️ NHC ADVISORY ALERT</div>
        <div style="color:#a8c4e0;font-size:12px;margin-top:4px;">{now_str} &nbsp;|&nbsp; {len(new_entries)} new update(s)</div>
      </div>
    </div>

    <!-- Body -->
    <div style="background:white;padding:20px 24px;border-radius:0 0 6px 6px;">

      <h3 style="color:#1a3a5c;margin:0 0 14px 0;font-size:15px;border-bottom:2px solid #e0e7ef;padding-bottom:8px;">📋 Active Storm Status</h3>
      {storm_blocks if storm_blocks else '<p style="color:#888;font-size:13px;">No active storms currently tracked.</p>'}

      <h3 style="color:#1a3a5c;margin:20px 0 14px 0;font-size:15px;border-bottom:2px solid #e0e7ef;padding-bottom:8px;">🆕 New Advisories This Check</h3>
      <table style="border-collapse:collapse;width:100%;">{new_rows}</table>

    </div>

    <p style="text-align:center;font-size:11px;color:#aaa;margin-top:12px;">
      Auto-generated NHC monitor &nbsp;•&nbsp; Checks every 15 minutes &nbsp;•&nbsp; nhc.noaa.gov
    </p>
  </div>
</body>
</html>
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
