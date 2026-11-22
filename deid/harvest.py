"""Corpus-wide harvest of identity-bearing fields.

Walks the input tree (and inside zip-like containers), extracts values from
fields that carry identities (authors, companies, users, paths, e-mails, ...)
and writes one JSON line per (file, field, value).  The output feeds the
identity registry; nothing here modifies files.
"""
import io
import json
import os
import re
import struct
import sys
import zipfile
import zlib
from multiprocessing import Pool

import olefile

EMAIL_RE = re.compile(rb'[A-Za-z0-9._%+\-]{1,64}@[A-Za-z0-9.\-]{1,100}\.[A-Za-z]{2,12}')
URL_RE = re.compile(rb'(?:https?|ftp)://[A-Za-z0-9.\-]{3,100}')
WINUSER_RE = re.compile(rb'[A-Za-z]:[\\/]+(?:Users|Documents and Settings|home)[\\/]+([^\\/\x00"\'<>|:*?\r\n]{1,40})', re.I)
UNIXUSER_RE = re.compile(rb'/(?:home|Users)/([A-Za-z0-9._\- ]{1,40})/')
WINUSER16_RE = re.compile(rb'[A-Za-z]\x00:\x00[\\/]\x00(?:U\x00s\x00e\x00r\x00s\x00|D\x00o\x00c\x00u\x00m\x00e\x00n\x00t\x00s\x00 \x00a\x00n\x00d\x00 \x00S\x00e\x00t\x00t\x00i\x00n\x00g\x00s\x00)[\\/]\x00((?:[^\\/\x00]\x00){1,40})', re.I)

SW_MARKER = b'\x14\x00\x06\x00\x08\x00'


def nib(b):
    return bytes(((x >> 4) | ((x & 15) << 4)) & 255 for x in b)


def sw_streams(d):
    """Yield (offset, name, data) for valid deflate records of a SW2015+ file."""
    for m in re.finditer(re.escape(SW_MARKER), d):
        ps = m.start()
        if ps + 26 > len(d):
            continue
        ts, crc, cs, us, nl = struct.unpack_from('<IIIII', d, ps + 6)
        if nl > 400 or cs > len(d):
            continue
        name = nib(d[ps + 26: ps + 26 + nl])
        start = ps + 26 + nl
        try:
            out = zlib.decompress(d[start:start + cs], -15)
        except Exception:
            continue
        if zlib.crc32(out) != crc or len(out) != us:
            continue
        yield ps, name.decode('latin-1'), out


def generic_scan(b, emit, where):
    for m in EMAIL_RE.finditer(b):
        emit(where, 'email', m.group(0).decode('latin-1'))
    for m in URL_RE.finditer(b):
        emit(where, 'url', m.group(0).decode('latin-1'))
    for m in WINUSER_RE.finditer(b):
        emit(where, 'winuser', m.group(1).decode('latin-1'))
    for m in UNIXUSER_RE.finditer(b):
        emit(where, 'unixuser', m.group(1).decode('latin-1'))
    for m in WINUSER16_RE.finditer(b):
        try:
            emit(where, 'winuser16', m.group(1).decode('utf-16le'))
        except Exception:
            pass


XML_FIELD_RE = re.compile(rb'<(dc:creator|cp:lastModifiedBy|Company|Manager|dc:publisher|cp:contributor|Author|Creator)>([^<]{1,300})</', re.I)
SWPROP_RE = re.compile(rb'<property name="([^"]{1,120})" pid="\d+"><vt:lpwstr>([^<]{1,300})</vt:lpwstr>|<property name="([^"]{1,120})" pid="\d+"><vt:lpstr>([^<]{1,300})</vt:lpstr>')


def scan_xmlish(b, emit, where):
    for m in XML_FIELD_RE.finditer(b):
        emit(where, 'xml:' + m.group(1).decode('latin-1'), m.group(2).decode('utf-8', 'replace'))
    for m in SWPROP_RE.finditer(b):
        k = (m.group(1) or m.group(3)).decode('utf-8', 'replace')
        v = (m.group(2) or m.group(4)).decode('utf-8', 'replace')
        emit(where, 'swprop:' + k, v)


def u16strings(b, minlen=4):
    for m in re.finditer(rb'(?:[\x20-\x7e]\x00){%d,200}' % minlen, b):
        yield m.group(0).decode('utf-16le')


