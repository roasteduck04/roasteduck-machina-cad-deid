"""Identities that are replaced inside PDFs only (text layer, OCR, metadata), never in file names or CAD
content: part / drawing / document numbers, third-party vendor names, postal addresses and phone numbers.

They are harvested from the PDF text scan (pdf.jsonl) and the input manifest.  Renaming files or CAD-internal
references by these values would break drawing/model links, so they stay PDF-only.
"""
import base64
import collections
import json
import os
import re
import zlib

TOKEN_RE = re.compile(r'(?<![A-Za-z0-9])[A-Za-z0-9]+(?:[-_.][A-Za-z0-9]+)*(?![A-Za-z0-9])')
STD_RE = re.compile(r'(?i)^(?:m\d|iso|din|en\d|en[-_]?iso|astm|ansi|asme|aisi|sae|jis|gb\d|iec|ul\d|nema|ip\d\d|awg|unc|unf|npt|'
                    r'bsp|rs\d{3}|r\d+$|no\d|nr\d|x\d|v\d|ver\d|rev\d|page\d|fig\d|tab\d|step\d|pt\d|ft\d|in\d|mm\d|'
                    r'\d+(?:\.\d+)?(?:mm|cm|in|ft|kg|g|v|w|a|ma|mah|hz|khz|mhz|nm|rpm|bar|psi|n|kn|deg)$)')
DEFAULT_NAME_RE = re.compile(r'(?i)^(?:part|sketch|body|assem|assembly|drawing|draw|pad|pocket|layer|view|sheet|plane|axis|face|'
                             r'edge|copy|untitled|new|test|page|fig|figure|image|img|dsc|scan|screenshot|model|component|'
                             r'feature|boss|extrude|cut|fillet|chamfer|hole|pattern|mirror|shell|rib|loft|sweep|item|detail|'
                             r'section|table|block|line|point|unnamed|document|file|frame|version)[-_ ]?\d')
DATE_RE = re.compile(r'^(?:19|20)\d\d[-_.]?[01]\d[-_.]?[0-3]\d$|^[0-3]?\d[-_.][01]?\d[-_.](?:19|20)?\d\d$')
DOCNUM_RE = re.compile(r'(?<![A-Za-z0-9])(?:[A-Z]{2,5}-)?([DETGFLMQ]\d{6,7}(?:-[vVxX]?\d{1,2})?)(?![A-Za-z0-9])')
LABEL_RE = re.compile(r'(?i)(?:dwg\.?\s*no\.?|drawing\s*(?:no\.?|nr\.?|number|#)|part\s*(?:no\.?|nr\.?|number|#)|p/n|'
                      r'doc(?:ument)?\.?\s*(?:no\.?|nr\.?|number|#)|zeichnungs?\s*-?\s*nr\.?|teile?\s*-?\s*nr\.?|'
                      r'sach\s*-?\s*nr\.?|art(?:ikel)?\.?\s*-?\s*nr\.?|n[°ºo]\s*de\s*plano|plano\s*n[°ºo]|'
                      r'r[ée]f(?:[ée]rence)?\.?)\s*[:#.]?\s*\n?\s*([A-Za-z0-9][A-Za-z0-9\-_./]{3,40})')


def code_like(t, allow_digits=False):
    al = re.sub(r'[^A-Za-z0-9]', '', t)
    if len(al) < 5 or len(t) > 40:
        return False
    nd = sum(c.isdigit() for c in al)
    if nd == 0:
        return False
    if nd == len(al):
        return allow_digits and not DATE_RE.match(t) and len(al) <= 12
    if nd / len(al) < 0.3:
        return False
    for run in re.findall(r'[A-Za-z]+', t):
        if len(run) > 4 and not (run.isupper() and len(run) <= 8):
            return False
    low = t.lower()
    if STD_RE.match(low) or DEFAULT_NAME_RE.match(t) or DATE_RE.match(t):
        return False
    if re.fullmatch(r'[\dx.,\-]+', low):
        return False
    if re.fullmatch(r'v?\d+(?:\.\d+)+[a-z]?', low):
        return False
    if re.fullmatch(r'[0-9a-f]{8,}', low) and re.search(r'[a-f]', low):
        return False      # hashes and catalogue numbers such as 12345A678 (a17: not identities, replacing them hurt)
    if re.fullmatch(r'[0-9a-f]{8}-[0-9a-f]{4}-.*', low):
        return False
    return True


NON_CAD_EXT = {'pdf', 'png', 'jpg', 'jpeg', 'tif', 'tiff', 'gif', 'bmp', 'zip', 'gz', 'z', 'tar', 'tgz', '7z', 'rar',
               'txt', 'md', 'doc', 'docx', 'xls', 'xlsx', 'csv', 'html', 'htm', 'resume-partial', ''}


