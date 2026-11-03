#!/usr/bin/env python3
"""
Build time-filtered copies of the S2 Underground Common Intelligence Picture.

Reads the newest master export in
    ArcGIS Data for ATAK (KMZs)/Common Intelligence Picture/Master Database/
and writes, for every layer in it:
    30-Day Pulls/S2Underground_Common_Intelligence_Picture_Last_30_Days.kmz
    90-Day Pulls/S2Underground_Common_Intelligence_Picture_Last_90_Days.kmz
The master export itself is the full database and is left untouched.

Rules
  * Event markers are kept when their date is inside the window.
  * Event markers with no readable date are dropped.
  * Static reference layers (STATIC_LAYERS) are kept whole, dated or not.
  * Static reference marker types inside event layers (STATIC_TYPES), such as
    protest/demonstration sites, are always kept.

Markers are cut out of the original doc.kml text instead of re-serialising
the XML, so styles, icons and pop-ups stay exactly as ArcGIS wrote them.
Output is byte-for-byte reproducible: running twice on the same input on the
same day gives identical files, so the workflow only commits real changes.

Standard library only. Run from the repository root:
    python3 .github/scripts/cip_filter.py            # today, Eastern time
    python3 .github/scripts/cip_filter.py --today 2026-11-03
"""

import argparse
import html
import os
import re
import sys
import zipfile
from datetime import date, datetime, timedelta

# ---------------------------------------------------------------- settings --

CIP_DIR = os.path.join("ArcGIS Data for ATAK (KMZs)", "Common Intelligence Picture")
MASTER_DIR = os.path.join(CIP_DIR, "Master Database")

# Windows in days, and where each output goes (relative to CIP_DIR).
WINDOWS = {
    30: os.path.join("30-Day Pulls", "S2Underground_Common_Intelligence_Picture_Last_30_Days.kmz"),
    90: os.path.join("90-Day Pulls", "S2Underground_Common_Intelligence_Picture_Last_90_Days.kmz"),
}

# Whole layers that describe places, not events. Matched against the layer
# (folder) name, case-insensitive.
STATIC_LAYERS = {
    "domesticterroristorganizationtracker",
}

# Marker types (the AttackType / InstallationType value) inside event layers
# that describe places, not events. Case-insensitive; the misspelled variant
# exists in the data.
STATIC_TYPES = {
    "protestdemonstrationsite",
    "historicmassdemonstrationsite",
    "historicmassdeomonstrationsite",
}

# Attribute names that hold an event's date, in order of preference.
DATE_FIELDS = ["date", "date of event"]
FALLBACK_DATE_FIELDS = ["date of report"]
TYPE_FIELDS = ["attacktype", "installationtype", "event type", "crimetype"]

TIMEZONE = "America/New_York"

# ------------------------------------------------------------------ parsing --

MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july",
     "august", "september", "october", "november", "december"], 1)}
MASTER_NAME = re.compile(
    r"_([A-Za-z]+)_(\d{1,2})_(\d{4})_Export_?v?(\d+)?\.kmz$", re.IGNORECASE)

FOLDER_OPEN = re.compile(r"<Folder\b[^>]*>")
FOLDER_CLOSE = re.compile(r"</Folder>")
PLACEMARK = re.compile(r"[ \t]*<Placemark\b.*?</Placemark>[ \t]*\r?\n?", re.S)
NAME = re.compile(r"<name>(.*?)</name>", re.S)
DESCRIPTION = re.compile(r"<description>(.*?)</description>", re.S)
ROW = re.compile(r"<tr[^>]*>\s*<td>(.*?)</td>\s*<td>(.*?)</td>\s*</tr>", re.S)
TAG = re.compile(r"<[^>]+>")
DOC_NAME = re.compile(r"(<Document\b[^>]*>\s*<name>)(.*?)(</name>)", re.S)
DATE_FORMATS = ["%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y", "%m-%d-%Y"]


def find_master(master_dir):
    """Newest master export by the date (then export number) in its name."""
    best = None
    for fn in os.listdir(master_dir):
        m = MASTER_NAME.search(fn)
        if not m:
            continue
        month = MONTHS.get(m.group(1).lower())
        if not month:
            continue
        try:
            d = date(int(m.group(3)), month, int(m.group(2)))
        except ValueError:
            continue
        key = (d, int(m.group(4) or 0), fn)
        if best is None or key > best:
            best = key
    if best is None:
        sys.exit(f"No master export found in {master_dir}")
    return os.path.join(master_dir, best[2])


def clean(s):
    """Strip tags and entities from one table cell."""
    return html.unescape(TAG.sub("", s)).strip()


def attributes(placemark_text):
    """The pop-up table of a placemark as {lowercased name: value}."""
    m = DESCRIPTION.search(placemark_text)
    if not m:
        return {}
    body = m.group(1).strip()
    if body.startswith("<![CDATA["):
        body = body[9:-3]
    else:
        body = html.unescape(body)
    out = {}
    for k, v in ROW.findall(body):
        key = clean(k).split("\n")[-1].strip().lower()
        val = clean(v)
        if val and val.lower() != "<null>":
            out.setdefault(key, val)
    return out


def parse_date(value):
    token = value.strip().split()[0] if value.strip() else ""
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(token, fmt).date()
        except ValueError:
            pass
    return None


def event_date(attrs):
    for field in DATE_FIELDS + FALLBACK_DATE_FIELDS:
        if field in attrs:
            d = parse_date(attrs[field])
            if d:
                return d
    return None