def scan_sw(d, emit, where):
    n = 0
    for ps, name, out in sw_streams(d):
        n += 1
        if name.startswith('docProps/') or name.startswith('swXmlContents') or name.endswith('.xml'):
            scan_xmlish(out, emit, where + '::' + name)
        if name in ('Header2', 'Contents/Config-0-ModelHeader') or name.startswith('_MO_VERSION') and 'Biography' in name:
            for s in u16strings(out, 3):
                emit(where + '::' + name, 'swhdr', s)
        generic_scan(out, emit, where + '::' + name)
    return n


STEP_FN_RE = re.compile(rb"FILE_NAME\s*\((.*?)\)\s*;", re.S)
STEP_STR_RE = re.compile(rb"'((?:[^']|'')*)'")
STEP_ENT_RE = re.compile(rb"=\s*(PERSON|ORGANIZATION|PERSON_AND_ORGANIZATION_ROLE|ADDRESS|ORGANIZATIONAL_ADDRESS|PERSONAL_ADDRESS|APPROVAL_PERSON_ORGANIZATION|PRODUCT)\s*\((.*?)\)\s*;", re.S)


def split_step_args(s):
    # returns list of top-level args (strings kept raw)
    out, depth, cur, inq = [], 0, b'', False
    i = 0
    while i < len(s):
        c = s[i:i + 1]
        if inq:
            cur += c
            if c == b"'":
                if s[i + 1:i + 2] == b"'":
                    cur += b"'"; i += 1
                else:
                    inq = False
        elif c == b"'":
            inq = True; cur += c
        elif c == b'(':
            depth += 1; cur += c
        elif c == b')':
            depth -= 1; cur += c
        elif c == b',' and depth == 0:
            out.append(cur.strip()); cur = b''
        else:
            cur += c
        i += 1
    out.append(cur.strip())
    return out


def scan_step(d, emit, where):
    head = d[:20000]
    m = STEP_FN_RE.search(head)
    if m:
        args = split_step_args(m.group(1))
        labels = ['fn_name', 'fn_time', 'fn_author', 'fn_org', 'fn_preproc', 'fn_origsys', 'fn_auth']
        for lab, a in zip(labels, args):
            for s in STEP_STR_RE.findall(a):
                v = s.decode('latin-1').strip()
                if v and lab != 'fn_time':
                    emit(where, 'step:' + lab, v)
    m = re.search(rb"FILE_DESCRIPTION\s*\((.*?)\)\s*;", head, re.S)
    if m:
        for s in STEP_STR_RE.findall(m.group(1)):
            emit(where, 'step:desc', s.decode('latin-1'))
    for m in STEP_ENT_RE.finditer(d):
        ent = m.group(1).decode()
        strs = [s.decode('latin-1') for s in STEP_STR_RE.findall(m.group(2))]
        if ent == 'PRODUCT':
            if strs:
                emit(where, 'step:PRODUCT', ' | '.join(strs[:3]))
        else:
            emit(where, 'step:' + ent, ' | '.join(strs))
    generic_scan(d, emit, where)


def scan_ole(path_or_bytes, emit, where):
    try:
        ole = olefile.OleFileIO(path_or_bytes)
    except Exception as e:
        emit(where, 'err', 'ole:' + str(e)[:80])
        return
    try:
        for entry in ole.listdir(streams=True, storages=False):
            name = '/'.join(entry)
            if entry[-1].startswith('\x05'):
                try:
                    props = ole.getproperties(entry, convert_time=True)
                except Exception:
                    continue
                for pid, val in props.items():
                    if isinstance(val, bytes):
                        try:
                            val = val.decode('utf-8')
                        except Exception:
                            val = val.decode('latin-1')
                    if isinstance(val, str) and val.strip():
                        emit(where + '::' + name, 'oleprop:%s:%s' % (entry[-1].strip('\x05'), pid), val.strip('\x00').strip())
            else:
                try:
                    sz = ole.get_size(entry)
                    if sz > 30_000_000:
                        continue
                    data = ole.openstream(entry).read()
                except Exception:
                    continue
                generic_scan(data, emit, where + '::' + name)
    finally:
        ole.close()


IGES_H_RE = re.compile(rb'(\d+)H')


