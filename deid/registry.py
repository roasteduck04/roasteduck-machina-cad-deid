"""Build the identity registry from (1) source folder names, (2) curated
organisations/people/projects and (3) the metadata harvest.

Output: registry.json = {"entries": {key: mode}, "digit_keys": [...]} where
key is a lower-case identity string and mode is strict|loose|word|path.
"""
import collections
import json
import os
import re
import sys

from . import curated

COMMON = set()
for p in ('/usr/share/dict/words', '/usr/share/dict/american-english'):
    if os.path.exists(p):
        for w in open(p, encoding='utf-8', errors='ignore'):
            w = w.strip()
            if w and w.islower():
                COMMON.add(w)
        break
COMMON |= set("""
part parts assembly model models step stl cad drawing drawings plate bracket mount base cover frame motor gear shaft
bolt nut screw washer spring bearing pulley sheet test sample samples final new old copy draft rev version
""".split())

GENERIC = set(x.strip().lower() for x in """
author|unknown|user|users|administrator|admin|owner|operator|name|none|noname|title|organization|organisation|company
a company|an author|windows user|pc user|utilisateur|usuario|benutzer|gebruiker|utente|default|temp|all users|public
guest|root|me|test|demo|-|.|..|unspecified|not specified|no author specified|no organization specified|unknown organisation
unknown authorisation|unknown authorization|confidential|yes|no|someone else|lab|it|r&d|cad|design|engineering|student
students|school|home|office|desktop|laptop|computer|pc|freecad|kicad|kicad stepup|ksu|ksu mcad|pcbnew|solidworks
solid edge|open cascade|occt|occ|spatial corp|step tools|step tools, inc.|st-developer|autodesk|autodesk inc|autodesk, inc.
autodesk inc.|microsoft|hp|zwcad software co.|zwsoft|zw3d|intergraph|sdrc|ptc|siemens|unigraphics solutions|iti
3d/eye, inc.|dassault systemes|dassault systèmes|dassault systèmes solidworks corporation|catia|onshape|rhino|ironcad
toolbox|express|windows 10 pro|microsoft office user|managed by terraform|description|part number|material|vendor|custom
apache-2.0|0.0.1|1|2|3|x|xx|xxx|n/a|na|tbd|?|anonymous|anon|nobody|system|sysadmin|local|localuser|workstation|space
users|documents|downloads|onedrive|dropbox|appdata|desktop|programs|program files|windows|temp|tmp|work|projects|project
cad / cam services|standard|library|template|templates|libraries|catalog|parts library|new|old|sdl_parasolid_customer_support
unigraphics|parasolid|bentley_systems|microstation_modeler|demo|china|p r c|title|cv|cad|weapon systems
lenovo|dell|asus|acer|msi|matplotlib|matplotlib.org|lgpl2+|freecad lgpl2+|assy|assembly|hp inc|owner-pc|user-pc|admin-pc|pc-user|mac|macbook|imac|ubuntu|pi|raspberrypi|vagrant|docker
huntsville, al|exton|sdl-cambridge-u.k.|unknown|i7cad|jj cad
""".replace('\n', '|').split('|') if x.strip())

IDENTITY_FIELD_RE = re.compile(
    r'author|saved by|enregistrement par|dissenyat|designed|designer|drawn|dessin|dibujado|checked|approved|engineer|company'
    r'|firma|entreprise|empresa|owner|created by|gespeichert|autor|ersteller|konstrukteur|bearbeiter|gezeichnet|gepr|zeichner'
    r'|responsable|proyectista|dise|revisado|aprobado|controlado|projeteur|verificado|manager|contact'
    r'|email|e-mail|phone|tel|address|adresse|разраб|провер|утв|контр|设计|审核|批准|制图|校对|作者|绘图', re.I)

