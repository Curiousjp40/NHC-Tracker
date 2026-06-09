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

# Only alert on the primary advisory products — not supplemental graphics/discussions
CORE_ADVISORY_PATTERNS = [
    r'public advisory',
    r'forecast advisory',
    r'special advisory',
    r'intermediate advisory',
    r'summary for tropical storm',
    r'summary for hurricane',
    r'tropical storm \w+ advisory number',
    r'hurricane \w+ advisory number',
]

SKIP_KEYWORDS = [
    "no tropical cyclones at this time",
    "formation not expected",
    "there are no tropical",
    "tropical weather outlook",
    "graphical tropical weather",
    "forecast discussion",
    "wind speed probabilities",
    "graphics",
    "rainfall potential",
    "arrival time",
    "wind history",
    "warnings and surface wind",
    "key messages",
    "rip currents",
    "storm surge",
]

def is_relevant(entry):
    text = (entry["title"] + " " + entry["summary"]).lower()
    if any(skip in text for skip in SKIP_KEYWORDS):
        return False
    return any(re.search(pattern, text) for pattern in CORE_ADVISORY_PATTERNS)

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

# Determine severity level across all new entries
all_new_text = " ".join((e["title"] + " " + e["summary"]).lower() for e in new_entries)
has_hurricane_warning = "hurricane warning" in all_new_text
has_hurricane_watch   = "hurricane watch" in all_new_text
has_ts_warning        = "tropical storm warning" in all_new_text
has_ts_watch          = "tropical storm watch" in all_new_text or "watch" in all_new_text

storm_names = list(storms.keys()) if storms else []
storm_name_str = ", ".join(storm_names) if storm_names else f"{len(new_entries)} update(s)"

if has_hurricane_warning or has_hurricane_watch:
    severity = "hurricane"
elif has_ts_warning or has_ts_watch:
    severity = "tropical_storm"
else:
    severity = "default"

# Subject line
if severity == "hurricane":
    subject = f"URGENT - NHC HURRICANE ALERT: {storm_name_str}"
elif severity == "tropical_storm":
    subject = f"NHC TROPICAL STORM ALERT: {storm_name_str}"
else:
    subject = f"NHC Advisory Update: {storm_name_str}"

# Header banner config
if severity == "hurricane":
    banner_bg    = "#8B0000"
    banner_emoji = "HURRICANE ALERT — IMMEDIATE ATTENTION REQUIRED"
    banner_label = "URGENT ALERT"
elif severity == "tropical_storm":
    banner_bg    = "#E65100"
    banner_emoji = "TROPICAL STORM ADVISORY ALERT"
    banner_label = "ADVISORY ALERT"
else:
    banner_bg    = "#1a5276"
    banner_emoji = "NHC ADVISORY UPDATE"
    banner_label = "ADVISORY UPDATE"

now_et = datetime.datetime.now(ZoneInfo("America/New_York"))
now_suffix = "EDT" if now_et.dst() else "EST"
now_str = now_et.strftime(f"%b %d, %Y %I:%M %p {now_suffix}")

# Top alert box content
alert_lines = []
for name, s in storms.items():
    watches  = list(dict.fromkeys(s["watches"]))
    warnings = list(dict.fromkeys(s["warnings"]))
    parts = [f"<strong>{name}</strong>"]
    if warnings:
        parts.append(", ".join(warnings))
    if watches:
        parts.append(", ".join(watches))
    alert_lines.append(" — ".join(parts))

alert_box_rows = "".join(
    f'<div style="margin:4px 0;font-size:15px;color:white;">{line}</div>'
    for line in alert_lines
) if alert_lines else '<div style="font-size:15px;color:white;">See advisory details below.</div>'

