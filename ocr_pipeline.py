"""
Core processing pipeline: takes a scanned PDF or image, OCRs every page,
classifies each page, and extracts STC / Form 337 records from it.

This mirrors the approach validated against a real FAA records file:
- Tesseract OCR with orientation auto-correction
- Keyword-based page classification
- Fuzzy regex extraction tuned to real-world OCR errors seen on FAA
  certificate templates (decorative fonts, 0/O confusion, dropped letters)

Accuracy is good but not perfect (see README) - low-confidence pages are
flagged for manual review rather than silently guessed.
"""
import os
import re
import sys
import subprocess
import tempfile
import glob
import pytesseract
from PIL import Image
from dateutil import parser as _dateparser


def _resource_base():
    """Base directory for bundled resources: the PyInstaller extraction dir
    when running as a frozen executable, or this file's directory otherwise."""
    if getattr(sys, 'frozen', False):
        return getattr(sys, '_MEIPASS', os.path.dirname(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def _bundled_binary(name):
    """Path to a bundled OCR/PDF binary if this is a frozen build that
    includes one, else None (caller falls back to whatever's on the
    system PATH, i.e. a normal pip-installed dev setup)."""
    base = _resource_base()
    for candidate in (os.path.join(base, 'bin', name),
                      os.path.join(base, 'bin', name + '.exe')):
        if os.path.exists(candidate):
            return candidate
    return None


_TESSERACT_BIN = _bundled_binary('tesseract')
if _TESSERACT_BIN:
    pytesseract.pytesseract.tesseract_cmd = _TESSERACT_BIN
    _bundled_tessdata = os.path.join(_resource_base(), 'tessdata')
    if os.path.isdir(_bundled_tessdata):
        os.environ['TESSDATA_PREFIX'] = _bundled_tessdata

_PDFTOPPM_BIN = _bundled_binary('pdftoppm') or 'pdftoppm'

STC_FUZZY = re.compile(r'[S\$][T1]?\s?[0O]?\d{3,5}[A-Z]{2,3}(?:-D)?')
REG_MARK = re.compile(r'\bN\d{2,5}[A-Z]{0,3}\b')
DATE_PATTERNS = [
    re.compile(r'\b\d{1,2}\s+[A-Z]{3,9}\.?\s+20\d{2}\b', re.IGNORECASE),
    re.compile(r'\b\d{1,2}/\d{1,2}/20\d{2}\b'),
    re.compile(r'\b[A-Z]{3,9}\.?\s+\d{1,2},?\s+20\d{2}\b', re.IGNORECASE),
    re.compile(r'\b\d{1,2}/[A-Z]{3,9}\.?/20\d{2}\b', re.IGNORECASE),  # e.g. "22/Feb/2022"
]

HEADER_TYPES = {
    'FAA_337', 'STC_certificate', 'airworthiness_certificate',
    'export_certificate', 'form_8130-6', 'trust_correspondence'
}


def _pdf_to_image_paths(filepath, dpi=300):
    """Render every page of a PDF (or a single image file) to on-disk PNGs,
    returning file paths rather than opened Image objects. Pages are opened
    and released one at a time by the caller instead of all being decoded
    into memory simultaneously - for a multi-page file at 300 DPI, holding
    every page open at once easily reaches multiple GB of RAM, which is more
    than small hosting instances have available."""
    ext = os.path.splitext(filepath)[1].lower()
    if ext in ('.png', '.jpg', '.jpeg', '.tif', '.tiff', '.bmp'):
        return [filepath], None

    tmpdir = tempfile.mkdtemp()
    prefix = os.path.join(tmpdir, 'pg')
    subprocess.run([_PDFTOPPM_BIN, '-png', '-r', str(dpi), filepath, prefix], check=True)
    files = sorted(glob.glob(prefix + '*.png'))
    return files, tmpdir


def _ocr_page(img):
    """OCR a single page image, auto-correcting rotation when possible."""
    rotated = img
    try:
        osd = pytesseract.image_to_osd(img)
        angle = int(re.search(r'Rotate: (\d+)', osd).group(1))
        if angle != 0:
            rotated = img.rotate(-angle, expand=True)
    except Exception:
        pass
    return pytesseract.image_to_string(rotated)


def _classify(text):
    clean_len = len(text.strip())
    if clean_len < 25:
        return 'blank_or_illegible'
    # Only look at the page HEADER (first ~250 chars). Matching keywords anywhere
    # on the page misfires on e.g. a 337's own body text listing "Installed the
    # following FAA Approved Supplemental Type Certificates" in a table -
    # that's a 337, not an STC certificate, even though the words appear on it.
    header = text[:250].upper()
    if 'MAJOR REPAIR AND ALTERATION' in header:
        return 'FAA_337'
    supplemental_variants = ('SUPPLEMENTAL', 'SUPPLENENTAL', 'SUPPLEMENIAL')
    if any(v in header for v in supplemental_variants) and \
       ('CERTIFICATE' in header or 'CERTIFICAT' in header or 'CERTIFECATE' in header) and \
       ('TYPE' in header or 'CUPE' in header or 'TYPO' in header or 'COPE' in header):
        return 'STC_certificate'
    if 'STANDARD AIRWORTHINESS CERTIFICATE' in header:
        return 'airworthiness_certificate'
    if 'EXPORT CERTIFICATE' in header:
        return 'export_certificate'
    if 'APPLICATION FOR' in header and 'AIRWORTHINESS' in header:
        return 'form_8130-6'
    if 'STATEMENT OF COMPLIANCE' in header:
        return 'form_8110-3'
    if 'BANK OF UTAH' in header or 'TRUST' in header:
        return 'trust_correspondence'
    return 'other_correspondence'


def _normalize_stc(raw):
    s = raw.upper().replace(' ', '')
    s = re.sub(r'^[S\$][T1]', 'ST', s)
    body = s[2:]
    # Digits sometimes OCR as 'O'; letters that follow are 2-letter location codes
    # (AT, LA, NY, SE, CE...) which don't contain O in practice, so this is safe.
    body = re.sub(r'O', '0', body)
    # Split into leading digit run + trailing suffix (letters, optional -D)
    m = re.match(r'^(\d+)([A-Z]{2,3}(?:-D)?)$', body)
    if m:
        digits, suffix = m.groups()
        # This dataset's STC numbers are consistently 5 digits (zero-padded).
        # A 4-digit capture usually means a leading '0' was dropped by OCR.
        if len(digits) == 4:
            digits = '0' + digits
        body = digits + suffix
    return 'ST' + body


def _extract_stc_number(text):
    # Restrict to the page HEADER only. The certificate's own number always
    # appears in the first couple of lines; searching the full page risks
    # matching a *cross-referenced* STC number mentioned later in the body
    # (e.g. "...required part of this STC: STxxxxx") instead of its own.
    header = text[:220]
    m = STC_FUZZY.search(header)
    if m:
        return _normalize_stc(m.group())
    # Fallback: OCR occasionally inserts a stray space inside the number
    # itself (e.g. "S042 75AT-D"), which breaks the regex above. Retry on a
    # whitespace-stripped copy of the header only (still not the whole page).
    compact = re.sub(r'\s+', '', header)
    m2 = STC_FUZZY.search(compact)
    if m2:
        return _normalize_stc(m2.group())
    return None


_COMPANY_SUFFIX = re.compile(
    # "Aviation" deliberately excluded - "Federal Aviation Administration" is
    # boilerplate on every single page's letterhead and would false-positive constantly.
    r'^[^\n]{0,60}\b(Corporation|Corp\.?|LLC|Inc\.?|Company|Co\.|Speed Merchants)\b[^\n]{0,20}$',
    re.IGNORECASE | re.MULTILINE,
)


def _extract_holder(text):
    header = text[:400]
    # Primary signal: "issued to/lo <name>" - but the word "issued" itself is one
    # of the most frequently OCR-mangled words on the decorative-font templates
    # ("tssued", "essued"), so this alone misses a lot of real cover pages.
    m = re.search(r'[il1t]ss?ued\s+(?:to|lo|te|fo)[:\s]+(.+)', header, re.IGNORECASE)
    if m:
        candidate = m.group(1).split('\n')[0].strip(' .,:')
        if 3 < len(candidate) < 80:
            return candidate
    # Fallback: company names almost always carry a legal-entity suffix
    # (Corporation, LLC, Inc, Co.) which OCRs far more reliably than "issued".
    m2 = _COMPANY_SUFFIX.search(header)
    if m2:
        candidate = m2.group(0).strip(' .,:')
        # Strip a leading "This certificate issued to" fragment if OCR partially caught it
        candidate = re.sub(r'^.*?(?:to|lo|te)\s+', '', candidate, flags=re.IGNORECASE)
        if 3 < len(candidate) < 80:
            return candidate
    return None


def _extract_reg(text):
    m = REG_MARK.search(text)
    return m.group() if m else None


def _extract_date(text):
    for pat in DATE_PATTERNS:
        m = pat.search(text)
        if m:
            return m.group()
    return None


_REQUIRED_STC = re.compile(
    r'required part of this STC:?\s*([S\$][T1]\s?[0O]?\d{3,5}[A-Z]{2,3}(?:-D)?)\s*,?\s*([^\n.]{0,150})',
    re.IGNORECASE,
)


_DESC_MARKER = re.compile(
    r'design\s+change\s*:?\s*\n*(.+?)(?=limitations\s+and\s+conditions'
    r'|this\s+certificate\s+and\s+the\s+supporting\s+data'
    r'|by\s+direction\s+of\s+the\s+administrator|$)',
    re.IGNORECASE | re.DOTALL,
)


_LIMITATIONS_BOILERPLATE = re.compile(
    r'^\s*(is compatible with previously approved modifications'
    r'|1\.\s*the installer must determine'
    r'|if the holder agrees to permit)',
    re.IGNORECASE,
)


def _extract_stc_description(text):
    """Pull the actual 'what was installed' text, not the certificate boilerplate
    that's identical on every STC (issued-to address, 'meets the airworthiness
    requirements of Part 25...', signature block, etc.). Returns '' rather than
    guessing if the real description section can't be confidently located -
    a vague boilerplate/address fragment is worse than admitting we don't know."""
    m = _DESC_MARKER.search(text)
    if m:
        excerpt = re.sub(r'\s+', ' ', m.group(1)).strip()
        if len(excerpt) > 20 and not _LIMITATIONS_BOILERPLATE.match(excerpt):
            return excerpt[:600]
    return ''


def _extract_337_summary(block_text):
    m = re.search(r'(?:Description of Work Accomplished)(.+)', block_text, re.IGNORECASE | re.DOTALL)
    excerpt = m.group(1) if m else block_text
    excerpt = re.sub(r'\s+', ' ', excerpt).strip()
    return excerpt[:600]


def parse_date_str(date_str):
    """Best-effort parse of a loosely-formatted date string into a datetime.
    Returns None if it can't be parsed - callers should treat that event as
    unranked (not silently guess 'oldest' or 'newest')."""
    if not date_str:
        return None
    try:
        return _dateparser.parse(date_str, fuzzy=True, dayfirst=False)
    except Exception:
        return None


_NEW_REG_LETTER = re.compile(
    r'New Registration Number is:?\s*(N\d{2,5}[A-Z]{0,3})', re.IGNORECASE)
_CURRENT_REG_LETTER = re.compile(
    r'Current Registration Number:?\s*(N\d{2,5}[A-Z]{0,3})', re.IGNORECASE)
_REG_FIELD = re.compile(
    r'REGISTRATION\s+MARKS?[:\s]*\n?\s*(N\d{2,5}[A-Z]{0,3})', re.IGNORECASE)


def _extract_registration_events(ptype, text, page_num):
    """
    Find (registration mark, effective date) pairs on a page. Returns a list
    since a single re-registration letter carries two marks (old + new).

    Confidence matters here because these events decide what tail number the
    app displays automatically:
      - 'high'   explicit "New Registration Number is X" letters, dated
      - 'medium' a Standard Airworthiness Certificate's own reg-mark + date
      - 'low'    any other page where a reg mark and a date both appear,
                 with no clear indication they belong together
    """
    events = []

    m_new = _NEW_REG_LETTER.search(text)
    if m_new:
        date_str = _extract_date(text)
        events.append({'reg_mark': m_new.group(1).upper(), 'date_str': date_str,
                        'page': page_num, 'confidence': 'high', 'source': 'registration_change_letter'})

    if ptype == 'airworthiness_certificate':
        m_field = _REG_FIELD.search(text[:400])
        reg = m_field.group(1).upper() if m_field else _extract_reg(text[:400])
        date_str = _extract_date(text)
        if reg and date_str:
            events.append({'reg_mark': reg, 'date_str': date_str,
                            'page': page_num, 'confidence': 'medium', 'source': 'airworthiness_certificate'})

    return events


def process_file(filepath, progress_callback=None):
    """
    Process one scanned file end to end.
    Returns {'stcs': {stc_number: {...}}, 'form337s': [...], 'flagged': [...]}

    progress_callback(page_num, total_pages), if given, is called after every
    single page is OCR'd (not just once per file). This matters a lot when
    running behind a hosting platform's reverse proxy: OCR-ing a multi-page
    file with no progress reporting means the connection can go silent for
    minutes, and some proxies will conclude the connection is dead and kill
    it (surfacing as a 502 in the browser) well before processing finishes.
    Firing a UI update after every page keeps the connection visibly alive.
    """
    filename = os.path.basename(filepath)
    image_paths, tmpdir = _pdf_to_image_paths(filepath)
    pages = []
    total = len(image_paths)
    try:
        for idx, path in enumerate(image_paths, start=1):
            with Image.open(path) as img:
                text = _ocr_page(img)
            ptype = _classify(text)
            pages.append({'page': idx, 'type': ptype, 'text': text})
            if progress_callback:
                progress_callback(idx, total)
    finally:
        # Clean up the rendered page images now that OCR is done with them -
        # both to free disk space and because they're no longer needed.
        if tmpdir:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)

    registration_events = []
    for p in pages:
        registration_events.extend(_extract_registration_events(p['type'], p['text'], p['page']))

    stc_entries = {}
    flagged = []

    # --- Pass 1: STC certificates ---
    # A "primary" STC page is one where we can see "issued to <holder>" - that's the
    # cover page of a genuine new certificate. Continuation/limitation sheets repeat
    # the certificate's own number in their header but often ALSO mention *other*
    # STC numbers as cross-references (e.g. "The following STC is a required part
    # of this STC: STxxxxx"). Re-extracting a number from those pages with a plain
    # first-match regex risks grabbing the wrong (cross-referenced) number, so
    # continuation pages are appended to whichever STC was most recently opened by
    # a primary page, rather than treated as a fresh entry.
    current_stc = None
    for p in pages:
        if p['type'] != 'STC_certificate':
            continue
        holder = _extract_holder(p['text'])
        is_primary = holder is not None

        if is_primary:
            num = _extract_stc_number(p['text'])
            if not num:
                flagged.append({'page': p['page'], 'reason': 'STC certificate page detected but number unreadable'})
                current_stc = None
                continue
            entry = stc_entries.setdefault(num, {'holder': None, 'description': '', 'pages': []})
            entry['holder'] = holder
            entry['description'] = _extract_stc_description(p['text'])
            current_stc = num
        elif current_stc is not None:
            entry = stc_entries[current_stc]
            # Deliberately NOT trying to recover a missing description from a
            # continuation page: those pages contain "Limitations and Conditions"
            # prose that sometimes legitimately contains the words "design change"
            # in an unrelated sentence (e.g. "...operating limitations developed
            # to meet the provisions of design changes..."), which the marker
            # regex would wrongly grab as if it were the real description. Only
            # the cover page reliably has the actual "Description of Type Design
            # Change:" section.
        else:
            # Continuation-looking page with no primary page seen yet in this file
            num = _extract_stc_number(p['text'])
            if not num:
                flagged.append({'page': p['page'], 'reason': 'STC continuation page found before its cover page'})
                continue
            entry = stc_entries.setdefault(num, {'holder': None, 'description': '', 'pages': []})
            current_stc = num

        entry['pages'].append(p['page'])

    # Any entry where we never found a real description (only boilerplate or
    # Limitations text) gets an explicit placeholder rather than showing
    # nothing or showing the wrong section, and is flagged for manual review.
    for num, entry in stc_entries.items():
        if len(entry['description'].strip()) < 20:
            entry['description'] = ('(Description could not be reliably read from this scan '
                                     '— verify against the source page.)')
            flagged.append({'page': entry['pages'][0] if entry['pages'] else 0,
                             'reason': f'STC {num}: description text too degraded to extract automatically'})

    # --- Pass 1b: prerequisite / cross-referenced STCs ---
    # A certificate like a SATCOM or HUD install will often say "The following
    # STC is a required part of this STC: STxxxxx, Installation of an Executive
    # Cabin Interior" - that's flagging a prerequisite completion STC. If that
    # prerequisite doesn't have its own cover page anywhere in this file (not
    # every CD includes every referenced cert), it would otherwise never make
    # the list at all. Catch those here and list them separately, flagged for
    # review since we only know about them second-hand.
    for p in pages:
        if p['type'] not in ('STC_certificate', 'FAA_337'):
            continue
        for m in _REQUIRED_STC.finditer(p['text']):
            raw_num, desc = m.groups()
            num = _normalize_stc(raw_num)
            if num in stc_entries:
                continue
            stc_entries[num] = {
                'holder': None,
                'description': (
                    '(Found only as a prerequisite reference inside another STC '
                    'certificate — likely a completion STC — no dedicated '
                    f'certificate page for it was found in this file.) {desc.strip()}'
                ),
                'pages': [p['page']],
            }
            flagged.append({
                'page': p['page'],
                'reason': f'STC {num} referenced as a required/prerequisite STC but has no cover page in this file — holder unknown, verify against source',
            })

    # --- Pass 2: Form 337s (header page + continuation pages until next header) ---
    form337s = []
    i = 0
    while i < len(pages):
        p = pages[i]
        if p['type'] == 'FAA_337':
            block_text = p['text']
            block_pages = [p['page']]
            j = i + 1
            while j < len(pages) and pages[j]['type'] not in HEADER_TYPES:
                block_text += '\n' + pages[j]['text']
                block_pages.append(pages[j]['page'])
                j += 1
            reg = _extract_reg(p['text'])
            date = _extract_date(block_text)
            summary = _extract_337_summary(block_text)
            stc_refs = sorted(set(_normalize_stc(m) for m in STC_FUZZY.findall(block_text)))
            if not reg:
                flagged.append({'page': p['page'], 'reason': '337 detected but registration mark unreadable'})
            form337s.append({
                'reg_mark': reg or '(unreadable)',
                'work_date': date or '(unreadable)',
                'summary': summary,
                'stc_refs': stc_refs,
                'pages': block_pages,
            })
            i = j
        else:
            i += 1

    return {
        'filename': filename,
        'stcs': stc_entries,
        'form337s': form337s,
        'flagged': flagged,
        'registration_events': registration_events,
        'total_pages': len(pages),
    }
