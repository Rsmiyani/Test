import os
import re
import time
import hashlib
from datetime import datetime, timedelta
import pandas as pd
from pathlib import Path
from urllib.parse import urljoin
from html import escape
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright
import yagmail

try:
    from openai import OpenAI
except Exception:
    OpenAI = None

from reportlab.lib.pagesizes import letter
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle

load_dotenv()

# Main sender Gmail. Override it from .env if needed.
GMAIL_ID = os.getenv("GMAIL_ID", "hroshan6198@gmail.com").strip()
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD", "").strip()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip()

# Deduplication and Cooldown Configuration
RECRUITER_COOLDOWN_DAYS = 5  # Block emailing the same recruiter if contacted within 5 days
DEDUPLICATE_SAME_JD = True   # Block emailing if this exact same JD was already processed

PROFILE_DIR = "linkedin_saved_login_nikhilkumar_manual_final"
OUTPUT = Path("output_nikhilkumar_manual_final")
OUTPUT.mkdir(exist_ok=True)

SENT_FILE = OUTPUT / "sent_emails.csv"

CONTINUOUS_MODE = True
MAX_CYCLES = 999999
SCROLL_ROUNDS_PER_CYCLE = 8
DELAY_SECONDS = 12
CYCLE_SLEEP_SECONDS = 10
MAX_EMAILS_PER_RUN = 999999
MAX_POST_TEXT_CHARS = 6500
TAILORED_SUMMARY_COUNT = 5
TAILORED_SPECIALTY_COUNT = 5
TAILORED_PROGIENCE_COUNT = 5
TAILORED_EXCEEGO_COUNT = 4
MIN_SECONDS_BETWEEN_EMAILS = 3
SCROLL_UNTIL_NEW_ROUNDS = 5
SKIP_ALREADY_SEEN_IN_RUN = True

CANDIDATE = {
    "name": "Nikhilkumar Bhuyakar",
    "email": "bhuyakarnikhilkumar677@gmail.com",
    "phone": "+1 (860) 796-4968",
    "linkedin": "https://www.linkedin.com/in/nikhilbuyakar-348757247/",
    "location": "Tampa, FL",
    "relocation": "Yes",
    "work_auth": "STEM OPT",
    "availability": "Within 2 Weeks",
    "experience": "6+ Years",
    "salary": "Open to Opportunities"
}

EMAIL_REGEX = re.compile(
    r"\b[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}\b"
)

# Catches emails written with spaced-out or bracketed "at"/"dot" to dodge
# scrapers, e.g. "name [at] gmail [dot] com", "name＠gmail．com". Every
# match gets normalized and re-validated against EMAIL_REGEX + a stoplist
# below, so this doesn't introduce false positives.
#
# Tier 1: bracket/paren/fullwidth forms only. Unambiguous — nobody writes
# "[at]" or "(dot)" by accident, so this pass needs no stoplist.
BRACKET_CANDIDATE_REGEX = re.compile(
    r"[a-zA-Z0-9._%+\-]+"
    r"\s*(?:\[\s*at\s*\]|\(\s*at\s*\)|\{\s*at\s*\}|＠)\s*"
    r"[a-zA-Z0-9.\-\s]{2,40}?"
    r"\s*(?:\[\s*dot\s*\]|\(\s*dot\s*\)|\{\s*dot\s*\}|．|\.)\s*"
    r"[a-zA-Z]{2,}",
    flags=re.IGNORECASE,
)

# Tier 2: bare " at " / " dot " word forms. Run only on text with tier-1
# matches masked out, and always stoplist-guarded below — this is the
# risky pass, so it's kept as a fallback rather than the primary path.
BARE_WORD_CANDIDATE_REGEX = re.compile(
    r"[a-zA-Z0-9._%+\-]+"
    r"\s*\bat\b\s*"
    r"[a-zA-Z0-9.\-\s]{2,40}?"
    r"\s*\bdot\b\s*"
    r"[a-zA-Z]{2,}",
    flags=re.IGNORECASE,
)

_AT_TOKEN_RE = re.compile(r"\[\s*at\s*\]|\(\s*at\s*\)|\{\s*at\s*\}|\bat\b|＠", flags=re.IGNORECASE)
_DOT_TOKEN_RE = re.compile(r"\[\s*dot\s*\]|\(\s*dot\s*\)|\{\s*dot\s*\}|\bdot\b|．", flags=re.IGNORECASE)


def deobfuscate_email_fragment(fragment):
    fragment = _AT_TOKEN_RE.sub("@", fragment)
    fragment = _DOT_TOKEN_RE.sub(".", fragment)
    fragment = re.sub(r"\s+", "", fragment)
    return fragment


_COMMON_WORD_STOPLIST = {
    "reach", "out", "is", "at", "to", "we", "are", "looking", "for",
    "this", "that", "here", "now", "please", "contact", "email",
    "send", "reply", "apply", "the", "and", "with", "from", "you",
    "your", "our", "top", "list", "see", "not", "dot", "com",
}


def _valid_candidate(candidate):
    if not EMAIL_REGEX.fullmatch(candidate):
        return False
    local_part = candidate.split("@", 1)[0].lower()
    domain_lead = candidate.split("@", 1)[1].split(".", 1)[0].lower()
    if len(local_part) < 3 or local_part in _COMMON_WORD_STOPLIST:
        return False
    if domain_lead in _COMMON_WORD_STOPLIST:
        return False
    return True


def extract_obfuscated_emails(text):
    """Find emails written with 'at'/'dot' obfuscation. Every candidate
    is normalized then re-checked against the strict EMAIL_REGEX, and the
    local-part/domain-lead tokens are checked against a common-word
    stoplist, so ordinary sentences using the words 'at' or 'dot' won't
    slip through unless they actually resolve to a plausible email."""
    found = []
    masked = text
    for match in BRACKET_CANDIDATE_REGEX.finditer(text):
        candidate = deobfuscate_email_fragment(match.group(0))
        if _valid_candidate(candidate):
            found.append(candidate)
            # mask out the matched span so tier 2 doesn't re-parse it
            # or let an earlier bare "at" swallow it
            start, end = match.span()
            masked = masked[:start] + (" " * (end - start)) + masked[end:]

    for match in BARE_WORD_CANDIDATE_REGEX.finditer(masked):
        candidate = deobfuscate_email_fragment(match.group(0))
        if _valid_candidate(candidate):
            found.append(candidate)

    return found

BAD_EMAIL_PREFIXES = {
    "noreply", "no-reply", "donotreply", "do-not-reply",
    "admin", "support", "help", "info", "contact",
    "sales", "marketing", "privacy", "security",
    "abuse", "postmaster", "mailer-daemon"
}

BAD_EMAIL_DOMAINS = {
    "linkedin.com",
    "example.com",
    "test.com"
}


def clean(text):
    if text is None:
        return ""
    text = str(text)
    text = text.replace("\u00a0", " ")
    text = text.replace("\u200b", "")
    text = text.replace("\ufeff", "")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def normalize_email(email):
    email = clean(email).lower()
    email = email.strip(".,;:()[]{}<>\"'")
    return email


def extract_emails(text):
    text = clean(text)
    found = EMAIL_REGEX.findall(text) + extract_obfuscated_emails(text)
    unique = []
    seen = set()
    for email in found:
        email = normalize_email(email)
        if email and email not in seen:
            seen.add(email)
            unique.append(email)
    return unique