EMAIL_RE = re.compile(r'[A-Za-z0-9._%+\-]{1,64}@[A-Za-z0-9.\-]{1,100}\.[A-Za-z]{2,12}')
GENERIC_DOMAINS = set("""
github.com gitlab.com google.com youtube.com w3.org www.w3.org schemas.openxmlformats.org purl.org ns.adobe.com
adobe.com microsoft.com schemas.microsoft.com solidworks.com www.solidworks.com autodesk.com www.autodesk.com
creativecommons.org freecad.org www.freecad.org wiki.freecad.org forum.freecad.org inkscape.org www.inkscape.org
sodipodi.sourceforge.net xmlns.com example.com example.org openxmlformats.org gmail.com googlemail.com outlook.com
hotmail.com yahoo.com qq.com 163.com icloud.com live.com protonmail.com gmx.de gmx.net web.de aol.com
schemas.slic3r.org schemas.bambulab.com bambulab.com prusa3d.com www.prusa3d.com ns.adobe.com iptc.org
www.iso.org iso.org opensource.org www.gnu.org gnu.org apache.org www.apache.org mozilla.org schema.org
schemas.android.com xml.org www.xml.org dublincore.org openoffice.org www.openoffice.org libreoffice.org
oasis-open.org docs.oasis-open.org kicad.org www.kicad.org kicad-pcb.org ezdxf.mozman.at
github.io raw.githubusercontent.com githubusercontent.com bitbucket.org sourceforge.net wikipedia.org en.wikipedia.org
""".split())
TLDS = set('com org net edu gov mil int io co uk de fr es it nl ch at be se no dk fi pl cz ru in au ca us eu info biz jp cn kr tw br mx ar ac pt gr hu ro sk si hr rs bg ua tr il za nz sg hk my th vn id ph ie lt lv ee is lu me tv cc ai dev app xyz tech online site cl co pe ve ec uy ir pk bd lk np eg ma ng ke edu.au'.split())
STOPWORDS = set('''firma konstrukteur gmbh owne etude co. kg s.a.s sas inc ltd llc corp ag kg sa bv srl dr.$ g 5 box unix isthe
supplier_firstname supplier_lastname definition_approvedby_firstname classofficer_firstname classofficer_lastname
sources github gitlab web catalog existing archives extracted corpus input www com org net edu http https
designer engineer drafter checker approver bureau etudes bureau.etudes utilisateur usuario benutzer'''.split())
KEEP_MAIL = {'gmail', 'googlemail', 'outlook', 'hotmail', 'yahoo', 'qq', '163', 'icloud', 'live', 'protonmail', 'gmx', 'web', 'aol'}


try:
    from wordfreq import zipf_frequency as _zf
except Exception:
    _zf = None


def rare_word(w, thr=3.6):
    if _zf is None:
        return True
    w = w.lower()
    return max(_zf(w, l) for l in ('en', 'de', 'fr', 'es', 'it', 'pt', 'nl')) < thr


def norm(s):
    s = s.replace('\x00', ' ').strip().strip('"\'').strip()
    s = re.sub(r'\s+', ' ', s)
    return s


def clean_val(v):
    # cut at first control / garbage char
    m = re.match(r'[^\x00-\x08\x0b-\x1f￿�]*', v)
    v = m.group(0) if m else v
    v = v.replace('&lt;', '<').replace('&gt;', '>').replace('&amp;', '&').replace('&apos;', "'").replace('&quot;', '"')
    return norm(v)


# ordinary vocabulary found as whole author / account values ('Support', 'Quality', 'Administrador');
# such values are pseudonymized field by field but are never corpus-wide keys
GENERIC_SINGLE = set("""
administrador administrateur apple artisan astro atlanta bambu barefoot beep black blah brown charger codex comm
copyright dach delete editor education electro family fast first global golden grow honor indigo infinity intel
language market mars master meta missouri notice nutzer phoenix portable profs quality rope shack software solo
support testing think trainer tutorial técnica usuário wavelength white wordpress dans veder pasa beira thai amor
zorg rich mark jack roman mathe broeder hades interconnect caster datasheet design2
academy advanced amber apache applied april arquitetos assist automated automation beach bearings bosch boss bridges
bright brit carnegie caro cdmx centre cerchio chairman communication communications connectivity contract
contributors coop corporation crow darwin dear development dick digital dimensional division download duke
electronics engenharia england evaluation executive fictional fixture frais free fábrica germany godard grab havre
holder hollander hope incorporated industrial insight installation institute instruments international item knight
koop last leader leuven license long lora lounge ltda luna mace main management manifold manual matter mechanical
mega mettere milan miles montagne network plastic politecnico poll products profe provided rail regression
resolution ridder robotics rosa russia science secretariat semiconductor servi services site sorta stasi stirling
supply synthetic systems technical technologies technology tertiary texas university ventures villa visionary
vitamins winter wolf worldwide zurich aaa abc ach asd des erf fab mex moi rca agi ola raf jan asa
bill coolant optics allen antriebe vista arrow fastener disclaimers
""".split())