def _ext(rel):
    parts = os.path.basename(rel).lower().split('.')
    e = parts[-1] if len(parts) > 1 else ''
    if e.isdigit() and len(parts) > 2:      # Creo versions: part.prt.3
        e = parts[-2]
    return e


def _family(code):
    """Prefix of a part number: its leading letters ('abc' for ABC-V1 1000.04-A, 'wxyz' for WXYZ-001) or
    its leading number of >= 4 digits ('123456' for 123456-94-BRACKET); None otherwise."""
    m = re.match(r'([A-Za-z]{2,}|\d{4,})', code)
    return m.group(1).lower() if m else None


def code_candidates(s, allow_digits=False):
    out = set()
    for m in TOKEN_RE.finditer(s):
        t = m.group(0).strip('.-_')
        if code_like(t, allow_digits):
            out.add(t)
        segs = re.split(r'[-_.]', t)
        if len(segs) > 1:
            for sg in segs:
                if code_like(sg, allow_digits):
                    out.add(sg)
    return out


ADDRESS_RES = [
    re.compile(r'(?<![\w])\d{1,6}\s+(?:[NSEW]\.?\s+)?[A-Z][A-Za-z]+(?:\s+[A-Z][A-Za-z]+){0,3}\s+'
               r'(?i:street|st\.|road|rd\.?|avenue|ave\.?|boulevard|blvd\.?|drive|dr\.|lane|ln\.|way|court|ct\.|place|'
               r'parkway|pkwy\.?|highway|hwy\.?)(?![\w])'),
    re.compile(r'(?<![\w])[A-Z][A-Za-z]+(?:\s+[A-Z][A-Za-z]+){0,2},\s*(?:[A-Z]{2}|[A-Z][a-z]{3,})\s+\d{5}(?:-\d{4})?(?![\w])'),
    re.compile(r'(?<![\w])[A-ZÄÖÜ][a-zäöüß]+(?:straße|strasse|str\.|weg|ring|platz|allee|gasse|damm|stieg)\s+\d{1,4}[a-z]?'
               r'(?:\s*[-–]\s*\d{1,4})?(?![\w])'),
    re.compile(r'(?<![\w])(?:[A-Z]{1,2}-)?\d{4,5}\s+[A-ZÄÖÜ][a-zäöüß]{2,}(?:[- ][A-ZÄÖÜ][a-zäöüß]+)?,\s*(?:Germany|Deutschland|'
               r'Austria|Switzerland|Schweiz|France|Spain|España|Italy|Italia|Belgium|Netherlands|Denmark|Sweden|Norway)'),
    re.compile(r'(?i)(?<![\w])(?:p\.?\s?o\.?\s?box|post\s+office\s+box|postfach)\s+\d{2,8}(?![\w])'),
]
PHONE_LABEL_RE = re.compile(r'(?i)(?<![a-z])(?:tel|tél|phone|fax|telefon|telefax|téléphone|teléfono|ph|mobile|cell|t|f)\s*[.:]\s*'
                            r'(\+?\(?\d[\d\s().\-/]{6,22}\d)')
PHONE_INTL_RE = re.compile(r'(?<![\w+])\+\d{1,3}[\s.\-]?\(?\d{1,4}\)?(?:[\s.\-]?\d{2,5}){2,4}(?![\w])')
COMPANY_RE = re.compile(r'(?<![\w&])((?:[A-Z][A-Za-z0-9&\'\-]+\s+){0,3}[A-Z][A-Za-z0-9&\'\-]+)\s*,?\s+'
                        r'(Inc\.?|Incorporated|GmbH(?:\s*&\s*Co\.?\s*KG)?|Ltd\.?|LLC|L\.L\.C\.|Corp\.?|Corporation|'
                        r'Co\.,?\s*Ltd\.?|AG|S\.A\.|S\.L\.|B\.V\.|S\.r\.l\.|SRL|S\.p\.A\.|Pty\.?\s*Ltd\.?|Limited|KG|A/S|'
                        r'SAS|SARL|Oy|AB)(?![A-Za-z])')
SOFTWARE_VENDORS = set("""
microsoft adobe apple google autodesk dassault solidworks ptc siemens bentley monotype linotype bitstream urw
ascender agfa adobe systems hewlett-packard hp ibm oracle intel amd nvidia mozilla wolfram mathworks ansys
altium cadence mentor kicad freecad onshape trimble esri corel xerox canon epson ricoh brother sony
""".split())