def blocked_emails():
    return {
        normalize_email(GMAIL_ID),
        normalize_email(CANDIDATE.get("email", "")),
        "bhuyakarnikhilkumar677@gmail.com",
        "hroshan6198@gmail.com",
        "hrithikroshan6198@gmail.com",
        "al@equestsolutions.com",
    }


def is_valid_recruiter_email(email):
    email = normalize_email(email)
    if not email or "@" not in email:
        return False
    if email in blocked_emails():
        return False
    local, domain = email.rsplit("@", 1)
    if domain in BAD_EMAIL_DOMAINS:
        return False
    if local in BAD_EMAIL_PREFIXES:
        return False
    if len(local) <= 1:
        return False
    return True


def filter_recruiter_emails(emails):
    valid = []
    seen = set()
    for email in emails:
        email = normalize_email(email)
        if is_valid_recruiter_email(email) and email not in seen:
            seen.add(email)
            valid.append(email)
    return valid


DAILY_RESPONSE_TARGET = 20

BENCHSALES_BLOCK_KEYWORDS = [
    "bench sales",
    "benchsales",
    "bench-sale",
    "bench_sale",
    "benchsales recruiter",
    "bench sales recruiter",
]

BENCHSALES_BLOCK_PATTERNS = [
    r"\bbench\s*sales\b",
    r"\bbench[-_\s]*sales\b",
    r"\bbenchsales\b",
]

JOB_REQUIREMENT_KEYWORDS = [
    "hiring", "we are hiring", "now hiring", "job opening", "opening", "open role",
    "requirement", "urgent requirement", "position", "role", "opportunity",
    "looking for", "need", "needed", "required", "contract", "fulltime",
    "full time", "w2", "c2c", "onsite", "remote", "hybrid", "job description",
    "jd", "data analyst", "bi analyst", "business analyst", "sql", "power bi",
    "tableau", "etl", "data engineer", "reporting analyst", "financial analyst"
]


def is_benchsales_post(post_text):
    low = clean(post_text).lower()
    for keyword in BENCHSALES_BLOCK_KEYWORDS:
        if keyword in low:
            return True, keyword
    for pattern in BENCHSALES_BLOCK_PATTERNS:
        if re.search(pattern, low, flags=re.IGNORECASE):
            return True, pattern
    return False, ""


def looks_like_real_job_requirement(post_text):
    low = clean(post_text).lower()
    return any(keyword in low for keyword in JOB_REQUIREMENT_KEYWORDS)


def should_send_to_post(post_text):
    blocked, reason = is_benchsales_post(post_text)
    if blocked:
        return False, f"Bench Sales skipped only: {reason}"
    if not looks_like_real_job_requirement(post_text):
        return False, "no clear job/recruiter signal"
    return True, "valid recruiter/job post"


def normalize_post_link(raw_link):
    link = clean(str(raw_link or ""))
    if not link:
        return ""
    link = link.replace("&amp;", "&")
    link = link.replace("%3A", ":").replace("%3a", ":")
    link = link.replace("%2F", "/").replace("%2f", "/")

    m = re.search(r"urn:li:activity:(\d+)", link)
    if m:
        return f"https://www.linkedin.com/feed/update/urn:li:activity:{m.group(1)}/"

    m = re.search(r"activity[-:](\d{10,})", link)
    if m:
        return f"https://www.linkedin.com/feed/update/urn:li:activity:{m.group(1)}/"

    if link.startswith("www.linkedin.com"):
        link = "https://" + link
    if link.startswith("/feed/update/") or link.startswith("/posts/"):
        link = "https://www.linkedin.com" + link

    link = link.split("?")[0].split("#")[0].rstrip("/ ") + "/"
    if "linkedin.com/feed/update/" in link or "linkedin.com/posts/" in link:
        return link
    if "linkedin.com" in link:
        return link
    return ""


def link_rank(link):
    link = clean(link).lower()
    if "/feed/update/" in link or "urn:li:activity:" in link:
        return 1
    if "/posts/" in link:
        return 2
    if "/jobs/view/" in link:
        return 3
    if "/in/" in link:
        return 9
    if "linkedin.com" in link:
        return 7
    return 99


def get_post_link_from_card(page, card):
    """
    Optimized data-urn lookup framework with smart fallback parameters.
    """
    try:
        # Method 1: Target structural tracking values first (data-urn)
        urn = card.evaluate("""
            el => {
                if (el.getAttribute('data-urn')) return el.getAttribute('data-urn');
                let childWithUrn = el.querySelector('[data-urn]');
                if (childWithUrn) return childWithUrn.getAttribute('data-urn');
                let actUrn = el.querySelector('[data-activity-urn]');
                if (actUrn) return actUrn.getAttribute('data-activity-urn');
                return null;
            }
        """)
        if urn:
            fixed = normalize_post_link(urn)
            if fixed:
                return fixed
    except Exception:
        pass

    try:
        # Method 2: Scan document layout anchors
        hrefs = card.evaluate("""
            el => Array.from(el.querySelectorAll('a[href]'))
                .map(a => a.href || a.getAttribute('href'))
                .filter(Boolean)
        """)
        fixed_links = []
        for href in hrefs:
            href = urljoin("https://www.linkedin.com", href)
            fixed = normalize_post_link(href)
            if fixed:
                fixed_links.append(fixed)
        if fixed_links:
            fixed_links = sorted(set(fixed_links), key=link_rank)
            return fixed_links[0]
    except Exception:
        pass

    try:
        # Method 3: Direct UI Copy Link Trigger Action
        buttons = card.locator("button").all()
        for btn in buttons:
            label = (btn.get_attribute("aria-label") or "").lower()
            try:
                btn_text = clean(btn.inner_text(timeout=200)).lower()
            except Exception:
                btn_text = ""
            if "more" in label or "control" in label or "actions" in label or btn_text in ["...", "more"]:
                btn.click(timeout=1500)
                page.wait_for_timeout(500)
                for option in ["Copy link to post", "Copy link to this post", "Copy link"]:
                    try:
                        opt = page.get_by_text(option, exact=False).first
                        if opt.count() > 0:
                            opt.click(timeout=1500)
                            page.wait_for_timeout(500)
                            copied = page.evaluate("navigator.clipboard.readText()")
                            page.keyboard.press("Escape")
                            fixed = normalize_post_link(copied)
                            if fixed:
                                return fixed
                    except Exception:
                        pass
        page.keyboard.press("Escape")
    except Exception:
        pass

    # Method 4 Fallback: Prevent card dropping by returning active screen location
    try:
        return page.url
    except Exception:
        return "https://www.linkedin.com/feed/"


def get_cards(page):
    cards = []
    try:
        more_buttons = page.get_by_text("more", exact=False)
        for i in range(min(more_buttons.count(), 20)):
            try:
                more_buttons.nth(i).click(timeout=800)
                page.wait_for_timeout(250)
            except Exception:
                pass
    except Exception:
        pass

    # Expanded query paths matching modern UI layers
    selectors = [
        "div.feed-shared-update-v2",
        "div[data-activity-urn]",
        "div[data-urn*='activity']",
        "li.reusable-search__result-container",
        "article",
        "main div"
    ]

    for selector in selectors:
        try:
            found = page.locator(selector).all()
            for card in found:
                try:
                    text = clean(card.inner_text(timeout=500))
                    if len(text) < 40:
                        continue
                    if not extract_emails(text):
                        continue
                    low = text.lower()
                    junk = [
                        "home my network jobs messaging",
                        "skip to main content",
                        "skip to search",
                        "sort by",
                        "content type"
                    ]
                    if any(j in low for j in junk):
                        continue
                    cards.append(card)
                except Exception:
                    pass
        except Exception:
            pass

    unique = []
    seen = set()
    for card in cards:
        try:
            text = clean(card.inner_text(timeout=500))
            key = post_key_from_text(text)
            if key not in seen:
                seen.add(key)
                unique.append(card)
        except Exception:
            pass
    return unique


