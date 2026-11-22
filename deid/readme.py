"""Generate README.txt (run instructions, limitations, source defects) from a run report.

python -m deid.readme <report.jsonl> <input_dir> <out_readme> [attempt]
Only final (de-identified) paths are written.
"""
import collections
import json
import os
import sys

TEMPLATE = """MACHINA DE-ID - submission README
=================================

Contents of this attempt
  corpus/      de-identified copy of the full input (every input file has exactly one output file;
               same folder structure, same file extensions, identifying path parts replaced)
  code/        complete pipeline source (deid/), dependency lock (requirements.txt) and run script
  README.txt   this file

How to run (exact)
  Ubuntu 22.04, Python 3.10, 32 cores, ~450 GB disk.
    apt-get install -y python3-venv p7zip-full ncompress wamerican tesseract-ocr libimage-exiftool-perl
    python3 -m venv /opt/venv && /opt/venv/bin/pip install -r code/requirements.txt
    bash get-input.sh --dest /data/input                      # official downloader
    bash code/run.sh /data/input /data/work /data/out/corpus   # scans, registry, rewrite, verify, README
  run.sh runs, in order: deid.harvest (identity fields of every format), deid.pdfscan / exiftool /
  deid.svgscan (PDF text + metadata, image EXIF/XMP, SVG metadata), deid.registry, deid.pipeline prepare,
  deid.pipeline run, deid.verify, deid.readme.  The registry/state/report files contain the original ->
  pseudonym mapping and are private: they are written outside the submission folder and are not included.
  Identity lists shipped with the code (curated.dat, reviewed.dat, ids_pdf.dat, vendors.dat) are stored
  compressed (base64 + zlib JSON) so the code folder carries no plain-text identifiers.

Method
  1. Harvest: every file (and every member of zip/FCStd/3MF containers) is parsed for identity-bearing
     fields: STEP FILE_NAME author/organization/authorization and PERSON/ORGANIZATION entities, IGES
     global-section author/organization, Parasolid header USER/SITE, SolidWorks docProps (core/app/custom
     properties, SW-Author, SW-Last Saved By, Company, designer fields), Inventor/Solid Edge/legacy
     SolidWorks OLE property sets, FreeCAD Document.xml CreatedBy/LastModifiedBy/Company, 3MF metadata,
     PDF Info/XMP author fields, lock files, user names inside embedded Windows/Unix paths, e-mails, URLs.
  2. Registry: values are split into person/organisation/account/e-mail/domain identities, generic
     placeholders ("Author", "Unknown", software vendor defaults, ...) are excluded, and identities that
     live in folder names (repository owners, organisations, people, web domains) are added.
  3. Pseudonyms: one global, deterministic mapping.  Every identity is rewritten word by word, so the same
     identity always gets the same pseudonym in every file, path and format, and distinct identities stay
     distinct.  Pseudonyms are pronounceable name-like words with exactly the same length, separators and
     letter case pattern as the original (e.g. an author field keeps a person-like value; e-mails stay
     e-mails, domains keep their TLD).  Equal length lets binary CAD containers be edited in place.
  4. Rewrite (same identity map everywhere):
     - text CAD (STEP/IGES/SAT/Parasolid text/OBJ/VRML/DXF/SVG/...): in-place, length-preserving; geometry,
       units, tolerances, entity ids and numbers are never touched (matches require word boundaries).
     - SolidWorks 2015+: each compressed stream is inflated, de-identified, re-deflated to exactly its
       original compressed size and its CRC fields updated; stream layout and offsets are unchanged.
     - OLE files (Inventor ipt/iam/idw/ipn, Solid Edge, SolidWorks <2015): property sets are parsed
       (standard Summary/DocumentSummary, Inventor Summary, Inventor Document Summary, Design Tracking
       Properties/Control, Solid Edge summary); every saved-account / author / designer / engineer /
       checker / company value gets its pseudonym (generic account names such as 'Administrator' included),
       and the same values are removed from every other stream of the file.  Legacy SolidWorks '__ZLB'
       compressed sub-streams are inflated, de-identified and re-deflated to their exact original size.
       All streams keep their size, so the compound-file layout and previews are untouched.
     - SolidWorks 2015+ docProps author / last-saved-by / company fields and PDF Author fields are always
       given a pseudonym (field-level), consistently with the rest of the corpus.
     - Output is deterministic: identical input files (duplicates, archive members and their .extracted
       copies) produce byte-identical output files.
     - PDF: Info/XMP metadata, bookmarks, links, annotations and attachments are de-identified; visible text
       occurrences are removed and the pseudonym written at the same position (when an identity is part of
       a longer token such as a part number, the whole token is rewritten so it stays contiguous).
       Every page is also rendered and OCR'd (tesseract): identities that exist only as pixels or vector
       outlines (scanned drawings, logos, outlined title blocks) are covered by a white box carrying the
       pseudonym; the rest of the drawing is left untouched.  Matching is done per text line with the
       same word-boundary rules as everywhere else (no substring hits), identities wrapped over two lines
       are handled, and inside PDFs part / drawing / document numbers, third-party vendor names, postal
       addresses and phone numbers are also replaced (consistently; file names keep them so that drawing
       and model files still pair up).  Part numbers come from file names, drawing-number labels and
       document-number patterns; other codes in page text are replaced only when they belong to the same
       numbering family as the project's own files, so component values, pin/net labels, IC and catalogue
       numbers and standards stay as engineering information.  Object-level XMP metadata, annotation authors and comments, and
       EXIF/XMP segments of embedded JPEG images are de-identified as well.
     - zip/FCStd/3MF/F3D/F3Z/ODG, gzip, tar, .Z and 7z archives are unpacked, every member is processed
       recursively and the archive is rebuilt with de-identified member names.
     - PNG text chunks and JPEG EXIF/XMP/comment segments are de-identified (CRCs recomputed).
     - Logo image files (raster or SVG files named as logos/brands/badges) are replaced by a neutral
       placeholder of the same format and pixel size / viewBox.
     - Names that only appear as visible drawing text (title blocks: "Drawn by", "Gezeichnet", initials +
       surname such as "J. Smith", copyright owners) are collected from all PDF text and replaced everywhere.
     - File and folder names use the same map; references to renamed files inside assemblies/drawings
       change identically, so links between drawings, models and assemblies keep resolving.
  5. Verify: every output is re-opened (PDF page count, zip integrity, OLE directory, SolidWorks stream
     CRCs, STEP statement count, image decode) and compared with its input.

Limitations and exceptions
  - OCR covers PDF pages (first 12 pages of each PDF plus every page without a text layer); text that exists
    only as pixels in other images (embedded CAD preview bitmaps, raster image files) is not OCR'd, and OCR
    misses stylised logos and rotated text.
  - Encrypted archives (password-protected zip/7z) cannot be opened and are kept byte-identical.
  - Text drawn as vector outlines in PDFs (no text layer) cannot be searched and is left unchanged.
  - Proprietary binary formats whose payload is compressed with undocumented schemes (DWG, CATIA, JT,
    parts of Inventor/Creo data) are only de-identified where identities are stored uncompressed.
  - RAR archives cannot be re-created with open-source tools; they are kept byte-identical so that they
    remain valid (listed below).
  - Non-Latin (e.g. Chinese/Cyrillic) personal names are only handled when they come from metadata fields.
{extra}
Source defects and special cases (final paths)
{defects}
"""