def _vendor_list():
    from .pdfids import load_blob
    return load_blob(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'vendors.dat')) or []


VENDOR_PDF_ONLY = set(v.lower() for v in _vendor_list())


class Builder:
    def __init__(self):
        self.entries = {}      # key -> mode
        self.digit_keys = set()
        self.source = collections.defaultdict(set)
        self.owners = set()
        self.url_skip = set()

    def add(self, s, mode='strict', why='', digits=False):
        s = norm(s)
        if not s:
            return
        k = s.lower()
        if k in GENERIC or k in STOPWORDS or k in GENERIC_DOMAINS or len(k) < 2:
            return
        if mode != 'pdf' and k in VENDOR_PDF_ONLY:
            mode = 'pdf'      # third-party vendors: replaced inside PDFs only
        if re.search(r'firstname|lastname|_name|organization_|person_|[$#*?{}]', k):
            return
        if not re.search(r'[^\W\d_]', k) and not digits:
            return  # pure digits/punct only when explicitly allowed
        if len(k) < 3 and mode != 'path':
            return
        if len(k) > 120:
            return
        prev = self.entries.get(k)
        rank = {'path': 0, 'meta': 0, 'pdf': 0, 'strict': 1, 'word': 2, 'loose': 3}
        if prev is None or rank[mode] > rank[prev]:
            self.entries[k] = mode
        if digits:
            self.digit_keys.add(k)
        self.source[k].add(why)

    def add_person_or_org(self, v, why):
        """Add a free-form identity value plus its distinctive parts."""
        v = clean_val(v)
        if not v or v.lower() in GENERIC:
            return
        # e-mails inside
        for e in EMAIL_RE.findall(v):
            self.add_email(e, why)
        v2 = EMAIL_RE.sub(' ', v)
        v2 = re.sub(r'[<>()\[\]"]', ' ', v2)
        v2 = norm(v2)
        # DOMAIN\user (MACHINE)
        m = re.match(r'^([^\\/ ]+)\\([^\\/()]+?)\s*(?:\(([^)]*)\))?$', v)
        if m:
            self.add_user(m.group(2), why)
            self.add(v, 'strict', why)
            self.add(m.group(1) + '\\' + m.group(2), 'word', why)
            for host in (m.group(1), m.group(3)):
                if host and len(host) >= 4 and host.lower() not in GENERIC and host.lower() not in COMMON \
                        and not re.fullmatch(r'(?i)(desktop|laptop|win|pc|user|admin)(-[a-z0-9]+)?', host):
                    self.add(host, 'strict', why + ':host')
            return
        parts = [p for p in re.split(r'\s*[;,/|]\s*|\s+-\s+|\s+and\s+|\s*&\s*', v2) if p]
        for part in parts:
            part = norm(part)
            if not part or part.lower() in GENERIC:
                continue
            words = part.split(' ')
            if len(words) == 1:
                self.add_token(part, why)
            else:
                self.add(part, 'strict', why)
                for w in words:
                    w2 = w.strip('.,:;')
                    if len(w2) >= 4 and w2.lower() not in COMMON and w2.lower() not in GENERIC and re.search(r'[^\W\d_]', w2):
                        self.add(w2, 'strict', why + ':w')
        if v2 != v and len(v2) > 3:
            self.add(v2, 'strict', why)

    def add_token(self, t, why):
        t = norm(t).strip('.,:;')
        lt = t.lower()
        if not t or lt in GENERIC:
            return
        if re.fullmatch(r'[\d\W_]+', t):
            if len(re.sub(r'\D', '', t)) >= 5:
                self.add(t, 'meta', why, digits=True)
            return
        if lt in COMMON or len(t) < 4:
            # too short / too common for global replacement: only inside metadata fields
            if len(t) >= 3 and lt not in COMMON and rare_word(re.sub(r'\d', '', lt) or lt, 3.3):
                self.add(t, 'meta', why)
            elif len(t) >= 2 and not t.isascii():
                self.add(t, 'meta', why)
            return
        else:
            self.add(t, 'strict', why)

    def add_user(self, u, why):
        u = clean_val(u)
        if not u or u.lower() in GENERIC:
            return
        if '~' in u:
            m83 = re.fullmatch(r'([A-Za-z0-9_\-]{2,6})~(\d)(\.\w{1,3})?', u)
            if m83 and m83.group(1).lower() not in GENERIC and m83.group(1).lower() not in ('admini', 'progra', 'docume', 'locals', 'temp', 'buildu'):
                self.add(m83.group(1) + '~' + m83.group(2), 'word', why + ':83')
            return
        if ' ' in u:
            self.add_person_or_org(u, why)
            return
        self.add_token(u, why)
        # path forms (also covers generic-looking user names)
        for pre in ('users\\', 'users/', 'home/', 'documents and settings\\'):
            if not re.fullmatch(r'[\d]+', u):
                self.add(pre + u, 'word', why + ':path')
            else:
                self.add(pre + u, 'word', why + ':path', digits=True)

    def add_email(self, e, why):
        e = e.strip('.').lower()
        tld = e.rsplit('.', 1)[-1]
        if tld not in TLDS:
            return
        self.add(e, 'word', why + ':email')
        local, dom = e.split('@', 1)
        if dom not in GENERIC_DOMAINS:
            self.add_domain(dom, why + ':email')

    def add_domain(self, d, why):
        d = d.lower().strip('.')
        if d.startswith('www.'):
            d = d[4:]
        if not d or d in GENERIC_DOMAINS or '.' not in d:
            return
        labels = d.split('.')
        # registrable part: last two labels (or three for co.uk style)
        if len(labels) >= 3 and labels[-2] in ('co', 'ac', 'com', 'org', 'edu', 'gov', 'net') and len(labels[-1]) == 2:
            reg = '.'.join(labels[-3:])
        else:
            reg = '.'.join(labels[-2:])
        if reg in GENERIC_DOMAINS:
            return
        self.add(d, 'word', why + ':dom')
        self.add(reg, 'word', why + ':dom')
        core = reg.split('.')[0]
        if why.split(':')[0] in ('webhost',) and len(core) >= 5 and core not in COMMON:
            self.add(core, 'strict', why + ':domcore')