def get_recruiter_name_from_card(card, post_text=""):
    """Extracts author/recruiter name from LinkedIn card or fallback regex."""
    selectors = [
        ".update-components-actor__name",
        ".feed-shared-actor__name",
        "span.update-components-actor__single-line-truncate",
        ".update-components-actor__title span",
        "a[data-actor-name]"
    ]
    for sel in selectors:
        try:
            loc = card.locator(sel).first
            if loc.count() > 0:
                name = clean(loc.inner_text(timeout=300))
                if name:
                    name = re.sub(r"•.*", "", name)
                    name = re.sub(r"\(.*?\)", "", name).strip()
                    if 2 <= len(name) <= 50 and not any(w in name.lower() for w in ["view", "follow", "connect", "linkedin"]):
                        return name
        except Exception:
            pass

    m = re.search(r"(?:regards|thanks|contact|from|by|i am|this is)\s*[:,\-]?\s*([A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,2})", post_text)
    if m:
        return m.group(1).strip()

    return "Hiring Manager"


def get_jd_fingerprint(post_text):
    """
    Extracts core text of the job description, ignoring emails, urls, phone numbers,
    and returns a unique SHA-256 hash representing the job description content.
    """
    text = clean(post_text).lower()
    text = EMAIL_REGEX.sub(" ", text)
    text = re.sub(r"https?://\S+", " ", text)
    text = re.sub(r"\+?\d[\d\s().-]{7,}\d", " ", text)
    words = re.findall(r"[a-z0-9+#./-]{2,}", text)
    core_text = " ".join(words)
    return hashlib.sha256(core_text.encode("utf-8")).hexdigest()[:16]


def already_sent(email, post_link=None, jd_hash=None):
    """
    Checks if an email should be skipped based on:
    1. Recruiter 5-day cooldown
    2. Same JD content comparison (by jd_hash)
    3. Same post link
    Returns (bool, str reason).
    """
    email = normalize_email(email)
    if not SENT_FILE.exists():
        return False, ""
    try:
        df = pd.read_csv(SENT_FILE)
    except Exception:
        return False, ""
    if df.empty:
        return False, ""

    # Support both recruiter_email and legacy email column names
    email_col = "recruiter_email" if "recruiter_email" in df.columns else ("email" if "email" in df.columns else None)
    if not email_col:
        return False, ""

    email_matches = df[df[email_col].astype(str).str.strip().str.lower() == email]
    if email_matches.empty:
        return False, ""

    # Check 1: Same JD hash check
    if DEDUPLICATE_SAME_JD and jd_hash and "jd_hash" in email_matches.columns:
        same_jd = email_matches[email_matches["jd_hash"].astype(str).str.strip() == str(jd_hash).strip()]
        if not same_jd.empty:
            return True, "already sent to this recruiter for this exact JD"

    # Check 2: Same post link check
    post_col = "post_url" if "post_url" in email_matches.columns else ("post_link" if "post_link" in email_matches.columns else None)
    if post_link and post_col:
        norm_link = normalize_post_link(post_link)
        if norm_link:
            same_link = email_matches[email_matches[post_col].astype(str).str.strip() == norm_link]
            if not same_link.empty:
                return True, "already sent to this recruiter for this post link"

    # Check 3: 5-Day Recruiter Cooldown check
    time_col = "timestamp" if "timestamp" in email_matches.columns else ("time" if "time" in email_matches.columns else None)
    if RECRUITER_COOLDOWN_DAYS and time_col:
        cutoff = datetime.now() - timedelta(days=RECRUITER_COOLDOWN_DAYS)
        for t_str in email_matches[time_col].dropna():
            try:
                sent_time = datetime.strptime(str(t_str).strip(), "%Y-%m-%d %H:%M:%S")
                if sent_time >= cutoff:
                    days_ago = (datetime.now() - sent_time).days
                    return True, f"recruiter was emailed {days_ago} day(s) ago (within {RECRUITER_COOLDOWN_DAYS}-day cooldown)"
            except Exception:
                pass

    return False, ""


def save_sent(recruiter_name, recruiter_email, role, post_url, job_description, email_sent_status="sent", jd_hash=""):
    """
    Saves full submission log record to CSV matching the requested schema.
    """
    recruiter_email = normalize_email(recruiter_email)
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")

    row = pd.DataFrame([{
        "recruiter_name": str(recruiter_name or "Hiring Manager").strip(),
        "recruiter_email": recruiter_email,
        "role": str(role or "Data Analyst").strip(),
        "post_url": normalize_post_link(post_url) or str(post_url or ""),
        "job_description": clean(job_description),
        "email_sent_status": str(email_sent_status or "sent"),
        "timestamp": timestamp,
        "jd_hash": str(jd_hash or "")
    }])

    cols = ["recruiter_name", "recruiter_email", "role", "post_url", "job_description", "email_sent_status", "timestamp", "jd_hash"]
    row = row[cols]

    if SENT_FILE.exists():
        try:
            old = pd.read_csv(SENT_FILE)
            row = pd.concat([old, row], ignore_index=True)
        except Exception:
            pass
    row.to_csv(SENT_FILE, index=False)


def make_run_state():
    return {
        "seen_post_keys": set(),
        "seen_email_jd_pairs": set(),
        "seen_emails_in_run": set(),
        "stats": {
            "cards": 0,
            "already_seen_cards": 0,
            "not_job": 0,
            "no_email": 0,
            "duplicate_emails": 0,
            "new_emails": 0,
            "sent": 0,
            "errors": 0,
        }
    }


def post_key_from_text(text):
    text = clean(text).lower()
    emails = filter_recruiter_emails(extract_emails(text))
    without_urls = re.sub(r"https?://\S+", " ", text)
    without_emails = EMAIL_REGEX.sub(" ", without_urls)
    words = re.findall(r"[a-z0-9+#./-]{2,}", without_emails)
    compact = " ".join(words[:90])
    email_part = "|".join(emails[:5])
    return f"{email_part}::{compact[:900]}"


def email_post_pair_key(email, post_link):
    return (normalize_email(email), normalize_post_link(post_link) or clean(post_link))


def stable_text_id(text):
    text = clean(text).lower()
    text = EMAIL_REGEX.sub(" ", text)
    text = re.sub(r"https?://\S+", " ", text)
    words = re.findall(r"[a-z0-9+#./-]{2,}", text)
    compact = "_".join(words[:28])
    return safe_filename(compact, max_len=80)


def print_run_stats(run_state, prefix="Run stats"):
    stats = run_state["stats"]
    print(
        f"{prefix}: cards={stats['cards']}, "
        f"seen={stats['already_seen_cards']}, "
        f"not_job={stats['not_job']}, "
        f"no_email={stats['no_email']}, "
        f"duplicates={stats['duplicate_emails']}, "
        f"new={stats['new_emails']}, "
        f"sent={stats['sent']}, "
        f"errors={stats['errors']}"
    )


