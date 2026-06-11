# -*- coding: utf-8 -*-
"""
معجم الرياض — Image Review App
================================
Streamlit app for reviewing AI-generated dictionary images.

DATA SOURCE: Google Sheet (read & write, no file uploads)
- Reads rows that have `image_url` populated
- Writes reviewer decisions back to the same row, same Sheet

The app NEVER:
- uploads or re-generates images
- runs the prompt-repair agent
- modifies original data columns

It ONLY adds/updates the 12 review columns listed in REVIEW_COLUMNS below.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple

import pandas as pd
import streamlit as st

# ─── Google Sheets ──────────────────────────────────────────────────────────
import gspread
from google.oauth2.service_account import Credentials
from gspread.exceptions import APIError, WorksheetNotFound

# ─── OpenAI (للـ Prompt Repair Agent) ──────────────────────────────────────
try:
    from openai import OpenAI
except ImportError:
    OpenAI = None   # سيُعرض خطأ واضح عند الضغط على الزر

# ═══════════════════════════════════════════════════════════════════════════
# CONFIG — edit only if column names in the Sheet ever change
# ═══════════════════════════════════════════════════════════════════════════
APP_TITLE       = "مراجعة صور معجم الرياض"
APP_SUBTITLE    = "لوحة مراجعة واعتماد الصور المعجمية"
PAGE_ICON       = "📖"

# Source columns (must exist in the Sheet — written by Colab)
COL_LEMMA       = "lemma.formRepresentations[0].form"
COL_NONDIAC     = "nonDiacriticsLemma"
COL_DEFINITION  = "senses.definition.textRepresentations[0].form"
COL_EXAMPLE     = "senses.examples[0].form"
COL_TRANSLATION = "senses.translations[0].form"
COL_ENGLISH     = "english_term"
COL_OBJ_DESC    = "object_description"
COL_PROMPT      = "image_prompt"
COL_NEG_PROMPT  = "negative_prompt"
COL_FILENAME    = "image_filename"
COL_UID         = "image_uid"
COL_ROW_NUMBER  = "sheet_row_number"
COL_IMAGE_URL   = "image_url"
COL_QUALITY     = "prompt_quality_note"
COL_GEN_STATUS  = "generation_status"
COL_REGEN_PROMPT = "regenerated_prompt"

# Prompt Repair Agent
AGENT_MODEL     = "gpt-4o-mini"
AGENT_TEMP      = 0.2
DEFAULT_REVIEWER_NAME = "reviewer"   # حقل اسم المراجع شيلناه — قيمة افتراضية

# Image generation
IMAGE_MODEL          = "gpt-image-1"
IMAGE_SIZE           = "1024x1024"
IMAGE_QUALITY        = "medium"
IMAGE_BACKGROUND_HEX = "#FAFAFA"

# Per-session generation safety
MAX_GENERATIONS_PER_SESSION = 10

# Test Mode default = ON (احتراماً لطلب المراجع: لا نلمس image_url إلا بإرادة صريحة)
TEST_MODE_DEFAULT = True

# Tracking columns (auto-added if missing)
COL_REGEN_COUNT          = "regeneration_count"
COL_LAST_REGEN_AT        = "last_regenerated_at"
COL_REGEN_HISTORY        = "regeneration_history"
COL_PROMPT_REPAIR_STATUS = "prompt_repair_status"
COL_PROMPT_REPAIR_NOTE   = "prompt_repair_note"
COL_TEST_PROMPT          = "test_regenerated_prompt"
COL_TEST_IMG_URL         = "test_regenerated_image_url"
COL_TEST_REGEN_AT        = "test_regenerated_at"
COL_TEST_NOTE            = "test_regeneration_note"
COL_PREVIOUS_URL         = "previous_image_url"

# Review columns (auto-created if missing)
REVIEW_COLUMNS: List[str] = [
    "review_status",
    "review_decision",
    "reviewer_name",
    "reviewed_at",
    "rejection_reason",
    "reviewer_visual_note",
    "needs_regeneration",
    "regeneration_request_status",
    "regenerated_prompt",
    "regeneration_note",
    "approved_image_url",
    "previous_image_url",
    # — تتبع إعادة التوليد —
    COL_REGEN_COUNT,
    COL_LAST_REGEN_AT,
    COL_REGEN_HISTORY,
    COL_PROMPT_REPAIR_STATUS,
    COL_PROMPT_REPAIR_NOTE,
    # — Test Mode —
    COL_TEST_PROMPT,
    COL_TEST_IMG_URL,
    COL_TEST_REGEN_AT,
    COL_TEST_NOTE,
]

# Review status values
ST_PENDING   = "pending"
ST_APPROVED  = "approved"
ST_REJECTED  = "regeneration_requested"

# Arabic labels for UI (status → label, color)
STATUS_LABELS: Dict[str, Tuple[str, str]] = {
    ST_PENDING:  ("قيد المراجعة",        "#9C9C9C"),
    ST_APPROVED: ("معتمد",                "#0E8E62"),
    ST_REJECTED: ("بانتظار إعادة التوليد", "#C58A1A"),
}

IMAGE_CATEGORY_LABEL = "اسم آلة"   # static for now (per Morooj's spec)

# Cache control
CACHE_TTL_SEC = 60   # re-fetch Sheet at most once a minute

# Riyadh timezone (UTC+3)
RIYADH_TZ = timezone(timedelta(hours=3))


# ═══════════════════════════════════════════════════════════════════════════
# Streamlit page setup + RTL styling
# ═══════════════════════════════════════════════════════════════════════════
st.set_page_config(
    page_title=APP_TITLE,
    page_icon=PAGE_ICON,
    layout="wide",
    initial_sidebar_state="expanded",
)

# Inject RTL + custom styling (Replit-like aesthetic)
st.markdown("""
<style>
    /* ── RTL ─────────────────────────────────────────── */
    html, body, [class*="css"] {
        direction: rtl;
        font-family: "Segoe UI", "Tahoma", "Arial", sans-serif;
    }
    .stApp {
        background-color: #F5F3EF;
    }
    [data-testid="stSidebar"] {
        background-color: #FBFAF7;
        border-left: 1px solid #E6E2DA;
    }
    [data-testid="stSidebar"] * { direction: rtl; text-align: right; }

    /* ── Headings ────────────────────────────────────── */
    h1, h2, h3, h4 { color: #2A2A2A; text-align: right; }
    .app-header {
        text-align: right;
        padding: 8px 0 18px 0;
        border-bottom: 1px solid #E6E2DA;
        margin-bottom: 20px;
    }
    .app-header .title { font-size: 26px; font-weight: 700; color: #2A2A2A; }
    .app-header .subtitle { font-size: 14px; color: #7A7A7A; margin-top: 4px; }

    /* ── Stat cards ──────────────────────────────────── */
    .stat-card {
        background: #FFFFFF;
        border: 1px solid #ECE7DE;
        border-radius: 14px;
        padding: 18px 20px;
        text-align: right;
        box-shadow: 0 1px 2px rgba(0,0,0,0.02);
    }
    .stat-label {
        font-size: 13px;
        color: #7A7A7A;
        margin-bottom: 8px;
        display: flex;
        align-items: center;
        justify-content: flex-end;
        gap: 8px;
    }
    .stat-value { font-size: 30px; font-weight: 700; color: #2A2A2A; }
    .stat-dot {
        width: 8px; height: 8px; border-radius: 50%; display: inline-block;
    }

    /* ── Table cards ─────────────────────────────────── */
    .row-card {
        background: #FFFFFF;
        border: 1px solid #ECE7DE;
        border-radius: 12px;
        padding: 14px 18px;
        margin-bottom: 10px;
        box-shadow: 0 1px 2px rgba(0,0,0,0.02);
    }
    .row-card:hover { border-color: #C58A1A; }

    /* ── Status pills ────────────────────────────────── */
    .pill {
        display: inline-block;
        padding: 4px 14px;
        border-radius: 999px;
        font-size: 12px;
        font-weight: 600;
        border: 1px solid transparent;
    }
    .pill-pending  { background: #F3F1ED; color: #6B6B6B; border-color: #E0DCD3; }
    .pill-approved { background: #E6F4EE; color: #0E8E62; border-color: #BFE3D1; }
    .pill-rejected { background: #FBEFD9; color: #C58A1A; border-color: #F1D9A8; }

    .category-pill {
        display: inline-block;
        padding: 4px 12px;
        border-radius: 8px;
        background: #F3F1ED;
        color: #5A5A5A;
        font-size: 12px;
    }

    /* ── Buttons ─────────────────────────────────────── */
    .stButton > button {
        border-radius: 10px;
        font-weight: 600;
        padding: 10px 16px;
        border: 1px solid #DDD7CB;
        background: #FFFFFF;
    }
    .stButton > button:hover { border-color: #C58A1A; }

    /* primary button (approve) — uses streamlit's primary type */
    .stButton > button[kind="primary"] {
        background: #0E8E62 !important;
        color: white !important;
        border: 1px solid #0E8E62 !important;
    }
    .stButton > button[kind="primary"]:hover {
        background: #0a7551 !important;
    }

    /* ── Inputs ──────────────────────────────────────── */
    .stTextInput input, .stTextArea textarea {
        direction: rtl;
        text-align: right;
        border-radius: 8px;
        border: 1px solid #DDD7CB !important;
        background: #FFFFFF !important;
        color: #1A1A1A !important;
        font-size: 14px !important;
        -webkit-text-fill-color: #1A1A1A !important;  /* Safari */
    }
    .stTextInput input::placeholder,
    .stTextArea textarea::placeholder {
        color: #9C9C9C !important;
        opacity: 1 !important;
        -webkit-text-fill-color: #9C9C9C !important;
    }

    /* ── Force readable colors on EVERY widget label ──── */
    label, label *,
    [data-testid="stWidgetLabel"],
    [data-testid="stWidgetLabel"] *,
    .stTextInput label, .stTextInput label *,
    .stTextArea label, .stTextArea label *,
    .stMultiSelect label, .stMultiSelect label *,
    .stSelectbox label, .stSelectbox label *,
    .stCheckbox label, .stCheckbox label *,
    .stRadio label, .stRadio label *,
    .stToggle label, .stToggle label * {
        color: #2A2A2A !important;
        -webkit-text-fill-color: #2A2A2A !important;
        font-weight: 500 !important;
        font-size: 14px !important;
        opacity: 1 !important;
    }

    /* Captions (st.caption) and small text */
    [data-testid="stCaptionContainer"],
    [data-testid="stCaptionContainer"] *,
    .stCaption, .stCaption *, small {
        color: #4A4A4A !important;
        -webkit-text-fill-color: #4A4A4A !important;
        font-size: 13px !important;
        opacity: 1 !important;
    }

    /* Plain text from st.write / st.markdown — but NOT inside our custom .row-card boxes */
    .stMarkdown p, .stMarkdown li, .stMarkdown span,
    .stText, [data-testid="stMarkdownContainer"] p,
    [data-testid="stMarkdownContainer"] li {
        color: #2A2A2A !important;
        -webkit-text-fill-color: #2A2A2A !important;
        opacity: 1 !important;
    }

    /* ── Multiselect: container + tags + dropdown + input ─ */
    .stMultiSelect [data-baseweb="select"] > div {
        background: #FFFFFF !important;
        border: 1px solid #DDD7CB !important;
        min-height: 44px !important;
    }
    .stMultiSelect [data-baseweb="select"] * {
        color: #2A2A2A !important;
        -webkit-text-fill-color: #2A2A2A !important;
    }
    .stMultiSelect [data-baseweb="tag"] {
        background-color: #FBEFD9 !important;
        border: 1px solid #F1D9A8 !important;
    }
    .stMultiSelect [data-baseweb="tag"] span,
    .stMultiSelect [data-baseweb="tag"] * {
        color: #6B4A0E !important;
        -webkit-text-fill-color: #6B4A0E !important;
        font-weight: 600 !important;
        font-size: 13px !important;
    }
    .stMultiSelect input {
        color: #2A2A2A !important;
        -webkit-text-fill-color: #2A2A2A !important;
    }
    .stMultiSelect [data-baseweb="select"] [class*="placeholder"],
    .stMultiSelect [data-baseweb="select"] [class*="placeholder"] * {
        color: #6B6B6B !important;
        -webkit-text-fill-color: #6B6B6B !important;
        opacity: 1 !important;
    }
    /* Dropdown list items */
    [data-baseweb="popover"] li,
    [data-baseweb="popover"] li *,
    [data-baseweb="menu"] li,
    [data-baseweb="menu"] li * {
        color: #2A2A2A !important;
        -webkit-text-fill-color: #2A2A2A !important;
    }

    /* ── Toggle / Checkbox: label text beside the switch ── */
    [data-testid="stCheckbox"],
    [data-testid="stCheckbox"] *,
    [data-baseweb="checkbox"] label,
    [data-baseweb="checkbox"] label *,
    .stCheckbox > label,
    .stCheckbox > label > div {
        color: #2A2A2A !important;
        -webkit-text-fill-color: #2A2A2A !important;
        font-size: 14px !important;
        font-weight: 500 !important;
        opacity: 1 !important;
    }

    /* ── Expander ──────────────────────────────────────── */
    [data-testid="stExpander"] summary,
    [data-testid="stExpander"] summary *,
    [data-testid="stExpander"] p,
    .streamlit-expanderHeader,
    .streamlit-expanderHeader * {
        color: #2A2A2A !important;
        -webkit-text-fill-color: #2A2A2A !important;
        font-weight: 500 !important;
        opacity: 1 !important;
    }

    /* ── Tabs labels ───────────────────────────────────── */
    .stTabs [data-baseweb="tab"],
    .stTabs [data-baseweb="tab"] * {
        color: #5A5A5A !important;
        -webkit-text-fill-color: #5A5A5A !important;
        font-weight: 500 !important;
    }
    .stTabs [aria-selected="true"],
    .stTabs [aria-selected="true"] * {
        color: #C58A1A !important;
        -webkit-text-fill-color: #C58A1A !important;
    }

    /* ── Progress text ─────────────────────────────────── */
    [data-testid="stProgress"],
    [data-testid="stProgress"] *,
    [data-testid="stProgress"] p,
    [data-testid="stProgress"] div {
        color: #2A2A2A !important;
        -webkit-text-fill-color: #2A2A2A !important;
        font-weight: 500 !important;
    }

    /* ── Alert messages (info / success / warning / error) ─ */
    [data-testid="stAlert"],
    [data-testid="stAlert"] *,
    [data-testid="stNotification"],
    [data-testid="stNotification"] * {
        color: #1A1A1A !important;
        -webkit-text-fill-color: #1A1A1A !important;
        opacity: 1 !important;
    }

    /* ── Sidebar specific overrides ─────────────────────── */
    [data-testid="stSidebar"] *,
    [data-testid="stSidebar"] label,
    [data-testid="stSidebar"] p,
    [data-testid="stSidebar"] span,
    [data-testid="stSidebar"] .stCaption,
    [data-testid="stSidebar"] [data-testid="stCaptionContainer"] {
        color: #2A2A2A !important;
        -webkit-text-fill-color: #2A2A2A !important;
        opacity: 1 !important;
    }
    [data-testid="stSidebar"] [data-testid="stCaptionContainer"] {
        color: #4A4A4A !important;
        -webkit-text-fill-color: #4A4A4A !important;
    }

    /* ── Button text ───────────────────────────────────── */
    .stButton > button,
    .stButton > button * {
        color: #2A2A2A !important;
        -webkit-text-fill-color: #2A2A2A !important;
    }
    .stButton > button[kind="primary"],
    .stButton > button[kind="primary"] * {
        color: #FFFFFF !important;
        -webkit-text-fill-color: #FFFFFF !important;
    }

    /* ── Hide streamlit chrome ───────────────────────── */
    #MainMenu, footer, .stDeployButton { visibility: hidden; }
</style>
""", unsafe_allow_html=True)


# ═══════════════════════════════════════════════════════════════════════════
# Early secrets validation — يطبع رسالة عربية واضحة قبل أي محاولة اتصال
# ═══════════════════════════════════════════════════════════════════════════
def _check_secrets() -> Optional[str]:
    """Return None if all required secrets exist, else a friendly error message."""
    try:
        secrets = st.secrets
    except Exception as e:
        return (
            "ملف `.streamlit/secrets.toml` غير موجود أو غير قابل للقراءة.\n\n"
            f"التفاصيل: `{type(e).__name__}: {e}`"
        )

    missing: List[str] = []
    if "gcp_service_account" not in secrets:
        missing.append("[gcp_service_account] — مفاتيح Google Service Account")
    if "sheet" not in secrets:
        missing.append("[sheet] — sheet_id و worksheet_name")
    else:
        if "sheet_id" not in secrets["sheet"]:
            missing.append("[sheet] → sheet_id")
    if "drive" not in secrets:
        # غير إجباري للقراءة، لكن مهم لتوليد الصور
        pass
    if "openai" not in secrets:
        # غير إجباري للقراءة، فقط للـ Agent والتوليد
        pass

    if missing:
        return (
            "بعض إعدادات `.streamlit/secrets.toml` ناقصة:\n\n"
            + "\n".join(f"- `{m}`" for m in missing)
        )
    return None


_secrets_err = _check_secrets()
if _secrets_err:
    st.markdown(
        f'<div class="app-header">'
        f'<div class="title">{APP_TITLE}</div>'
        f'<div class="subtitle">{APP_SUBTITLE}</div>'
        f'</div>',
        unsafe_allow_html=True,
    )
    st.error(f"⚠️ **إعدادات ناقصة**\n\n{_secrets_err}")
    with st.expander("📋 الحل: ملف secrets.toml الصحيح"):
        st.code("""# .streamlit/secrets.toml

[sheet]
sheet_id        = "PASTE_YOUR_GOOGLE_SHEET_ID"
worksheet_name  = "Sheet1"

[drive]
folder_name = "Moarjam_AlRiyadh_Images"

[openai]
api_key = "sk-..."

[gcp_service_account]
type                        = "service_account"
project_id                  = "your-project-id"
private_key_id              = "abc123..."
private_key                 = \"\"\"-----BEGIN PRIVATE KEY-----
...PASTE THE FULL PRIVATE KEY INCLUDING NEWLINES...
-----END PRIVATE KEY-----
\"\"\"
client_email                = "review-app@your-project.iam.gserviceaccount.com"
client_id                   = "..."
auth_uri                    = "https://accounts.google.com/o/oauth2/auth"
token_uri                   = "https://oauth2.googleapis.com/token"
auth_provider_x509_cert_url = "https://www.googleapis.com/oauth2/v1/certs"
client_x509_cert_url        = "..."
universe_domain             = "googleapis.com"
""", language="toml")
        st.markdown(
            "**ملاحظات:**\n"
            "- `[gcp_service_account]` يجي من ملف JSON من Google Cloud.\n"
            "- نفس الـ client_email يجب أن يكون له Editor على الـ Sheet ومجلد Drive.\n"
            "- `[openai]` و `[drive]` اختياريان لكن يلزمان لزر التوليد.\n"
            "- بعد التعديل، أعيدي تشغيل التطبيق."
        )
    st.stop()


# ═══════════════════════════════════════════════════════════════════════════
# Google Sheets connection
# ═══════════════════════════════════════════════════════════════════════════
SHEET_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]


@st.cache_resource
def get_gsheet_client() -> gspread.Client:
    """Build an authenticated gspread client from Streamlit secrets."""
    creds_info = dict(st.secrets["gcp_service_account"])
    creds = Credentials.from_service_account_info(creds_info, scopes=SHEET_SCOPES)
    return gspread.authorize(creds)


def get_worksheet() -> gspread.Worksheet:
    """Open the configured Sheet + worksheet."""
    client = get_gsheet_client()
    sheet_id = st.secrets["sheet"]["sheet_id"]
    ws_name  = st.secrets["sheet"].get("worksheet_name", "enriched")
    sh = client.open_by_key(sheet_id)
    try:
        return sh.worksheet(ws_name)
    except WorksheetNotFound:
        # fall back to the first sheet, surface a warning
        st.warning(f"الورقة '{ws_name}' غير موجودة — استخدمت أول ورقة في الملف.")
        return sh.sheet1


def ensure_review_columns(ws: gspread.Worksheet) -> List[str]:
    """Make sure all REVIEW_COLUMNS exist in the header row. Appends missing ones."""
    headers = ws.row_values(1)
    missing = [c for c in REVIEW_COLUMNS if c not in headers]
    if missing:
        start_col = len(headers) + 1
        end_col = start_col + len(missing) - 1
        # Build A1 range for the missing header cells
        from gspread.utils import rowcol_to_a1
        rng = f"{rowcol_to_a1(1, start_col)}:{rowcol_to_a1(1, end_col)}"
        ws.update(rng, [missing])
        headers = headers + missing
    return headers


@st.cache_data(ttl=CACHE_TTL_SEC, show_spinner="جاري تحميل البيانات من Google Sheet...")
def load_sheet_data() -> Tuple[pd.DataFrame, List[str]]:
    """Read full sheet → DataFrame. Returns (df, headers)."""
    ws = get_worksheet()
    ensure_review_columns(ws)
    records = ws.get_all_records()   # list of dicts keyed by header
    df = pd.DataFrame(records)
    headers = ws.row_values(1)
    # Normalize: NaN/None → ""
    df = df.fillna("").astype(str).replace({"nan": "", "None": ""})
    return df, headers


def update_review_in_sheet(
    image_uid: str,
    image_filename: str,
    sheet_row_number: str,
    updates: Dict[str, str],
) -> bool:
    """
    Find the row by image_uid (primary) / image_filename / sheet_row_number
    and update only the columns in `updates`. Returns True on success.
    """
    ws = get_worksheet()
    headers = ws.row_values(1)

    # Build a header → column-index map (1-indexed for gspread)
    h_idx = {h: i + 1 for i, h in enumerate(headers)}

    # Sanity: ensure all update keys exist as columns
    for k in updates:
        if k not in h_idx:
            raise RuntimeError(f"العمود '{k}' غير موجود في رؤوس الشيت.")

    # Read everything once to find the matching row
    all_values = ws.get_all_values()
    if not all_values:
        raise RuntimeError("الشيت فاضي.")
    body = all_values[1:]   # skip header

    target_row_idx: Optional[int] = None   # 1-indexed sheet row

    uid_col_idx = h_idx.get(COL_UID)
    fname_col_idx = h_idx.get(COL_FILENAME)
    rownum_col_idx = h_idx.get(COL_ROW_NUMBER)

    # Priority 1: match by image_uid
    if uid_col_idx and image_uid:
        for i, row in enumerate(body):
            if (uid_col_idx - 1) < len(row) and row[uid_col_idx - 1] == image_uid:
                target_row_idx = i + 2   # +2 = header offset + 1-indexed
                break
    # Priority 2: match by image_filename
    if target_row_idx is None and fname_col_idx and image_filename:
        for i, row in enumerate(body):
            if (fname_col_idx - 1) < len(row) and row[fname_col_idx - 1] == image_filename:
                target_row_idx = i + 2
                break
    # Priority 3: match by sheet_row_number
    if target_row_idx is None and rownum_col_idx and sheet_row_number:
        for i, row in enumerate(body):
            if (rownum_col_idx - 1) < len(row) and row[rownum_col_idx - 1] == sheet_row_number:
                target_row_idx = i + 2
                break

    if target_row_idx is None:
        raise RuntimeError(
            f"لم يُعثَر على الصف المطابق "
            f"(uid={image_uid!r}, filename={image_filename!r}, row#={sheet_row_number!r})."
        )

    # Build a batch update for just the changed cells
    from gspread.utils import rowcol_to_a1
    batch = []
    for k, v in updates.items():
        a1 = rowcol_to_a1(target_row_idx, h_idx[k])
        batch.append({"range": a1, "values": [[v]]})
    ws.batch_update(batch, value_input_option="USER_ENTERED")

    # Invalidate cached DataFrame so the UI re-reads
    load_sheet_data.clear()
    return True


# ═══════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════
def now_riyadh_iso() -> str:
    return datetime.now(RIYADH_TZ).strftime("%Y-%m-%d %H:%M:%S")


def normalize_review_status(value: str) -> str:
    """Map whatever's in the Sheet to one of {pending, approved, rejected}."""
    v = (value or "").strip().lower()
    if v in ("approved", "معتمد"):
        return ST_APPROVED
    if v in ("regeneration_requested", "rejected", "مرفوض", "بانتظار إعادة التوليد"):
        return ST_REJECTED
    return ST_PENDING


def reviewable_df(df: pd.DataFrame) -> pd.DataFrame:
    """Only rows that actually have an image_url (visible to reviewers)."""
    if COL_IMAGE_URL not in df.columns:
        return df.iloc[0:0]
    mask = df[COL_IMAGE_URL].astype(str).str.strip().str.len() > 0
    return df[mask].copy().reset_index(drop=True)


def status_pill_html(status: str) -> str:
    label, _ = STATUS_LABELS[status]
    klass = {
        ST_PENDING:  "pill-pending",
        ST_APPROVED: "pill-approved",
        ST_REJECTED: "pill-rejected",
    }[status]
    return f'<span class="pill {klass}">{label}</span>'


def drive_thumbnail_url(url: str, width: int = 800) -> str:
    """Convert Drive viewer URLs to a direct thumbnail URL that st.image can load."""
    if not url:
        return url
    # already a uc?id= or thumbnail link → return as-is
    m = re.search(r"[?&]id=([a-zA-Z0-9_-]+)", url)
    if m:
        return f"https://drive.google.com/thumbnail?id={m.group(1)}&sz=w{width}"
    # /file/d/<ID>/view style
    m = re.search(r"/d/([a-zA-Z0-9_-]+)", url)
    if m:
        return f"https://drive.google.com/thumbnail?id={m.group(1)}&sz=w{width}"
    return url


# ═══════════════════════════════════════════════════════════════════════════
# Arabic search normalization (لا يغيّر القيم الأصلية في الشيت)
# ═══════════════════════════════════════════════════════════════════════════
_ARABIC_DIACRITICS = re.compile("[\u064B-\u065F\u0670]")   # tashkeel + superscript alef
_TATWEEL = "\u0640"


def normalize_arabic(text: str) -> str:
    """تطبيع للبحث فقط: إزالة التشكيل + توحيد الحروف + إزالة المسافات الزائدة."""
    if not text:
        return ""
    if not isinstance(text, str):
        text = str(text)
    text = unicodedata.normalize("NFC", text)
    text = _ARABIC_DIACRITICS.sub("", text)
    text = text.replace(_TATWEEL, "")
    # توحيد الحروف
    text = (
        text.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا")
            .replace("ى", "ي").replace("ؤ", "و").replace("ئ", "ي")
            .replace("ة", "ه")
    )
    text = re.sub(r"\s+", " ", text).strip().lower()
    return text


# ═══════════════════════════════════════════════════════════════════════════
# Prompt Repair Agent
# ═══════════════════════════════════════════════════════════════════════════
@st.cache_resource
def get_openai_client() -> Optional["OpenAI"]:
    """Build the OpenAI client from secrets. Returns None if not configured."""
    if OpenAI is None:
        return None
    try:
        key = st.secrets["openai"]["api_key"]
    except (KeyError, FileNotFoundError):
        return None
    if not key:
        return None
    return OpenAI(api_key=key)


AGENT_SYSTEM_PROMPT = """أنت Prompt Repair Agent محدود الصلاحية لمشروع توليد صور قاموس عربي.

# مهمتك الوحيدة
تأخذ برومت توليد صور موجود وتعدّله بناءً على ملاحظات منظَّمة من مراجع بشري.
الناتج المتوقع: نص برومت إنجليزي واحد فقط (أو الكلمة INSUFFICIENT_NOTE)، بدون مقدمة ولا شرح.

# المدخلات المنظَّمة التي ستصلك
1. قائمة فئات مشاكل (مختارة من قائمة موحَّدة).
2. نص حر اختياري بتفاصيل إضافية (≤200 حرف).
3. نص حر اختياري بوصف الصورة المطلوبة (≤250 حرف).

# دليل التعامل مع كل فئة مشكلة
- "الخلفية غير صحيحة" → أعِد التشديد على #FAFAFA seamless background, no beige, no gradient.
- "الزاوية خاطئة" → استخدم وصف المراجع لتحديد الزاوية (front view / side view / three-quarter view / top-down view).
- "الشكل غير دقيق" → استخدم وصف المراجع البصري لإعادة وصف العنصر بشكل أدق (الأبعاد، النسب، الأجزاء).
- "الإضاءة سيئة" → اضبط "soft diffused studio lighting from upper-left, very subtle shadow".
- "الألوان غير مناسبة" → استخدم الألوان المذكورة في التعريف العربي أو في وصف المراجع، بدون اختراع.
- "التفاصيل ناقصة" → أضف العناصر المذكورة في وصف المراجع (مثل: عدد الخطاطيف، طول العمود، نوع القاعدة).
- "الصورة مقصوصة" → شدّد على "full object visible, not cropped, ample breathing room around subject".
- "غير ذلك" → عالج المشكلة كما هي مذكورة في النص الحر.

# قيود صارمة لا تتجاوزها أبداً
- مُحرّم تماماً تغيير معنى الكلمة العربية أو نوع العنصر المُصوَّر.
- مُحرّم اختراع عنصر جديد أو إضافة سياق أو تفاصيل غير موجودة في التعريف العربي أو وصف المراجع.
- ابدأ من البرومت الأصلي حرفياً، وعدّل فقط الجوانب المرتبطة بفئات المشاكل المختارة.
- لا تحوّل الأسلوب إلى رسم/كرتون/illustration/3D-render/painting.
- لا تضف نصوصاً على الصورة، شعارات، علامات مائية، أيدي، أو أشخاص.

# قواعد ثابتة يجب أن تبقى موجودة في البرومت الجديد
- Clean studio product photography.
- Pure neutral near-white seamless background (exactly #FAFAFA), uniform, no warmth, no gradient, no vignette.
- Single centered subject, photorealistic, ultra-sharp focus.
- 1:1 square composition.
- Soft diffused studio lighting, very subtle natural shadow.
- Full object visible, not cropped, ample breathing room.
- Negative constraints: no text, no logos, no watermarks, no hands, no people, no cartoon, no illustration style, no clutter, no beige background, no warm yellow tint.

# في حالة عدم الكفاية
أرجع كلمة واحدة: INSUFFICIENT_NOTE
إذا تحقق أيٌّ مما يلي:
- قائمة فئات المشاكل فارغة، والنصوص الحرة فارغة أو غامضة جداً ("سيئة"، "ما تعجبني").
- المراجع يطلب تغيير العنصر المُصوَّر نفسه (مثلاً يطلب صورة شيء مختلف عن الكلمة).
- الملاحظة تتعارض مع التعريف العربي.

# الناتج
نص برومت إنجليزي واحد متصل (أو الكلمة INSUFFICIENT_NOTE). بدون عناوين، بدون قوائم، بدون JSON، بدون شرح."""


def _normalize_for_compare(s: str) -> str:
    """Normalize for sanity checks: lowercase, no punctuation, no diacritics."""
    s = normalize_arabic(s or "").lower()
    return re.sub(r"[^\w\u0600-\u06FF\s]", " ", s)


def validate_regenerated_prompt(
    new_prompt: str,
    english_term: str,
    object_description: str,
    word_clean: str,
    reviewer_visual_note: str,
) -> Tuple[bool, str]:
    """
    Defensive checks on the agent's output. Returns (ok, repair_note).
    Returns (False, reason) if the prompt looks like a hallucination.
    """
    if not new_prompt or not new_prompt.strip():
        return False, "البرومت الناتج فارغ."

    if new_prompt.strip().upper() == "INSUFFICIENT_NOTE":
        return False, "الإيجنت أعلن أن ملاحظة المراجع غير كافية. الرجاء توضيح ملاحظتك."

    np_norm = _normalize_for_compare(new_prompt)

    # 1) Must still reference the subject (english_term OR Arabic word OR description noun)
    anchors = []
    if english_term:
        anchors.extend(english_term.lower().split()[:4])
    if word_clean:
        anchors.append(normalize_arabic(word_clean))
    if object_description:
        # first 6 tokens of the description are the noun + adjectives
        anchors.extend(object_description.lower().split()[:6])
    anchors = [a for a in anchors if a and len(a) > 2]

    if anchors and not any(a in np_norm for a in anchors):
        return False, (
            "البرومت الجديد لا يحتوي على أي مرجع للكلمة أو الترجمة أو الوصف "
            "(احتمال اختراع موضوع مختلف)."
        )

    # 2) Must mention the locked-in style anchors
    required_phrases = ["#FAFAFA", "photorealistic"]
    missing = [p for p in required_phrases if p.lower() not in new_prompt.lower()]
    if missing:
        return False, f"البرومت الجديد لا يذكر قواعد الأساس: {missing}"

    # 3) Must NOT contain illustration/cartoon style words
    forbidden = ["cartoon", "anime", "illustration style", "painting style",
                  "watercolor", "3d render", "low poly"]
    for f in forbidden:
        if f in new_prompt.lower():
            return False, f"البرومت الجديد يحتوي على أسلوب ممنوع: '{f}'."

    # 4) If reviewer left a visual note, prompt should reflect at least one keyword
    rv_note = (reviewer_visual_note or "").strip()
    if rv_note and len(rv_note) > 8:
        rv_norm = _normalize_for_compare(rv_note)
        # extract distinctive words (>=3 chars) from the note
        note_tokens = [t for t in rv_norm.split() if len(t) >= 3]
        if note_tokens and not any(t in np_norm for t in note_tokens):
            return False, "البرومت الجديد لا يعكس ملاحظة المراجع البصرية."

    return True, "passed validation"


def display_word(row) -> str:
    """
    Return the word to display in the UI:
      - nonDiacriticsLemma if available
      - else: lemma with diacritics removed
    Original sheet values are NOT modified.
    """
    nd = str(row.get(COL_NONDIAC, "") or "").strip()
    if nd:
        return nd
    lemma = str(row.get(COL_LEMMA, "") or "").strip()
    return remove_diacritics_only(lemma)


def remove_diacritics_only(text: str) -> str:
    """Remove tashkeel only — without letter unification (preserves أ إ ى ة)."""
    if not text:
        return ""
    text = unicodedata.normalize("NFC", str(text))
    text = _ARABIC_DIACRITICS.sub("", text)
    text = text.replace(_TATWEEL, "")
    return re.sub(r"\s+", " ", text).strip()





def run_prompt_repair_agent(
    word: str,
    definition_ar: str,
    translation: str,
    object_description: str,
    image_prompt: str,
    negative_prompt: str,
    rejection_reason: str,
    reviewer_visual_note: str,
    issues: Optional[List[str]] = None,
) -> str:
    """يستدعي gpt-4o-mini ويرجع regenerated_prompt واحد كنص.
    يقبل قائمة issues منظَّمة (من multiselect) بالإضافة للنصوص الحرة."""
    client = get_openai_client()
    if client is None:
        raise RuntimeError(
            "مفتاح OpenAI غير مُعَدّ. أضيفي في secrets.toml قسم [openai] "
            "وفيه api_key = \"sk-...\""
        )

    issues_block = "(لم تُحدَّد فئات)"
    if issues:
        issues_block = "\n".join(f"- {x}" for x in issues)

    user_msg = f"""## معلومات الصف

- الكلمة العربية: {word or "(غير محدد)"}
- التعريف العربي: {definition_ar or "(غير محدد)"}
- الترجمة الإنجليزية: {translation or "(غير محدد)"}
- الوصف البصري (object_description): {object_description or "(غير محدد)"}

## البرومت الأصلي المُستخدَم

{image_prompt or "(غير محدد)"}

## القيود السلبية (negative_prompt)

{negative_prompt or "(غير محدد)"}

## ملاحظات المراجع — مدخلات منظَّمة

### فئات المشاكل المختارة من القائمة:
{issues_block}

### تفاصيل إضافية (نص حر، اختياري):
{rejection_reason or "(لم يُذكر)"}

### تصوّر المراجع للصورة المطلوبة (نص حر، اختياري):
{reviewer_visual_note or "(لم يُذكر)"}

## المطلوب

أعطني نسخة محسَّنة من البرومت تعالج تحديداً الفئات المختارة + الملاحظات،
مع الحفاظ على معنى الكلمة وكل القواعد الثابتة.
ردّ بنص البرومت الجديد فقط، أو INSUFFICIENT_NOTE — لا شيء آخر."""

    resp = client.chat.completions.create(
        model=AGENT_MODEL,
        temperature=AGENT_TEMP,
        messages=[
            {"role": "system", "content": AGENT_SYSTEM_PROMPT},
            {"role": "user", "content": user_msg},
        ],
    )
    return (resp.choices[0].message.content or "").strip()


def propose_clarification_question(
    repair_note: str,
    issues: List[str],
    reason: str,
    visual_note: str,
) -> str:
    """Build ONE targeted Arabic question to show the reviewer when the agent's
    output failed validation or returned INSUFFICIENT_NOTE."""
    rn = (repair_note or "").lower()

    if "insufficient" in rn or "غير كافية" in repair_note:
        if not issues and not reason and not visual_note:
            return ("لم تُحدِّدي أي مشكلة. اختاري على الأقل بنداً واحداً "
                    "من قائمة المشاكل، أو اكتبي وصفاً موجزاً للصورة المطلوبة.")
        if not visual_note:
            return ("اختياراتك واضحة لكن نحتاج وصفاً بصرياً موجزاً "
                    "للصورة المطلوبة (مثال: «شماعة سوداء بقاعدة دائرية و٦ خطاطيف»).")
        return ("ملاحظتك ما زالت غامضة. أعطينا تفصيلاً ملموساً (لون/عدد/شكل) "
                "بدلاً من وصف عام.")

    if "البرومت الناتج فارغ" in repair_note:
        return "حدث خطأ تقني. أعيدي المحاولة بعد لحظات."

    if "ملاحظة المراجع البصرية" in repair_note:
        return ("ما اللون أو الشكل أو الزاوية المطلوبة بالضبط؟ "
                "اكتبيها بإيجاز في سطرين على الأكثر.")

    if "أسلوب ممنوع" in repair_note:
        return ("وصفك يقترب من أسلوب رسم/كرتون. نريد صورة فوتوغرافية واقعية. "
                "أعيدي صياغة الملاحظة بمصطلحات فوتوغرافية.")

    if "لا يحتوي على أي مرجع" in repair_note:
        return ("ملاحظتك ابتعدت عن الكلمة الأصلية. "
                "ركّزي على تعديل الصورة الحالية لا استبدالها بشيء مختلف.")

    if "قواعد الأساس" in repair_note:
        return "حدث خطأ في توليد البرومت. أعيدي المحاولة."

    return f"يحتاج توضيحاً: {repair_note}"


# ═══════════════════════════════════════════════════════════════════════════
# Image Generation (gpt-image-1) + Google Drive Upload
# ═══════════════════════════════════════════════════════════════════════════
import base64
import io

def generate_image_bytes(prompt: str, quality: Optional[str] = None) -> Tuple[bool, bytes, str]:
    """
    Generate one image using gpt-image-1. Returns (ok, png_bytes, error_msg).
    quality: "low" | "medium" | "high" — defaults to IMAGE_QUALITY (medium).
    """
    client = get_openai_client()
    if client is None:
        return False, b"", "OpenAI client غير مُعَدّ."

    use_quality = quality or IMAGE_QUALITY
    try:
        resp = client.images.generate(
            model=IMAGE_MODEL,
            prompt=prompt,
            size=IMAGE_SIZE,
            quality=use_quality,
            n=1,
        )
        b64 = getattr(resp.data[0], "b64_json", None)
        if not b64:
            return False, b"", "API لم يُرجع b64_json."
        return True, base64.b64decode(b64), ""
    except Exception as e:
        return False, b"", f"{type(e).__name__}: {e}"


@st.cache_resource
def get_drive_service():
    """Build a Google Drive v3 service from the same service-account secrets."""
    try:
        from googleapiclient.discovery import build
    except ImportError:
        return None, "مكتبة google-api-python-client غير مُثبَّتة."
    try:
        creds_info = dict(st.secrets["gcp_service_account"])
    except (KeyError, FileNotFoundError):
        return None, "secrets يفتقد [gcp_service_account]."
    drive_scopes = [
        "https://www.googleapis.com/auth/drive.file",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_info(creds_info, scopes=drive_scopes)
    service = build("drive", "v3", credentials=creds, cache_discovery=False)
    return service, None


def find_drive_folder_id(folder_name: str) -> Tuple[Optional[str], str]:
    """
    Look up the Drive folder ID by name. Service account must have access.
    Returns (folder_id, error_msg). If multiple folders match, returns error.
    """
    service, err = get_drive_service()
    if service is None:
        return None, err

    q = (
        f"name = '{folder_name}' and "
        f"mimeType = 'application/vnd.google-apps.folder' and trashed = false"
    )
    try:
        result = service.files().list(
            q=q, fields="files(id, name, owners)",
            supportsAllDrives=True, includeItemsFromAllDrives=True,
        ).execute()
    except Exception as e:
        return None, f"فشل البحث عن المجلد: {type(e).__name__}: {e}"

    files = result.get("files", [])
    if not files:
        return None, (
            f"لم يُعثَر على مجلد باسم '{folder_name}'. "
            "تأكدي أن الـ service account له صلاحية Editor على المجلد."
        )
    if len(files) > 1:
        ids = ", ".join(f["id"] for f in files)
        return None, (
            f"وجدت {len(files)} مجلدات بنفس الاسم. "
            f"أضيفي في secrets [drive] → folder_id بدل folder_name. IDs: {ids}"
        )
    return files[0]["id"], ""


def upload_image_to_drive(
    image_bytes: bytes, filename: str
) -> Tuple[bool, str, str, str]:
    """
    Upload bytes as a PNG to the configured Drive folder.
    Returns (ok, drive_file_id, public_url, error_msg).
    """
    service, err = get_drive_service()
    if service is None:
        return False, "", "", err

    # Folder ID: prefer secrets[drive][folder_id], else look up by folder_name
    try:
        drive_cfg = st.secrets["drive"]
    except (KeyError, FileNotFoundError):
        return False, "", "", "secrets يفتقد قسم [drive]."

    folder_id = drive_cfg.get("folder_id", "").strip() if hasattr(drive_cfg, "get") else ""
    if not folder_id:
        folder_name = drive_cfg.get("folder_name", "Moarjam_AlRiyadh_Images") if hasattr(drive_cfg, "get") else "Moarjam_AlRiyadh_Images"
        folder_id, err = find_drive_folder_id(folder_name)
        if not folder_id:
            return False, "", "", err

    try:
        from googleapiclient.http import MediaIoBaseUpload
        media = MediaIoBaseUpload(io.BytesIO(image_bytes), mimetype="image/png")
        metadata = {"name": filename, "parents": [folder_id]}
        file = service.files().create(
            body=metadata, media_body=media, fields="id",
            supportsAllDrives=True,
        ).execute()
        file_id = file["id"]

        # Make link-shareable
        try:
            service.permissions().create(
                fileId=file_id, body={"type": "anyone", "role": "reader"},
                supportsAllDrives=True,
            ).execute()
        except Exception:
            pass

        url = f"https://drive.google.com/uc?id={file_id}"
        return True, file_id, url, ""
    except Exception as e:
        return False, "", "", f"{type(e).__name__}: {e}"


# ═══════════════════════════════════════════════════════════════════════════
# Unified regeneration pipeline (one-button, end-to-end)
# ═══════════════════════════════════════════════════════════════════════════
def run_regeneration_pipeline(
    row: pd.Series,
    issues: List[str],
    reason: str,
    visual_note: str,
    test_mode: bool,
    fast_mode: bool,
    reviewer_name: str,
) -> Tuple[bool, str, str]:
    """
    Full background pipeline:
      1. Save rejection metadata.
      2. Run agent → English prompt (not shown to user).
      3. Validate defensively.
      4. If invalid → save note + return (False, clarification_q, "").
      5. If valid → generate image with gpt-image-1.
      6. Upload to Drive.
      7. Update Sheet (image_url or test_* depending on test_mode).
    Returns (success, message_or_clarification, new_image_url).

    On ANY failure after the rejection metadata is saved:
      - image_url is NEVER touched.
      - The full error is persisted to `regeneration_note` (or
        `test_regeneration_note` in test mode) and shown via st.error.
    """
    uid_val          = str(row.get(COL_UID, ""))
    filename_val     = str(row.get(COL_FILENAME, ""))
    row_number_val   = str(row.get(COL_ROW_NUMBER, ""))

    # Pick the note column based on mode (per spec — never change image_url on failure)
    err_note_col = COL_TEST_NOTE if test_mode else "regeneration_note"

    def _persist_error(err_msg: str) -> None:
        """Write the error message to the right note column. Never touches image_url."""
        try:
            update_review_in_sheet(
                image_uid=uid_val,
                image_filename=filename_val,
                sheet_row_number=row_number_val,
                updates={
                    err_note_col:                  err_msg[:480],
                    "regeneration_request_status": "failed",
                },
            )
        except Exception:
            pass  # logging only — must not raise from inside an error handler

    progress = st.progress(0, text="💾 حفظ ملاحظات المراجع في الشيت...")

    # — Step 1: persist rejection metadata —
    combined_reason_parts = list(issues)
    if reason.strip():
        combined_reason_parts.append(reason.strip())
    combined_reason = " | ".join(combined_reason_parts) or "(لم يُذكر)"

    try:
        update_review_in_sheet(
            image_uid=uid_val,
            image_filename=filename_val,
            sheet_row_number=row_number_val,
            updates={
                "review_status":               ST_REJECTED,
                "review_decision":             "rejected",
                "rejection_reason":            combined_reason,
                "reviewer_visual_note":        visual_note.strip(),
                "needs_regeneration":          "yes",
                "regeneration_request_status": "in_progress",
                "reviewer_name":               reviewer_name,
                "reviewed_at":                 now_riyadh_iso(),
                "approved_image_url":          "",
            },
        )
    except Exception as e:
        err = f"فشل حفظ ملاحظات الرفض: {type(e).__name__}: {e}"
        progress.empty()
        st.error(err)
        # nothing to persist yet — sheet write itself failed
        return False, err, ""

    # ── Step 2: agent — build new prompt ──────────────────────────────────
    progress.progress(10, text="🤖 بدأ إنشاء البرومت...")
    try:
        english = str(row.get(COL_ENGLISH) or row.get(COL_TRANSLATION) or "")
        new_prompt = run_prompt_repair_agent(
            word=str(row.get(COL_LEMMA, "")),
            definition_ar=str(row.get(COL_DEFINITION, "")),
            translation=english,
            object_description=str(row.get(COL_OBJ_DESC, "")),
            image_prompt=str(row.get(COL_PROMPT, "")),
            negative_prompt=str(row.get(COL_NEG_PROMPT, "")),
            rejection_reason=reason.strip(),
            reviewer_visual_note=visual_note.strip(),
            issues=issues,
        )
    except Exception as e:
        err = f"فشل إنشاء البرومت (الإيجنت): {type(e).__name__}: {e}"
        _persist_error(err)
        progress.empty()
        st.error(err)
        return False, err, ""

    progress.progress(25, text="✅ تم إنشاء البرومت — جاري التحقق منه...")

    # ── Step 3: defensive validation ──────────────────────────────────────
    ok, repair_note = validate_regenerated_prompt(
        new_prompt=new_prompt,
        english_term=english,
        object_description=str(row.get(COL_OBJ_DESC, "")),
        word_clean=display_word(row),
        reviewer_visual_note=visual_note.strip(),
    )

    if not ok:
        # save the failure note (no prompt) and return a clarification question
        try:
            update_review_in_sheet(
                image_uid=uid_val,
                image_filename=filename_val,
                sheet_row_number=row_number_val,
                updates={
                    COL_PROMPT_REPAIR_STATUS:      "needs_clarification",
                    COL_PROMPT_REPAIR_NOTE:        repair_note,
                    "regeneration_request_status": "needs_clarification",
                },
            )
        except Exception:
            pass
        progress.empty()
        clarification = propose_clarification_question(
            repair_note=repair_note,
            issues=issues,
            reason=reason.strip(),
            visual_note=visual_note.strip(),
        )
        # image_url not touched — by design
        return False, clarification, ""

    # ── Step 4: save the validated prompt (background, hidden from UI) ────
    try:
        update_review_in_sheet(
            image_uid=uid_val,
            image_filename=filename_val,
            sheet_row_number=row_number_val,
            updates={
                COL_REGEN_PROMPT:          new_prompt,
                COL_PROMPT_REPAIR_STATUS:  "ok",
                COL_PROMPT_REPAIR_NOTE:    repair_note,
            },
        )
    except Exception:
        pass  # non-fatal — continue

    quality = "low" if fast_mode else IMAGE_QUALITY

    # ── Step 5: generate image ────────────────────────────────────────────
    progress.progress(40, text=f"🎨 بدأ توليد الصورة من OpenAI (جودة: {quality})...")
    gen_ok, img_bytes, err = generate_image_bytes(new_prompt, quality=quality)
    if not gen_ok:
        msg = f"فشل توليد الصورة من OpenAI: {err}"
        _persist_error(msg)
        progress.empty()
        st.error(msg)
        return False, msg, ""

    progress.progress(65, text=f"✅ تم استلام الصورة من OpenAI ({len(img_bytes)} bytes)")

    # ── Step 6: upload to Drive ───────────────────────────────────────────
    progress.progress(72, text="☁️ بدأ رفع الصورة إلى Google Drive...")
    base_fname = filename_val or "image.png"
    ts = datetime.now(RIYADH_TZ).strftime("%Y%m%d_%H%M%S")
    suffix = "_test" if test_mode else "_regen"
    if base_fname.endswith(".png"):
        new_fname = base_fname.replace(".png", f"{suffix}_{ts}.png")
    else:
        new_fname = f"{base_fname}{suffix}_{ts}.png"

    up_ok, file_id, new_url, up_err = upload_image_to_drive(img_bytes, new_fname)
    if not up_ok:
        msg = f"فشل رفع الصورة إلى Drive: {up_err}"
        _persist_error(msg)
        progress.empty()
        st.error(msg)
        return False, msg, ""

    progress.progress(85, text="✅ تم رفع الصورة إلى Drive — جاري تحديث الشيت...")

    # ── Step 7: write final state ─────────────────────────────────────────
    now_ts = now_riyadh_iso()
    st.session_state["gen_count"] = st.session_state.get("gen_count", 0) + 1

    if test_mode:
        # ⚠️ image_url is NOT touched in Test Mode (per spec).
        updates = {
            COL_TEST_PROMPT:               new_prompt,
            COL_TEST_IMG_URL:              new_url,
            COL_TEST_REGEN_AT:             now_ts,
            COL_TEST_NOTE:                 f"test_generation; file_id={file_id}; quality={quality}",
            "regeneration_request_status": "test_completed",
        }
    else:
        prev_url = str(row.get(COL_IMAGE_URL, ""))
        old_count = str(row.get(COL_REGEN_COUNT, "0") or "0")
        try:
            new_count = int(old_count) + 1
        except ValueError:
            new_count = 1
        history_line = (
            f"[{now_ts}] regenerated → {new_url} "
            f"(prev: {prev_url[:60]}; quality: {quality})"
        )
        old_history = str(row.get(COL_REGEN_HISTORY, ""))
        new_history = (old_history + "\n" + history_line).strip()
        updates = {
            COL_IMAGE_URL:                 new_url,
            COL_PREVIOUS_URL:              prev_url,
            COL_REGEN_COUNT:               str(new_count),
            COL_LAST_REGEN_AT:             now_ts,
            COL_REGEN_HISTORY:             new_history,
            "regeneration_request_status": "completed",
            "needs_regeneration":          "no",
            "review_status":               ST_PENDING,
            "review_decision":             "",
            # clear any previous failure note for this row
            "regeneration_note":           "",
        }

    try:
        update_review_in_sheet(
            image_uid=uid_val,
            image_filename=filename_val,
            sheet_row_number=row_number_val,
            updates=updates,
        )
    except Exception as e:
        msg = f"تم التوليد ورُفعت الصورة لكن فشل تحديث الشيت: {type(e).__name__}: {e}"
        _persist_error(msg)
        progress.empty()
        st.error(msg)
        return False, msg, new_url

    progress.progress(95, text="💾 تم تحديث الشيت")
    progress.progress(100, text="✅ تم عرض النتيجة")
    progress.empty()

    msg = (
        "✅ تم التوليد. النتيجة محفوظة في أعمدة test_* (الأصل لم يُلمَس)."
        if test_mode else
        "✅ تم استبدال الصورة بالنسخة الجديدة وحفظ السجل في الشيت."
    )
    return True, msg, new_url


# ═══════════════════════════════════════════════════════════════════════════
# UI — Sidebar (filters, search, refresh)
# ═══════════════════════════════════════════════════════════════════════════
with st.sidebar:
    st.markdown("### 🔍 الفلاتر")

    search_query = st.text_input("ابحث بالكلمة أو المعنى أو الترجمة", value="")
    if st.button("🔄 تحديث البيانات", use_container_width=True):
        load_sheet_data.clear()
        st.rerun()

    st.divider()
    st.caption("الفلتر يحدد الصفوف المعروضة في الجدول.")
    filter_choice = st.radio(
        "الحالة",
        options=["الكل", "قيد المراجعة", "معتمد", "مرفوض / إعادة توليد"],
        index=0,
    )

# اسم المراجع لم يعد مطلوبًا من السايدبار — قيمة افتراضية ثابتة
reviewer_name = DEFAULT_REVIEWER_NAME


# ═══════════════════════════════════════════════════════════════════════════
# Load data
# ═══════════════════════════════════════════════════════════════════════════
try:
    df_full, headers = load_sheet_data()
except KeyError as e:
    st.error(
        "تعذّر قراءة الـ secrets. تأكدي من ملف `.streamlit/secrets.toml` "
        f"وأنه يحتوي على `[gcp_service_account]` و `[sheet]`. التفاصيل: {e}"
    )
    st.stop()
except APIError as e:
    st.error(f"خطأ من Google Sheets API: {e}")
    st.stop()
except Exception as e:
    st.error(f"فشل تحميل البيانات: {type(e).__name__}: {e}")
    st.stop()

if df_full.empty:
    st.warning("الشيت فاضي.")
    st.stop()

df_view = reviewable_df(df_full)

# Compute normalized status per row
if "review_status" not in df_view.columns:
    df_view["review_status"] = ""
df_view["_status"] = df_view["review_status"].apply(normalize_review_status)


# ═══════════════════════════════════════════════════════════════════════════
# Header + stats
# ═══════════════════════════════════════════════════════════════════════════
st.markdown(
    f'<div class="app-header">'
    f'<div class="title">{APP_TITLE}</div>'
    f'<div class="subtitle">{APP_SUBTITLE}</div>'
    f'</div>',
    unsafe_allow_html=True,
)

total      = len(df_view)
n_pending  = int((df_view["_status"] == ST_PENDING).sum())
n_approved = int((df_view["_status"] == ST_APPROVED).sum())
n_rejected = int((df_view["_status"] == ST_REJECTED).sum())

def stat_card(label: str, value: int, color_hex: str = "#9C9C9C"):
    return f"""
    <div class="stat-card">
      <div class="stat-label">
        <span class="stat-dot" style="background:{color_hex};"></span>
        <span>{label}</span>
      </div>
      <div class="stat-value">{value}</div>
    </div>
    """

c1, c2, c3, c4 = st.columns(4)
with c1: st.markdown(stat_card("إجمالي الصور",      total,      "#3A3A3A"), unsafe_allow_html=True)
with c2: st.markdown(stat_card("قيد المراجعة",      n_pending,  "#9C9C9C"), unsafe_allow_html=True)
with c3: st.markdown(stat_card("معتمدة",             n_approved, "#0E8E62"), unsafe_allow_html=True)
with c4: st.markdown(stat_card("بانتظار إعادة توليد", n_rejected, "#C58A1A"), unsafe_allow_html=True)

st.write("")


# ═══════════════════════════════════════════════════════════════════════════
# Filter + search
# ═══════════════════════════════════════════════════════════════════════════
filter_map = {
    "الكل":                       None,
    "قيد المراجعة":               ST_PENDING,
    "معتمد":                      ST_APPROVED,
    "مرفوض / إعادة توليد":         ST_REJECTED,
}
status_filter = filter_map[filter_choice]
filtered = df_view if status_filter is None else df_view[df_view["_status"] == status_filter]

if search_query.strip():
    q_norm = normalize_arabic(search_query)
    if q_norm:
        # ابحث بدون تشكيل عبر 6 أعمدة:
        # الكلمة بالتشكيل + بدون تشكيل + التعريف + الترجمة + english_term + object_description
        search_cols = [
            COL_LEMMA, COL_NONDIAC, COL_DEFINITION,
            COL_TRANSLATION, COL_ENGLISH, COL_OBJ_DESC,
        ]
        mask = pd.Series([False] * len(filtered), index=filtered.index)
        for col in search_cols:
            if col not in filtered.columns:
                continue
            normalized = filtered[col].astype(str).map(normalize_arabic)
            mask = mask | normalized.str.contains(q_norm, na=False, regex=False)
        filtered = filtered[mask]

filtered = filtered.reset_index(drop=True)


# ═══════════════════════════════════════════════════════════════════════════
# Detail view OR list view
# ═══════════════════════════════════════════════════════════════════════════
def render_detail(row: pd.Series):
    """Detail page for one image — image on the left, fields + actions on the right."""
    top = st.columns([1, 6, 1])
    with top[2]:
        if st.button("⬅️ رجوع للجدول", use_container_width=True):
            st.session_state.pop("selected_uid", None)
            st.rerun()

    col_img, col_info = st.columns([1.1, 1], gap="large")

    # ── Image side ──────────────────────────────────────────────────────
    with col_img:
        url = str(row.get(COL_IMAGE_URL, "")).strip()
        if url:
            thumb = drive_thumbnail_url(url, width=900)
            try:
                st.image(thumb, use_column_width=True)
            except Exception:
                st.markdown(f"[فتح الصورة في تبويب جديد]({url})")
        else:
            st.info("لا توجد صورة مرتبطة بهذا الصف.")

        st.caption(str(row.get(COL_FILENAME, "")))

        st.markdown(
            f'<div class="row-card" style="margin-top:14px;">'
            f'<div style="font-size:12px;color:#7A7A7A;margin-bottom:6px;">'
            f'البرومت المستخدم'
            f'</div>'
            f'<div style="font-family: monospace; font-size:13px; '
            f'background:#FAF8F4; padding:10px; border-radius:8px; '
            f'white-space: pre-wrap; direction:ltr; text-align:left;">'
            f'{str(row.get(COL_PROMPT, "")).strip() or "—"}'
            f'</div>'
            f'</div>',
            unsafe_allow_html=True,
        )

    # ── Info + actions side ─────────────────────────────────────────────
    with col_info:
        word_for_display = display_word(row)
        st.markdown(
            f'<div class="row-card">'
            f'<div style="font-size:28px;font-weight:700;">{word_for_display}</div>'
            f'</div>',
            unsafe_allow_html=True,
        )

        def info_row(label, value, mono=False):
            value_html = str(value or "—")
            style = "font-family:monospace;direction:ltr;text-align:left;" if mono else ""
            return (
                f'<div style="display:flex;justify-content:space-between;'
                f'padding:10px 0;border-bottom:1px solid #F1EEE8;gap:18px;">'
                f'<div style="color:#7A7A7A;font-size:13px;min-width:130px;">{label}</div>'
                f'<div style="color:#2A2A2A;font-size:14px;{style}">{value_html}</div>'
                f'</div>'
            )

        english = (row.get(COL_ENGLISH) or row.get(COL_TRANSLATION) or "").strip() or "—"

        st.markdown(
            '<div class="row-card">' +
            info_row("المعنى المعتمد",   row.get(COL_DEFINITION, "")) +
            info_row("الترجمة الإنجليزية", english) +
            info_row("الوصف البصري",     row.get(COL_OBJ_DESC, "")) +
            info_row("نوع الصورة",       f'<span class="category-pill">{IMAGE_CATEGORY_LABEL}</span>') +
            info_row("المعرّف",          row.get(COL_UID, ""), mono=True) +
            info_row("اسم الملف",        row.get(COL_FILENAME, ""), mono=True) +
            info_row("رقم الصف",         row.get(COL_ROW_NUMBER, ""), mono=True) +
            '</div>',
            unsafe_allow_html=True,
        )

        # Current status pill
        st.markdown(
            f'<div style="margin:14px 0;">الحالة الحالية: '
            f'{status_pill_html(normalize_review_status(row.get("review_status","")))}</div>',
            unsafe_allow_html=True,
        )

        # ── Review action panel ─────────────────────────────────────────
        st.markdown("### إجراء المراجعة")

        tab_approve, tab_reject = st.tabs(["✅ اعتماد الصورة", "❌ رفض / إعادة توليد"])

        with tab_approve:
            st.write("اعتماد الصورة الحالية كما هي.")
            if st.button("اعتماد الصورة", type="primary",
                         use_container_width=True, key="btn_approve"):
                try:
                    update_review_in_sheet(
                        image_uid=str(row.get(COL_UID, "")),
                        image_filename=str(row.get(COL_FILENAME, "")),
                        sheet_row_number=str(row.get(COL_ROW_NUMBER, "")),
                        updates={
                            "review_status":       ST_APPROVED,
                            "review_decision":     "approved",
                            "approved_image_url":  str(row.get(COL_IMAGE_URL, "")),
                            "needs_regeneration":  "no",
                            "reviewer_name":       reviewer_name,
                            "reviewed_at":         now_riyadh_iso(),
                            "rejection_reason":    "",
                            "reviewer_visual_note": "",
                            "regeneration_request_status": "",
                        },
                    )
                    st.success("✅ تم الاعتماد وحفظه في الشيت.")
                    st.rerun()
                except Exception as e:
                    st.error(f"فشل الحفظ: {type(e).__name__}: {e}")

        with tab_reject:
            uid_val = str(row.get(COL_UID, "") or row.get(COL_FILENAME, "") or "")

            st.markdown(
                '<div style="color:#2A2A2A;font-size:14px;font-weight:500;'
                'margin:4px 0 8px 0;">'
                'اختاري المشكلة من القائمة، وأضيفي وصفاً موجزاً اختيارياً للصورة المطلوبة.'
                '</div>',
                unsafe_allow_html=True,
            )

            # ── 1) Structured issue picker (covers 80% of cases) ────────────
            ISSUE_OPTIONS = [
                "الخلفية غير صحيحة",
                "الزاوية خاطئة",
                "الشكل غير دقيق",
                "الإضاءة سيئة",
                "الألوان غير مناسبة",
                "التفاصيل ناقصة",
                "الصورة مقصوصة",
                "غير ذلك",
            ]
            issues = st.multiselect(
                "ما المشكلة في الصورة؟ (يمكن اختيار أكثر من واحدة)",
                options=ISSUE_OPTIONS,
                placeholder="اختاري واحدة أو أكثر…",
                key=f"issues_{uid_val}",
            )

            # ── 2) ONE short free-text field (merged) ───────────────────────
            visual_note = st.text_area(
                "وصف الصورة المطلوبة (اختياري) — سطران كحد أقصى",
                value="",
                max_chars=250,
                height=68,
                placeholder="مثال: شماعة سوداء بقاعدة دائرية و٦ خطاطيف، خلفية بيضاء.",
                key=f"reject_note_{uid_val}",
            )
            # internal: reason kept for pipeline compatibility (empty if only multiselect used)
            reason = ""

            # ── 3) Mode toggles ─────────────────────────────────────────────
            col_t, col_f = st.columns(2)
            with col_t:
                test_mode = st.toggle(
                    "🧪 Test Mode (لا يستبدل الصورة الأصلية)",
                    value=TEST_MODE_DEFAULT,
                    key=f"test_mode_{uid_val}",
                    help="في Test Mode تُحفظ النتيجة في أعمدة test_* فقط.",
                )
            with col_f:
                fast_mode = st.toggle(
                    "⚡ توليد سريع (جودة أقل)",
                    value=False,
                    key=f"fast_mode_{uid_val}",
                    help="quality=low — أسرع ~3 أضعاف، مناسب للتجارب.",
                )

            # ── 4) Session counter ──────────────────────────────────────────
            session_count = st.session_state.get("gen_count", 0)
            remaining = MAX_GENERATIONS_PER_SESSION - session_count
            st.caption(
                f"رصيد التوليد في الجلسة: "
                f"{max(remaining, 0)} / {MAX_GENERATIONS_PER_SESSION}"
            )
            if 0 < remaining <= 2:
                st.warning(f"⚠️ متبقي {remaining} توليدات فقط في هذه الجلسة.")
            elif remaining <= 0:
                st.error(
                    "⛔ تجاوزتِ الحد الأقصى للجلسة. أعيدي تحميل الصفحة لإعادة العداد."
                )

            # ── 5) Pre-flight: must have at least one signal ────────────────
            has_input = bool(issues) or bool(reason.strip()) or bool(visual_note.strip())
            can_regen = has_input and remaining > 0

            help_txt = None
            if not has_input:
                help_txt = "اختاري مشكلة واحدة على الأقل أو اكتبي وصفاً مختصراً."
            elif remaining <= 0:
                help_txt = "وصلتِ الحد الأقصى للجلسة."

            # ── 6) Show pending clarification (if any from a previous run) ──
            clar_key = f"clar_q_{uid_val}"
            pending_q = st.session_state.get(clar_key, "")
            if pending_q:
                st.info(f"❓ **سؤال توضيحي:** {pending_q}")

            # ── 7) THE single regenerate button ─────────────────────────────
            label = "🔄 إعادة التوليد"
            if test_mode:
                label += " (Test Mode)"
            else:
                label += " — 🚨 استبدال الأصل"

            if st.button(
                label,
                type="primary",
                use_container_width=True,
                disabled=not can_regen,
                help=help_txt,
                key=f"btn_regen_{uid_val}",
            ):
                success, message, new_url = run_regeneration_pipeline(
                    row=row,
                    issues=issues,
                    reason=reason,
                    visual_note=visual_note,
                    test_mode=test_mode,
                    fast_mode=fast_mode,
                    reviewer_name=reviewer_name,
                )

                if success:
                    # clear any old clarification
                    st.session_state[clar_key] = ""
                    st.success(message)
                    if new_url:
                        try:
                            st.image(
                                drive_thumbnail_url(new_url, width=600),
                                caption="الصورة الجديدة",
                                width=420,
                            )
                        except Exception:
                            st.markdown(f"[فتح الصورة الجديدة]({new_url})")
                    # delay-free rerun: let the user see the image first
                    if st.button("🔄 تحديث الصفحة لعرض التغييرات",
                                 key=f"refresh_{uid_val}"):
                        st.rerun()
                else:
                    # message is the clarification question
                    st.session_state[clar_key] = message
                    st.warning(f"❓ {message}")

            st.divider()

            # ── 8) Fallback: save rejection only (no generation) ────────────
            with st.expander("حفظ طلب الرفض فقط بدون توليد الآن"):
                st.caption(
                    "لو ودك تحفظي الملاحظات دون أن تستهلكي توليداً الآن، "
                    "استخدمي هذا الخيار. الصف يتحوّل لحالة «بانتظار إعادة التوليد»."
                )
                if st.button(
                    "💾 حفظ الرفض فقط",
                    use_container_width=True,
                    disabled=not has_input,
                    help=("اختاري مشكلة أو اكتبي ملاحظة." if not has_input else None),
                    key=f"btn_save_only_{uid_val}",
                ):
                    combined_parts = list(issues)
                    if reason.strip():
                        combined_parts.append(reason.strip())
                    combined = " | ".join(combined_parts) or "(لم يُذكر)"
                    try:
                        update_review_in_sheet(
                            image_uid=str(row.get(COL_UID, "")),
                            image_filename=str(row.get(COL_FILENAME, "")),
                            sheet_row_number=str(row.get(COL_ROW_NUMBER, "")),
                            updates={
                                "review_status":               ST_REJECTED,
                                "review_decision":             "rejected",
                                "rejection_reason":            combined,
                                "reviewer_visual_note":        visual_note.strip(),
                                "needs_regeneration":          "yes",
                                "regeneration_request_status": "pending",
                                "reviewer_name":               reviewer_name,
                                "reviewed_at":                 now_riyadh_iso(),
                                "approved_image_url":          "",
                            },
                        )
                        st.success("✅ تم حفظ طلب إعادة التوليد.")
                        st.rerun()
                    except Exception as e:
                        st.error(f"فشل الحفظ: {type(e).__name__}: {e}")


def render_table(df: pd.DataFrame):
    """Main list view — table of all reviewable rows."""
    if df.empty:
        st.info("لا توجد صفوف مطابقة للفلتر الحالي.")
        return

    # Table header
    cols = st.columns([1, 2, 3.5, 1.5, 1.3, 1.3])
    headers_ar = ["الصورة", "الكلمة", "المعنى", "نوع الصورة", "الحالة", ""]
    for col, h in zip(cols, headers_ar):
        col.markdown(
            f'<div style="color:#7A7A7A;font-size:13px;padding:6px 0;'
            f'border-bottom:1px solid #E6E2DA;">{h}</div>',
            unsafe_allow_html=True,
        )

    for i in range(len(df)):
        row = df.iloc[i]
        cols = st.columns([1, 2, 3.5, 1.5, 1.3, 1.3])

        # image thumb
        with cols[0]:
            url = str(row.get(COL_IMAGE_URL, "")).strip()
            if url:
                try:
                    st.image(drive_thumbnail_url(url, width=120), width=60)
                except Exception:
                    st.markdown("🖼️")
            else:
                st.markdown(
                    '<div style="font-size:22px;color:#C9C4B8;">🖼️</div>',
                    unsafe_allow_html=True,
                )

        # word + row number
        with cols[1]:
            st.markdown(
                f'<div style="font-weight:600;font-size:16px;color:#2A2A2A;">'
                f'{display_word(row)}</div>'
                f'<div style="font-family:monospace;font-size:11px;color:#9C9C9C;">'
                f'#{row.get(COL_ROW_NUMBER, "")} · {row.get(COL_FILENAME, "")[:35]}'
                f'</div>',
                unsafe_allow_html=True,
            )

        # meaning
        with cols[2]:
            txt = str(row.get(COL_DEFINITION, "")).strip()
            if len(txt) > 130:
                txt = txt[:130] + "..."
            st.markdown(
                f'<div style="font-size:14px;color:#3A3A3A;">{txt or "—"}</div>',
                unsafe_allow_html=True,
            )

        # category
        with cols[3]:
            st.markdown(
                f'<span class="category-pill">{IMAGE_CATEGORY_LABEL}</span>',
                unsafe_allow_html=True,
            )

        # status pill
        with cols[4]:
            st.markdown(
                status_pill_html(row["_status"]),
                unsafe_allow_html=True,
            )

        # open button
        with cols[5]:
            uid_val = str(row.get(COL_UID, "") or row.get(COL_FILENAME, "") or i)
            if st.button("فتح للمراجعة", key=f"open_{uid_val}_{i}",
                         use_container_width=True):
                st.session_state["selected_uid"] = uid_val
                st.rerun()

        st.markdown(
            '<div style="height:1px;background:#F1EEE8;margin:6px 0;"></div>',
            unsafe_allow_html=True,
        )


# ── Route: detail or list ───────────────────────────────────────────────────
selected_uid = st.session_state.get("selected_uid")
if selected_uid:
    # find the row in the FULL view (not filtered) so the user keeps access
    df_search = df_view
    mask = (
        (df_search.get(COL_UID, pd.Series([""]*len(df_search))).astype(str) == selected_uid) |
        (df_search.get(COL_FILENAME, pd.Series([""]*len(df_search))).astype(str) == selected_uid)
    )
    matched = df_search[mask]
    if matched.empty:
        st.warning("لم يُعثَر على الصف. ربما حُذف من الشيت.")
        if st.button("رجوع للجدول"):
            st.session_state.pop("selected_uid", None)
            st.rerun()
    else:
        render_detail(matched.iloc[0])
else:
    render_table(filtered)