def main():
    rep, inp, out = sys.argv[1], sys.argv[2], sys.argv[3]
    cats = collections.defaultdict(list)
    for line in open(rep, encoding='utf-8'):
        r = json.loads(line)
        o = r['out']
        notes = r['notes']
        src = os.path.join(inp, r['in'])
        if 'empty' in notes:
            cats['Empty (0-byte) files in the source, kept as empty files'].append(o)
        for n in notes:
            k = n.split(':')[0]
            if k == 'rar_not_rebuilt':
                cats['RAR archives kept byte-identical (no RAR writer); content not de-identified'].append(o)
            elif k == '7z_encrypted':
                cats['Password-protected 7z archives kept byte-identical (cannot be opened)'].append(o)
            elif k in ('zip_bad', 'gz_bad', 'tar_bad', 'Z_bad', '7z_extract_fail'):
                cats['Truncated/invalid archives in the source (cannot be opened); identifiers replaced in place where visible'].append(o)
            elif k == 'pdf_open_fail':
                cats['Files with .pdf extension that are not valid PDFs in the source'].append(o)
            elif k == 'pdf_encrypted':
                cats['Encrypted PDFs (no password); metadata left'].append(o)
            elif k == 'zip_encrypted':
                cats['Password-protected zip archives; member content not de-identified'].append(o)
            elif k == 'sw_nofit':
                cats['SolidWorks files where one stream could not be re-compressed into its slot (stream left as is)'].append(o + '  [' + n.split(':', 1)[1] + ']')
            elif k.startswith('EXC'):
                cats['Files that raised an error during processing (copied with in-place replacement)'].append(o)
        if r['in'].endswith('.resume-partial'):
            cats['Partial downloads in the source (*.resume-partial, truncated archives)'].append(o)
        try:
            if os.path.getsize(src) < 300 and os.path.getsize(src) > 0:
                head = open(src, 'rb').read(64)
                if head.startswith(b'version https://git-lfs'):
                    cats['Git LFS pointer files in the source (the CAD payload was never included)'].append(o)
        except Exception:
            pass
    lines = []
    for k, v in sorted(cats.items()):
        lines.append('  * %s: %d file(s)' % (k, len(v)))
        for p in sorted(v)[:400]:
            lines.append('      corpus/' + p)
        if len(v) > 400:
            lines.append('      ... and %d more' % (len(v) - 400))
    extra = ''
    if os.environ.get('DEID_STEMS') == 'person':
        extra = ('  - File names are renamed only for person / account / e-mail / domain identities; organisation, vendor,\n'
                 '    project and part tokens stay in file names (and in SolidWorks internal references) so that assemblies,\n'
                 '    drawings and models keep resolving each other.  Folder names are de-identified for all identities.\n')
    if os.environ.get('DEID_SW') == 'off':
        extra += ('  - SolidWorks 2015+ containers are kept byte-identical (rewriting their compressed sections broke\n'
                  '    references and container checks); their names follow the file-name rules above and OLE-based\n'
                  '    SolidWorks files (<2015) are still rewritten field by field.\n')
    elif os.environ.get('DEID_SW') == 'docprops':
        extra += '  - SolidWorks 2015+ containers: only the document property streams (author, company) are rewritten.\n'
    if os.environ.get('DEID_PDF_INITIALS', '1') == '1':
        extra += ('  - Drafter initials (three capitals in a drawn / checked / approved cell or a dated revision-table row,\n'
                  '    seen in at most two sources) are treated as person identities inside PDFs.\n')
    if os.environ.get('DEID_VENDORS', 'on') == 'off':
        extra += ('  - Third-party supplier names (component vendors in bills of materials) are not treated as identities\n'
                  '    and stay in PDFs; names of the sources themselves (people, organisations, projects) are replaced.\n')
    extra += ('  - Run settings for this attempt: DEID_STEMS=%s DEID_SW=%s DEID_PDF_INITIALS=%s DEID_VENDORS=%s\n'
              '    DEID_PROTECT_REFS=%s (set them before run.sh).\n'
              % (os.environ.get('DEID_STEMS', 'all'), os.environ.get('DEID_SW', 'all'), os.environ.get('DEID_PDF_INITIALS', '1'),
                 os.environ.get('DEID_VENDORS', 'on'), os.environ.get('DEID_PROTECT_REFS', '1')))
    open(out, 'w', encoding='utf-8').write(TEMPLATE.format(defects='\n'.join(lines), extra=extra))
    print({k: len(v) for k, v in cats.items()})


if __name__ == '__main__':
    main()