def safe_filename(text, max_len=70):
    text = clean(text)
    text = re.sub(r"[^a-zA-Z0-9]+", "_", text).strip("_")
    return (text[:max_len] or "LinkedIn_Job_Post")


def reportlab_text(text):
    text = escape(clean(text))
    text = text.replace("&lt;b&gt;", "<b>").replace("&lt;/b&gt;", "</b>")
    return text


def detect_job_focus(post_text):
    low = clean(post_text).lower()
    focus_map = [
        ("Data Analyst", ["data analyst", "sql", "reporting", "kpi"]),
        ("BI Analyst / Power BI Developer", ["power bi", "dax", "power query", "bi analyst", "business intelligence"]),
        ("Tableau Developer", ["tableau", "dashboard", "visualization"]),
        ("ETL / Data Engineer", ["etl", "elt", "ssis", "azure data factory", "snowflake", "databricks", "data pipeline"]),
        ("SQL / Business Analyst", ["business analyst", "requirement gathering", "sttm", "uat", "stakeholder"]),
    ]
    best_title = "Data Analyst"
    best_score = 0
    for title, words in focus_map:
        score = sum(1 for word in words if word in low)
        if score > best_score:
            best_title = title
            best_score = score
    return best_title


def extract_job_keywords(post_text):
    low = clean(post_text).lower()
    # Only skills that actually appear on the candidate's real resume —
    # this list is his true skill set, not a generic wishlist, so a
    # match here means he genuinely has that skill.
    keywords = [
        "SQL", "PL/SQL", "T-SQL", "Python", "R", "Power BI", "Tableau",
        "DAX", "Power Query", "Excel", "Advanced Excel", "Pivot Tables",
        "Azure Data Factory", "Azure Synapse", "Snowflake", "Databricks",
        "SSIS", "ETL", "ELT", "ADLS", "AWS", "S3", "SQL Server", "MySQL",
        "PostgreSQL", "MongoDB", "Azure SQL", "Jira", "Git", "CI/CD",
        "Agile", "Scrum", "KPI Reporting", "Financial Reporting",
        "Regulatory Reporting", "Reconciliation", "Data Quality",
        "Data Profiling", "Data Modeling", "Data Warehousing",
        "Data Validation", "Window Functions", "Stored Procedures",
        "STTM", "UAT", "VBA", "Pandas", "NumPy"
    ]
    found = []
    for keyword in keywords:
        pattern = re.escape(keyword.lower())
        if re.search(rf"(?<![a-z0-9]){pattern}(?![a-z0-9])", low):
            found.append(keyword)
    return found[:18]


def clean_job_text_for_ai(post_text):
    text = clean(post_text)
    text = EMAIL_REGEX.sub("[recruiter email removed]", text)
    text = re.sub(r"https?://\S+", "[link removed]", text)
    return text[:MAX_POST_TEXT_CHARS]


def default_tailoring(post_text):
    focus = detect_job_focus(post_text)
    keywords = extract_job_keywords(post_text)
    if not keywords:
        keywords = ["SQL", "Power BI", "Tableau", "ETL", "Data Modeling", "KPI Reporting"]

    keyword_text = ", ".join(keywords[:10])
    return {
        "target_title": focus,
        "job_keywords": keywords,
        "summary": [
            f"Data Analyst with 6+ years of experience transforming business, finance, operations, and compliance data into reporting, dashboards, and decision-support insights, with strong alignment to {keyword_text}.",
            "Hands-on expertise in SQL, PL/SQL, and T-SQL — joins, CTEs, window functions, stored procedures, triggers, views, and query performance tuning.",
            "Power BI and Tableau dashboard developer — DAX, Power Query, data modeling, drill-down reporting, automated refresh, KPI scorecards, and executive reporting.",
            "Proficient across Azure Data Factory, Snowflake, Databricks, Azure Synapse, SQL Server, MySQL, PostgreSQL, and Python-based ETL/ELT workflows.",
            "Agile/Scrum collaborator with experience in requirement gathering, STTM documentation, source-to-target validation, UAT support, and production reporting support."
        ],
        "skills": [
            f"<b>Matched Job Skills:</b> {keyword_text}",
            "<b>SQL/Data:</b> SQL, PL/SQL, T-SQL, Stored Procedures, Triggers, Views, CTEs, Window Functions, Query Optimization, Data Modeling",
            "<b>BI Tools:</b> Power BI, Tableau, DAX, Power Query, Excel, Advanced Excel, Pivot Tables, Power Automate",
            "<b>ETL/Cloud:</b> Azure Data Factory, Azure Synapse, Snowflake, Databricks, SSIS, ETL/ELT Pipelines, ADLS, AWS S3",
            "<b>Programming:</b> Python, R, VBA, Pandas, NumPy, Automation Scripts, Data Validation Scripts",
            "<b>Databases & Tools:</b> SQL Server, MySQL, PostgreSQL, MongoDB, Azure SQL, Jira, Git, CI/CD basics, Agile, Scrum"
        ],
        "experience": {
            "Specialty Appliances": [
                f"Developed executive Power BI dashboards using DAX, Power Query, slicers, drill-downs, bookmarks, and automated refresh schedules, with emphasis on {keyword_text} for leadership reporting.",
                "Built SQL-based reporting datasets using joins, CTEs, window functions, stored procedures, views, and query optimization to support finance, sales, inventory, and operations teams.",
                "Designed ETL pipelines using Azure Data Factory, SQL, Snowflake, and Databricks to ingest, transform, validate, and publish business-ready datasets.",
                "Created automated data validation, reconciliation, and exception reports using SQL and Python to improve reporting accuracy and reduce manual review effort.",
                "Performed data profiling, cleansing, deduplication, mapping, and root-cause analysis for inconsistent source data across ERP, CRM, and operational systems."
            ],
            "Progience Technologies": [
                "Analyzed finance, risk, compliance, customer, and operational datasets to deliver SQL reports, Power BI dashboards, Tableau views, and Excel-based business insights.",
                "Designed PL/SQL and T-SQL scripts for extraction, transformation, reconciliation, data cleansing, and reporting database preparation.",
                "Created data models, stored procedures, triggers, views, validation logic, and audit-support datasets for recurring business reporting requirements.",
                "Built Tableau and Power BI dashboards for cost, revenue, margin, customer trends, productivity, SLA performance, and month-end reporting.",
                "Supported QA and UAT by preparing test data, validating report logic, comparing source-to-target values, and resolving data defects before production release."
            ],
            "Exceego Infolabs": [
                "Prepared accounting, revenue, taxation, and transaction datasets for reporting, analysis, dashboarding, and business review meetings.",
                "Executed SQL queries to analyze trends, exceptions, missing values, duplicate records, mismatches, and operational performance metrics.",
                "Built Excel and Power BI reports using pivot tables, charts, formulas, data refresh processes, and structured reporting templates.",
                "Supported ETL testing, report validation, data reconciliation, and documentation for reporting system enhancements."
            ]
        }
    }