def marker_type(attrs):
    for field in TYPE_FIELDS:
        if field in attrs:
            return attrs[field].replace(" ", "").lower()
    return ""


def layers(kml):
    """[(start, end, name)] for each top-level layer folder."""
    out = []
    pos = 0
    while True:
        o = FOLDER_OPEN.search(kml, pos)
        if not o:
            return out
        c = FOLDER_CLOSE.search(kml, o.end())
        if not c:
            return out
        n = NAME.search(kml, o.end(), c.start())
        out.append((o.start(), c.end(), html.unescape(n.group(1)).strip() if n else ""))
        pos = c.end()


def classify(kml):
    """Every placemark as (start, end, layer, date or None, static?)."""
    spans = layers(kml)
    items = []
    for m in PLACEMARK.finditer(kml):
        layer = next((n for s, e, n in spans if s <= m.start() < e), "")
        attrs = attributes(m.group(0))
        static = (layer.lower() in STATIC_LAYERS
                  or marker_type(attrs) in STATIC_TYPES)
        items.append((m.start(), m.end(), layer, event_date(attrs), static))
    return items


# ------------------------------------------------------------------ output --

def filtered_kml(kml, items, keep):
    parts, pos = [], 0
    for (start, end, *_), k in zip(items, keep):
        if not k:
            parts.append(kml[pos:start])
            pos = end
    parts.append(kml[pos:])
    return "".join(parts)


def write_kmz(src_path, dst_path, new_kml, doc_title):
    new_kml = DOC_NAME.sub(lambda m: m.group(1) + html.escape(doc_title, quote=False) + m.group(3),
                           new_kml, count=1)
    with zipfile.ZipFile(src_path) as src:
        infos = src.infolist()
        kml_info = next(i for i in infos if i.filename.lower().endswith(".kml"))
        original = src.read(kml_info).decode("utf-8")
        # Drop images the full map used that no kept marker uses any more.
        # Files the original never mentions are kept, just in case.
        def needed(name):
            base = os.path.basename(name)
            return base not in original or base in new_kml
        tmp = dst_path + ".tmp"
        os.makedirs(os.path.dirname(dst_path), exist_ok=True)
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as out:
            for info in infos:
                if info.is_dir():
                    continue
                if info is kml_info:
                    data = new_kml.encode("utf-8")
                elif needed(info.filename):
                    data = src.read(info)
                else:
                    continue
                zi = zipfile.ZipInfo(info.filename, date_time=info.date_time)
                zi.compress_type = zipfile.ZIP_DEFLATED
                zi.external_attr = 0o644 << 16
                out.writestr(zi, data)
    os.replace(tmp, dst_path)


def today_eastern():
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo(TIMEZONE)).date()
    except Exception:
        return datetime.utcnow().date()


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--today", help="YYYY-MM-DD (default: today, Eastern time)")
    ap.add_argument("--master", help="master KMZ to use (default: newest)")
    ap.add_argument("--out-dir", default=CIP_DIR, help="where the Pulls folders go")
    args = ap.parse_args()

    today = date.fromisoformat(args.today) if args.today else today_eastern()
    master = args.master or find_master(MASTER_DIR)
    with zipfile.ZipFile(master) as z:
        kml_name = next(n for n in z.namelist() if n.lower().endswith(".kml"))
        kml = z.read(kml_name).decode("utf-8")

    items = classify(kml)
    if not items:
        sys.exit(f"{master}: no markers found; refusing to publish empty files")

    layer_names = list(dict.fromkeys(i[2] for i in items))
    report = [f"Source: `{os.path.basename(master)}` ({len(items)} markers)",
              f"Today: {today} ({TIMEZONE})", ""]
    future = sum(1 for i in items if i[3] and i[3] > today)
    totals = {}

    for days, rel in WINDOWS.items():
        cutoff = today - timedelta(days=days)
        keep = [static or (d is not None and d >= cutoff)
                for _, _, _, d, static in items]
        events_kept = sum(1 for k, i in zip(keep, items) if k and not i[4])
        totals[days] = events_kept
        if events_kept == 0:
            sys.exit(f"Last {days} days: no event markers left; the source looks "
                     f"broken or stale. Nothing written.")

        title = f"S2U Common Intelligence Picture - Last {days} Days"
        dst = os.path.join(args.out_dir, rel)
        write_kmz(master, dst, filtered_kml(kml, items, keep), title)

        report.append(f"### Last {days} days (events on or after {cutoff})")
        report.append(f"`{rel}` - {os.path.getsize(dst) / 1e6:.2f} MB")
        report.append("")
        report.append("| Layer | Kept | Dropped: old | Dropped: no date |")
        report.append("|---|---:|---:|---:|")
        for name in layer_names:
            rows = [(k, i) for k, i in zip(keep, items) if i[2] == name]
            kept = sum(1 for k, _ in rows if k)
            old = sum(1 for k, i in rows if not k and i[3] is not None)
            undated = sum(1 for k, i in rows if not k and i[3] is None)
            label = name + (" (static, kept whole)" if name.lower() in STATIC_LAYERS else "")
            report.append(f"| {label} | {kept} | {old} | {undated} |")
        report.append("")

    if totals[30] > totals[90]:
        sys.exit("30-day file has more events than the 90-day file; aborting")
    if future:
        report.append(f"Note: {future} marker(s) are dated after today and were kept.")

    text = "\n".join(report)
    print(text)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write(text + "\n")


if __name__ == "__main__":
    main()