INIT_STOP = set('QTY REV DWG MAT NTS REF PCS SEE DCC TBD ISO DIN CAD PDF STD GEN ALL PER THE AND DATE NAME SIGN '
                'SCALE SHEET SIZE TITLE PART ITEM NOTE ENG CHK APP APPD DRN DES MFG QA QC NA NO OK YES TOL FIN '
                'DIM SEC VIEW DET BOM ASSY ASM REQ SPEC STK WT KG MM IN FT PSI USA EU UK CNC'.split())


def _digits(s):
    return sum(c.isdigit() for c in s)


def harvest(pdfscan, manifest, rare_word):
    """-> dict identity -> (kind, source) of PDF-only identities."""
    out = {}
    if not pdfscan or not os.path.exists(pdfscan):
        return out
    texts = []
    tokcoll = collections.defaultdict(set)
    for line in open(pdfscan, encoding='utf-8'):
        try:
            r = json.loads(line)
        except Exception:
            continue
        p = r.get('p', '')
        sp = p.split('/')
        coll = '/'.join(sp[:5]) if len(sp) > 2 and sp[1] == 'existing' else '/'.join(sp[:3])
        t = r.get('t') or ''
        md = r.get('md') or {}
        if isinstance(md, str):
            mt = re.search(r"'title':\s*'([^']*)'", md)
            title = mt.group(1) if mt else ''
        else:
            title = md.get('title') or ''
        t2 = t + '\n' + title
        texts.append((p, coll, t2))
        for m in TOKEN_RE.finditer(t2):
            tok = m.group(0).strip('.-_').lower()
            tokcoll[tok].add(coll)
            for sg in re.split(r'[-_.]', tok):
                if len(sg) >= 5:
                    tokcoll[sg].add(coll)
    # part / document numbers
    cands = {}
    stemfam = collections.defaultdict(set)     # collection -> letter prefixes of part numbers naming its files
    # DEID_PDF_STEMS=cad: part numbers come only from names of CAD / drawing files; a code that only names
    # documents or images (vendor datasheets such as XY74ABC123.pdf, manuals) is a product number, not an identity
    # of the source (it can still come back as a document number, a labelled drawing number or a family code).
    stems_mode = os.environ.get('DEID_PDF_STEMS', 'all')
    if manifest and os.path.exists(manifest):
        for line in open(manifest, encoding='utf-8'):
            if line.startswith('#'):
                continue
            rel = line.split('\t')[0]
            if stems_mode == 'cad' and _ext(rel) in NON_CAD_EXT:
                continue
            stem = os.path.basename(rel).rsplit('.', 1)[0]
            sp = rel.split('/')
            coll = '/'.join(sp[:5]) if len(sp) > 2 and sp[1] == 'existing' else '/'.join(sp[:3])
            for c in code_candidates(stem, allow_digits=True):
                cands.setdefault(c, 'partno:stem')
                f = _family(c)
                if f:
                    stemfam[coll].add(f)
    for p, coll, t in texts:
        for m in DOCNUM_RE.finditer(t):
            cands.setdefault(m.group(1), 'partno:docnum')
            base = m.group(1).split('-')[0]
            cands.setdefault(base, 'partno:docnum')
        for m in LABEL_RE.finditer(t):
            for c in code_candidates(m.group(1), allow_digits=True):
                cands.setdefault(c, 'partno:label')
        # Codes found anywhere in page text are mostly not identities (schematic pad/net labels, component
        # values such as 0.47uF / 500ms, IC and catalogue numbers, standards); replacing them damages BOMs and
        # engineering text and the context around real identities.  Default 'family': keep only codes that
        # share a letter prefix with a part number naming a file of the same collection (the project's own
        # numbering, e.g. ABC-V1 1420.06 next to ABC-V1 1000.04-A.pdf).  '1' = every code, '0' = none.
        mode = os.environ.get('DEID_PDF_CODES_TEXT', 'family')
        if mode in ('1', 'family'):
            fams = stemfam.get(coll, ())
            for c in code_candidates(t):
                if mode == '1' or _family(c) in fams:
                    cands.setdefault(c, 'partno:text')
    for c, why in cands.items():
        colls = tokcoll.get(c.lower())
        if not colls:
            continue          # never visible in any PDF: nothing to gain
        lim = 6 if why != 'partno:text' else 2
        if len(colls) > lim:
            continue          # widely shared code: a standard / catalogue item, not an identity of one source
        if why == 'partno:text' and _digits(c) < 3:
            continue
        out[c] = ('partno', why)
    # addresses, phones, companies
    for p, coll, t in texts:
        for rx in ADDRESS_RES:
            for m in rx.finditer(t):
                a = re.sub(r'\s+', ' ', m.group(0)).strip(' ,')
                if 6 <= len(a) <= 80:
                    out.setdefault(a, ('address', 'pdftext'))
        for m in PHONE_LABEL_RE.finditer(t):
            ph = m.group(1).strip()
            if 7 <= _digits(ph) <= 15 and (ph.startswith('+') or re.search(r'[\s\-()/.]', ph)):
                out.setdefault(ph, ('phone', 'pdftext'))
        for m in PHONE_INTL_RE.finditer(t):
            ph = m.group(0).strip()
            if 8 <= _digits(ph) <= 15:
                out.setdefault(ph, ('phone', 'pdftext'))
        for m in COMPANY_RE.finditer(t):
            name, suf = m.group(1).strip(), m.group(2)
            first = name.split()[0].lower()
            if first in SOFTWARE_VENDORS or name.lower() in SOFTWARE_VENDORS:
                continue
            if len(name) < 3 or name.lower() in ('the', 'and', 'of', 'all', 'by', 'with'):
                continue
            full = re.sub(r'\s+', ' ', m.group(0)).strip()
            out.setdefault(full, ('vendor', 'pdftext:company'))
            words = name.split()
            if (len(words) >= 2 and any(rare_word(w, 5.0) for w in words)) or                     (len(words) == 1 and len(name) >= 4 and rare_word(name, 3.2)):   # not 'Revision', 'use is'
                out.setdefault(name, ('vendor', 'pdftext:company'))
    if os.environ.get('DEID_PDF_INITIALS', '1') == '1':
        for v in harvest_initials(texts, rare_word):
            out.setdefault(v, ('person', 'pdftext:initials'))
    return out