def ai_tailoring(post_text):
    if not OPENAI_API_KEY or OpenAI is None:
        return None

    prompt = f"""
Create resume tailoring content for this candidate and LinkedIn job post.

Rules:
- Do not invent employers, degrees, certifications, immigration status, years of experience, or any skill/technology not listed in the candidate facts below.
- Only select and reword real accomplishments — do not fabricate new ones.
- Keep grammar professional and simple.
- Tailor wording to the job post using only skills the candidate actually has.
- Keep the resume the same length as the original resume, not a short summary.
- Return only valid JSON with keys:
  target_title: string
  job_keywords: array of strings
  summary: array of 5 strings
  skills: array of 6 strings, HTML <b> labels allowed
  experience: object with keys "Specialty Appliances", "Progience Technologies", "Exceego Infolabs"
- Specialty Appliances must have 5 bullet strings.
- Progience Technologies must have 5 bullet strings.
- Exceego Infolabs must have 4 bullet strings.

Candidate facts:
Name: {CANDIDATE['name']}
Email: {CANDIDATE['email']}
Phone: {CANDIDATE['phone']}
Location: {CANDIDATE['location']}
Work authorization: {CANDIDATE['work_auth']}
Experience: {CANDIDATE['experience']}
Real skill set: SQL, PL/SQL, T-SQL, Python, R, Power BI, Tableau, DAX, Power Query, Excel, Azure Data Factory, Azure Synapse, Snowflake, Databricks, SSIS, ETL/ELT, AWS S3, SQL Server, MySQL, PostgreSQL, MongoDB, Jira, Git, Agile/Scrum, KPI/Financial/Regulatory Reporting, Reconciliation, Data Quality, Data Modeling, Data Warehousing.
Real employment history (do not use any employer/title/date outside this list):
- Data Analyst, Specialty Appliances, GA (Mar 2024 - Present)
- System Analyst / Data Analyst, Progience Technologies Pvt Ltd, Hyderabad, India (Sep 2018 - Feb 2022)
- Data Analyst, Exceego Infolabs Pvt Ltd, Hyderabad, India (Feb 2018 - Aug 2018)

LinkedIn job post:
{clean_job_text_for_ai(post_text)}
"""
    try:
        client = OpenAI(api_key=OPENAI_API_KEY)
        response = client.chat.completions.create(
            model=OPENAI_MODEL,
            temperature=0.25,
            messages=[
                {"role": "system", "content": "You are a careful resume tailoring assistant. You write truthful, clean, ATS-friendly resume content."},
                {"role": "user", "content": prompt}
            ],
        )
        raw = response.choices[0].message.content.strip()
        raw = re.sub(r"^```json\s*|\s*```$", "", raw, flags=re.IGNORECASE | re.DOTALL).strip()
        import json
        data = json.loads(raw)
        required = ["target_title", "job_keywords", "summary", "skills", "experience"]
        if isinstance(data, dict) and all(key in data for key in required):
            return data
    except Exception as e:
        print("AI tailoring skipped, using built-in tailoring instead:", e)
    return None


def pad_list(items, fallback_items, count):
    clean_items = [clean(item) for item in (items or []) if clean(item)]
    clean_fallback = [clean(item) for item in fallback_items if clean(item)]
    merged = clean_items[:]
    for item in clean_fallback:
        if len(merged) >= count:
            break
        if item not in merged:
            merged.append(item)
    return merged[:count]


def ensure_long_tailoring(tailoring, post_text):
    fallback = default_tailoring(post_text)
    tailoring = tailoring or {}
    experience = tailoring.get("experience") or {}
    fallback_experience = fallback["experience"]

    return {
        "target_title": clean(tailoring.get("target_title") or fallback["target_title"]),
        "job_keywords": pad_list(tailoring.get("job_keywords"), fallback["job_keywords"], 18),
        "summary": pad_list(tailoring.get("summary"), fallback["summary"], TAILORED_SUMMARY_COUNT),
        "skills": pad_list(tailoring.get("skills"), fallback["skills"], 6),
        "experience": {
            "Specialty Appliances": pad_list(
                experience.get("Specialty Appliances"),
                fallback_experience["Specialty Appliances"],
                TAILORED_SPECIALTY_COUNT
            ),
            "Progience Technologies": pad_list(
                experience.get("Progience Technologies"),
                fallback_experience["Progience Technologies"],
                TAILORED_PROGIENCE_COUNT
            ),
            "Exceego Infolabs": pad_list(
                experience.get("Exceego Infolabs"),
                fallback_experience["Exceego Infolabs"],
                TAILORED_EXCEEGO_COUNT
            ),
        }
    }


def get_tailoring(post_text):
    tailored = ai_tailoring(post_text)
    if tailored:
        print("AI tailored resume content created for this post.")
        return ensure_long_tailoring(tailored, post_text)
    print("Built-in tailored resume content created for this post.")
    return ensure_long_tailoring(default_tailoring(post_text), post_text)


