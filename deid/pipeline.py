"""De-identification pipeline.

  python -m deid.pipeline prepare <registry.json> <state.json> <input_dir>
  python -m deid.pipeline run <state.json> <input_dir> <output_dir> <report.jsonl> [--workers N] [--only PREFIX]

prepare: freezes the identity list and the pseudonym word map (deterministic).
run:     rewrites every input file into output_dir under its de-identified path.
"""
import gzip
import io
import json
import os
import random
import re
import shutil
import struct
import subprocess
import sys
import tarfile
import tempfile
import time
import traceback
import zipfile
import zlib
from multiprocessing import Pool

from .core import Matcher, Pseudonymizer, apply_case, pseudo_det
from . import olefields

BS = chr(92)
# experiment switch: DEID_PDF_TEXT=none leaves visible PDF page text untouched (metadata etc. still cleaned)
PDF_TEXT_MODE = os.environ.get('DEID_PDF_TEXT', 'full')
# diagnostic probes (not used for normal runs):
#   overlay: paint every PDF page white and draw only the pseudonyms on top (text layer as normal)
#   aggr:    replace every word of >=3 letters in PDF page text (upper bound of text-layer removal)
PDF_PROBE = os.environ.get('DEID_PDF_PROBE', '')
# span mode: rewrite whole text spans that contain identities (keeps part numbers contiguous)
PDF_SPAN = os.environ.get('DEID_PDF_SPAN', '1') == '1'
PDF_GUARD = True
# 'exact' (default): line-level matching with boundary rules; 'legacy': search_for() substring path (a1-a10)
PDF_MATCH = os.environ.get('DEID_PDF_MATCH', 'exact')
# OCR pass: names that exist only as pixels or vector outlines (scans, logos, outlined title blocks)
PDF_OCR = os.environ.get('DEID_PDF_OCR', '1') == '1'
OCR_PAGES = int(os.environ.get('DEID_OCR_PAGES', '12'))   # OCR the first pages, plus any later page without text
OCR_MAX_PAGES = int(os.environ.get('DEID_OCR_MAX', '40'))
OCR_DPI = int(os.environ.get('DEID_OCR_DPI', '200'))
# probe: inside a drawing PDF keep its own file-name part number when a model file with the same stem exists
PDF_KEEP_OWN_STEM = os.environ.get('DEID_PDF_KEEP_OWN_STEM', '0') == '1'
PAIRED = set()
_TESSDATA = None
WORD_RE = re.compile(r'[A-Za-z]{3,}')
PDF_AUTHOR_KEEP = set(['unknown', 'anonymous', '(anonymous)', 'author', 'none', 'n/a', '-', 'solidworks', 'autocad', 'nx', 'camscanner', 'python-docx', 'microsoft office user'])
PROTECTED_ACCOUNT_WORDS = set('user users owner admin operator guest public temp default system all users everyone root author none unknown name'.split())

# ----------------------------------------------------------------------------
# state

STATE = None
PZ = None
MC = None      # content matcher
MP = None      # metadata matcher (includes path-only and metadata-only keys)
MN = None      # file/folder-name matcher
MPDF = None    # PDF matcher: content keys + PDF-only keys (part/document numbers, vendors, project names)
MSTEM = None   # person/account/e-mail/domain keys only (file names and SolidWorks internals in DEID_STEMS=person)
OWNERS = set()
STEM_MODE = os.environ.get('DEID_STEMS', 'all')
# SolidWorks 2015+ containers: 'all' (every stream), 'docprops' (only the docProps property XML), 'off'
SW_MODE = os.environ.get('DEID_SW', 'all')
# wipe-guard fallback: cover text-layer identities visually (white box + pseudonym) instead of leaving them
PDF_COVER_FALLBACK = os.environ.get('DEID_PDF_COVER_FALLBACK', '1') == '1'


def load_state(path):
    global STATE, PZ, MC, MP, MN, MPDF, OWNERS, MSTEM, PAIRED
    STATE = json.load(open(path, encoding='utf-8'))
    PZ = Pseudonymizer()
    PZ.words.load(STATE['wordmap'])
    PZ.digit_keys = set(STATE.get('digit_keys', []))
    PZ.cache.update(STATE['pseudo'])
    ents = STATE['entries']
    MC = Matcher(PZ, {k: v for k, v in ents.items() if v not in ('path', 'meta', 'pdf')})
    # PDF-only keys are applied inside PDFs (text layer, OCR, metadata) but never to file names or CAD content
    MPDF = Matcher(PZ, {k: ('strict' if v == 'pdf' else v) for k, v in ents.items() if v not in ('path', 'meta')})
    OWNERS = set(STATE.get('owners', []))
    PAIRED = set(STATE.get('paired_stems', []))
    kinds = STATE.get('kinds', {})
    MSTEM = Matcher(PZ, {k: v for k, v in ents.items() if v not in ('path', 'meta', 'pdf') and kinds.get(k) == 'person'})
    # metadata contexts (property sets, headers, docProps): content keys + path + metadata-only keys
    # (path-only and metadata-only keys are NOT used inside files: account fields are handled field-level,
    #  and blunt short keys corrupted part numbers in property sets)
    MP = MC
    # file and folder names: content keys + folder-only keys, never metadata-only keys, so that
    # renamed files keep matching the (content-matcher) references to them inside other files
    MN = Matcher(PZ, {k: ('strict' if v == 'path' else v) for k, v in ents.items() if v not in ('meta', 'pdf')})


STRUCT_TOKEN_RE = re.compile(rb'(?:=\s*|\(\s*)([A-Z][A-Z0-9_]{2,60})\s*\(|<\/?([A-Za-z_][\w:.\-]{2,60})[\s>/]|\b([A-Za-z_][\w]{2,40})="')


def prepare(reg_path, state_path, input_dir):
    reg = json.load(open(reg_path, encoding='utf-8'))
    entries = reg['entries']
    # safety net: never treat format vocabulary as an identity
    vocab = set()
    files = []
    for dp, dn, fn in os.walk(input_dir):
        if '.get-input' in dp:
            continue
        for f in fn:
            if f.lower().endswith(('.step', '.stp', '.svg', '.xml', '.dxf', '.igs', '.iges', '.wrl', '.obj', '.x_t', '.sat')):
                files.append(os.path.join(dp, f))
    files.sort()
    random.seed(7)
    random.shuffle(files)
    for p in files[:4000]:
        try:
            with open(p, 'rb') as h:
                b = h.read(3_000_000)
        except Exception:
            continue
        for m in STRUCT_TOKEN_RE.finditer(b):
            for g in m.groups():
                if g:
                    vocab.add(g.decode('latin-1').lower())
        if p.lower().endswith('.dxf'):
            for m in re.finditer(rb'\n\s*0\r?\n([A-Z_]{3,30})\r?\n', b):
                vocab.add(m.group(1).decode().lower())
    dropped = [k for k in entries if k in vocab]
    for k in dropped:
        del entries[k]
    print('dropped vocab keys:', len(dropped), sorted(dropped)[:80])
    pz = Pseudonymizer()
    pz.digit_keys = set(reg.get('digit_keys', []))
    # forbid real identity words as pseudo output
    for k in entries:
        for w in re.findall(r'[^\W\d_]+', k):
            if len(w) >= 3:
                pz.words.forbidden.add(w)
    owners = reg.get('owners', [])
    for o in owners:
        for w in re.findall(r'[^\W\d_]+', o):
            if len(w) >= 3:
                pz.words.forbidden.add(w)
    pseudo = {}
    for k in sorted(entries):
        pseudo[k] = pz.pseudo(k)
    for o in sorted(owners):
        pseudo[o] = pz.pseudo(o)
    kinds = {k: 'person' for k in entries if _personish(k, reg.get('source', {}).get(k, []), owners)}
    # stems that name both a drawing PDF and a native/neutral model in the same folder
    MODEL_EXT = ('.sldprt', '.sldasm', '.slddrw', '.step', '.stp', '.ipt', '.iam', '.idw', '.par', '.psm', '.asm',
                 '.prt', '.f3d', '.fcstd', '.x_t', '.igs', '.iges', '.dxf', '.dwg', '.stl', '.3mf')
    per_dir = {}
    for dp, dn, fn in os.walk(input_dir):
        if '.get-input' in dp:
            continue
        for f in fn:
            st, ex = os.path.splitext(f.lower())
            per_dir.setdefault((dp, st), set()).add(ex)
    paired = sorted(set(st for (dp, st), exs in per_dir.items() if '.pdf' in exs and any(e in exs for e in MODEL_EXT)))
    state = {'entries': entries, 'wordmap': pz.words.dump(), 'pseudo': pseudo, 'owners': owners, 'kinds': kinds,
             'paired_stems': paired,
             'digit_keys': sorted(pz.digit_keys), 'created': time.time()}
    json.dump(state, open(state_path, 'w', encoding='utf-8'), ensure_ascii=False)
    print('state:', len(entries), 'entries', len(state['wordmap']), 'words')


# ----------------------------------------------------------------------------
# helpers

PERSON_SRC = ('owner', 'winuser', 'unixuser', 'lockfile', 'pdf:author', 'xmp:creator', 'xmp:author', 'step:person',
              'step:fn_author', 'iges:author', 'fc:createdby', 'fc:lastmodifiedby', 'swprop:sw-author',
              'swprop:sw-last saved by', 'swprop:designer', 'swprop:author', 'oleprop:summaryinformation:4',
              'oleprop:summaryinformation:8', 'reviewed:person', 'pdftext:label', 'pdftext:initial', 'xt:user',
              'exif', 'svg', 'pdftext:email', 'email')


def _personish(k, srcs, owners):
    """Identities that may rename a FILE (not only a folder): people, accounts, e-mails, domains, owners."""
    if '@' in k or k in owners:
        return True
    if re.fullmatch(r'[a-z0-9.\-]+\.[a-z]{2,6}', k) and not re.fullmatch(r'[\d.]+', k):
        return True
    return any(s.lower().startswith(PERSON_SRC) for s in srcs)


def deid_name(name, is_file=True):
    """De-identify one path component; keeps the extension untouched."""
    if not name:
        return name
    if is_file:
        # file names use the content matcher (references inside other files stay valid); with
        # DEID_STEMS=person only person/account/e-mail/domain identities rename a file, so files keep
        # the names that assemblies and drawings refer to
        M = MSTEM if STEM_MODE == 'person' else MC
        if '.' in name.strip('.'):
            stem, ext = name.rsplit('.', 1)
            return M.text(stem) + '.' + ext
        return M.text(name)
    return MN.text(name)


def _owner_component(comp, idx):
    segs = comp.split('__')
    if len(segs) > idx and segs[idx]:
        o = segs[idx]
        if o.lower() in OWNERS and o.lower() in PZ.cache:
            segs[idx] = apply_case(o, PZ.pseudo(o.lower()))
        else:
            segs[idx] = MN.text(o)
    for i in range(len(segs)):
        if i != idx:
            segs[i] = MN.text(segs[i])
    return '__'.join(segs)