# drafter initials in title blocks and revision tables ('DRAWN | ABC', '012345 | DEF | ABC | 1/2/18')
_INI_LABEL = re.compile(r'(?i)^\s*(?:drawn(?: by)?|drn|dr\.?|designed(?: by)?|designer|dsgn|checked(?: by)?|checker|chkd?|ck\.?|'
                        r'approved(?: by)?|approval|appd|appr|app\.?|engr?\.?|engineer|mfg|q\.?a\.?|by|author|created by)\s*[:.]?\s*$')
_INI = re.compile(r'^([A-Z]{3})(?:\s*/\s*([A-Z]{3}))?$')
_INI_DATE = re.compile(r'^\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4}$|^\d{1,2}\s+[A-Z]{3}\s+\d{2,4}$')
_INI_STOP = set('''REV QTY TBD ECO ECN DWG SEE ALL AND THE NTS PCS STD REF DIA MAX MIN TYP NOM SHT ISO DIN ANS SAE ASM PRT
DRW PDF CAD UNS USA GBR DEU ITA ESP FRA NUM DATE SIZE CAGE SST ALU FAB WLD HEX SOC FLT BHC SHC TAP DRL CBR CSK THK RAD
DEG MIL INS PNL BRK MTG ENG MFG APP CHK DRN DSN REQ LTR OFF AIR LED PCB USB GND VCC SMD THT DIP STL DXF STP IGS DWF CMD
RCO ENP ENT CLK SDA SCL PWM LPT HPT DCC BRS BRZ SYN CTC SYM RST INT EXT NEW OLD NOT YES ADD QTY SET TOP END LCD RGB RAM
ROM CPU MCU ADC DAC SPI TXD RXD CAN NTC PTC NPN PNP FET VIN OUT OUTPUT INPUT EOL NIL NON VAR ABS PLA TPU PET PVC POM
FTC GPL CPO PCM CPI VCP VSS VDD MIT BSD APL
'''.split())


def harvest_initials(texts, rare_word):
    cnt = collections.Counter()
    colls = collections.defaultdict(set)
    for p, coll, t in texts:
        lines = [x.strip() for x in t.splitlines()]
        for i, ln in enumerate(lines):
            m = _INI.match(ln)
            if not m:
                continue
            win = lines[max(0, i - 3):i + 4]
            if not (any(_INI_LABEL.match(w) for w in win) or any(_INI_DATE.match(w) for w in win)):
                continue
            for v in (g for g in m.groups() if g):
                if v in _INI_STOP or not rare_word(v.lower(), 3.5):
                    continue
                cnt[v] += 1
                colls[v].add('/'.join(p.split('/')[:3]))     # top-level source (drawing-search counts once)
    return [v for v in cnt if len(colls[v]) <= 2]


def load_blob(path):
    """Identity lists are shipped compressed so the code folder carries no plain-text corpus strings."""
    if not os.path.exists(path):
        return None
    return json.loads(zlib.decompress(base64.b64decode(open(path, 'rb').read())).decode('utf-8'))


def save_blob(obj, path):
    open(path, 'wb').write(base64.b64encode(zlib.compress(json.dumps(obj, ensure_ascii=False).encode('utf-8'), 9)))