def create_tailored_resume(post_text, post_link):
    tailoring = get_tailoring(post_text)
    target_title = clean(tailoring.get("target_title") or "Data Analyst")
    name_part = safe_filename(target_title)
    cand_name_part = safe_filename(CANDIDATE.get("name", "Candidate"))
    link_part = safe_filename(post_link.replace("https://www.linkedin.com/", ""))[-32:]
    path = OUTPUT / f"{cand_name_part}_Resume_{name_part}_{link_part}.pdf"

    doc = SimpleDocTemplate(
        str(path),
        pagesize=letter,
        rightMargin=38,
        leftMargin=38,
        topMargin=28,
        bottomMargin=28
    )

    styles = getSampleStyleSheet()
    title = ParagraphStyle("TailoredTitle", parent=styles["Title"], fontSize=19, leading=21, textColor="#0f172a", alignment=1, spaceAfter=2)
    subtitle = ParagraphStyle("TailoredSubtitle", parent=styles["Normal"], fontSize=10.5, leading=12.5, textColor="#1e3a8a", alignment=1, spaceAfter=3)
    contact = ParagraphStyle("TailoredContact", parent=styles["Normal"], fontSize=8.8, leading=11, textColor="#111827", alignment=1, spaceAfter=1.5)
    section = ParagraphStyle("TailoredSection", parent=styles["Heading2"], fontSize=11.3, leading=13.5, textColor="#1d4ed8", spaceBefore=8, spaceAfter=3.5)
    normal = ParagraphStyle("TailoredNormal", parent=styles["Normal"], fontSize=8.8, leading=11, spaceAfter=3)
    bullet = ParagraphStyle("TailoredBullet", parent=styles["Normal"], fontSize=8.7, leading=10.8, leftIndent=12, firstLineIndent=-10, spaceAfter=2.5)
    job_title = ParagraphStyle("TailoredJobTitle", parent=styles["Normal"], fontSize=9.4, leading=11.5, textColor="#0f172a", spaceBefore=5, spaceAfter=1.5)
    tech_stack = ParagraphStyle("TailoredTechStack", parent=styles["Normal"], fontSize=8.0, leading=10, textColor="#374151", spaceAfter=2.5, leftIndent=12)

    story = []

    def p(txt):
        story.append(Paragraph(reportlab_text(txt), normal))

    def b(txt):
        story.append(Paragraph("&bull; " + reportlab_text(txt), bullet))

    def sec(txt):
        story.append(Paragraph(txt, section))

    def ts(txt):
        story.append(Paragraph(reportlab_text(txt), tech_stack))

    story.append(Paragraph(reportlab_text(CANDIDATE["name"]), title))
    story.append(Paragraph(reportlab_text(target_title), subtitle))
    story.append(Paragraph(reportlab_text(f"{CANDIDATE['work_auth']} | {CANDIDATE['phone']} | {CANDIDATE['email']} | {CANDIDATE['linkedin']}"), contact))
    story.append(Paragraph(reportlab_text(f"Location: {CANDIDATE['location']} | Availability: {CANDIDATE['availability']} | Relocation: {CANDIDATE['relocation']} | Experience: {CANDIDATE['experience']}"), contact))

    sec("PROFESSIONAL SUMMARY")
    for item in tailoring.get("summary", []):
        b(item)

    sec("TECHNICAL SKILLS")
    for item in tailoring.get("skills", []):
        p(item)

    sec("PROFESSIONAL HIGHLIGHTS")
    for item in [
        "Delivered SQL-driven reports, Power BI dashboards, Tableau visualizations, ETL validation reports, and finance-ready analytics for business stakeholders.",
        "Created reliable reporting datasets through SQL extraction, transformation, cleansing, profiling, reconciliation, and source-to-target validation.",
        "Improved dashboard usability through KPI cards, slicers, drill-through pages, DAX measures, Power Query transformations, and optimized data models.",
        "Supported finance, risk, compliance, operations, sales, and leadership reporting with accurate datasets and clear business documentation.",
        "Worked closely with product owners, business users, data engineers, QA analysts, and managers in Agile delivery environments."
    ]:
        b(item)

    sec("PROFESSIONAL EXPERIENCE")
    experience = tailoring.get("experience", {})

    story.append(Paragraph("<b>Data Analyst | Specialty Appliances, GA | Mar 2024 - Present</b>", job_title))
    for item in experience.get("Specialty Appliances", []):
        b(item)
    ts("Tech Stack: SQL, T-SQL, Power BI, Tableau, DAX, Power Query, Python, Azure Data Factory, Snowflake, Databricks, Azure Synapse, Excel, Jira, Agile Scrum")

    story.append(Paragraph("<b>System Analyst / Data Analyst | Progience Technologies Pvt Ltd, Hyderabad, India | Sep 2018 - Feb 2022</b>", job_title))
    for item in experience.get("Progience Technologies", []):
        b(item)
    ts("Tech Stack: SQL, PL/SQL, T-SQL, Power BI, Tableau, Excel, SSIS, Data Warehousing, SQL Server, MySQL, PostgreSQL, Jira, Git, Agile")

    story.append(Paragraph("<b>Data Analyst | Exceego Infolabs Pvt Ltd, Hyderabad, India | Feb 2018 - Aug 2018</b>", job_title))
    for item in experience.get("Exceego Infolabs", []):
        b(item)

    sec("PROJECT & DELIVERY EXPERIENCE")
    for item in [
        "Power BI Executive Reporting Suite: Built KPI dashboards for revenue, cost, operations, and trend analysis with DAX measures and automated refresh.",
        "SQL Reconciliation Framework: Created SQL validation scripts to compare source and target datasets, highlight mismatches, and improve reporting trust.",
        "ETL Modernization: Supported Azure Data Factory pipelines for structured ingestion, transformation, cleansing, and reporting-ready data publishing.",
        "Financial Analytics Reporting: Delivered recurring reporting for month-end close, variance analysis, compliance checks, and stakeholder decision-making.",
        "Data Quality Initiative: Built validation rules, profiling queries, duplicate checks, exception reports, and reconciliation outputs to improve data reliability."
    ]:
        b(item)

    sec("CORE STRENGTHS")
    p("Data Analysis | Business Intelligence | SQL Reporting | Power BI | Tableau | ETL Validation | Data Warehousing | Data Modeling | "
      "Financial Reporting | Regulatory Reporting | KPI Dashboards | Reconciliation | Data Quality | Data Profiling | Source-to-Target Mapping | "
      "Stakeholder Communication | Agile Scrum | Jira | Documentation | UAT Support | Production Reporting Support")

    sec("EDUCATION")
    p("<b>Master's in Business Analytics</b> - Sacred Heart University, Fairfield, Connecticut")
    p("<b>Bachelor's in Commerce</b> - Osmania University, Hyderabad, India")

    sec("CERTIFICATIONS / PROFESSIONAL LEARNING")
    for item in [
        "Microsoft Power BI Data Analyst Associate - Professional Learning",
        "SQL for Data Analytics and Business Intelligence - Professional Training",
        "Azure Data Factory, Snowflake, and Databricks ETL Practices - Professional Learning",
        "Agile Scrum and Jira Delivery Practices - Professional Learning"
    ]:
        b(item)

    sec("KEY COMPETENCIES")
    p("SQL | PL/SQL | T-SQL | Python | Power BI | Tableau | DAX | Power Query | Azure Data Factory | Snowflake | Databricks | Azure Synapse | SSIS | "
      "Excel | Advanced Excel | Financial Reporting | KPI Reporting | Data Modeling | Data Warehousing | Data Cleansing | Data Quality | Data Validation | "
      "Reconciliation | Regulatory Reporting | Jira | Agile Scrum")

    doc.build(story)
    return path, tailoring


