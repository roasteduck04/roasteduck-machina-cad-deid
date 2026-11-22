"""Person/organisation candidates from visible drawing text (title blocks, copyright lines).

Used by registry.py on the PDF text scan.  Patterns:
  * initial(s) + surname        'J. Doe', 'J.DOE', 'A.Smith', 'M A Smith'
  * label + value on one line   'Drawn by: Jane Doe', 'Gezeichnet: Doe'
  * value on the line after a label, when it looks like a name
  * copyright / (c) lines       '(c) by: Doe, 2016', 'Copyright 2019 Acme Ltd'
"""
import re

LABEL = (r'drawn(?: by)?|drn|designed(?: by)?|designer|design(?:ed)? by|dsgn|checked(?: by)?|chk(?:\'?d)?|approved(?: by)?|appd|appr'
         r'|engineer|eng(?:ineer)?\.?|author|created by|creator|drafter|draftsman|modell?ed by|made by|prepared by|owner'
         r'|gezeichnet|gez\.?|bearbeitet|bearb\.?|gepr(?:ü|ue)ft|gepr\.?|konstrukteur|ersteller|zeichner|erstellt(?: von)?'
         r'|dessin[ée](?: par)?|v[ée]rifi[ée](?: par)?|approuv[ée](?: par)?|auteur|dibujado(?: por)?|dibuj[óo]|revisado(?: por)?'
         r'|aprobado(?: por)?|dise[ñn]ado(?: por)?|proyect[óo]|desenhado|verificado|aprovado|disegnato|controllato|approvato')
LABEL_LINE = re.compile(r'(?i)^\s*(?:' + LABEL + r')\s*[:.\-]?\s*(.{2,60})$')
LABEL_ONLY = re.compile(r'(?i)^\s*(?:' + LABEL + r')\s*[:.]?\s*$')
INITIAL_SURNAME = re.compile(r'(?<![\w.])((?:[A-Z]\.\s?){1,3}[A-Z][a-zA-Z\'\-]{2,20})(?![\w.])')
COPYRIGHT = re.compile(r'(?i)(?:©|\(c\)|copyright)\s*(?:by:?\s*)?(?:\d{4}(?:\s*[-–]\s*\d{4})?[, ]*)?([A-Z][\w.&\'\- ]{2,50}?)(?:,|\s\d{4}|\.|$)')
NAME_WORD = re.compile(r"^(?:[A-Z][a-zà-ÿ'\-]{1,20}|[A-Z]{2,20}|[A-Z]\.?)$")

STOP = set("""
date datum scale sheet size title material finish weight mass rev revision qty quantity part number drawing dwg no description
checked approved drawn designed name signature unit units tolerance tolerances general iso din ansi asme mm inch inches
third angle projection first do not scale remove burrs break edges sharp unless otherwise specified dimensions are in
surface treatment heat steel aluminum aluminium stainless brass plastic copper nylon delrin pom abs pla petg acrylic
assembly part item view section detail top bottom left right front back side isometric note notes sheet of the and for
format a4 a3 a2 a1 a0 rev. ltr eco ecn status released prototype draft preliminary approval project customer client
company department dept engineering design manufacturing quality production document type file model
""".split())


def name_like(v, maxtok=4):
    v = v.strip(' :;,.-')
    if not v or len(v) > 60 or len(v) < 3:
        return None
    if re.search(r'\d{3,}|[=/\\@#%*+<>]|\bmm\b', v):
        return None
    toks = v.replace(',', ' ').split()
    if not 1 <= len(toks) <= maxtok:
        return None
    if not all(NAME_WORD.match(t) for t in toks):
        return None
    if any(t.lower().strip('.') in STOP for t in toks):
        return None
    if all(len(t.strip('.')) <= 1 for t in toks):
        return None
    return ' '.join(toks)


def _good_multi(nm):
    toks = nm.split()
    return len(toks) >= 2 and any(re.search(r'[a-z]', t) for t in toks)


def candidates(text):
    out = []
    lines = [ln.strip() for ln in text.split('\n')]
    for i, ln in enumerate(lines):
        m = LABEL_LINE.match(ln)
        if m:
            nm = name_like(m.group(1))
            if nm and (_good_multi(nm) or (len(nm.split()) >= 2 and len(nm) >= 8)):
                out.append(('label', nm))
        elif LABEL_ONLY.match(ln) and i + 1 < len(lines):
            nm = name_like(lines[i + 1])
            if nm and _good_multi(nm):
                out.append(('label_next', nm))
        for m in INITIAL_SURNAME.finditer(ln):
            nm = m.group(1)
            sur = nm.replace('.', ' ').split()[-1]
            if sur.lower() not in STOP and not re.fullmatch(r'X+', sur, re.I) and len(sur) >= 3:
                out.append(('initial', nm))
        for m in COPYRIGHT.finditer(ln):
            nm = name_like(m.group(1), maxtok=6)
            if nm and len(nm) >= 4:
                out.append(('copyright', nm))
    return out
