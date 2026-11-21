"""
lab_analyser.py - simple blood-report checker (NOT a doctor).

Reads text from a PDF or TXT report, finds common lab values and compares them with
typical adult reference ranges. Reports from different labs print their own ranges, so
always trust the range printed on the report and your doctor over this page.
Scanned images / photos cannot be read (no OCR).
"""
import re

# key: (label, unit, [regex aliases], male_range, female_range)   range = (low, high), None = no limit
TESTS = [
    ("hb", "Hemoglobin", "g/dL", [r"h(?:a)?emoglobin(?!\s*a1c)", r"\bhb\b(?!a1c)"], (13.0, 17.0), (12.0, 15.0)),
    ("wbc", "WBC (total leukocyte count)", "/cumm", [r"total\s+leu[ck]ocyte\s+count", r"\btlc\b", r"\bwbc\b"], (4000, 11000), (4000, 11000)),
    ("plt", "Platelet count", "/cumm", [r"platelet(?:\s+count)?", r"\bplt\b"], (150000, 450000), (150000, 450000)),
    ("rbc", "RBC count", "million/cumm", [r"\brbc(?:\s+count)?\b", r"red\s+blood\s+cell(?:s)?(?:\s+count)?"], (4.5, 5.9), (4.1, 5.1)),
    ("glu", "Fasting blood glucose", "mg/dL", [r"fasting\s+(?:blood\s+)?(?:glucose|sugar)", r"\bfbs\b", r"fasting\s+plasma\s+glucose"], (70, 100), (70, 100)),
    ("a1c", "HbA1c", "%", [r"hba1c", r"glyc(?:o)?sylated\s+h(?:a)?emoglobin"], (4.0, 5.6), (4.0, 5.6)),
    ("chol", "Total cholesterol", "mg/dL", [r"total\s+cholesterol", r"cholesterol\s*,?\s*total"], (None, 200), (None, 200)),
    ("ldl", "LDL cholesterol", "mg/dL", [r"\bldl(?:\s+cholesterol)?"], (None, 100), (None, 100)),
    ("hdl", "HDL cholesterol", "mg/dL", [r"\bhdl(?:\s+cholesterol)?"], (40, None), (50, None)),
    ("tg", "Triglycerides", "mg/dL", [r"triglycerides?"], (None, 150), (None, 150)),
    ("creat", "Serum creatinine", "mg/dL", [r"(?:serum\s+)?creatinine"], (0.7, 1.3), (0.6, 1.1)),
    ("urea", "Blood urea", "mg/dL", [r"blood\s+urea(?!\s+nitrogen)", r"\burea\b"], (15, 40), (15, 40)),
    ("tsh", "TSH", "mIU/L", [r"\btsh\b", r"thyroid\s+stimulating\s+hormone"], (0.4, 4.0), (0.4, 4.0)),
    ("alt", "ALT / SGPT", "U/L", [r"\balt\b", r"\bsgpt\b"], (7, 56), (7, 56)),
    ("ast", "AST / SGOT", "U/L", [r"\bast\b", r"\bsgot\b"], (10, 40), (10, 40)),
    ("bili", "Total bilirubin", "mg/dL", [r"total\s+bilirubin", r"bilirubin\s*,?\s*total"], (0.1, 1.2), (0.1, 1.2)),
    ("na", "Sodium", "mmol/L", [r"\bsodium\b", r"\bna\+?\b"], (135, 145), (135, 145)),
    ("k", "Potassium", "mmol/L", [r"\bpotassium\b"], (3.5, 5.1), (3.5, 5.1)),
    ("vitd", "Vitamin D (25-OH)", "ng/mL", [r"vitamin\s*d(?:\s*,?\s*25[\s-]*(?:oh|hydroxy)[a-z\s]*)?"], (30, 100), (30, 100)),
    ("b12", "Vitamin B12", "pg/mL", [r"vitamin\s*b[\s-]*12", r"\bb12\b"], (200, 900), (200, 900)),
]

NUM = r"[^\d\n]{0,40}?(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)"


def extract_text(path):
    """Returns (text, reason_if_failed)."""
    low = path.lower()
    try:
        if low.endswith(".txt"):
            with open(path, "r", encoding="utf-8", errors="ignore") as fh:
                return fh.read(), ""
        if low.endswith(".pdf"):
            from pypdf import PdfReader
            text = "\n".join((p.extract_text() or "") for p in PdfReader(path).pages)
            if len(text.strip()) < 20:
                return "", "This PDF looks like a scan (no readable text). Photos and scans can't be read without OCR."
            return text, ""
    except Exception as exc:
        return "", f"Could not read this file ({exc})."
    return "", "Only PDF and TXT reports with real text can be analysed. Images, scans and Word files are not supported."


def _range(male, female, gender):
    g = (gender or "").strip().lower()
    if g.startswith("f"):
        return female
    if g.startswith("m"):
        return male
    lows = [x[0] for x in (male, female) if x[0] is not None]
    highs = [x[1] for x in (male, female) if x[1] is not None]
    return (min(lows) if lows else None, max(highs) if highs else None)


def _normalise(key, value):
    # some labs print counts in thousands (e.g. WBC 7.2, platelets 250)
    if key == "wbc" and value < 100:
        return value * 1000
    if key == "plt" and value < 1000:
        return value * 1000
    return value


def analyse(text, gender=""):
    results, seen = [], set()
    for key, label, unit, aliases, male, female in TESTS:
        for alias in aliases:
            m = re.search(alias + NUM, text, re.IGNORECASE)
            if not m:
                continue
            try:
                value = _normalise(key, float(m.group(m.lastindex).replace(",", "")))
            except (ValueError, IndexError):
                continue
            lo, hi = _range(male, female, gender)
            if lo is not None and value < lo:
                flag = "Low"
            elif hi is not None and value > hi:
                flag = "High"
            else:
                flag = "Normal"
            line_start = text.rfind("\n", 0, m.start()) + 1
            line_end = text.find("\n", m.end())
            line = text[line_start: line_end if line_end != -1 else len(text)].strip()[:120]
            if key in seen:
                break
            seen.add(key)
            if lo is None:
                rng = f"below {hi:g}"
            elif hi is None:
                rng = f"above {lo:g}"
            else:
                rng = f"{lo:g} - {hi:g}"
            results.append({"label": label, "value": value, "unit": unit, "range": rng, "flag": flag, "line": line})
            break
    return results
