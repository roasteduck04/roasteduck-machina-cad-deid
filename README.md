# roasteduck — Machina CAD De-ID

Team **roasteduck**'s entry for the Builders Buildathon *Machina — CAD De-ID* track: de-identify a
188,722-file / 140 GB corpus of real engineering data (STEP, IGES, SolidWorks, Inventor, Solid Edge,
Creo, FreeCAD, Fusion, DXF/DWG, SVG, STL, PDFs, archives …) so that people, accounts, organisations,
projects and contact details are replaced by consistent pseudonyms, while every file keeps working
as engineering data.

**Final score: 833 / 1000** (best of 20 scored attempts). Per-attempt results are in [RESULTS.md](RESULTS.md).

## How it works

`deid/` is a two-pass pipeline:

1. **Harvest** (`harvest.py`, `pdfscan.py`, `svgscan.py`, `exiftool`): read every identity-bearing field
   in every format: OLE property sets (Inventor / Solid Edge / SolidWorks < 2015), SolidWorks 2015+
   document properties, STEP/IGES headers, FreeCAD/Fusion/3MF metadata, PDF Info/XMP and page text,
   image EXIF/XMP, SVG metadata, Windows user paths, e-mails, web domains, repository owners.
2. **Registry** (`registry.py`, `pdfids.py`, `textnames.py`): turn the harvest into one frozen identity
   list. Common words are dropped. Every key has a matching mode: strict word boundaries, word, loose,
   path-only or PDF-only. PDF-only identities are part/drawing/document numbers, third-party vendors,
   postal addresses, phone numbers and drafter initials; they are replaced inside PDFs only, never in file
   names or CAD content, so drawings and models keep referring to each other.
3. **Pseudonyms** (`core.py`): deterministic, word-level and **same length, same letter case** as the
   original. The same identity therefore gets the same pseudonym in every file, path and format, and
   binary containers can be edited in place without moving any byte offsets.
4. **Rewrite** (`pipeline.py`), one handler per format:
   - OLE compound files: field-level property-set rewrite, cross-stream residue, compressed `__ZLB` chunks.
   - SolidWorks 2015+: per-stream inflate / replace / exact-size deflate. It is off in the final runs,
     because rewriting broke references for little gain.
   - Text CAD formats (STEP, IGES, DXF, SVG, OBJ …): string-literal-aware replacement.
   - PDFs:
     - metadata and XMP (also object-level);
     - text-layer redaction with the pseudonym written back at the same baseline, using per-line exact
       matching with the same word-boundary rules (no substring hits, wrapped lines handled);
     - **OCR covering**: tesseract via PyMuPDF finds identities that exist only as pixels or vector
       outlines and covers each one with a white box carrying the pseudonym;
     - links, annotations, bookmarks, attachments, embedded JPEG EXIF/XMP;
     - a structural pass with pikepdf and a damage guard.
   - Archives (zip/FCStd/3MF/F3D, tar, gz, .Z, 7z): rebuilt with de-identified members. Zstandard members
     are supported. RAR and encrypted archives are kept byte-identical.
   - File and folder names: folders are de-identified for all identities. File names only for
     person/account/e-mail/domain identities (`DEID_STEMS=person`), so assemblies keep resolving their parts.
5. **Verify** (`verify.py`): every output is re-opened with an independent reader and compared with its
   input (container integrity, CRCs, STEP statement counts, image decode, archive members).

## Running

```bash
# system tools: tesseract-ocr, exiftool (libimage-exiftool-perl), p7zip-full, qpdf, ncompress
python3 -m venv /opt/venv && /opt/venv/bin/pip install -r requirements.txt
# settings of the best attempt (833)
export DEID_STEMS=person DEID_SW=off DEID_PDF_INITIALS=1 DEID_VENDORS=on
bash run.sh /data/input /data/work /data/out/corpus      # scans, registry, rewrite, verify, README
```

The rewrite runs in parallel on all cores (≈30 min for the full corpus on 32 vCPUs, including OCR).

**Identity lists are not included.** The run also reads four compressed lists from `deid/`:
`curated.dat`, `reviewed.dat`, `ids_pdf.dat` and `vendors.dat`. They hold hand-reviewed and
model-labelled identities taken from the private corpus. Publishing them would publish the very identities
this project removes, so they stay private. Without them the pipeline still runs on the identities it
harvests automatically, with lower recall. `tools/build_ids.py` shows how labelled identities are merged
into `ids_pdf.dat`.

## Switches

| variable | values | meaning |
|---|---|---|
| `DEID_STEMS` | `all` / `person` | which identities rename file names |
| `DEID_SW` | `all` / `docprops` / `off` | SolidWorks 2015+ rewrite |
| `DEID_PDF_CODES_TEXT` | `family` / `1` / `0` | codes found in PDF text: only the project's own numbering family (default), all, none |
| `DEID_PDF_INITIALS` | `1` / `0` | drafter initials from title blocks and revision tables |
| `DEID_VENDORS` | `on` / `off` | replace third-party vendor names inside PDFs |
| `DEID_PROTECT_REFS` | `1` / `0` | keep names of corpus files inside binary CAD references |
| `DEID_PDF_OCR`, `DEID_OCR_PAGES`, `DEID_OCR_DPI` | | OCR covering (default 12 pages + pages without text, 200 dpi) |

## Tools

- `tools/simgrade.py`: local simulation of the PDF checks (removal, cross-PDF consistency, triplets, scope damage)
  against a labelled answer key (TSV: file, type, identifier). The key itself is private.
- `tools/build_ids.py`: merge labelled identities into the compressed PDF identity list.

## What moved the score (summary)

- PDF checks read the **rendered page**: OCR covering of outlined or scanned identities gave +87.
- **Precision matters as much as recall.** Replacing things that are not identities hurt the cross-PDF
  checks: generic words, pin labels, component values, catalogue numbers. Removing about 26,500 such
  regex-harvested codes was worth +8.
- Part numbers that name files are identities. Keeping them in drawings cost 31 points.
- Leaving SolidWorks 2015+ files byte-identical: +19 across drawing/model, packaging and relationships.
- More OCR (more pages, higher dpi) and adding lower-precision labels both lowered the score.

## License

MIT, see [LICENSE](LICENSE).