def deid_relpath(rel):
    parts = rel.split('/')
    out = []
    for i, p in enumerate(parts):
        last = i == len(parts) - 1
        if i == 2 and parts[0] == 'sources' and parts[1] == 'github' and not last:
            out.append(_owner_component(p, 0))
        elif i == 2 and parts[0] == 'sources' and parts[1] == 'gitlab' and not last:
            out.append(_owner_component(p, 1))
        elif i == 1 and parts[0] == 'archives' and last and '__' in p:
            stem, ext = p.rsplit('.', 1)
            out.append(_owner_component(stem, 0) + '.' + ext)
        else:
            out.append(deid_name(p, is_file=last))
    return '/'.join(out)


def deid_inner_path(p):
    # path inside an archive ('/'-separated, may end with '/')
    if p.endswith('/'):
        return '/'.join(deid_name(x, False) for x in p[:-1].split('/')) + '/'
    parts = p.split('/')
    return '/'.join([deid_name(x, False) for x in parts[:-1]] + [deid_name(parts[-1], True)])


class Rep:
    """Per-file report."""

    def __init__(self, rel):
        self.rel = rel
        self.notes = []
        self.hits = []

    def note(self, s):
        self.notes.append(s)


def bytes_text(b, rep, minlen=3):
    return MC.bytes(b, rep.hits, min_len_bin=minlen)


# ----------------------------------------------------------------------------
# SolidWorks 2015+ container

SW_MARKER = b'\x14\x00\x06\x00\x08\x00'


def nib(b):
    return bytes(((x >> 4) | ((x & 15) << 4)) & 255 for x in b)


