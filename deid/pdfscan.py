# Parallel scan of all PDFs: metadata, xmp, embedded files, text (all pages, capped)
import os, sys, json
from multiprocessing import Pool
import pymupdf as fitz
fitz.TOOLS.mupdf_display_errors(False)
root = sys.argv[1]; out = sys.argv[2]
def work(p):
    r = {'p': os.path.relpath(p, root)}
    try:
        doc = fitz.open(p)
        if not doc.is_pdf:
            r['err'] = 'notpdf'; return r
        r['md'] = {k: v for k, v in (doc.metadata or {}).items() if v}
        try: r['xmp'] = (doc.get_xml_metadata() or '')[:4000]
        except Exception: pass
        try: r['emb'] = doc.embfile_names()
        except Exception: pass
        r['np'] = doc.page_count
        txt = []
        for i, pg in enumerate(doc):
            if i >= 30: break
            txt.append(pg.get_text())
        r['t'] = '\f'.join(txt)[:200000]
    except Exception as e:
        r['err'] = str(e)[:200]
    return r
files = []
for dp, dn, fn in os.walk(root):
    if '.get-input' in dp: continue
    for f in fn:
        if f.lower().endswith('.pdf'): files.append(os.path.join(dp, f))
with Pool(32) as pool, open(out, 'w') as fo:
    for r in pool.imap_unordered(work, files, chunksize=8):
        fo.write(json.dumps(r, ensure_ascii=False) + '\n')
print('done', len(files))
