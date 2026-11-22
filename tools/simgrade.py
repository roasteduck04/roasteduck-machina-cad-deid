"""Local simulation of the machina-deid-1 PDF checks (not shipped in attempts).

python tools/simgrade.py --state STATE.json --gt GT1.tsv[,GT2.tsv] --pdfs DIR [--env KEY=VAL ...] [--out RESULT.json]

Answer key: TSV rows  file<TAB>type<TAB>identifier  (identities that literally occur in that sample PDF).
For every sample PDF the current pipeline (deid.pipeline.h_pdf) is run in-process and the output checked:
  removal      identifier absent from every surface we can read locally: page text layer, Info, document and
               object-level XMP streams, annotation /Contents /T /Subj, bookmarks (whitespace/case folded).
  consistency  identifiers occurring in >= 2 sample PDFs: the text found in the output at the identifier's
               original position must be the same non-original string in every file.
  triplets     consistent pairs whose pseudonym also differs from another identity's pseudonym.
  scope        share of the original non-identifier tokens still present (proxy for drawing/model and
               engineering checks; a8 showed these read the text layer).
Calibration (competitor + our a10): server removal ~= recall - 5 points; PDF points ~= 300r + 200r^2 + 100r^3.
OCR is not available locally (no tesseract), so identities that exist only as pixels/outlines are not seen.
"""
import argparse
import collections
import io
import json
import os
import re
import sys
import unicodedata

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))


def fold(s):
    s = unicodedata.normalize('NFKD', s)
    s = ''.join(c for c in s if not unicodedata.combining(c))
    return re.sub(r'\s+', ' ', s).lower().strip()


def present(ident_folded, text_folded):
    return re.search(r'(?<![a-z0-9])' + re.escape(ident_folded) + r'(?![a-z0-9])', text_folded) is not None


def surfaces(data):
    import pymupdf as fitz
    import pikepdf
    out = []
    doc = fitz.open(stream=data, filetype='pdf')
    pages = []
    for p in doc:
        t = p.get_text()
        pages.append(t)
        out.append(t)
        for an in p.annots() or []:
            i = an.info
            out.extend(i.get(k, '') for k in ('content', 'title', 'subject'))
    out.extend(str(v) for v in (doc.metadata or {}).values() if v)
    try:
        out.extend(x[1] for x in doc.get_toc())
    except Exception:
        pass
    try:
        pdf = pikepdf.open(io.BytesIO(data))
        for obj in pdf.objects:
            if isinstance(obj, pikepdf.Stream) and str(obj.get('/Type')) == '/Metadata':
                try:
                    out.append(obj.read_bytes().decode('utf-8', 'replace'))
                except Exception:
                    pass
    except Exception:
        pass
    return doc, pages, fold('\n'.join(out))