def scan_iges(d, emit, where):
    # collect global section (G lines), columns 1-72
    lines = d.split(b'\n')
    g = b''
    for ln in lines[:200]:
        ln = ln.rstrip(b'\r')
        if len(ln) >= 73 and ln[72:73] == b'G':
            g += ln[:72]
        elif len(ln) >= 73 and ln[72:73] == b'S':
            emit(where, 'iges:start', ln[:72].decode('latin-1').strip())
    if not g:
        return
    # parse parameters with delimiters , ;  and Hollerith strings
    pd, rd = b',', b';'
    i, fields, cur = 0, [], b''
    # first two may redefine delims
    while i < len(g):
        m = re.match(rb'\s*(\d+)H', g[i:])
        if m:
            n = int(m.group(1)); st = i + m.end()
            fields.append(g[st:st + n]); i = st + n
            # skip to delimiter
            while i < len(g) and g[i:i + 1] not in (pd, rd):
                i += 1
            i += 1
            continue
        j = i
        while j < len(g) and g[j:j + 1] not in (pd, rd):
            j += 1
        fields.append(g[i:j].strip()); i = j + 1
    labels = {2: 'sender_pid', 3: 'filename', 4: 'native_sys', 5: 'preproc', 11: 'receiver_pid', 20: 'author', 21: 'org'}
    for idx, lab in labels.items():
        if idx < len(fields) and fields[idx]:
            emit(where, 'iges:' + lab, fields[idx].decode('latin-1'))
    generic_scan(d[:200000], emit, where)


PS_HDR_RE = re.compile(rb'(SITE|USER|KEY|FILE|FRU|APPL|MC|MC_ID)=([^;\r\n]{1,300});')


def scan_parasolid(d, emit, where):
    for m in PS_HDR_RE.finditer(d[:5000]):
        emit(where, 'xt:' + m.group(1).decode(), m.group(2).decode('latin-1'))
    generic_scan(d[:100000], emit, where)


def scan_fcstd_member(name, b, emit, where):
    if name.endswith('Document.xml'):
        for m in re.finditer(rb'<Property name="(CreatedBy|LastModifiedBy|Company|Comment|Id|License|LicenseURL|Label|FileName|Meta)"[^>]*>\s*<(?:String|Map)[^>]*?value="([^"]{0,400})"', b):
            emit(where, 'fc:' + m.group(1).decode(), m.group(2).decode('utf-8', 'replace'))
    generic_scan(b, emit, where)


def scan_3mf_member(name, b, emit, where):
    if name.lower().endswith('.model'):
        for m in re.finditer(rb'<metadata name="([^"]+)"[^>]*>([^<]{0,400})</metadata>', b[:200000]):
            emit(where, '3mf:' + m.group(1).decode('latin-1'), m.group(2).decode('utf-8', 'replace'))
    generic_scan(b[:2_000_000], emit, where)


def scan_zip(d, emit, where, depth=0, kind='zip'):
    try:
        zf = zipfile.ZipFile(io.BytesIO(d))
    except Exception as e:
        emit(where, 'err', 'zip:' + str(e)[:60])
        return
    for zi in zf.infolist():
        emit(where, 'zipname', zi.filename)
        if zi.is_dir() or zi.file_size > 200_000_000:
            continue
        try:
            b = zf.read(zi)
        except Exception as e:
            emit(where, 'err', 'zipread:' + str(e)[:60])
            continue
        scan_bytes(zi.filename, b, emit, where + '!' + zi.filename, depth + 1)