def create_resume():
    path = OUTPUT / "Nikhilkumar_Bhuyakar_Data_Analyst_Resume.pdf"

    doc = SimpleDocTemplate(
        str(path),
        pagesize=letter,
        rightMargin=24,
        leftMargin=24,
        topMargin=24,
        bottomMargin=24
    )

    styles = getSampleStyleSheet()

    title = ParagraphStyle(
        "TightTitle",
        parent=styles["Title"],
        fontSize=19,
        leading=21,
        textColor="#0f172a",
        alignment=1,
        spaceAfter=2
    )

    subtitle = ParagraphStyle(
        "TightSubtitle",
        parent=styles["Normal"],
        fontSize=9.5,
        leading=11,
        textColor="#1e3a8a",
        alignment=1,
        spaceAfter=3
    )

    contact = ParagraphStyle(
        "ContactLine",
        parent=styles["Normal"],
        fontSize=8.2,
        leading=10,
        textColor="#111827",
        alignment=1,
        spaceAfter=1
    )

    section = ParagraphStyle(
        "CompactSection",
        parent=styles["Heading2"],
        fontSize=10.0,
        leading=12,
        textColor="#1d4ed8",
        spaceBefore=5,
        spaceAfter=2
    )

    normal = ParagraphStyle(
        "CompactNormal",
        parent=styles["Normal"],
        fontSize=7.8,
        leading=9.2,
        spaceAfter=2
    )

    bullet = ParagraphStyle(
        "CompactBullet",
        parent=styles["Normal"],
        fontSize=7.6,
        leading=9.0,
        leftIndent=9,
        firstLineIndent=-6,
        spaceAfter=1
    )

    job_title = ParagraphStyle(
        "JobTitle",
        parent=styles["Normal"],
        fontSize=8.2,
        leading=10,
        textColor="#0f172a",
        spaceBefore=2,
        spaceAfter=1
    )

    story = []

    def p(txt):
        story.append(Paragraph(txt, normal))

    def b(txt):
        story.append(Paragraph("- " + txt, bullet))

    def sec(txt):
        story.append(Paragraph(txt, section))

    # Header block
    story.append(Paragraph(CANDIDATE["name"], title))
    story.append(Paragraph("Data Analyst | BI Analyst", subtitle))
    story.append(Paragraph(f"{CANDIDATE['work_auth']} | {CANDIDATE['phone']} | {CANDIDATE['email']} | {CANDIDATE['linkedin']}", contact))
    story.append(Paragraph(f"Location: {CANDIDATE['location']} | Availability: {CANDIDATE['availability']} | Preferences: {CANDIDATE['salary']}", contact))

    sec("PROFESSIONAL SUMMARY")
    summary_pts = [
        "Data Analyst with 6+ years of experience transforming business, finance, operations, and compliance data into accurate reporting, dashboards, and decision-support insights.",
        "Strong hands-on expertise in SQL, Python, R, AWS, S3, SSIS, Data Quality, Window Functions, CI/CD, Agile, PL/SQL, T-SQL, Power BI, Tableau, and Excel.",
        "Skilled in writing optimized SQL, PL/SQL, and T-SQL queries using joins, CTEs, window functions, stored procedures, triggers, views, and query performance tuning.",
        "Experienced in Power BI and Tableau dashboard development including DAX, Power Query, data modeling, drill-down reporting, automated refresh, and executive reporting.",
        "Proficient in Azure Data Factory, Snowflake, Databricks, Azure Synapse, SQL Server, MySQL, PostgreSQL, Excel, Python scripting, and ETL/ELT reporting workflows."
    ]
    for item in summary_pts:
        b(item)

    sec("CORE SKILLS MATRIX")
    skills = [
        "<b>Analytics:</b> Data Analysis, Business Analysis, KPI Reporting, Financial Reporting, Regulatory Reporting, Reconciliation, Data Quality, Data Profiling",
        "<b>SQL/Data:</b> SQL, PL/SQL, T-SQL, Stored Procedures, Triggers, Views, CTEs, Window Functions, Query Optimization, Data Modeling",
        "<b>BI Tools:</b> Power BI, Tableau, DAX, Power Query, Excel, Advanced Excel, Pivot Tables, Power Automate",
        "<b>ETL/Cloud:</b> Azure Data Factory, Azure Synapse, Snowflake, Databricks, SSIS, ETL/ELT Pipelines, ADLS, AWS S3",
        "<b>Programming:</b> Python, R, VBA, Pandas, NumPy, Automation Scripts, Data Validation Scripts",
        "<b>Databases & Tools:</b> SQL Server, MySQL, PostgreSQL, MongoDB, Azure SQL, Jira, Git, CI/CD basics, Agile, Scrum"
    ]
    for item in skills:
        p(item)

    sec("PROFESSIONAL EXPERIENCE")

    story.append(Paragraph("<b>Data Analyst | Specialty Appliances, GA | Mar 2024 - Present</b>", job_title))
    specialty_pts = [
        "Developed executive Power BI dashboards using DAX, Power Query, slicers, drill-downs, bookmarks, KPI cards, and automated refresh schedules for leadership reporting.",
        "Built SQL-based reporting datasets using joins, CTEs, window functions, stored procedures, views, and query optimization to support finance, sales, inventory, and operations teams.",
        "Designed ETL pipelines using Azure Data Factory, SQL, Snowflake, and Databricks to ingest, transform, validate, and publish business-ready datasets.",
        "Created automated data validation, reconciliation, and exception reports using SQL and Python to improve reporting accuracy and reduce manual review effort.",
        "Performed data profiling, cleansing, deduplication, mapping, and root-cause analysis for inconsistent source data across ERP, CRM, and operational systems."
    ]
    for item in specialty_pts:
        b(item)

    story.append(Paragraph("<b>System Analyst / Data Analyst | Progience Technologies Pvt Ltd, Hyderabad, India | Sep 2018 - Feb 2022</b>", job_title))
    progience_pts = [
        "Analyzed finance, risk, compliance, customer, and operational datasets to deliver SQL reports, Power BI dashboards, Tableau views, and Excel-based business insights.",
        "Designed PL/SQL and T-SQL scripts for extraction, transformation, reconciliation, data cleansing, and reporting database preparation.",
        "Created data models, stored procedures, triggers, views, validation logic, and audit-support datasets for recurring business reporting requirements.",
        "Built Tableau and Power BI dashboards for cost, revenue, margin, customer trends, productivity, SLA performance, and month-end reporting.",
        "Supported QA and UAT by preparing test data, validating report logic, comparing source-to-target values, and resolving data defects before production release."
    ]
    for item in progience_pts:
        b(item)

    story.append(Paragraph("<b>Data Analyst | Exceego Infolabs Pvt Ltd, Hyderabad, India | Feb 2018 - Aug 2018</b>", job_title))
    exceego_pts = [
        "Prepared accounting, revenue, taxation, and transaction datasets for reporting, analysis, dashboarding, and business review meetings.",
        "Executed SQL queries to analyze trends, exceptions, missing values, duplicate records, mismatches, and operational performance metrics.",
        "Built Excel and Power BI reports using pivot tables, charts, formulas, data refresh processes, and structured reporting templates.",
        "Supported ETL testing, report validation, data reconciliation, and documentation for reporting system enhancements."
    ]
    for item in exceego_pts:
        b(item)

    sec("EDUCATION")
    p("<b>Master's in Business Analytics</b> - Sacred Heart University, Fairfield, Connecticut")
    p("<b>Bachelor's in Commerce</b> - Osmania University, Hyderabad, India")

    doc.build(story)
    return path


def format_jd_snippet(post_text):
    text = clean(post_text)
    # collapse all consecutive spaces, newlines, and tabs into a single clean line
    text = re.sub(r"\s+", " ", text).strip()
    return text


def email_body(post_text, tailoring=None):
    tailoring = tailoring or {}
    target_title = clean(tailoring.get("target_title") or "Data Analyst")
    job_keywords = [clean(k) for k in tailoring.get("job_keywords", []) if clean(k)]
    keyword_line = ", ".join(job_keywords[:5]) or "SQL, Power BI, and Tableau"
    cleaned_jd = format_jd_snippet(post_text)
    relocation_status = "Open for relocation" if CANDIDATE.get("relocation") in ["Yes", "Open for relocation", True] else CANDIDATE.get("relocation", "Open for relocation")

    return f"""Dear Hiring Team,

I came across your posting for a {target_title} position. My hands-on experience with {keyword_line} maps directly to what you are looking for, and I would welcome the opportunity to be considered.

Please find my submission details below for your review:

--- SUBMISSION DETAILS ---
• Candidate Name: {CANDIDATE['name']}
• Applied Role: {target_title}
• Total Experience: {CANDIDATE['experience']}
• Phone / Contact: {CANDIDATE['phone']}
• Email Address: {CANDIDATE['email']}
• Current Location: {CANDIDATE['location']}
• Relocation: {relocation_status}
• Work Authorization: {CANDIDATE['work_auth']}
• Availability: {CANDIDATE['availability']}
• Rate / Compensation: {CANDIDATE['salary']}
• LinkedIn Profile: {CANDIDATE['linkedin']}

I have attached my updated resume for your review. Are you available for a brief call sometime this week to discuss this position? Thank you for your time and consideration; I look forward to hearing from you.

Best regards,
{CANDIDATE['name']}
Phone: {CANDIDATE['phone']} | Email: {CANDIDATE['email']}
LinkedIn: {CANDIDATE['linkedin']}

--------------------------------------------------
Referenced Job Description / Snippet:
{cleaned_jd}
"""