def build(manifest, harvest, out):
    b = Builder()
    # 1. source folder identities
    owners = set()
    for line in open(manifest, encoding='utf-8'):
        if line.startswith('#'):
            continue
        p = line.split('\t')[0].split('/')
        if len(p) < 3:
            continue
        if p[1] == 'github':
            owners.add(p[2].split('__')[0])
        elif p[1] == 'gitlab':
            seg = p[2].split('__')
            if len(seg) >= 2:
                owners.add(seg[1])
        elif p[1] in ('web', 'catalog') and '.' in p[2] and ' ' not in p[2]:
            b.add_domain(p[2], 'webhost')
    for o in owners:
        lo = o.lower()
        if lo in GENERIC:
            continue
        b.owners.add(lo)
        if lo in COMMON or len(o) < 4 or lo in ('freecad', 'nasa', 'ecto', 'winder', 'evadne', 'ccct', 'person343'):
            continue
        elif len(o) >= 7 and lo not in COMMON:
            b.add(o, 'loose', 'owner')
        else:
            b.add(o, 'strict', 'owner')
    for s, mode in curated.ORGS_PEOPLE:
        b.add(s, mode, 'curated')
    # 2. harvest
    if harvest:
        spread = collections.defaultdict(set)
        for line in open(harvest, encoding='utf-8'):
            if '"url"' not in line:
                continue
            try:
                where, field, value = json.loads(line)
            except Exception:
                continue
            m = re.match(r'(?:https?|ftp)://([^/:]+)', value)
            if m:
                dom = m.group(1).lower()
                dom = dom[4:] if dom.startswith('www.') else dom
                spread['.'.join(dom.split('.')[-2:])].add('/'.join(where.split('/')[:3]))
        b.url_skip = {d for d, s in spread.items() if len(s) >= 3}
        print('url domains skipped (spread>=3):', len(b.url_skip))
        for line in open(harvest, encoding='utf-8'):
            try:
                where, field, value = json.loads(line)
            except Exception:
                continue
            if field == 'zipname' or field.startswith('step:PRODUCT') or field in ('swhdr', 'textline', 'err', 'fc:Label'):
                continue
            if field in ('winuser', 'winuser16', 'unixuser', 'lockfile', 'xt:USER'):
                b.add_user(value, field)
            elif field == 'email':
                b.add_email(value, field)
            elif field == 'url':
                m = re.match(r'(?:https?|ftp)://([^/:]+)', value)
                if m:
                    dom = m.group(1).lower()
                    if '.'.join(dom.split('.')[-2:]) not in b.url_skip:
                        b.add_domain(dom, 'url')
            elif field in ('step:fn_author', 'step:fn_org', 'step:fn_auth', 'iges:author', 'iges:org', 'xml:dc',
                           'xml:cp', 'xml:Manager', 'fc:CreatedBy', 'fc:LastModifiedBy', 'fc:Company', 'xt:SITE',
                           'oleprop:SummaryInformation:4', 'oleprop:SummaryInformation:8',
                           'oleprop:DocumentSummaryInformation:14', 'oleprop:DocumentSummaryInformation:15'):
                b.add_person_or_org(value, field)
            elif field == 'xml:Company':
                if 'solidworks' not in value.lower() and 'autodesk' not in value.lower():
                    b.add_person_or_org(value, field)
            elif field.startswith('swprop:'):
                pname = field[7:]
                if IDENTITY_FIELD_RE.search(pname) and 'date' not in pname.lower():
                    b.add_person_or_org(value, field)
            elif field.startswith('oleprop:'):
                # Inventor design tracking / summary: pids carrying people
                fs = field.split(':')
                if (fs[1].startswith('Qz4dgm') and fs[2] in ('16', '13', '14')) or (fs[1].startswith('Zrxrt') and fs[2] in ('4', '8')) \
                        or (fs[1].startswith('C3teagxw') and fs[2] in ('8', '20')):
                    v = re.split(r'\s{2,}|[\x00-\x1f]', value)[0]
                    if not re.fullmatch(r'[\d\s]+', v):
                        b.add_person_or_org(v, field)
            elif field.startswith('step:') and field[5:] in ('PERSON', 'ORGANIZATION'):
                parts = [norm(x) for x in value.split('|')]
                parts = [x for x in parts[1:] if x and not re.match(r'(?i)^(unspecified|person_|organization_|id_|nauo|none|last|first|mr\.?|mrs\.?|ms\.?|dr\.?|president|r&d|lab|unknown|\d+)$', x)]
                if field == 'step:PERSON' and len(parts) >= 2:
                    b.add_person_or_org(parts[1] + ' ' + parts[0], field)
                    b.add_person_or_org(parts[0] + ' ' + parts[1], field)
                for x in parts[:2]:
                    b.add_person_or_org(x, field)
            elif field.startswith('3mf:') and field[4:].lower() in ('designer', 'copyright', 'author', 'company', 'designercomment'):
                b.add_person_or_org(value, field)
    # 3. PDF metadata + visible title-block names, image EXIF, SVG creators
    base = os.path.dirname(harvest) if harvest else '.'
    pdfscan = os.path.join(base, 'pdf.jsonl')
    if os.path.exists(pdfscan):
        from . import textnames
        tcount = collections.Counter()
        for line in open(pdfscan, encoding='utf-8'):
            try:
                r = json.loads(line)
            except Exception:
                continue
            md = r.get('md') or {}
            if md.get('author'):
                b.add_person_or_org(md['author'], 'pdf:author')
            x = r.get('xmp') or ''
            for blk in re.findall(r'<dc:creator>(.*?)</dc:creator>', x, re.S):
                for li in re.findall(r'<rdf:li[^>]*>([^<]{2,200})</rdf:li>', blk):
                    b.add_person_or_org(li, 'xmp:creator')
            for v in re.findall(r'<pdf:Author>([^<]{2,200})</pdf:Author>|pdf:Author="([^"]{2,200})"', x):
                b.add_person_or_org(v[0] or v[1], 'xmp:author')
            seen = set()
            for kind, nm in textnames.candidates((r.get('t') or '')[:200000]):
                if (kind, nm) not in seen:
                    seen.add((kind, nm))
                    tcount[(kind, nm)] += 1
        ORG_SUFFIX = re.compile(r'\b(Ltd|Limited|Inc|Incorporated|GmbH|Corporation|Corp|Group|LLC|AG|BV|Company|Technologies|'
                                r'Instruments|Research|Automation|Systems|Industries|Electronics|University|Institute)\b\.?')
        # contact details in visible PDF text: e-mails, source-specific domains, labelled phone numbers
        PEMAIL = re.compile(r'[A-Za-z0-9._%+\-]{1,64}@[A-Za-z0-9.\-]{1,80}\.[A-Za-z]{2,10}')
        PURL = re.compile(r'(?:https?://|www\.)([A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)+)')
        PPHONE = re.compile(r'(?:\+\d{1,3}[\s.\-]?)?(?:\(\d{2,4}\)[\s.\-]?)?\d{2,4}[\s.\-]\d{3,4}[\s.\-]\d{3,5}')
        dom_src = collections.defaultdict(set)
        for line in open(pdfscan, encoding='utf-8'):
            try:
                r = json.loads(line)
            except Exception:
                continue
            t = r.get('t') or ''
            if not t:
                continue
            src = '/'.join(r['p'].split('/')[:3])
            for e in PEMAIL.findall(t):
                b.add_email(e, 'pdftext:email')
            for d in PURL.findall(t):
                dom_src[d.lower().strip('.')].add(src)
            for ln in t.split('\n'):
                if re.search(r'(?i)\b(?:tel|phone|fax|ph|mob|mobile|telefon|téléphone|t:|f:)', ln):
                    for ph in PPHONE.findall(ln):
                        ph = ph.strip()
                        if len(re.sub(r'\D', '', ph)) >= 8:
                            b.add(ph, 'word', 'pdftext:phone', digits=True)
        for d, srcs in dom_src.items():
            reg = '.'.join(d.split('.')[-2:])
            if len(srcs) <= 3 and reg not in b.url_skip and '.' in d:
                b.add_domain(d, 'pdftext:url')
        for (kind, nm), n in tcount.items():
            why = 'pdftext:' + kind
            toks = nm.split()
            if kind in ('label', 'label_next'):
                if all(re.fullmatch(r"[A-Z][a-zà-ÿ'\-]+|[A-Z]\.?", t) for t in toks) and \
                        sum(1 for t in toks if len(t.strip('.')) > 1) >= 2:
                    b.add_person_or_org(nm, why)
            elif kind == 'initial':
                sur = nm.replace('.', ' ').split()[-1]
                if len(sur) >= 4 and sur.lower() not in COMMON and sur.lower() not in GENERIC and rare_word(sur):
                    b.add(nm, 'strict', why)
            elif kind == 'copyright':
                if ORG_SUFFIX.search(nm) and len(toks) >= 2:
                    b.add(nm, 'strict', why)
                elif len(toks) == 1 and re.fullmatch(r"[A-Z][a-zà-ÿ'\-]{3,}", nm) and nm.lower() not in COMMON:
                    b.add(nm, 'strict', why)
    exif = os.path.join(base, 'exif.json')
    if os.path.exists(exif):
        txt = open(exif, encoding='utf-8', errors='replace').read()
        dec = json.JSONDecoder()
        i = 0
        while True:
            j = txt.find('[', i)
            if j < 0:
                break
            try:
                objs, i = dec.raw_decode(txt, j)
            except Exception:
                i = j + 1
                continue
            for o in objs:
                for k in ('Artist', 'XPAuthor', 'Creator', 'Copyright', 'OwnerName', 'By-line', 'Author', 'Rights', 'CopyrightNotice', 'Credit'):
                    v = o.get(k)
                    if isinstance(v, list):
                        v = ', '.join(map(str, v))
                    if v:
                        b.add_person_or_org(str(v), 'exif:' + k)
    svgmeta = os.path.join(base, 'svgmeta.jsonl')
    if os.path.exists(svgmeta):
        for line in open(svgmeta, encoding='utf-8'):
            try:
                field, value = json.loads(line)
            except Exception:
                continue
            if 'matplotlib' in value.lower() or 'http' in value or 'lgpl' in value.lower():
                continue
            b.add_person_or_org(value.replace('[', ' ').replace(']', ' '), 'svg:' + field)
    from .pdfids import load_blob
    reviewed = load_blob(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'reviewed.dat'))
    if reviewed:
        for it in reviewed:
            sv = it['s'].strip()
            if len(sv) >= 3:
                b.add(sv, 'strict', 'reviewed:' + it.get('t', ''))
    # 4. PDF-only identities (applied inside PDFs, never to file names or CAD content): part / drawing /
    #    document numbers, vendor companies, postal addresses and phone numbers from the PDF scan, plus the
    #    identities labelled in the recall pass over every source collection (compressed list ids_pdf.dat)
    from . import pdfids
    npdf = 0
    for sv, (kind, why) in pdfids.harvest(pdfscan, manifest, rare_word).items():
        b.add(sv, 'pdf', why, digits=bool(re.search(r'\d', sv)))
        npdf += 1
    # DEID_VENDORS=off: third-party supplier names (component vendors in BOMs) are not identities of the sources
    vendors_on = os.environ.get('DEID_VENDORS', 'on') != 'off'
    if vendors_on:
        for v in _vendor_list():
            b.add(v, 'pdf', 'vendor:list')
    lab = pdfids.load_blob(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'ids_pdf.dat')) or []
    for typ, sv in lab:
        if typ == 'vendor' and not vendors_on:
            continue
        b.add(sv, 'pdf', 'labelled:' + typ, digits=bool(re.search(r'\d', sv)))
    print('pdf-only candidates: rules', npdf, 'labelled', len(lab))
    # a key kept for paths only (curated 'path' mode, e.g. a company that is also a surname) that the recall pass
    # labelled in PDF text is an identity there too: apply it to content as well
    if os.environ.get('DEID_REG_PROMOTE_PATH', '1') == '1':
        for k, m in list(b.entries.items()):
            if m == 'path' and any(s.startswith('labelled:') for s in b.source.get(k, ())):
                b.entries[k] = 'strict'
    # drop keys that are substrings of format-critical tokens
    for k in list(b.entries):
        if k in ('solidworks', 'freecad', 'parasolid', 'step', 'iso', 'din', 'ansi', 'part', 'assembly', 'dassault'):
            del b.entries[k]
    # Common single words harvested automatically (author / account / person fields split into words,
    # generic account names such as 'support' or 'quality') are not identities on their own: as
    # corpus-wide keys they rename ordinary engineering words ('main', 'black', 'plastic', 'support')
    # in drawings, BOMs and file names.  Full multi-word values stay, and identity fields (authors,
    # saved-by accounts) are still pseudonymized field by field.  Curated / reviewed / owner keys stay.
    dropped = []
    for k in list(b.entries):
        if k in GENERIC_SINGLE and not any(s.startswith(('curated', 'reviewed', 'owner')) for s in b.source.get(k, ())):
            dropped.append(k)
            del b.entries[k]
    print('dropped generic single-word keys:', len(dropped), sorted(dropped)[:40])
    json.dump({'entries': b.entries, 'digit_keys': sorted(b.digit_keys), 'owners': sorted(b.owners),
               'source': {k: sorted(v)[:5] for k, v in b.source.items()}}, open(out, 'w'), ensure_ascii=False, indent=0)
    print('entries', len(b.entries), collections.Counter(b.entries.values()))


if __name__ == '__main__':
    build(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 and sys.argv[2] != '-' else None, sys.argv[3])
