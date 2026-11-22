"""Structural verification of an output corpus against the input.

python -m deid.verify <report.jsonl> <input_dir> <output_dir> [sample_n]
Checks that every output file opens the same way the input did (PDF page
count, ZIP member count, OLE stream list, SolidWorks stream count/CRCs,
STEP header, image decode) and reports regressions.
"""
import io
import json
import os
import random
import re
import struct
import sys
import zipfile
import zlib
from multiprocessing import Pool

SW_MARKER = b'\x14\x00\x06\x00\x08\x00'


def sw_count(d):
    n = 0
    for m in re.finditer(re.escape(SW_MARKER), d):
        ps = m.start()
        if ps + 26 > len(d):
            continue
        ts, crc, cs, us, nl = struct.unpack_from('<IIIII', d, ps + 6)
        nl &= 0xFFFF
        if nl > 400 or cs > len(d):
            continue
        st = ps + 26 + nl
        try:
            out = zlib.decompress(d[st:st + cs], -15)
        except Exception:
            continue
        if zlib.crc32(out) == crc and len(out) == us:
            n += 1
    return n


def sig(path):
    try:
        d = open(path, 'rb').read()
    except Exception as e:
        return ('missing',)
    low = path.lower()
    if not d:
        return ('empty',)
    try:
        if d[:4] == b'%PDF':
            import pymupdf as fitz
            fitz.TOOLS.mupdf_display_errors(False)
            doc = fitz.open(stream=d, filetype='pdf')
            return ('pdf', doc.page_count)
        if d[:4] == b'PK\x03\x04':
            z = zipfile.ZipFile(io.BytesIO(d))
            bad = z.testzip()
            return ('zip', len(z.infolist()), bad is None)
        if d[:4] == b'\xd0\xcf\x11\xe0':
            import olefile
            o = olefile.OleFileIO(d)
            return ('ole', len(o.listdir()))
        if SW_MARKER in d[:4096] and low.endswith(('.sldprt', '.sldasm', '.slddrw', '.slddrt', '.sldlfp', '.prtdot', '.asmdot', '.drwdot')):
            return ('sw', sw_count(d))
        if d[:12] == b'ISO-10303-21':
            return ('step', len(d), d.count(b';'))
        if d[:8] == b'\x89PNG\r\n\x1a\n' or d[:2] == b'\xff\xd8':
            from PIL import Image
            im = Image.open(io.BytesIO(d))
            im.load()
            return ('img', im.size)
        return ('raw', len(d))
    except Exception as e:
        return ('ERR', str(e)[:60])


def check(pair):
    a, b = pair
    sa, sb = sig(a), sig(b)
    return (a, b, sa, sb)


def main():
    rep, inp, out = sys.argv[1], sys.argv[2], sys.argv[3]
    n = int(sys.argv[4]) if len(sys.argv) > 4 else 0
    rows = [json.loads(l) for l in open(rep, encoding='utf-8')]
    if n:
        random.seed(1)
        rows = random.sample(rows, min(n, len(rows)))
    pairs = [(os.path.join(inp, r['in']), os.path.join(out, r['out'])) for r in rows]
    bad = 0
    with Pool(32) as p:
        for a, b, sa, sb in p.imap_unordered(check, pairs, chunksize=8):
            if sa != sb and not (sa[0] == 'raw' and sb[0] == 'raw') and not (sa[0] == 'step' and sb[0] == 'step' and sa[2] == sb[2]):
                bad += 1
                if bad <= 40:
                    print('DIFF', os.path.relpath(a, inp)[:120], sa, sb)
    print('checked', len(pairs), 'diffs', bad)


if __name__ == '__main__':
    main()