# Storm cards
storm_blocks = ""
for name, s in storms.items():
    watches  = list(dict.fromkeys(s["watches"]))
    warnings = list(dict.fromkeys(s["warnings"]))
    flags    = list(dict.fromkeys(s["status_flags"]))

    wind = extract_wind_speed(s["latest_summary"])
    loc  = extract_location(s["latest_summary"])

    if warnings:
        border_color = "#8B0000"
    elif watches:
        border_color = "#E65100"
    else:
        border_color = "#7f8c8d"

    if warnings:
        warning_pills = "".join(
            '<span style="background:#fde8e8;color:#8B0000;padding:4px 10px;border-radius:3px;'
            'font-size:13px;font-weight:bold;margin-right:4px;">' + w + '</span>'
            for w in warnings
        )
    else:
        warning_pills = '<span style="color:#888;font-size:13px;">None</span>'

    if watches:
        watch_pills = "".join(
            '<span style="background:#fff3e0;color:#E65100;padding:4px 10px;border-radius:3px;'
            'font-size:13px;font-weight:bold;margin-right:4px;">' + w + '</span>'
            for w in watches
        )
    else:
        watch_pills = '<span style="color:#888;font-size:13px;">None</span>'

    if flags:
        flag_html = "".join(
            '<div style="background:#e8f5e9;color:#2e7d32;padding:4px 8px;border-radius:3px;'
            'font-size:13px;margin-bottom:3px;">' + f + '</div>'
            for f in flags
        )
    else:
        flag_html = '<span style="color:#888;font-size:13px;">No significant status changes</span>'

    wind_row = (
        '<tr><td style="padding:5px 10px;color:#555;font-weight:bold;font-size:13px;white-space:nowrap;">Wind Speed</td>'
        '<td style="padding:5px 10px;font-size:13px;">' + wind + '</td></tr>'
    ) if wind else ""

    loc_row = (
        '<tr style="background:#f9f9f9;"><td style="padding:5px 10px;color:#555;font-weight:bold;font-size:13px;white-space:nowrap;">Location</td>'
        '<td style="padding:5px 10px;font-size:13px;">' + loc + '</td></tr>'
    ) if loc else ""

    storm_blocks += (
        '<div style="border-left:5px solid ' + border_color + ';border-radius:6px;margin-bottom:18px;'
        'overflow:hidden;box-shadow:0 1px 4px rgba(0,0,0,0.08);">'
        '<div style="background:#1a3a5c;padding:12px 16px;">'
        '<span style="color:white;font-size:16px;font-weight:bold;">&#127744; ' + s["name"] + '</span>'
        '<span style="color:#a8c4e0;font-size:13px;margin-left:10px;">' + s["feed"] + '</span>'
        '</div>'
        '<div style="background:white;padding:0;">'
        '<table style="width:100%;border-collapse:collapse;">'
        '<tr><td style="padding:5px 10px;color:#555;font-weight:bold;font-size:13px;white-space:nowrap;">Latest Advisory</td>'
        '<td style="padding:5px 10px;font-size:13px;">' + s["latest_title"] + '</td></tr>'
        '<tr style="background:#f9f9f9;"><td style="padding:5px 10px;color:#555;font-weight:bold;font-size:13px;white-space:nowrap;">Published</td>'
        '<td style="padding:5px 10px;font-size:13px;">' + to_et(s["latest_published"]) + '</td></tr>'
        + wind_row + loc_row +
        '<tr><td style="padding:5px 10px;color:#555;font-weight:bold;font-size:13px;white-space:nowrap;vertical-align:top;">Warnings</td>'
        '<td style="padding:8px 10px;">' + warning_pills + '</td></tr>'
        '<tr style="background:#f9f9f9;"><td style="padding:5px 10px;color:#555;font-weight:bold;font-size:13px;white-space:nowrap;vertical-align:top;">Watches</td>'
        '<td style="padding:8px 10px;">' + watch_pills + '</td></tr>'
        '<tr><td style="padding:5px 10px;color:#555;font-weight:bold;font-size:13px;white-space:nowrap;vertical-align:top;">Status</td>'
        '<td style="padding:8px 10px;">' + flag_html + '</td></tr>'
        '<tr style="background:#f0f4f8;"><td style="padding:5px 10px;color:#555;font-weight:bold;font-size:13px;white-space:nowrap;vertical-align:top;">Summary</td>'
        '<td style="padding:8px 10px;font-size:13px;border-left:3px solid ' + border_color + ';">' + s["latest_summary"] + '</td></tr>'
        '<tr><td colspan="2" style="padding:10px 16px;text-align:right;">'
        '<a href="' + s["link"] + '" style="background:#1a3a5c;color:white;padding:7px 16px;border-radius:4px;'
        'text-decoration:none;font-size:13px;font-weight:bold;">View Full Advisory &#8594;</a>'
        '</td></tr>'
        '</table></div></div>'
    )