def send_email(to_email, post_text, resume_path, tailoring=None, jd_hash="", recruiter_name="Hiring Manager", post_link=""):
    to_email = normalize_email(to_email)
    tailoring = tailoring or {}
    target_title = clean(tailoring.get("target_title") or "Data Analyst")

    if to_email in blocked_emails():
        print("Skipped own/candidate email:", to_email)
        return False

    is_dup, dup_reason = already_sent(to_email, post_link=post_link, jd_hash=jd_hash)
    if is_dup:
        print(f"Duplicate skipped ({dup_reason}):", to_email)
        return False

    yag = yagmail.SMTP(GMAIL_ID, GMAIL_APP_PASSWORD)

    subject = f"Submission : {target_title} - Open for relocation"
    contents = email_body(post_text, tailoring)

    cc_list = [c for c in [CANDIDATE.get("email"), "quinn@jpitstaffing.com"] if c]

    yag.send(
        to=to_email,
        cc=cc_list,
        bcc="kim@jpitstaffing.com",
        subject=subject,
        contents=contents,
        attachments=str(resume_path)
    )

    save_sent(
        recruiter_name=recruiter_name,
        recruiter_email=to_email,
        role=target_title,
        post_url=post_link,
        job_description=post_text,
        email_sent_status="sent",
        jd_hash=jd_hash
    )

    print("Email sent to recruiter:", to_email, f"({recruiter_name})")
    print("CC sent to:", ", ".join(cc_list))
    print("BCC sent to: kim@jpitstaffing.com")
    print("Subject:", subject)

    time.sleep(MIN_SECONDS_BETWEEN_EMAILS)
    return True


def login_and_manual_search(page):
    page.goto("https://www.linkedin.com/feed/", timeout=60000)
    page.wait_for_timeout(5000)

    print("LinkedIn opened.")
    print("This bot will NOT search keyword automatically.")
    print("Use a search like: DevOps AND (W2 OR Fulltime) AND (AWS OR Kubernetes) -C2C -Hotlist -Bench")
    print("You manually search the topic, click Posts, set filter, scroll until emails are visible.")
    print("The code will skip ONLY Bench Sales / Benchsales posts. Other recruiter posts are allowed.")
    try:
        input("After recruiter email posts are visible, press ENTER here: ")
    except Exception:
        print("Waiting 10 seconds for search results...")
        time.sleep(10)


def scroll_page(page):
    for _ in range(SCROLL_ROUNDS_PER_CYCLE):
        page.mouse.wheel(0, 1800)
        page.wait_for_timeout(900)


def process_visible_posts(page, total_sent, run_state):
    cards = get_cards(page)
    run_state["stats"]["cards"] += len(cards)

    print("Visible posts with recruiter email found:", len(cards))

    if len(cards) == 0:
        print("No recruiter email posts detected. Open LinkedIn POSTS tab and make sure emails are visible on screen.")
        return total_sent

    for index, card in enumerate(cards, start=1):
        try:
            text = clean(card.inner_text(timeout=2000))
            post_key = post_key_from_text(text)

            if SKIP_ALREADY_SEEN_IN_RUN and post_key in run_state["seen_post_keys"]:
                run_state["stats"]["already_seen_cards"] += 1
                continue
            run_state["seen_post_keys"].add(post_key)

            allowed, reason = should_send_to_post(text)
            if not allowed:
                run_state["stats"]["not_job"] += 1
                print(f"{index}. skipped: {reason}")
                continue

            emails = filter_recruiter_emails(extract_emails(text))

            if not emails:
                run_state["stats"]["no_email"] += 1
                print(f"{index}. skipped: no valid recruiter email")
                continue

            recruiter_name = get_recruiter_name_from_card(card, text)

            # Patched link engine containing URN parsing configurations and fallback structures
            post_link = get_post_link_from_card(page, card)
            if not normalize_post_link(post_link):
                post_link = page.url if "linkedin.com" in page.url else "https://www.linkedin.com/feed/"

            print(f"{index}. processing post link: {post_link} (Recruiter: {recruiter_name})")

            # Calculate JD fingerprint for exact content comparison
            jd_hash = get_jd_fingerprint(text)

            sendable_emails = []
            for email in emails:
                pair_key = (email, jd_hash)
                if pair_key in run_state["seen_email_jd_pairs"]:
                    run_state["stats"]["duplicate_emails"] += 1
                    print(f"{index}. duplicate skipped in current run (same recruiter & JD): {email}")
                    continue

                is_dup, dup_reason = already_sent(email, post_link=post_link, jd_hash=jd_hash)
                if is_dup:
                    run_state["stats"]["duplicate_emails"] += 1
                    print(f"{index}. duplicate skipped ({dup_reason}): {email}")
                    continue

                run_state["seen_email_jd_pairs"].add(pair_key)
                sendable_emails.append(email)

            if not sendable_emails:
                print(f"{index}. skipped: all recruiter emails were already sent or in cooldown")
                continue

            run_state["stats"]["new_emails"] += len(sendable_emails)
            resume_path, tailoring = create_tailored_resume(text, post_link)
            print(f"{index}. tailored resume ready: {resume_path}")

            for email in sendable_emails:
                if total_sent >= MAX_EMAILS_PER_RUN:
                    return total_sent

                if send_email(
                    to_email=email,
                    post_text=text,
                    resume_path=resume_path,
                    tailoring=tailoring,
                    jd_hash=jd_hash,
                    recruiter_name=recruiter_name,
                    post_link=post_link
                ):
                    total_sent += 1
                    run_state["stats"]["sent"] += 1
                    run_state["seen_emails_in_run"].add(email)

        except Exception as e:
            run_state["stats"]["errors"] += 1
            print(f"{index}. error:", e)

    return total_sent


def main():
    if not GMAIL_ID or not GMAIL_APP_PASSWORD:
        print("Please add your Gmail App Password in the .env file.")
        print("Example .env:")
        print("GMAIL_ID=hroshan6198@gmail.com")
        print("GMAIL_APP_PASSWORD=your_16_digit_app_password")
        return

    if OPENAI_API_KEY and OpenAI is not None:
        print("AI resume tailoring is ON.")
    else:
        print("AI resume tailoring is OFF because OPENAI_API_KEY is not set.")
        print("The bot will still create a clean tailored resume using built-in keyword matching.")

    total_sent = 0
    cycle = 1
    run_state = make_run_state()

    with sync_playwright() as p:
        browser = p.chromium.launch_persistent_context(
            user_data_dir=PROFILE_DIR,
            headless=False,
            viewport={"width": 1400, "height": 900},
            permissions=["clipboard-read", "clipboard-write"],
            args=["--start-maximized"]
        )

        page = browser.new_page()

        login_and_manual_search(page)

        while CONTINUOUS_MODE and cycle <= MAX_CYCLES:
            print(f"\nStarting cycle {cycle}")

            total_sent = process_visible_posts(page, total_sent, run_state)

            scroll_page(page)

            total_sent = process_visible_posts(page, total_sent, run_state)

            print("Cycle completed:", cycle)
            print("Total emails sent:", total_sent)
            print_run_stats(run_state, prefix="Session stats")
            print("Daily response target:", DAILY_RESPONSE_TARGET, "responses/day. More quality posts = better chance, not guaranteed.")

            if total_sent >= MAX_EMAILS_PER_RUN:
                break

            cycle += 1
            print(f"Waiting {CYCLE_SLEEP_SECONDS} seconds before next cycle...")
            time.sleep(CYCLE_SLEEP_SECONDS)

        browser.close()

    print("Completed.")
    print("Total emails sent:", total_sent)


if __name__ == "__main__":
    main()