def pseudo_at(orig_doc, out_doc, ident):
    """Text found in the output where the identifier sat in the original (first hit)."""
    import pymupdf as fitz
    for pno, page in enumerate(orig_doc):
        try:
            hits = page.search_for(ident)
        except Exception:
            hits = []
        if hits and pno < len(out_doc):
            r = hits[0]
            r = fitz.Rect(r.x0 - 1, r.y0 - 0.5, r.x1 + 1, r.y1 + 0.5)
            t = out_doc[pno].get_text('text', clip=r)
            return fold(t)
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--state', required=True)
    ap.add_argument('--gt', required=True)
    ap.add_argument('--pdfs', required=True)
    ap.add_argument('--env', action='append', default=[])
    ap.add_argument('--out')
    ap.add_argument('--offset', type=float, default=5.0, help='server removal = recall*300 - offset')
    a = ap.parse_args()
    for kv in a.env:
        k, v = kv.split('=', 1)
        os.environ[k] = v
    import pymupdf as fitz
    fitz.TOOLS.mupdf_display_errors(False)
    from deid import pipeline as P
    P.load_state(a.state)
    for kv in a.env:  # module-level switches read at import time
        k, v = kv.split('=', 1)
        if k == 'DEID_PDF_SPAN':
            P.PDF_SPAN = v == '1'
        if k == 'DEID_PDF_OCR':
            P.PDF_OCR = v == '1'
    gt = collections.defaultdict(list)
    for path in a.gt.split(','):
        for i, line in enumerate(open(path, encoding='utf-8')):
            parts = line.rstrip('\n').split('\t')
            if i == 0 and parts[0].lower() == 'file':
                continue
            if len(parts) >= 3 and parts[2].strip():
                gt[parts[0].strip()].append((parts[1].strip().lower(), parts[2].strip()))
    per_type = collections.Counter()
    per_type_ok = collections.Counter()
    missed = collections.defaultdict(list)
    where = collections.defaultdict(dict)   # folded ident -> {file: pseudo}
    scope_tot = scope_ok = 0
    for f, items in sorted(gt.items()):
        p = os.path.join(a.pdfs, f)
        if not os.path.exists(p):
            continue
        data = open(p, 'rb').read()
        rep = P.Rep(f)
        try:
            outb = P.h_pdf(data, rep, f)
        except Exception as e:
            print('ERR', f, e)
            continue
        odoc, opages, _ = surfaces(data)
        ndoc, npages, folded = surfaces(outb)
        idents = set()
        for typ, ident in items:
            fi = fold(ident)
            if len(fi) < 2:
                continue
            idents.add(ident)
            per_type[typ] += 1
            if present(fi, folded):
                missed[typ].append((f, ident))
            else:
                per_type_ok[typ] += 1
            ps = pseudo_at(odoc, ndoc, ident)
            if ps is not None:
                where[fi][f] = ps
        # scope damage: original tokens outside identifiers
        id_tokens = set(t for ident in idents for t in ident.split())
        for po, pn in zip(opages, npages):
            oc = collections.Counter(t for t in po.split() if t not in id_tokens)
            nc = collections.Counter(pn.split())
            for t, c in oc.items():
                scope_tot += c
                scope_ok += min(c, nc.get(t, 0))
    tot = sum(per_type.values())
    ok = sum(per_type_ok.values())
    r = ok / max(tot, 1)
    pairs = cons = 0
    pseudo_of = {}
    for fi, d in where.items():
        if len(d) >= 2:
            vals = list(d.values())
            for i in range(len(vals) - 1):
                pairs += 1
                if vals[i] and vals[i] == vals[i + 1] and vals[i] != fi:
                    cons += 1
            pseudo_of[fi] = vals[0]
    trip_tot = trip_ok = 0
    keys = sorted(pseudo_of)
    for i, fi in enumerate(keys):
        d = list(where[fi].values())
        if len(d) < 2:
            continue
        other = keys[(i + 1) % len(keys)]
        trip_tot += 1
        if d[0] and d[0] == d[1] and d[0] != fi and pseudo_of.get(other) != d[0]:
            trip_ok += 1
    res = {
        'identities': tot, 'removed': ok, 'recall': round(r, 4),
        'recall_by_type': {t: '%d/%d' % (per_type_ok[t], per_type[t]) for t in sorted(per_type)},
        'consistency': '%d/%d' % (cons, pairs), 'triplets': '%d/%d' % (trip_ok, trip_tot),
        'scope_kept': round(scope_ok / max(scope_tot, 1), 4),
        'predicted_pdf_points': {
            'removal': round(300 * r - a.offset),
            'consistency': round(200 * r * r), 'triplets': round(100 * r ** 3)},
        'missed_examples': {t: v for t, v in missed.items()},
    }
    print(json.dumps({k: v for k, v in res.items() if k != 'missed_examples'}, indent=1))
    for t, v in sorted(missed.items(), key=lambda x: -len(x[1])):
        print('MISSED %-8s %4d  e.g. %s' % (t, len(v), '; '.join(i for _, i in v[:8])))
    if a.out:
        json.dump(res, open(a.out, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)


if __name__ == '__main__':
    main()