# New advisory rows
new_rows = ""
for e in new_entries:
    new_rows += (
        '<tr><td colspan="2" style="padding:10px 12px;background:#1a5276;color:white;font-weight:bold;">'
        + e["feed"] + '</td></tr>'
        '<tr><td style="padding:6px 12px;font-weight:bold;width:110px;">Advisory</td>'
        '<td style="padding:6px 12px;">'
        '<span style="background:#c0392b;color:white;font-size:12px;font-weight:bold;padding:3px 8px;'
        'border-radius:3px;margin-right:6px;letter-spacing:1px;">&#x1F195; NEW</span>'
        + e["title"] + '</td></tr>'
        '<tr style="background:#f2f3f4;"><td style="padding:6px 12px;font-weight:bold;">Published</td>'
        '<td style="padding:6px 12px;">' + to_et(e["published"]) + '</td></tr>'
        '<tr><td style="padding:6px 12px;font-weight:bold;vertical-align:top;">Summary</td>'
        '<td style="padding:6px 12px;background:#eaf4fb;border-left:4px solid #1a5276;">'
        + e["summary"][:400] + '</td></tr>'
        '<tr><td></td><td style="padding:6px 12px;">'
        '<a href="' + e["link"] + '" style="color:#1a5276;font-weight:bold;">View full advisory &#8594;</a>'
        '</td></tr>'
    )

html_body = """
<html>
<head>
<style>
@keyframes flash {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.4; }
}
.flash-badge {
  animation: flash 1s infinite;
  display: inline-block;
}
</style>
</head>
<body style="margin:0;padding:0;background:#f0f2f5;font-family:Arial,sans-serif;color:#222;">
  <div style="max-width:680px;margin:0 auto;padding:20px 0;">

    <!-- Severity banner -->
    <div style="background:""" + banner_bg + """;border-radius:6px 6px 0 0;padding:20px 24px;text-align:center;">
      <div style="color:white;font-size:11px;letter-spacing:3px;text-transform:uppercase;margin-bottom:6px;opacity:0.85;">
        National Hurricane Center — """ + banner_label + """
      </div>
      <div style="color:white;font-size:24px;font-weight:bold;letter-spacing:1px;">
        """ + banner_emoji + """
      </div>
      <div style="color:rgba(255,255,255,0.8);font-size:12px;margin-top:6px;">
        """ + now_str + """ &nbsp;|&nbsp; """ + str(len(new_entries)) + """ new update(s)
      </div>
    </div>

    <!-- Top alert box -->
    <div style="background:""" + banner_bg + """;padding:16px 24px;border-top:1px solid rgba(255,255,255,0.2);">
      <div style="color:white;font-size:12px;font-weight:bold;letter-spacing:2px;text-transform:uppercase;margin-bottom:8px;opacity:0.85;">
        Active Storms
      </div>
      """ + alert_box_rows + """
      <div style="margin-top:12px;padding-top:10px;border-top:1px solid rgba(255,255,255,0.3);text-align:center;">
        <a href="https://www.nhc.noaa.gov" style="color:white;font-size:14px;font-weight:bold;
           background:rgba(255,255,255,0.2);padding:8px 20px;border-radius:4px;text-decoration:none;
           display:inline-block;border:1px solid rgba(255,255,255,0.5);">
          &#9888; CHECK NHC.NOAA.GOV FOR FULL DETAILS &#9888;
        </a>
      </div>
    </div>

    <!-- Body -->
    <div style="background:white;padding:20px 24px;border-radius:0 0 6px 6px;">

      <h3 style="color:#1a3a5c;margin:0 0 14px 0;font-size:15px;border-bottom:2px solid #e0e7ef;padding-bottom:8px;">
        &#128203; Active Storm Status
      </h3>
      """ + (storm_blocks if storm_blocks else '<p style="color:#888;font-size:13px;">No active storms currently tracked.</p>') + """

      <h3 style="color:#1a3a5c;margin:20px 0 14px 0;font-size:15px;border-bottom:2px solid #e0e7ef;padding-bottom:8px;">
        <span class="flash-badge" style="background:#c0392b;color:white;font-size:12px;font-weight:bold;
          padding:3px 8px;border-radius:3px;margin-right:8px;letter-spacing:1px;">&#x1F195; NEW</span>
        New Advisories This Check
      </h3>
      <table style="border-collapse:collapse;width:100%;">""" + new_rows + """</table>

    </div>

    <!-- Footer -->
    <p style="text-align:center;font-size:12px;color:#c0392b;font-weight:bold;margin-top:14px;">
      This is an automated OIC weather alert. Check
      <a href="https://www.nhc.noaa.gov" style="color:#c0392b;">nhc.noaa.gov</a>
      for the latest information.
    </p>
    <p style="text-align:center;font-size:11px;color:#aaa;margin-top:4px;">
      Auto-generated NHC monitor &nbsp;•&nbsp; Checks every 15 minutes
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