def _fixed_empty_blocks(n):
    """n empty fixed-Huffman blocks, last one final; returns bytes (zero-padded)."""
    bits = []
    for i in range(n):
        bits += [1 if i == n - 1 else 0, 1, 0] + [0] * 7
    out = bytearray((len(bits) + 7) // 8)
    for i, bt in enumerate(bits):
        if bt:
            out[i // 8] |= 1 << (i % 8)
    return bytes(out)


def _verify(stream, data):
    try:
        o = zlib.decompressobj(-15)
        return o.decompress(stream) == data and o.eof and not o.unused_data
    except Exception:
        return False


def _prefix_pad(stream, size, data):
    """Prepend empty deflate blocks (5-byte stored / 10-bit fixed) so the stream is exactly size bytes."""
    L = len(stream)
    if L > size:
        return None
    val = int.from_bytes(stream, 'little')
    stored = b'\x00\x00\x00\xff\xff'  # empty non-final stored block (byte aligned)
    tried = set()
    for k in range(0, 8):
        # k fixed empty non-final blocks (10 bits each) followed by the stream, bit-packed
        fixed = 0
        for j in range(k):
            fixed |= 1 << (10 * j + 1)
        tail_val = (val << (10 * k)) | fixed
        for nb_tail in ((10 * k + 8 * L + 7) // 8 - 1, (10 * k + 8 * L + 7) // 8):
            if nb_tail <= 0:
                continue
            rest = size - nb_tail
            if rest < 0 or rest % 5:
                continue
            if (k, nb_tail) in tried:
                continue
            tried.add((k, nb_tail))
            try:
                tail = tail_val.to_bytes(nb_tail, 'little')
            except OverflowError:
                continue
            cand = stored * (rest // 5) + tail
            if len(cand) == size and _verify(cand, data):
                return cand
    return None


def deflate_exact(data, size):
    """Raw deflate stream of exactly `size` bytes that inflates to data, or None."""
    streams = []
    if len(data) > 2_000_000:
        combos = [(lv, zlib.Z_DEFAULT_STRATEGY, 8) for lv in (6, 9, 1, 3)]
    else:
        combos = [(lv, st, mem) for mem in (8, 9) for lv in (6, 9, 5, 7, 8, 4, 3, 2, 1)
                  for st in (zlib.Z_DEFAULT_STRATEGY, zlib.Z_FILTERED)]
    for level, strat, mem in combos:
        c = zlib.compressobj(level, zlib.DEFLATED, -15, mem, strat)
        full = c.compress(data) + c.flush()
        if len(full) == size and _verify(full, data):
            return full
        streams.append(full)
        if level == 9 and mem == 8 and strat == zlib.Z_DEFAULT_STRATEGY and len(full) > size * 1.15 + 64:
            break
    streams.sort(key=len, reverse=True)
    for st in streams:
        if len(st) <= size - 1:
            r = _prefix_pad(st, size, data)
            if r is not None:
                return r
    try:
        import zopfli.zopfli as zz
        if len(data) > 1_500_000:
            return None
        for it in (5, 15):
            if len(data) > 300_000 and it > 5:
                break
            zs = zz.compress(data, numiterations=it, gzip_mode=0)[2:-4]
            if len(zs) == size and _verify(zs, data):
                return zs
            if len(zs) < size:
                r = _prefix_pad(zs, size, data)
                if r is not None:
                    return r
    except Exception:
        pass
    return None


SW_ACCT_RE = re.compile(rb'<property name="([^"]{1,120})" pid="\d+"><vt:lpw?str>([^<]{1,200})</vt:lpw?str>'
                        rb'|<(?:dc:creator|cp:lastModifiedBy|Company|Manager)>([^<]{1,200})</')
SW_ACCT_NAME_RE = re.compile(r'(?i)author|saved by|enregistr|gespeich|guardad|autor|auteur|salvat|opgeslagen|designed|'
                             r'drawn|dessin|dibuj|checked|approved|engineer|company|firma|entreprise|empresa|dissenyat')
DEFAULT_COMPANY = ('dassault syst', 'solidworks corp', 'autodesk')


def _sw_field_level(out, rep):
    def sub(m):
        if m.group(3) is not None:
            val = m.group(3)
            whole = m.group(0)
        else:
            if not SW_ACCT_NAME_RE.search(m.group(1).decode('utf-8', 'replace')):
                return m.group(0)
            val = m.group(2)
            whole = m.group(0)
        v = val.decode('utf-8', 'replace')
        vs = v.strip()
        if not vs or vs in ('-', '--') or any(x in vs.lower() for x in DEFAULT_COMPANY):
            return whole
        nv = MP.text(v)
        if nv == v:
            nv = v.replace(vs, apply_case(vs, pseudo_det(PZ, vs)))
        nb = nv.encode('utf-8')
        if len(nb) != len(val):
            nb = (nb + b'x' * len(val))[:len(val)]
        rep.hits.append('sw_field')
        g = 3 if m.group(3) is not None else 2
        i = m.start(g) - m.start(0)
        return whole[:i] + nb + whole[i + len(val):]
    return SW_ACCT_RE.sub(sub, out)


def _local_account_matcher(accounts):
    keys = {}
    for v in accounts:
        k = v.lower().strip()
        if not k or any(x in k for x in DEFAULT_COMPANY):
            continue
        if len(k) >= 3 and k not in PROTECTED_ACCOUNT_WORDS and re.search(r'[^\W\d_]', k):
            keys[k] = 'strict'
        if 2 <= len(k) <= 40 and ' ' not in k:
            for pre in ('users' + BS, 'users/', 'home/', 'documents and settings' + BS):
                keys[pre + k] = 'word'
    if not keys:
        return None
    for k in keys:
        if k not in PZ.cache:
            for pre in ('users' + BS, 'users/', 'home/', 'documents and settings' + BS):
                if k.startswith(pre):
                    PZ.cache[k] = pre + pseudo_det(PZ, k[len(pre):])
                    break
            else:
                PZ.cache[k] = pseudo_det(PZ, k)
    return Matcher(PZ, keys)


def h_sw(d, rep, name):
    if SW_MODE == 'off':
        rep.note('sw_kept')
        return d
    src = bytes(d)
    d = bytearray(d)
    marks = [m.start() for m in re.finditer(re.escape(SW_MARKER), src)]
    by_key = {}
    recs = []
    for ps in marks:
        if ps + 26 > len(src):
            continue
        by_key.setdefault(src[ps + 10: ps + 22], []).append(ps)
        ts, crc, cs, us, nl = struct.unpack_from('<IIIII', src, ps + 6)
        nl &= 0xFFFF
        if nl > 400 or cs > len(src):
            continue
        start = ps + 26 + nl
        if start + cs > len(src):
            continue
        try:
            out = zlib.decompress(src[start:start + cs], -15)
        except Exception:
            continue
        if zlib.crc32(out) != crc or len(out) != us:
            continue
        recs.append((ps, start, crc, cs, us, nl, out))
    changed = failed = 0
    newcrc_for = {}
    accounts = set()
    for ps, start, crc, cs, us, nl, out in recs:
        sname = nib(src[ps + 26: ps + 26 + nl]).decode('latin-1')
        if sname.startswith('docProps'):
            for m in SW_ACCT_RE.finditer(out):
                nm = (m.group(1) or b'').decode('utf-8', 'replace')
                val = (m.group(2) if m.group(2) is not None else m.group(3)).decode('utf-8', 'replace').strip()
                if val and (m.group(3) is not None or SW_ACCT_NAME_RE.search(nm)):
                    accounts.add(val)
    local = _local_account_matcher(accounts)
    for ps, start, crc, cs, us, nl, out in recs:
        sname = nib(src[ps + 26: ps + 26 + nl]).decode('latin-1')
        if sname in ('Preview', 'PreviewPNG') or sname.endswith(('.png', '.bmp', '.jpg')):
            continue
        if SW_MODE == 'docprops' and not sname.startswith('docProps'):
            continue
        if sname.startswith('docProps'):
            new = MP.bytes(_sw_field_level(out, rep), rep.hits, min_len_bin=3)
            if local is not None:
                new = local.bytes(new, rep.hits, min_len_bin=3)
        elif sname.startswith(('swXmlContents', '_rels', '[Content')) or sname.endswith('.xml'):
            new = bytes_text(out, rep)
        elif STEM_MODE == 'person':
            # keep internal references to (unrenamed) part/drawing files intact: people/accounts only
            new = MSTEM.bytes(out, rep.hits, min_len_bin=5, min_len_u16=3)
        elif sname in ('Header2', 'SwDocContentMgr/SwDocContentMgrInfo') or sname.endswith('ModelHeader') or 'Biography' in sname:
            new = MP.bytes(out, rep.hits, min_len_bin=5, min_len_u16=3)
        else:
            new = MC.bytes(out, rep.hits, min_len_bin=5, min_len_u16=3)
        if local is not None and not sname.startswith('docProps'):
            new = local.bytes(new, rep.hits, min_len_bin=4, min_len_u16=3)
        if new == out:
            continue
        comp = deflate_exact(new, cs)
        if comp is None:
            failed += 1
            rep.note('sw_nofit:' + sname)
            continue
        d[start:start + cs] = comp
        newcrc = zlib.crc32(new)
        struct.pack_into('<I', d, ps + 10, newcrc)
        newcrc_for[struct.pack('<III', crc, cs, us)] = newcrc
        changed += 1
    # central directory / duplicate headers that point at modified data
    local_ps = {r[0] for r in recs}
    for key, newcrc in newcrc_for.items():
        for p2 in by_key.get(key, []):
            if p2 not in local_ps:
                struct.pack_into('<I', d, p2 + 10, newcrc)
    if changed:
        rep.note('sw_streams_changed:%d' % changed)
    if failed:
        rep.note('sw_streams_failed:%d' % failed)
    return bytes(d)


# ----------------------------------------------------------------------------
# OLE compound files (Inventor, Solid Edge, SolidWorks <2015, ...)

def _zlb_rewrite(data, rep, local):
    """Legacy SolidWorks '__ZLB' sub-stream: 16-byte magic + chunks [u32 unc][u32 comp][zlib]."""
    buf = bytearray(data)
    pos = 16
    n = 0
    while pos + 8 <= len(buf):
        unc, comp = struct.unpack_from('<II', buf, pos)
        if unc == 0 and comp == 0:
            break
        st = pos + 8
        if comp < 6 or st + comp > len(buf):
            break
        try:
            raw = zlib.decompress(bytes(buf[st:st + comp]))
        except Exception:
            break
        new = MC.bytes(raw, rep.hits, min_len_bin=5, min_len_u16=3)
        if local is not None:
            new = local.bytes(new, rep.hits, min_len_bin=4, min_len_u16=3)
        if new != raw:
            body = deflate_exact(new, comp - 6)
            if body is None:
                rep.note('zlb_nofit')
            else:
                buf[st + 2:st + comp - 4] = body
                buf[st + comp - 4:st + comp] = struct.pack('>I', zlib.adler32(new) & 0xffffffff)
                n += 1
        pos = st + comp
    if n:
        rep.note('zlb_chunks_changed:%d' % n)
    return bytes(buf)


def h_ole(d, rep, name):
    import olefile
    with tempfile.NamedTemporaryFile(delete=False, suffix='.ole', dir=TMPDIR) as t:
        t.write(d)
        tmp = t.name
    try:
        ole = olefile.OleFileIO(tmp, write_mode=True)
        n = 0
        entries = []
        for entry in ole.listdir(streams=True, storages=False):
            try:
                sz = ole.get_size(entry)
                if sz > 400_000_000:
                    continue
                entries.append(entry)
            except Exception:
                continue
        # 1. identity fields of the property sets (accounts, designers, company): always pseudonymised
        accounts = {}
        for entry in entries:
            if not entry[-1].startswith(chr(5)):
                continue
            try:
                for lab, v, off, nb, enc in olefields.identity_fields(ole.openstream(entry).read()):
                    accounts[v.strip()] = lab
            except Exception:
                pass
        local = None
        keys = {}
        for v in accounts:
            k = v.lower().strip()
            if len(k) >= 3 and k not in PROTECTED_ACCOUNT_WORDS and re.search(r'[^\W\d_]', k):
                keys[k] = 'strict'
            if 2 <= len(k) <= 40 and ' ' not in k:
                for pre in ('users\\', 'users/', 'home/', 'documents and settings\\'):
                    keys[pre + k] = 'word'
        if keys:
            for k in keys:
                if k not in PZ.cache:
                    PZ.cache[k] = pseudo_det(PZ, k)
            local = Matcher(PZ, keys)
        for entry in entries:
            try:
                data = ole.openstream(entry).read()
            except Exception:
                continue
            if entry[-1].startswith(chr(5)):
                buf = bytearray(data)
                try:
                    for lab, v, off, nb, enc in olefields.identity_fields(data):
                        vs = v.strip()
                        newv = MP.text(v, rep.hits)
                        if newv == v:
                            newv = v.replace(vs, apply_case(vs, pseudo_det(PZ, vs)))
                        if newv != v:
                            olefields.rewrite(buf, off, nb, enc, v, newv)
                            rep.hits.append('ole_field:' + lab)
                except Exception as e:
                    rep.note('ole_field_err:' + str(e)[:40])
                new = MP.bytes(bytes(buf), rep.hits, min_len_bin=3)
            elif entry[-1].endswith('__ZLB') and len(data) > 24:
                new = _zlb_rewrite(data, rep, local)
            else:
                new = bytes_text(data, rep)
            if local is not None:
                new = local.bytes(new, rep.hits, min_len_bin=3)
            if new != data:
                try:
                    ole.write_stream(entry, new)
                    n += 1
                except Exception as e:
                    rep.note('ole_write_fail:' + str(e)[:60])
        ole.close()
        if n:
            rep.note('ole_streams_changed:%d' % n)
        with open(tmp, 'rb') as h:
            out = h.read()
        return out
    except Exception as e:
        rep.note('ole_err:' + str(e)[:80])
        return bytes_text(d, rep, 5)
    finally:
        try:
            os.unlink(tmp)
        except Exception:
            pass


# ----------------------------------------------------------------------------
# PDF

FONT_NAME_KEYS = ('/BaseFont', '/FontName', '/FontFamily')


XMP_PERSON_RE = re.compile(r'(<(?:dc:creator|xmp:Author|pdf:Author|photoshop:AuthorsPosition|photoshop:Credit|'
                           r'xmpRights:Owner|Iptc4xmpCore:CreatorContactInfo)\b[^>]*>\s*(?:<rdf:(?:Seq|Bag|Alt)>\s*)?'
                           r'(?:<rdf:li[^>]*>)?)([^<]{1,200})(<)')


def _xmp_person(v, rep):
    vs = v.strip()
    if not vs or vs.lower() in PDF_AUTHOR_KEEP or not re.search(r'[^\W\d_]', vs):
        return v
    nv = MPDF.text(v)
    if nv != v:
        return nv
    rep.hits.append('xmp_person_field')
    return v.replace(vs, apply_case(vs, pseudo_det(PZ, vs)))


def _jpeg_meta_bytes(raw, rep, M):
    """Same-length de-identification of APP1 (Exif/XMP), APP13 (IPTC) and COM segments of a JPEG."""
    if raw[:2] != b'\xff\xd8':
        return None
    out = bytearray(raw)
    pos = 2
    changed = False
    while pos + 4 <= len(raw) and raw[pos] == 0xFF:
        marker = raw[pos + 1]
        if marker in (0xD9, 0xDA):
            break
        if marker == 0x01 or 0xD0 <= marker <= 0xD7:
            pos += 2
            continue
        seglen = struct.unpack('>H', raw[pos + 2:pos + 4])[0]
        if seglen < 2:
            break
        if marker in (0xE1, 0xED, 0xFE):
            seg = raw[pos + 4:pos + 2 + seglen]
            nseg = M.bytes(seg, rep.hits, min_len_bin=4, min_len_u16=3)
            if nseg != seg and len(nseg) == len(seg):
                out[pos + 4:pos + 2 + seglen] = nseg
                changed = True
        pos += 2 + seglen
    return bytes(out) if changed else None


def _pdf_struct_pass(data, rep):
    """Object-level pass over a saved PDF: de-identify string objects and font-name values.
    Never edits stream data or dictionary keys (those carry resource references)."""
    try:
        import pikepdf
    except Exception:
        return data
    try:
        pdf = pikepdf.open(io.BytesIO(data))
    except Exception:
        return data
    count = [0]

    def fix(container, key, v):
        if isinstance(v, pikepdf.String):
            try:
                sv = str(v)
            except Exception:
                return
            nv = (MPDF if PDF_MATCH != 'legacy' else MP).text(sv, rep.hits)
            if nv != sv:
                container[key] = pikepdf.String(nv)
                count[0] += 1
        elif isinstance(v, pikepdf.Name) and isinstance(key, str) and key in FONT_NAME_KEYS:
            sv = str(v)[1:]
            nv = MC.text(sv, rep.hits)
            if nv != sv:
                container[key] = pikepdf.Name('/' + nv)
                count[0] += 1
        elif isinstance(v, (pikepdf.Dictionary, pikepdf.Array)) and not v.is_indirect:
            walk(v)

    def walk(o, depth=0):
        if depth > 12:
            return
        if isinstance(o, pikepdf.Array):
            for i in range(len(o)):
                fix(o, i, o[i])
        else:
            try:
                items = list(o.items())
            except Exception:
                return
            for k, v in items:
                if k in ('/Contents',) and isinstance(v, (pikepdf.Stream, pikepdf.Array)):
                    continue
                fix(o, k, v)

    MX = MPDF if PDF_MATCH != 'legacy' else MP

    def meta_stream(obj):
        # object-level XMP (images, pages, fonts, form XObjects): same rules as the document XMP
        try:
            raw = obj.read_bytes()
            x = raw.decode('utf-8')
        except Exception:
            return
        nx = MX.text(x, rep.hits)
        nx = XMP_PERSON_RE.sub(lambda m: m.group(1) + _xmp_person(m.group(2), rep) + m.group(3), nx)
        if nx != x:
            obj.write(nx.encode('utf-8'))
            count[0] += 1

    def jpeg_stream(obj):
        # EXIF / XMP / IPTC / comment segments of embedded JPEGs (same-length edits, scan data untouched)
        try:
            raw = obj.read_raw_bytes()
        except Exception:
            return
        nb = _jpeg_meta_bytes(raw, rep, MX)
        if nb is not None and nb != raw:
            obj.write(nb, filter=pikepdf.Name('/DCTDecode'), decode_parms=obj.get('/DecodeParms'))
            count[0] += 1

    try:
        root_meta = pdf.Root.get('/Metadata')
        root_og = root_meta.objgen if root_meta is not None and root_meta.is_indirect else None
    except Exception:
        root_og = None
    try:
        for obj in pdf.objects:
            if isinstance(obj, (pikepdf.Dictionary, pikepdf.Stream, pikepdf.Array)):
                if isinstance(obj, pikepdf.Dictionary) and obj.get('/Type') == '/Metadata':
                    continue
                if PDF_MATCH != 'legacy' and isinstance(obj, pikepdf.Stream):
                    if obj.get('/Type') == '/Metadata' and obj.objgen != root_og:
                        meta_stream(obj)
                    elif obj.get('/Subtype') == '/Image' and obj.get('/Filter') == '/DCTDecode':
                        jpeg_stream(obj)
                walk(obj)
        if not count[0]:
            return data
        buf = io.BytesIO()
        pdf.save(buf, deterministic_id=True)
        rep.note('pdf_struct_fixed:%d' % count[0])
        return buf.getvalue()
    except Exception as e:
        rep.note('pdf_struct_err:' + str(e)[:50])
        return data


_AGGR_ROT = {(1, 0): 0, (0, -1): 90, (-1, 0): 180, (0, 1): 270}


def _exact_page(page, rep, fitz, M, skip=frozenset()):
    """Identity matches computed on every text line with the matcher's own boundary rules (no substring
    search): each match is removed once (exactly its characters) and its pseudonym written at the same
    baseline.  When an identity is glued into a longer token of a short span (part numbers, file names),
    the whole span is rewritten so the token stays contiguous in the extracted text.  Identities wrapped
    from one line to the next line of the same block are matched across the line break."""
    tp = page.get_textpage()
    added = 0
    inserts = []

    def emit(chars, st, en, pseudo, rot, key):
        nonlocal added
        boxes = [fitz.Rect(chars[i][1]) for i in range(st, en) if chars[i][1] and chars[i][0].strip()]
        if not boxes:
            return
        r = boxes[0]
        for bx in boxes[1:]:
            r |= bx
        if r.is_empty or r.width <= 0 or r.height <= 0:
            return
        horiz = rot in (0, 180)
        added += _redact_box(page, fitz, r, horiz)
        length = r.width if horiz else r.height
        size = chars[st][3].get('size') or (r.height if horiz else r.width)
        inserts.append((fitz.Point(chars[st][2]), pseudo, _fit_fs(fitz, pseudo, size, length), rot))

    def pseudo_for(orig, key):
        return apply_case(orig, PZ.cache[key] if key in PZ.cache else pseudo_det(PZ, key))

    for b in page.get_text('rawdict', textpage=tp).get('blocks', []):
        blines = []
        for ln in b.get('lines', []):
            chars = []
            spans = []
            for sp in ln.get('spans', []):
                a0 = len(chars)
                for ch in sp.get('chars', []):
                    c = ch.get('c', '')
                    if len(c) != 1:
                        c = (c or ' ')[:1]
                    chars.append((c, ch.get('bbox'), ch.get('origin'), sp))
                spans.append((a0, len(chars), sp))
            dx, dy = ln.get('dir', (1, 0))
            rot = _AGGR_ROT.get((round(dx), round(dy)), 0)
            used = [False] * len(chars)
            blines.append((chars, rot, used))
            if not chars:
                continue
            s = ''.join(c[0] for c in chars)
            found = M.find_text(s)
            if skip:
                found = [x for x in found if x[2] not in skip]
            if not found:
                continue
            horiz = rot in (0, 180)
            done = set()
            if PDF_SPAN:
                for st, en, key in found:
                    glued = (st > 0 and (s[st - 1].isalnum() or s[st - 1] in '-_.')) or                             (en < len(s) and (s[en].isalnum() or s[en] in '-_.'))
                    if not glued:
                        continue
                    for a0, a1, sp in spans:
                        if a0 <= st and en <= a1 and (a0, a1) not in done:
                            stext = s[a0:a1]
                            if len(stext.strip()) > 48:
                                break
                            hits = []
                            if skip and any(x[2] in skip for x in M.find_text(stext)):
                                break
                            ns = M.text(stext, hits)
                            if ns == stext or len(ns) != len(stext):
                                break
                            r = fitz.Rect(sp['bbox'])
                            if r.is_empty:
                                break
                            done.add((a0, a1))
                            for x in range(a0, a1):
                                used[x] = True
                            added += _redact_box(page, fitz, r, horiz)
                            length = r.width if horiz else r.height
                            inserts.append((fitz.Point(chars[a0][2]), ns.rstrip(),
                                            _fit_fs(fitz, ns.rstrip(), sp.get('size') or 6, length), rot))
                            rep.hits.extend(hits)
                            break
            for st, en, key in found:
                if any(a0 <= st and en <= a1 for a0, a1 in done):
                    continue
                emit(chars, st, en, pseudo_for(s[st:en], key), rot, key)
                for x in range(st, en):
                    used[x] = True
                rep.hits.append(key)
        # identities wrapped onto the next line of the same block
        for (c1, r1, u1), (c2, r2, u2) in zip(blines, blines[1:]):
            if not c1 or not c2 or r1 != r2:
                continue
            e1 = len(c1)
            while e1 > 0 and not c1[e1 - 1][0].strip():
                e1 -= 1
            b2 = 0
            while b2 < len(c2) and not c2[b2][0].strip():
                b2 += 1
            if e1 == 0 or b2 >= len(c2):
                continue
            s1 = ''.join(c[0] for c in c1[:e1])
            s2 = ''.join(c[0] for c in c2[b2:])
            joined = s1 + ' ' + s2
            for st, en, key in M.find_text(joined):
                if key in skip:
                    continue
                if not (st < e1 < en - 1):
                    continue          # does not cross the line break
                if any(u1[x] for x in range(st, e1)) or any(u2[b2 + x] for x in range(0, en - e1 - 1)):
                    continue
                pseudo = pseudo_for(joined[st:en], key)
                emit(c1, st, e1, pseudo[:e1 - st], r1, key)
                emit(c2, b2, b2 + en - e1 - 1, pseudo[e1 - st + 1:], r2, key)
                for x in range(st, e1):
                    u1[x] = True
                for x in range(b2, b2 + en - e1 - 1):
                    u2[x] = True
                rep.hits.append(key)
    return added, inserts


def _redact_box(page, fitz, r, horiz):
    # shrink across the baseline so neighbouring lines survive
    if horiz:
        rr = fitz.Rect(r.x0 + 0.01 * r.width, r.y0 + 0.22 * r.height, r.x1 - 0.01 * r.width, r.y1 - 0.22 * r.height)
    else:
        rr = fitz.Rect(r.x0 + 0.22 * r.width, r.y0 + 0.01 * r.height, r.x1 - 0.22 * r.width, r.y1 - 0.01 * r.height)
    page.add_redact_annot(rr, fill=False, cross_out=False)
    return 1


def _fit_fs(fitz, text, size, length):
    fs = max(1.0, min(float(size), 60))
    try:
        w = fitz.get_text_length(text, fontname='helv', fontsize=fs)
        if w > length * 1.02 and w > 0:
            fs = max(1.0, fs * length / w)
    except Exception:
        pass
    return fs


def _apply_and_insert(page, fitz, rep, inserts, page_inserts):
    try:
        page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE,
                              graphics=getattr(fitz, 'PDF_REDACT_LINE_ART_NONE', 0),
                              text=getattr(fitz, 'PDF_REDACT_TEXT_REMOVE', 0))
    except Exception as e:
        rep.note('pdf_redact_err:' + str(e)[:60])
        _drop_redact_annots(page, fitz)
        return
    if PDF_PROBE == 'overlay':
        page_inserts[page.number] = inserts
        return
    sh = page.new_shape()
    for base, pseudo, fs, rot in inserts:
        try:
            sh.insert_text(base, pseudo, fontsize=fs, fontname='helv', rotate=rot, color=(0, 0, 0))
        except Exception as e:
            rep.note('pdf_insert_err:' + str(e)[:40])
    sh.commit()


def _cover_instead(page, fitz, inserts):
    """Turn pending redaction rectangles into white boxes drawn on top (no content-stream edit) and write the
    pseudonyms over them."""
    rects = []
    try:
        for an in list(page.annots(types=[fitz.PDF_ANNOT_REDACT]) or []):
            rects.append(fitz.Rect(an.rect))
            page.delete_annot(an)
    except Exception:
        return
    sh = page.new_shape()
    for r in rects:
        pad = 0.15 * min(r.width, r.height)
        sh.draw_rect(fitz.Rect(r.x0 - pad, r.y0 - 3 * pad, r.x1 + pad, r.y1 + 3 * pad))
        sh.finish(color=None, fill=(1, 1, 1), width=0)
    for base, pseudo, fs, rot in inserts:
        try:
            sh.insert_text(base, pseudo, fontsize=fs, fontname='helv', rotate=rot, color=(0, 0, 0))
        except Exception:
            pass
    sh.commit(overlay=True)


def _drop_redact_annots(page, fitz):
    try:
        for an in list(page.annots(types=[fitz.PDF_ANNOT_REDACT]) or []):
            page.delete_annot(an)
    except Exception:
        pass


def _span_page(page, rep, fitz):
    """Replace whole text spans that contain an identity: the span is removed and its full
    de-identified text is written back at the same baseline, so tokens such as part numbers that
    embed an identity stay contiguous in the extracted text (e.g. 'ACME3DSigma-M04' -> 'XXXX3XSigma-M04')."""
    tp = page.get_textpage()
    added = 0
    inserts = []
    rects = []
    for b in page.get_text('dict', textpage=tp).get('blocks', []):
        for ln in b.get('lines', []):
            dx, dy = ln.get('dir', (1, 0))
            rot = _AGGR_ROT.get((round(dx), round(dy)))
            if rot is None or abs(abs(dx) + abs(dy) - 1) > 0.02:
                continue  # skewed text: left to the per-identity path
            for sp in ln.get('spans', []):
                s = sp.get('text', '')
                if not s.strip():
                    continue
                if len(s.strip()) > 48:
                    continue  # prose lines: per-identity path is enough
                # only where an identity is glued to other characters of the same token
                # (part numbers, file names such as 'ACME3DSigma-M04'); stand-alone names use the
                # per-identity path
                glued = False
                for st, en, key in MC.find_text(s):
                    if (st > 0 and (s[st - 1].isalnum() or s[st - 1] in '-_.')) or                             (en < len(s) and (s[en].isalnum() or s[en] in '-_.')):
                        glued = True
                        break
                if not glued:
                    continue
                hits = []
                ns = MC.text(s, hits)
                if ns == s or len(ns) != len(s):
                    continue
                r = fitz.Rect(sp['bbox'])
                if r.is_empty or r.width <= 0 or r.height <= 0:
                    continue
                if rot in (0, 180):
                    length, h = r.width, r.height
                    rr = fitz.Rect(r.x0 + 0.01 * r.width, r.y0 + 0.22 * h, r.x1 - 0.01 * r.width, r.y1 - 0.22 * h)
                else:
                    length, h = r.height, r.width
                    rr = fitz.Rect(r.x0 + 0.22 * h, r.y0 + 0.01 * r.height, r.x1 - 0.22 * h, r.y1 - 0.01 * r.height)
                fs = max(1.0, min(float(sp.get('size') or h), 60))
                try:
                    w = fitz.get_text_length(ns, fontname='helv', fontsize=fs)
                    if w > length * 1.02:
                        fs = max(1.0, fs * length / w)
                except Exception:
                    pass
                page.add_redact_annot(rr, fill=False, cross_out=False)
                added += 1
                inserts.append((fitz.Point(sp['origin']), ns, fs, rot))
                rects.append(r)
                rep.hits.extend(hits)
    return added, inserts, rects


def _aggr_page(page, rep, fitz):
    """Probe only: replace every word with >= 3 Latin letters on the page by its pseudonym."""
    dirs = {}
    tp = page.get_textpage()
    for b in page.get_text('dict', textpage=tp).get('blocks', []):
        for li, ln in enumerate(b.get('lines', [])):
            dx, dy = ln.get('dir', (1, 0))
            dirs[(b.get('number'), li)] = _AGGR_ROT.get((round(dx), round(dy)), 0)
    todo = []
    for x0, y0, x1, y1, w, bno, lno, wno in page.get_text('words', textpage=tp):
        if sum(1 for c in w if 'a' <= c.lower() <= 'z') < 3:
            continue
        r = fitz.Rect(x0, y0, x1, y1)
        if r.is_empty or r.width <= 0 or r.height <= 0:
            continue
        rot = dirs.get((bno, lno), 0)
        pseudo = apply_case(w, pseudo_det(PZ, w))
        if rot in (0, 180):
            h, length = r.height, r.width
            rr = fitz.Rect(x0 + 0.03 * r.width, y0 + 0.22 * h, x1 - 0.03 * r.width, y1 - 0.22 * h)
            base = fitz.Point(x0, y1 - 0.18 * h) if rot == 0 else fitz.Point(x1, y0 + 0.18 * h)
        else:
            h, length = r.width, r.height
            rr = fitz.Rect(x0 + 0.22 * h, y0 + 0.03 * r.height, x1 - 0.22 * h, y1 - 0.03 * r.height)
            base = fitz.Point(x1 - 0.18 * h, y1) if rot == 90 else fitz.Point(x0 + 0.18 * h, y0)
        fs = max(1.5, min(h * 0.85, 60))
        try:
            tw = fitz.get_text_length(pseudo, fontname='helv', fontsize=fs)
            if tw > length * 1.02:
                fs = max(1.0, fs * length / tw)
        except Exception:
            pass
        page.add_redact_annot(rr, fill=False, cross_out=False)
        todo.append((base, pseudo, fs, rot))
        rep.hits.append('probe_word')
    if not todo:
        return 0
    page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE,
                          graphics=getattr(fitz, 'PDF_REDACT_LINE_ART_NONE', 0),
                          text=getattr(fitz, 'PDF_REDACT_TEXT_REMOVE', 0))
    sh = page.new_shape()
    for base, pseudo, fs, rot in todo:
        try:
            sh.insert_text(base, pseudo, fontsize=fs, fontname='helv', rotate=rot, color=(0, 0, 0))
        except Exception:
            pass
    sh.commit()
    return len(todo)


def _tessdata():
    global _TESSDATA
    if _TESSDATA is None:
        import glob
        c = sorted(glob.glob('/usr/share/tesseract-ocr/*/tessdata')) + glob.glob('/usr/share/tessdata')
        c = [x for x in c if os.path.exists(os.path.join(x, 'eng.traineddata'))]
        _TESSDATA = c[0] if c else ''
    return _TESSDATA


def _ocr_cover(doc, rep, fitz, skip=frozenset()):
    """Find identities in the rendered page (OCR) and cover each one with a white box carrying its
    pseudonym.  Only known identities are touched; everything else on the drawing stays visible."""
    td = _tessdata()
    if not td:
        return 0
    os.environ.setdefault('OMP_THREAD_LIMIT', '1')
    n = 0
    done = 0
    for page in doc:
        if done >= OCR_MAX_PAGES:
            break
        try:
            txt = page.get_text()
        except Exception:
            txt = ''
        if page.number >= OCR_PAGES and len(txt.strip()) >= 30:
            continue
        r = page.rect
        long_in = max(r.width, r.height) / 72.0
        dpi = int(min(OCR_DPI, 7000 / max(long_in, 0.1)))
        if dpi < 50:
            continue
        try:
            tp = page.get_textpage_ocr(dpi=dpi, full=True, tessdata=td)
            otxt = page.get_text(textpage=tp)
        except Exception as e:
            rep.note('ocr_err:' + str(e)[:40])
            continue
        done += 1
        origs = {}
        for st, en, key in (MPDF if PDF_MATCH != 'legacy' else MC).find_text(otxt):
            if key in skip:
                continue
            o = otxt[st:en]
            if NL in o or len(o.strip()) < 4:
                continue
            origs[o] = key
        # names that are not in the registry but sit in a title-block field of the rendered page
        # ('Designed by: <name>', copyright owner): same detector as for text layers
        try:
            from . import textnames
            for kind, nm in textnames.candidates(otxt):
                if kind in ('label', 'label_next', 'copyright') and len(nm) >= 4 and nm not in origs                         and MPDF.text(nm) == nm:
                    origs[nm] = None
        except Exception:
            pass
        if not origs:
            continue
        sh = page.new_shape()
        cnt = 0
        for orig, key in origs.items():
            if key is None:
                pseudo = apply_case(orig, pseudo_det(PZ, orig))
                key = 'ocr_name'
            else:
                pseudo = apply_case(orig, PZ.pseudo(key))
            try:
                quads = page.search_for(orig, textpage=tp, quads=True)
            except Exception:
                quads = []
            for q in quads:
                rr = q.rect
                if rr.is_empty or rr.width <= 0 or rr.height <= 0:
                    continue
                vx, vy = q.ur.x - q.ul.x, q.ur.y - q.ul.y
                if abs(vx) >= abs(vy):
                    rot = 0 if vx >= 0 else 180
                    length, h = rr.width, rr.height
                else:
                    rot = 90 if vy < 0 else 270
                    length, h = rr.height, rr.width
                pad = 0.12 * h
                box = fitz.Rect(rr.x0 - pad, rr.y0 - pad, rr.x1 + pad, rr.y1 + pad)
                sh.draw_rect(box)
                sh.finish(color=None, fill=(1, 1, 1), width=0)
                fs = max(1.5, min(h * 0.8, 60))
                try:
                    w = fitz.get_text_length(pseudo, fontname='helv', fontsize=fs)
                    if w > length * 1.02:
                        fs = max(1.0, fs * length / w)
                except Exception:
                    pass
                base = fitz.Point(q.ll.x + (q.ul.x - q.ll.x) * 0.18, q.ll.y + (q.ul.y - q.ll.y) * 0.18)
                sh.insert_text(base, pseudo, fontsize=fs, fontname='helv', rotate=rot, color=(0, 0, 0))
                cnt += 1
                rep.hits.append(key)
        if cnt:
            sh.commit(overlay=True)
            n += cnt
    if n:
        rep.note('pdf_ocr_covered:%d' % n)
    return n


def h_pdf(d, rep, name, skip_pages=frozenset()):
    import pymupdf as fitz
    fitz.TOOLS.mupdf_display_errors(False)
    try:
        doc = fitz.open(stream=d, filetype='pdf')
    except Exception as e:
        rep.note('pdf_open_fail')
        return bytes_text(d, rep, 4)
    if not doc.is_pdf:
        return bytes_text(d, rep, 4)
    if doc.needs_pass:
        if not doc.authenticate(''):
            rep.note('pdf_encrypted')
            return bytes_text(d, rep, 5)
    changed = False
    MX = MPDF if PDF_MATCH != 'legacy' else MP   # PDF contexts: content keys + PDF-only keys
    try:
        md = doc.metadata or {}
        new_md = {}
        author_map = {}
        for k, v in md.items():
            if isinstance(v, str) and k not in ('format', 'encryption'):
                nv = MX.text(v, rep.hits)
                if k == 'author' and nv == v and v.strip() and v.strip().lower() not in PDF_AUTHOR_KEEP:
                    vs = v.strip()
                    nv = v.replace(vs, apply_case(vs, pseudo_det(PZ, vs)))
                    author_map[v] = nv
                    rep.hits.append('pdf_author_field')
                new_md[k] = nv
                if nv != v:
                    changed = True
        if changed:
            doc.set_metadata({k: v for k, v in new_md.items() if k in (
                'author', 'producer', 'creator', 'title', 'subject', 'keywords', 'creationDate', 'modDate', 'trapped')})
    except Exception as e:
        rep.note('pdf_md_err:' + str(e)[:60])
    try:
        x = doc.get_xml_metadata()
        if x:
            nx = MX.text(x, rep.hits)
            for ov, nv in (author_map if 'author_map' in dir() else {}).items():
                nx = nx.replace(ov, nv)
            if nx != x:
                doc.set_xml_metadata(nx)
                changed = True
    except Exception:
        pass
    # visible text
    nred = 0
    wiped = set()
    base_words = {}
    if not skip_pages:
        for page in doc:
            try:
                base_words[page.number] = len(WORD_RE.findall(MX.text(page.get_text())))
            except Exception:
                pass
    page_inserts = {}
    own_skip = frozenset()
    if PDF_KEEP_OWN_STEM and name:
        stem = os.path.splitext(os.path.basename(name))[0]
        if stem.lower() in PAIRED:
            from .pdfids import code_candidates, DOCNUM_RE
            own = [stem.lower()] + [c.lower() for c in code_candidates(stem, allow_digits=True)]
            for m in DOCNUM_RE.finditer(stem):
                own += [m.group(1).lower(), m.group(1).split('-')[0].lower()]
            own_skip = frozenset(own)
            rep.note('pdf_own_stem_kept')
    for page in doc:
        if page.number in skip_pages and PDF_COVER_FALLBACK and PDF_MATCH != 'legacy' and PDF_TEXT_MODE != 'none':
            # text could not be redacted safely (shared form XObjects): cover the identities instead
            try:
                added, inserts = _exact_page(page, rep, fitz, MPDF)
                if added:
                    _cover_instead(page, fitz, inserts)
                    changed = True
            except Exception as e:
                rep.note('pdf_cover_err:' + str(e)[:40])
                _drop_redact_annots(page, fitz)
            continue
        if page.number in skip_pages or PDF_TEXT_MODE == 'none':
            continue
        if PDF_PROBE == 'aggr':
            try:
                n = _aggr_page(page, rep, fitz)
            except Exception as e:
                rep.note('probe_aggr_err:' + str(e)[:40])
                n = 0
            if n:
                nred += n
                changed = True
            continue
        if PDF_MATCH != 'legacy':
            try:
                added, inserts = _exact_page(page, rep, fitz, MPDF, own_skip)
            except Exception as e:
                rep.note('pdf_exact_err:' + str(e)[:40])
                _drop_redact_annots(page, fitz)
                added, inserts = 0, []
            if added:
                _apply_and_insert(page, fitz, rep, inserts, page_inserts)
                nred += added
                changed = True
            continue
        try:
            txt = page.get_text()
        except Exception:
            continue
        found = MC.find_text(txt)
        if not found:
            continue
        origs = {}
        for st, en, key in found:
            origs[txt[st:en]] = key
        added = 0
        inserts = []
        span_rects = []
        if PDF_SPAN:
            try:
                added, inserts, span_rects = _span_page(page, rep, fitz)
            except Exception as e:
                rep.note('pdf_span_err:' + str(e)[:40])
                added, inserts, span_rects = 0, [], []
        for orig, key in origs.items():
            if NL in orig:
                continue
            pseudo = apply_case(orig, PZ.pseudo(key))
            try:
                quads = page.search_for(orig, quads=True)
            except Exception:
                quads = []
            for q in quads:
                r = q.rect
                if r.is_empty or r.width <= 0 or r.height <= 0:
                    continue
                if span_rects:
                    c = fitz.Point((r.x0 + r.x1) / 2, (r.y0 + r.y1) / 2)
                    if any(c in sr for sr in span_rects):
                        continue
                vx, vy = q.ur.x - q.ul.x, q.ur.y - q.ul.y
                if abs(vx) >= abs(vy):
                    rot = 0 if vx >= 0 else 180
                else:
                    rot = 90 if vy < 0 else 270
                length = (vx * vx + vy * vy) ** 0.5
                hx, hy = q.ll.x - q.ul.x, q.ll.y - q.ul.y
                height = (hx * hx + hy * hy) ** 0.5
                if length <= 0 or height <= 0:
                    continue
                fs = max(1.5, min(height * 0.85, 60))
                try:
                    w = fitz.get_text_length(pseudo, fontname='helv', fontsize=fs)
                    if w > length * 1.02:
                        fs = max(1.0, fs * length / w)
                except Exception:
                    pass
                # shrink the removal area a little so neighbouring characters survive
                sx, sy = (0.03, 0.22) if rot in (0, 180) else (0.22, 0.03)
                rr = fitz.Rect(r.x0 + sx * r.width, r.y0 + sy * r.height, r.x1 - sx * r.width, r.y1 - sy * r.height)
                try:
                    page.add_redact_annot(rr, fill=False, cross_out=False)
                    added += 1
                except Exception:
                    continue
                base = fitz.Point(q.ll.x + (q.ul.x - q.ll.x) * 0.18, q.ll.y + (q.ul.y - q.ll.y) * 0.18)
                inserts.append((base, pseudo, fs, rot))
                rep.hits.append(key)
        if added:
            try:
                page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE,
                                      graphics=getattr(fitz, 'PDF_REDACT_LINE_ART_NONE', 0),
                                      text=getattr(fitz, 'PDF_REDACT_TEXT_REMOVE', 0))
                nred += added
                changed = True
            except Exception as e:
                rep.note('pdf_redact_err:' + str(e)[:60])
            if PDF_PROBE == 'overlay':
                page_inserts[page.number] = inserts
                inserts = []
            for base, pseudo, fs, rot in inserts:
                try:
                    page.insert_text(base, pseudo, fontsize=fs, fontname='helv', rotate=rot, color=(0, 0, 0))
                except Exception as e:
                    rep.note('pdf_insert_err:' + str(e)[:40])

        # links
        try:
            for ln in page.get_links():
                u = ln.get('uri')
                if u:
                    nu = MC.text(u, rep.hits)
                    if nu != u:
                        ln['uri'] = nu
                        page.update_link(ln)
                        changed = True
        except Exception:
            pass
        # annotations
        try:
            for an in page.annots() or []:
                info = an.info
                upd = False
                for k in ('content', 'title', 'subject'):
                    v = info.get(k)
                    if v:
                        nv = MC.text(v, rep.hits)
                        if nv != v:
                            info[k] = nv
                            upd = True
                if upd:
                    an.set_info(info)
                    an.update()
                    changed = True
        except Exception:
            pass
    if PDF_MATCH != 'legacy':
        # links and annotations on every page (also pages without identity text); annotation authors
        # (/T of markup annotations) are always pseudonymized, like other author fields
        for page in doc:
            try:
                for ln in page.get_links():
                    u = ln.get('uri')
                    if u:
                        nu = MPDF.text(u, rep.hits)
                        if nu != u:
                            ln['uri'] = nu
                            page.update_link(ln)
                            changed = True
            except Exception:
                pass
            try:
                for an in page.annots() or []:
                    info = an.info
                    upd = False
                    for k in ('content', 'title', 'subject'):
                        v = info.get(k)
                        if not v:
                            continue
                        nv = MPDF.text(v, rep.hits)
                        if k == 'title' and nv == v and v.strip() and v.strip().lower() not in PDF_AUTHOR_KEEP \
                                and re.search(r'[^\W\d_]', v):
                            vs = v.strip()
                            nv = v.replace(vs, apply_case(vs, pseudo_det(PZ, vs)))
                            rep.hits.append('pdf_annot_author')
                        if nv != v:
                            info[k] = nv
                            upd = True
                    if upd:
                        an.set_info(info)
                        an.update()
                        changed = True
            except Exception:
                pass
    if nred:
        rep.note('pdf_text_redactions:%d' % nred)
    if PDF_PROBE == 'overlay':
        for page in doc:
            try:
                page.draw_rect(fitz.Rect(-20000, -20000, 20000, 20000), color=None, fill=(1, 1, 1), width=0, overlay=True)
                for base, pseudo, fs, rot in page_inserts.get(page.number, []):
                    page.insert_text(base, pseudo, fontsize=fs, fontname='helv', rotate=rot, color=(0, 0, 0))
                changed = True
            except Exception as e:
                rep.note('probe_overlay_err:' + str(e)[:40])
    if PDF_OCR and not PDF_PROBE:
        try:
            if _ocr_cover(doc, rep, fitz, own_skip):
                changed = True
        except Exception as e:
            rep.note('ocr_fail:' + str(e)[:40])
    # outline
    try:
        toc = doc.get_toc(simple=False)
        if toc:
            ntoc = []
            ch = False
            for item in toc:
                t = MX.text(item[1], rep.hits)
                if t != item[1]:
                    ch = True
                ntoc.append([item[0], t, item[2]] + item[3:])
            if ch:
                doc.set_toc(ntoc)
                changed = True
    except Exception:
        pass
    # attachments
    try:
        for en in doc.embfile_names():
            info = doc.embfile_info(en)
            fb = doc.embfile_get(en)
            fname = info.get('filename') or en
            nb = process_bytes(fname, fb, rep, depth=1)
            nfname = deid_name(os.path.basename(fname))
            ndesc = MX.text(info.get('desc') or '', rep.hits)
            if nb != fb or nfname != fname or ndesc != (info.get('desc') or ''):
                doc.embfile_upd(en, nb, filename=nfname, ufilename=nfname, desc=ndesc)
                changed = True
                rep.note('pdf_attachment')
    except Exception as e:
        rep.note('pdf_emb_err:' + str(e)[:60])
    if changed:
        try:
            out = doc.tobytes(garbage=1, deflate=True, no_new_id=True)
        except Exception as e:
            rep.note('pdf_save_err:' + str(e)[:60])
            out = d
    else:
        out = d
    doc.close()
    if nred and not skip_pages and out is not d and PDF_PROBE != 'aggr' and PDF_GUARD:
        # check the saved file: redaction inside shared form XObjects can wipe text on other pages
        try:
            chk = fitz.open(stream=out, filetype='pdf')
            for page in chk:
                nb = base_words.get(page.number, 0)
                na = len(WORD_RE.findall(page.get_text()))
                if na < 0.85 * nb and nb - na > 5:
                    wiped.add(page.number)
            chk.close()
        except Exception:
            pass
        if wiped:
            rep.note('pdf_text_kept_whole_doc:%d' % len(wiped))
            return h_pdf(d, rep, name, skip_pages=frozenset(range(100000)))
    return _pdf_struct_pass(out, rep)  # object-level only: no raw byte edits of streams


# ----------------------------------------------------------------------------
# containers

def h_zip(d, rep, name, depth):
    try:
        zin = zipfile.ZipFile(io.BytesIO(d))
        infos = zin.infolist()
    except Exception as e:
        rep.note('zip_bad:' + str(e)[:50])
        return bytes_text(d, rep, 5)
    if any(zi.flag_bits & 1 for zi in infos):
        rep.note('zip_encrypted')
        return bytes_text(d, rep, 5)
    out = io.BytesIO()
    dirty = [False]
    xdir = None
    if any(zi.compress_type not in (0, 8, 12, 14, 93) and not zi.is_dir() for zi in infos):
        # members compressed with methods Python cannot read (e.g. Deflate64 in Fusion .f3d): extract with 7z
        xdir = tempfile.mkdtemp(dir=TMPDIR)
        src = os.path.join(xdir, 'in.zip')
        open(src, 'wb').write(d)
        r = subprocess.run(['7z', 'x', '-y', '-pnopassword', '-o' + os.path.join(xdir, 'x'), src],
                           capture_output=True, stdin=subprocess.DEVNULL, timeout=1800)
        if r.returncode != 0:
            shutil.rmtree(xdir, ignore_errors=True)
            xdir = None
        else:
            rep.note('zip_7z_extract')
            dirty[0] = True     # members are re-compressed with deflate

    if any(zi.compress_type == 93 for zi in infos):
        dirty[0] = True         # zstd members are re-compressed with deflate

    def read_member(zi):
        if zi.compress_type == 93:
            # Zstandard (Fusion 360 archives): decompress the raw member data directly
            import zstandard
            off = zi.header_offset
            nlen, xlen = struct.unpack('<HH', d[off + 26:off + 30])
            start = off + 30 + nlen + xlen
            raw = d[start:start + zi.compress_size]
            return zstandard.ZstdDecompressor().decompress(raw, max_output_size=max(zi.file_size, 1))
        if xdir is not None and zi.compress_type not in (0, 8, 12, 14):
            p = os.path.join(xdir, 'x', zi.filename)
            return open(p, 'rb').read()
        return zin.read(zi)

    try:
        with zipfile.ZipFile(out, 'w', allowZip64=True) as zout:
            if zin.comment:
                zout.comment = MC.bytes(zin.comment, rep.hits)
                if zout.comment != zin.comment:
                    dirty[0] = True
            for zi in infos:
                nname = deid_inner_path(zi.filename)
                if nname != zi.filename:
                    rep.hits.append('zipname')
                    dirty[0] = True
                if zi.comment and MC.bytes(zi.comment) != zi.comment:
                    dirty[0] = True
                zi2 = zipfile.ZipInfo(nname, date_time=zi.date_time)
                zi2.compress_type = zi.compress_type if zi.compress_type in (0, 8, 12, 14) else 8
                zi2.external_attr = zi.external_attr
                zi2.create_system = zi.create_system
                zi2.comment = MC.bytes(zi.comment or b'', rep.hits)
                if zi.is_dir():
                    zout.writestr(zi2, b'')
                    continue
                try:
                    b = read_member(zi)
                except Exception as e:
                    rep.note('zip_member_unreadable:' + str(e)[:40])
                    raise
                nb = process_bytes(zi.filename, b, rep, depth + 1)
                if nb != b:
                    dirty[0] = True
                zout.writestr(zi2, nb, compress_type=zi2.compress_type)
    except Exception as e:
        rep.note('zip_rebuild_fail:' + str(e)[:60])
        return bytes_text(d, rep, 5)
    finally:
        if xdir is not None:
            shutil.rmtree(xdir, ignore_errors=True)
    if not dirty[0]:
        return d
    return out.getvalue()


def h_gz(d, rep, name, depth):
    try:
        flg = d[3]
        mtime = struct.unpack_from('<I', d, 4)[0]
        fname = None
        if flg & 8:
            pos = 10
            if flg & 4:
                xlen = struct.unpack_from('<H', d, 10)[0]
                pos += 2 + xlen
            end = d.index(b'\x00', pos)
            fname = d[pos:end].decode('latin-1')
        inner = gzip.decompress(d)
    except Exception as e:
        rep.note('gz_bad:' + str(e)[:40])
        return bytes_text(d, rep, 5)
    iname = fname or (name[:-3] if name.lower().endswith('.gz') else name[:-4] + '.tar' if name.lower().endswith('.tgz') else name)
    nb = process_bytes(iname, inner, rep, depth + 1)
    nfname = deid_name(fname) if fname else None
    if nb == inner and nfname == fname:
        return d
    out = io.BytesIO()
    with gzip.GzipFile(filename=nfname or '', mode='wb', fileobj=out, mtime=mtime, compresslevel=9) as g:
        g.write(nb)
    res = out.getvalue()
    if not fname:
        # GzipFile writes FNAME only if given; ensure flag cleared when none
        pass
    return res


def h_tar(d, rep, name, depth):
    try:
        tin = tarfile.open(fileobj=io.BytesIO(d), mode='r:')
        members = tin.getmembers()
    except Exception as e:
        rep.note('tar_bad:' + str(e)[:40])
        return bytes_text(d, rep, 5)
    out = io.BytesIO()
    dirty = False
    try:
        with tarfile.open(fileobj=out, mode='w', format=tin.format) as tout:
            for m in members:
                m2 = tarfile.TarInfo(deid_inner_path(m.name))
                m2.mode, m2.mtime, m2.type, m2.uid, m2.gid = m.mode, m.mtime, m.type, m.uid, m.gid
                m2.uname = MP.text(m.uname or '')
                m2.gname = MP.text(m.gname or '')
                if m2.name != m.name or m2.uname != (m.uname or '') or m2.gname != (m.gname or ''):
                    dirty = True
                if m.linkname:
                    m2.linkname = deid_inner_path(m.linkname)
                    dirty = dirty or m2.linkname != m.linkname
                if m.isfile():
                    b = tin.extractfile(m).read()
                    nb = process_bytes(m.name, b, rep, depth + 1)
                    dirty = dirty or nb != b
                    m2.size = len(nb)
                    tout.addfile(m2, io.BytesIO(nb))
                else:
                    tout.addfile(m2)
    except Exception as e:
        rep.note('tar_rebuild_fail:' + str(e)[:60])
        return bytes_text(d, rep, 5)
    if not dirty:
        return d
    return out.getvalue()


def h_Z(d, rep, name, depth):
    try:
        inner = subprocess.run(['uncompress', '-c'], input=d, capture_output=True, check=True, timeout=1800).stdout
    except Exception as e:
        rep.note('Z_bad')
        return bytes_text(d, rep, 5)
    iname = name[:-2]
    nb = process_bytes(iname, inner, rep, depth + 1)
    if nb == inner:
        return d
    try:
        res = subprocess.run(['compress', '-c'], input=nb, capture_output=True, timeout=1800)
        if res.returncode not in (0, 2) or not res.stdout:
            raise RuntimeError('compress failed')
        return res.stdout
    except Exception:
        rep.note('Z_recompress_fail')
        return d


def h_7z(d, rep, name, depth):
    tdir = tempfile.mkdtemp(dir=TMPDIR)
    try:
        src = os.path.join(tdir, 'in.7z')
        open(src, 'wb').write(d)
        ex = os.path.join(tdir, 'x')
        r = subprocess.run(['7z', 'x', '-y', '-pnopassword', '-o' + ex, src], capture_output=True,
                           stdin=subprocess.DEVNULL, timeout=1800)
        if r.returncode != 0:
            if b'encrypted' in (r.stderr + r.stdout).lower() or b'password' in (r.stderr + r.stdout).lower():
                rep.note('7z_encrypted')
                return d
            rep.note('7z_extract_fail')
            return bytes_text(d, rep, 5)
        ox = os.path.join(tdir, 'o')
        os.makedirs(ox)
        dirty = False
        for dp, dn, fn in os.walk(ex):
            for f in fn:
                p = os.path.join(dp, f)
                if os.path.islink(p):
                    continue
                rel = os.path.relpath(p, ex).replace(os.sep, '/')
                nrel = deid_inner_path(rel)
                b = open(p, 'rb').read()
                nb = process_bytes(f, b, rep, depth + 1)
                dirty = dirty or nb != b or nrel != rel
                op = os.path.join(ox, nrel)
                os.makedirs(os.path.dirname(op), exist_ok=True)
                open(op, 'wb').write(nb)
                st = os.stat(p)
                os.utime(op, (st.st_mtime, st.st_mtime))
            for dd in dn:
                rel = os.path.relpath(os.path.join(dp, dd), ex).replace(os.sep, '/')
                os.makedirs(os.path.join(ox, deid_inner_path(rel + '/')), exist_ok=True)
        if not dirty:
            return d
        dst = os.path.join(tdir, 'out.7z')
        r = subprocess.run(['7z', 'a', '-t7z', '-mx=7', dst, '.'], cwd=ox, capture_output=True,
                           stdin=subprocess.DEVNULL, timeout=3600)
        if r.returncode != 0:
            rep.note('7z_pack_fail')
            return bytes_text(d, rep, 5)
        return open(dst, 'rb').read()
    finally:
        shutil.rmtree(tdir, ignore_errors=True)


# ----------------------------------------------------------------------------
# images

def h_png(d, rep, name):
    if d[:8] != b'\x89PNG\r\n\x1a\n':
        return d
    out = [d[:8]]
    pos = 8
    try:
        while pos + 8 <= len(d):
            ln = struct.unpack('>I', d[pos:pos + 4])[0]
            typ = d[pos + 4:pos + 8]
            body = d[pos + 8:pos + 8 + ln]
            crc = d[pos + 8 + ln:pos + 12 + ln]
            if typ in (b'tEXt', b'iTXt', b'eXIf', b'iCCP') and typ != b'iCCP':
                nb = MC.bytes(body, rep.hits)
                if typ == b'tEXt' and nb == body:
                    pass
                if nb != body:
                    crc = struct.pack('>I', zlib.crc32(typ + nb) & 0xffffffff)
                    body = nb
            elif typ == b'zTXt':
                try:
                    k, rest = body.split(b'\x00', 1)
                    txt = zlib.decompress(rest[1:])
                    ntxt = MC.bytes(txt, rep.hits)
                    if ntxt != txt:
                        body = k + b'\x00\x00' + zlib.compress(ntxt)
                        crc = struct.pack('>I', zlib.crc32(typ + body) & 0xffffffff)
                except Exception:
                    pass
            out.append(struct.pack('>I', len(body)) + typ + body + crc)
            pos += 12 + ln
            if typ == b'IEND':
                break
        out.append(d[pos:])
        return b''.join(out)
    except Exception:
        return d


def h_jpeg(d, rep, name):
    if d[:2] != b'\xff\xd8':
        return d
    pos = 2
    out = bytearray(d)
    try:
        while pos + 4 <= len(d):
            if d[pos] != 0xFF:
                break
            marker = d[pos + 1]
            if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                pos += 2
                continue
            if marker == 0xDA:  # start of scan
                break
            ln = struct.unpack('>H', d[pos + 2:pos + 4])[0]
            seg = d[pos + 4:pos + 2 + ln]
            if 0xE0 <= marker <= 0xEF or marker == 0xFE:
                ns = MC.bytes(seg, rep.hits)
                out[pos + 4:pos + 2 + ln] = ns
            pos += 2 + ln
    except Exception:
        pass
    return bytes(out)


# ----------------------------------------------------------------------------
# dispatch

SW_EXT = ('.sldprt', '.sldasm', '.slddrw', '.slddrt', '.sldlfp', '.prtdot', '.asmdot', '.drwdot', '.sldblk', '.sldstd')
OPAQUE_EXT = ('.dwg', '.jt', '.catpart', '.catproduct', '.catdrawing', '.x_b', '.u3d', '.mf1', '.par', '.3ds', '.brp',
              '.smt', '.smb', '.f3d', '.xmt_bin', '.sab', '.ipt', '.iam')


LOGO_RE = re.compile(r'logo|brand|letterhead|watermark|badge|signature', re.I)
LOGO_EXT = ('.png', '.jpg', '.jpeg', '.gif', '.bmp', '.tif', '.tiff', '.svg', '.webp', '.ico')


def h_logo(d, rep, name, ext):
    """Replace a logo image with a neutral placeholder of the same size and format."""
    try:
        if ext == '.svg':
            m = re.search(rb'<svg\b[^>]*>', d[:20000])
            if not m:
                return None
            tag = m.group(0).decode('utf-8', 'replace')
            attrs = {}
            for k in ('width', 'height', 'viewBox'):
                mm = re.search(r'\b%s="([^"]*)"' % k, tag)
                if mm:
                    attrs[k] = mm.group(1)
            a = ' '.join('%s="%s"' % kv for kv in attrs.items())
            vb = attrs.get('viewBox')
            if vb:
                p = vb.replace(',', ' ').split()
                rect = '<rect x="%s" y="%s" width="%s" height="%s" fill="#c8c8c8"/>' % tuple(p[:4]) if len(p) == 4 else \
                    '<rect width="100%" height="100%" fill="#c8c8c8"/>'
            else:
                rect = '<rect width="100%" height="100%" fill="#c8c8c8"/>'
            out = ('<?xml version="1.0" encoding="UTF-8"?>\n<svg xmlns="http://www.w3.org/2000/svg" %s>%s</svg>\n'
                   % (a, rect)).encode('utf-8')
            rep.note('logo_replaced')
            return out
        from PIL import Image, ImageDraw
        im = Image.open(io.BytesIO(d))
        fmt = im.format
        w, h = im.size
        mode = im.mode if im.mode in ('RGB', 'RGBA', 'L', 'LA', 'P', '1', 'CMYK') else 'RGB'
        if mode == 'P':
            mode = 'RGBA' if 'transparency' in im.info else 'RGB'
        fill = {'RGB': (200, 200, 200), 'RGBA': (200, 200, 200, 255), 'L': 200, 'LA': (200, 255), '1': 1,
                'CMYK': (0, 0, 0, 55)}[mode]
        new = Image.new(mode, (w, h), fill)
        dr = ImageDraw.Draw(new)
        if mode in ('RGB', 'RGBA'):
            dr.rectangle([w * 0.1, h * 0.1, w * 0.9, h * 0.9], outline=(120, 120, 120) + ((255,) if mode == 'RGBA' else ()),
                         width=max(1, min(w, h) // 30))
        buf = io.BytesIO()
        kw = {}
        if fmt == 'JPEG':
            kw['quality'] = 90
        new.save(buf, format=fmt or 'PNG', **kw)
        rep.note('logo_replaced')
        return buf.getvalue()
    except Exception as e:
        rep.note('logo_fail:' + str(e)[:40])
        return None


def _lockfile(d, rep):
    """Office/SolidWorks lock file '~$name': starts with the user name of whoever had the file open."""
    m = re.match(rb'([ -~]{2,64})', d)
    if not m:
        return MC.bytes(d, rep.hits, min_len_bin=3)
    v = m.group(1).decode('latin-1')
    vs = v.strip()
    if not vs:
        return d
    nv = MC.text(v, rep.hits)
    if nv == v:
        nv = v.replace(vs, apply_case(vs, pseudo_det(PZ, vs)))
    rep.hits.append('lockfile_field')
    return nv.encode('latin-1', 'replace')[:len(v)].ljust(len(v), b' ') + d[len(v):]


def process_bytes(name, d, rep, depth=0):
    low = name.lower()
    base = os.path.basename(low)
    ext = os.path.splitext(low)[1]
    if not d:
        return d
    if depth > 6:
        return bytes_text(d, rep, 5)
    if base.startswith('~$') and len(d) < 4096:
        return _lockfile(d, rep)
    if LOGO_RE.search(base) and ext in LOGO_EXT:
        nd = h_logo(d, rep, name, ext)
        if nd is not None:
            return nd
    head = d[:8]
    if head[:4] == b'%PDF' or (ext == '.pdf' and b'%PDF' in d[:1024]):
        return h_pdf(d, rep, name)
    if head[:4] == b'PK\x03\x04' or (head[:4] == b'PK\x05\x06'):
        return h_zip(d, rep, name, depth)
    if head[:4] == b'\xd0\xcf\x11\xe0':
        return h_ole(d, rep, name)
    if ext in SW_EXT and SW_MARKER in d[:4096]:
        return h_sw(d, rep, name)
    if head[:2] == b'\x1f\x8b':
        return h_gz(d, rep, name, depth)
    if head[:2] == b'\x1f\x9d':
        return h_Z(d, rep, name, depth)
    if head[:6] == b'7z\xbc\xaf\x27\x1c':
        return h_7z(d, rep, name, depth)
    if head[:4] == b'Rar!':
        rep.note('rar_not_rebuilt')
        return d  # no RAR writer available: keep the archive byte-identical (valid)
    if ext == '.tar' or (len(d) > 512 and d[257:262] == b'ustar'):
        return h_tar(d, rep, name, depth)
    if head[:8] == b'\x89PNG\r\n\x1a\n':
        return h_png(d, rep, name)
    if head[:2] == b'\xff\xd8':
        return h_jpeg(d, rep, name)
    if ext == '.stl' and d[:5].lower() != b'solid' and len(d) >= 84:
        n = struct.unpack_from('<I', d, 80)[0]
        if 84 + 50 * n == len(d):
            return bytes_text(d[:80], rep) + d[80:]
    if ext in OPAQUE_EXT:
        return bytes_text(d, rep, 5)
    # headers of neutral formats carry author/organisation/user fields: metadata matcher there
    hend = 0
    if d[:12] == b'ISO-10303-21':
        hend = d.find(b'ENDSEC;', 0, 200000)
    elif ext in ('.igs', '.iges'):
        m = re.search(rb'\n.{72}D', d[:400000])
        hend = m.start() if m else 0
    elif ext in ('.x_t', '.x_b', '.xmt_txt', '.xmt_bin'):
        hend = d.find(b'**END_OF_HEADER', 0, 20000)
    if hend > 0:
        return MP.bytes(d[:hend], rep.hits, min_len_bin=3) + bytes_text(d[hend:], rep, 3)
    # text-like formats and everything else
    return bytes_text(d, rep, 3)


TMPDIR = None
OUT = None
INP = None


def work(rel):
    rep = Rep(rel)
    t0 = time.time()
    src = os.path.join(INP, rel)
    nrel = deid_relpath(rel)
    dst = os.path.join(OUT, nrel)
    try:
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        if os.path.lexists(dst):
            os.unlink(dst)
        sz = os.path.getsize(src)
        if sz == 0:
            open(dst, 'wb').close()
            rep.note('empty')
        else:
            with open(src, 'rb') as h:
                d = h.read()
            nd = process_bytes(os.path.basename(rel), d, rep, 0)
            with open(dst, 'wb') as h:
                h.write(nd)
            if nd != d:
                rep.note('changed')
    except Exception as e:
        rep.note('EXC:' + repr(e)[:200])
        try:
            if os.path.lexists(dst):
                os.unlink(dst)
            shutil.copyfile(src, dst)
            with open(dst, 'r+b') as h:
                b = h.read()
                nb = MC.bytes(b, rep.hits, min_len_bin=5)
                h.seek(0)
                h.write(nb)
        except Exception:
            pass
    hits = {}
    for k in rep.hits:
        hits[k] = hits.get(k, 0) + 1
    return {'in': rel, 'out': nrel, 'notes': rep.notes, 'hits': hits, 't': round(time.time() - t0, 2)}


def _protect_refs(inp):
    """Names of corpus files that keep their name: inside binary CAD content a match within such a name
    (a component / model reference such as AB_XYZ_003_P_143.PRT) is left alone so the reference still resolves."""
    man = os.path.join(inp, '.get-input', 'manifest.tsv')
    names = set()
    if not os.path.exists(man):
        return frozenset()
    for line in open(man, encoding='utf-8'):
        if line.startswith('#'):
            continue
        base = os.path.basename(line.split('\t')[0])
        if not base or deid_name(base) != base:
            continue
        t = base.lower()
        for _ in range(3):
            if len(t) >= 4:
                names.add(t)
            if '.' not in t:
                break
            t = t.rsplit('.', 1)[0]
    return frozenset(names)


def init_worker(state_path, inp, out, tmpdir):
    global INP, OUT, TMPDIR
    INP, OUT, TMPDIR = inp, out, tmpdir
    load_state(state_path)
    if os.environ.get('DEID_PROTECT_REFS', '1') == '1':
        MC.protect = _protect_refs(inp)


NL = chr(10)


def _fallback_one(args):
    """Last resort for a file that crashed a worker: in-place replacement only."""
    rel, = args
    rep = Rep(rel)
    src = os.path.join(INP, rel)
    nrel = deid_relpath(rel)
    dst = os.path.join(OUT, nrel)
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    b = open(src, 'rb').read()
    nb = b if b[:4] == b'Rar!' else MC.bytes(b, rep.hits, min_len_bin=5)
    if os.path.lexists(dst):
        os.unlink(dst)
    open(dst, 'wb').write(nb)
    rep.note('fallback_inplace')
    return {'in': rel, 'out': nrel, 'notes': rep.notes, 'hits': {}, 't': 0}


def run(state_path, inp, out, report, workers=32, only=None, resume=False, exts=None):
    from concurrent.futures import ProcessPoolExecutor, as_completed
    from concurrent.futures.process import BrokenProcessPool
    tmpdir = os.path.join(os.path.dirname(out.rstrip('/')), 'tmp_deid')
    os.makedirs(tmpdir, exist_ok=True)
    os.makedirs(out, exist_ok=True)
    rels = []
    for dp, dn, fn in os.walk(inp):
        if '.get-input' in dp:
            continue
        for f in fn:
            rel = os.path.relpath(os.path.join(dp, f), inp).replace(os.sep, '/')
            if only and not any(rel.startswith(o) for o in only):
                continue
            if exts and rel.rsplit('.', 1)[-1].lower() not in exts:
                continue
            rels.append(rel)
    done = set()
    if resume and os.path.exists(report):
        for line in open(report, encoding='utf-8'):
            try:
                done.add(json.loads(line)['in'])
            except Exception:
                pass
    rels = [r for r in rels if r not in done]
    sizes = {r: os.path.getsize(os.path.join(inp, r)) for r in rels}
    load_state(state_path)
    outs = {}
    for r in rels:
        o = deid_relpath(r)
        if o in outs:
            print('COLLISION', r, outs[o], o)
        outs[o] = r
    print('files', len(rels), 'already done', len(done), flush=True)
    t0 = time.time()
    n = 0
    crashes = {}
    todo = sorted(rels, key=lambda r: -sizes[r])
    with open(report, 'a' if resume else 'w', encoding='utf-8') as fo:
        while todo:
            big = [r for r in todo if sizes[r] > 150_000_000]
            small = [r for r in todo if sizes[r] <= 150_000_000]
            retry = []
            for batch, nw in ((big, min(6, workers)), (small, workers)):
                if not batch:
                    continue
                ex = ProcessPoolExecutor(nw, initializer=init_worker, initargs=(state_path, inp, out, tmpdir))
                futs = {ex.submit(work, r): r for r in batch}
                try:
                    for f in as_completed(futs):
                        r = futs[f]
                        try:
                            res = f.result()
                        except BrokenProcessPool:
                            retry.append(r)
                            continue
                        except Exception as e:
                            retry.append(r)
                            continue
                        fo.write(json.dumps(res, ensure_ascii=False) + NL)
                        n += 1
                        if n % 5000 == 0:
                            fo.flush()
                            print(n, round(time.time() - t0), flush=True)
                finally:
                    ex.shutdown(wait=False, cancel_futures=True)
            todo = []
            for r in retry:
                crashes[r] = crashes.get(r, 0) + 1
                if crashes[r] >= 2:
                    init_worker(state_path, inp, out, tmpdir)
                    res = _fallback_one((r,))
                    fo.write(json.dumps(res, ensure_ascii=False) + NL)
                    print('FALLBACK', r, flush=True)
                else:
                    todo.append(r)
            if todo:
                print('retrying', len(todo), flush=True)
    print('done', n, round(time.time() - t0))


if __name__ == '__main__':
    cmd = sys.argv[1]
    if cmd == 'prepare':
        prepare(sys.argv[2], sys.argv[3], sys.argv[4])
    elif cmd == 'run':
        args = sys.argv[2:]
        workers = 32
        only = None
        if '--workers' in args:
            i = args.index('--workers'); workers = int(args[i + 1]); del args[i:i + 2]
        if '--only' in args:
            i = args.index('--only'); only = args[i + 1].split(','); del args[i:i + 2]
        exts = None
        if '--exts' in args:
            i = args.index('--exts'); exts = set(args[i + 1].lower().split(',')); del args[i:i + 2]
        resume = '--resume' in args
        args = [a for a in args if a != '--resume']
        run(args[0], args[1], args[2], args[3], workers, only, resume, exts)