def scan_bytes(name, d, emit, where, depth=0):
    low = name.lower()
    ext = os.path.splitext(low)[1]
    if not d:
        return
    if os.path.basename(low).startswith('~$') and len(d) < 200:
        emit(where, 'lockfile', d.split(b'\x00')[0].decode('latin-1'))
        return
    if d[:4] == b'PK\x03\x04' and depth < 3:
        if ext == '.fcstd':
            try:
                zf = zipfile.ZipFile(io.BytesIO(d))
                for zi in zf.infolist():
                    emit(where, 'zipname', zi.filename)
                    scan_fcstd_member(zi.filename, zf.read(zi), emit, where + '!' + zi.filename)
            except Exception as e:
                emit(where, 'err', 'fc:' + str(e)[:60])
            return
        if ext == '.3mf':
            try:
                zf = zipfile.ZipFile(io.BytesIO(d))
                for zi in zf.infolist():
                    emit(where, 'zipname', zi.filename)
                    if zi.file_size < 300_000_000:
                        scan_3mf_member(zi.filename, zf.read(zi), emit, where + '!' + zi.filename)
            except Exception as e:
                emit(where, 'err', '3mf:' + str(e)[:60])
            return
        scan_zip(d, emit, where, depth)
        return
    if d[:4] == b'\xd0\xcf\x11\xe0':
        scan_ole(d, emit, where)
        return
    if ext in ('.sldprt', '.sldasm', '.slddrw', '.slddrt', '.sldlfp', '.prtdot', '.asmdot', '.drwdot') and SW_MARKER in d[:4096]:
        scan_sw(d, emit, where)
        return
    if d[:12] == b'ISO-10303-21' or ext in ('.step', '.stp'):
        scan_step(d, emit, where)
        return
    if ext in ('.igs', '.iges'):
        scan_iges(d, emit, where)
        return
    if ext in ('.x_t', '.x_b', '.xmt_txt', '.xmt_bin') or d[:30].startswith(b'**ABCDEFGHIJ'):
        scan_parasolid(d, emit, where)
        return
    if ext in ('.svg', '.xml', '.ipj', '.urdf', '.html', '.htm', '.dae', '.kicad_pcb'):
        if d[:2] in (b'\xff\xfe',):
            try:
                d = d.decode('utf-16').encode('utf-8')
            except Exception:
                pass
        scan_xmlish(d, emit, where)
        for m in re.finditer(rb'(?:inkscape:export-filename|sodipodi:docname|inkscape:version)="([^"]{1,300})"', d[:500000]):
            emit(where, 'svgattr', m.group(1).decode('utf-8', 'replace'))
        generic_scan(d[:5_000_000], emit, where)
        return
    if ext == '.stl':
        if d[:5].lower() == b'solid':
            emit(where, 'stl:solid', d[:200].split(b'\n')[0].decode('latin-1'))
        else:
            emit(where, 'stl:hdr', d[:80].decode('latin-1').strip('\x00 '))
        return
    if ext == '.pdf':
        return  # handled by pdf scan
    if ext in ('.png', '.jpg', '.jpeg', '.tif', '.tiff', '.gif', '.bmp'):
        return  # handled by exiftool
    # generic: text headers + generic regexes on first 4 MB
    generic_scan(d[:4_000_000], emit, where)
    if ext == '.sat' or ext == '.sab':
        emit(where, 'sat:hdr', d[:200].split(b'\n')[1].decode('latin-1') if b'\n' in d[:200] else '')
    if ext in ('.obj', '.wrl', '.dxf', '.unv', '.neu', '.ps', '.eps') or re.match(rb'#UGC', d[:5]):
        for m in re.finditer(rb'(?i)(?:created by|author|designer|drawn by|copyright|\(c\)|company)[^\r\n]{0,120}', d[:2_000_000]):
            emit(where, 'textline', m.group(0).decode('latin-1'))


def work(path):
    rows = []
    def emit(where, field, value):
        if value is None:
            return
        value = str(value)
        if not value.strip():
            return
        rows.append((where, field, value[:500]))
    rel = os.path.relpath(path, ROOT)
    try:
        sz = os.path.getsize(path)
        if sz > 1_500_000_000:
            emit(rel, 'err', 'too_big')
        else:
            with open(path, 'rb') as h:
                d = h.read()
            scan_bytes(os.path.basename(path), d, emit, rel)
    except Exception as e:
        emit(rel, 'err', 'exc:' + repr(e)[:200])
    return rows


ROOT = None


def main():
    global ROOT
    ROOT = sys.argv[1]
    out = sys.argv[2]
    files = []
    for dp, dn, fn in os.walk(ROOT):
        if '.get-input' in dp:
            continue
        for f in fn:
            files.append(os.path.join(dp, f))
    files.sort(key=lambda p: -os.path.getsize(p))
    n = 0
    with Pool(32) as pool, open(out, 'w') as fo:
        for rows in pool.imap_unordered(work, files, chunksize=4):
            n += 1
            for r in rows:
                fo.write(json.dumps(r, ensure_ascii=False) + '\n')
            if n % 10000 == 0:
                print(n, flush=True)
    print('done', n)


if __name__ == '__main__':
    main()
