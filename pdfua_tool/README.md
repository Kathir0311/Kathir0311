# Dezquare PDF Accessibility Studio v4.33.0

Automated PDF/UA-1 tagging plus an Acrobat-style manual tag editor.

The engine proposes a complete tag tree; you review and correct it; the corrected
tree — not the guess — is written into the PDF. Output is checked against
ISO 14289-1 machine checks (and veraPDF, when installed) and delivered with an
Excel report.

---

## Quick start

```bash
pip install -r requirements.txt
python app.py                       # http://localhost:8000  (PORT=… to change)
```

Python 3.10+. Works on Linux and Windows. No database: each job is a folder
under `jobs/`, settings live in `config.json`.

Production (single process — the job queue and model cache live in-process):

```bash
pip install waitress
python app.py                                  # V4.28: uses waitress when it is installed, 24 threads
# or:  waitress-serve --threads=24 --port=8000 app:app
```

Environment variables: `PDFUA_JOBS` (jobs folder), `PDFUA_CONFIG` (settings file),
`PDFUA_WORKERS` (files at the same time when not set in Settings), `PDFUA_JOB_TIMEOUT_MIN`
(a job running longer is stopped, default 480 — the AI checks of a big book take time), `HOST`, `PORT`.
V4.28, for many people at the same time: `PDFUA_THREADS` (web-server threads, default 24; waitress's own default
of 4 would let four people's requests block everybody else's), `PDFUA_SERVER=flask` (force Flask's development
server), `PDFUA_LEARNERS` (learner processes at the same time, default 2), `PDFUA_LIVE_DELAY` (seconds after the
last edit before the tool learns from it, default 4).

**Queue.** Each analyse / build / AI-alt-text run happens in its own Python
process (`core/jobrunner.py`), because PyMuPDF is not thread-safe — running it
in web-server threads could freeze the queue with files stuck on "waiting".
The file list shows the queue position and a **Stop** button; the job page
shows what is running ahead of it. Each job folder has a `worker.log` with
anything the worker printed (first place to look when a job fails).
Editing a file is refused (HTTP 409) while it is being analysed, built or
AI-drafted, instead of silently waiting.

### Optional components

| Component | What it adds | Setup |
|---|---|---|
| **Tesseract** | OCR text layer for scanned pages | install `tesseract` (+ `tesseract-ocr-tam` etc.); detected automatically |
| **veraPDF** | real PDF/UA-1 conformance verdict on every build | install the veraPDF CLI, put the path to `verapdf` / `verapdf.bat` in Settings |
| **Ollama** | AI alt-text drafts, tag-role review, table checks, drawn-zone reading, reference lists | Settings → server URL + models → Test connection |

Models are picked from what is installed on the server. Leave the model fields
blank (auto), or name one; a named model that is not installed is replaced by an
installed model of the same kind, and **Test connection** shows exactly which
models will be used. If no vision model is installed, install one:

```bash
ollama list                         # what the server has
ollama pull qwen2.5vl:7b            # alt text (llama3.2-vision, gemma3:4b, llava also work)
ollama pull qwen2.5:7b              # role review / table headers / the AI brain (any chat model works)
ollama pull nomic-embed-text        # V4.25: the brain's memory in the AI's own vectors (optional)
```

**Settings this installation starts with** (and an older `config.json` is
brought up to once): the AI **on**, **600 s** for a model to answer, **5 files**
at the same time on **10 CPU cores** each, and the **Smart AI (all samples)**
profile as the upload default (V4.25 — before, Best quality; an installation whose
default was Default, Best quality or Academain is moved to Smart AI once). Settings → **Use the recommended settings** puts
them back. After that, what you change in Settings stays — an update never
ships a `config.json`.

While the model is answering a drawn zone (up to the timeout), the rest of the
editor keeps working: the tool lets go of its MuPDF lock while it only waits
for Ollama.

---

## Workflow

**A file that already has tags is never overwritten silently (V4.2).** Some PDFs
arrive here after somebody has already worked on them locally, so on upload the
tool looks at the file first; if it carries a structure tree it stops and asks,
showing what is in there (`7 700 tagged element(s) — TD×2 979, TH×1 944, …`):

- **Yes — load the tags as they are** → the file is imported: every element, its
  reading order and its attributes come into the editor exactly as they are in
  the file, and nothing is analysed.
- **No — analyse the file from scratch** → the usual full analysis; the existing
  tags are ignored.
- **Let the tool decide** → the old `auto` behaviour (keep good tags, re-tag weak
  ones); the screen says which way it would go.

The question is the `existing_tags = ask` default. Set that option to `auto`,
`import` or `retag` on the upload form (or in a profile, the CLI or a batch run)
to answer it in advance; batch runs that build without review never stop to ask.


1. **Upload** one PDF (opens the editor) or several (batch). Pick a profile,
   optionally adjust options.
2. **Preflight** makes a working copy: unpacks text inside form XObjects,
   embeds missing standard fonts, repairs Unicode maps, OCRs scanned pages.
   Rendering is unchanged (verified pixel-for-pixel in the test suite).
3. **Analysis** proposes the tag tree — or, for an already-tagged file, **imports**
   its existing tags (`existing_tags = auto` decides by quality).
4. **Review** in the editor. Low-confidence tags are outlined in amber; the
   Issues tab lists everything that still breaks PDF/UA.
5. **Build** writes the tags into the content streams, sets metadata and
   bookmarks, validates, and produces the report.
6. **Move to QA** with the PAC report (V4.23) → **QA** corrects, comments, sends
   to rework or approves → **Final QA** → **Delivery** (see V4.23.0 below).

---

## What the engine does automatically

**Cross-references and links (V4, `core/xref.py`)**
Everything a reader clicks is already written on the page, so these are found by
rule and matched against targets collected from the same file — no AI, nothing
invented. A link is only created when its destination was found here.

| Kind | Found | Destination |
|---|---|---|
| Web address | `https://…`, `www.…` | that address (`link_web`, **on**) |
| E-mail address | `name@site.org` | `mailto:` (`link_web`, **on**) |
| Contents entry | the page number at the end of a contents line | that printed page (`link_toc`, **on**) |
| Chapter / section / part | "Chapter 3", "Section 2.1", "§2.1", "Part II", "Appendix A" | that heading (`link_sections`) |
| Figure / table reference | "see Figure 3", "Table 2.1", "Fig. 4b" | that figure or table (`link_figures_tables`) |
| Bibliography reference | "[12]", "Smith (2019)" | that entry in the reference list (`link_references`) |
| Index entry / page reference | index page numbers, "see page 12" | that printed page (`link_index`) |

- **Printed page numbers are mapped to real pages** from the page-number
  artifacts, so "page 12" lands on the sheet that prints 12, not the 12th sheet.
- **The page does not change**: the annotation has no border (`/Border [0 0 0]`)
  and carries the Print flag and a `/Contents` description, inside a `Link`
  element with `OBJR` — what PDF/UA 7.18.5 asks for.
- **A mention never links to itself**: the heading "Chapter 2", the caption
  "Table 1" and the entry "[2] …" are definitions, not references.
- `cross_reference_tags` additionally wraps each mention in a `Reference` tag.
- The editor's **Links** tab lists everything found, with the target it matched
  or "no target found", a tick box per kind and **Apply and search again** —
  nothing is written until you switch a kind on.
- **A file analysed before (or with the search switched off) can be searched
  from the editor**: the Links tab offers **Search the text for links now**, and
  Issues offers **Link the contents entries (n)** whenever a `TOCI` has nothing
  to click (PAC's "TOCI contains Link"). Both read the text as it is now — the
  file is not re-analysed and no tag is moved. The Issues list names each such
  entry, and after the search the Links tab says how many mentions found no
  target, so a contents line that cannot be matched to a printed page is
  visible rather than silent.

**Mathematics (V4, `core/mathspeak.py`)**
- `detect_formulas` (**on**): a line that is an expression (`a² + b² = c²`,
  `3 × 4 ÷ 2 = 6`, `H₂O`) becomes a `Formula`, and its `/Alt` is the formula
  **read aloud** — "a squared plus b squared equals c squared". PDF/UA requires
  an `/Alt` on every Formula, and "equation" is not one.
- Prose that merely mentions maths stays a paragraph; phonics notation like
  `/m/ /t/ /a/` is not read as division.
- An equation set larger than the body text is a display equation, not a heading.
- `to_latex()` gives the same expression in LaTeX for the report.
- Formulas drawn as graphics are handled by the vision model (**Read formulas
  aloud** in the AI dialog): is it maths, is it decorative, how is it spoken,
  and the LaTeX.

**AI helpers (V4) — draft, you approve**
All of these are buttons in the editor's **AI** dialog; each one asks the model
only about the items that still need it, and nothing is written until you press
**apply** on a line.

**Which model does which job.** Settings → Local AI → **Model per job** gives
every job its own model, so a small fast one answers the short text questions
and the best vision model reads the pictures. Left on *General*, the tool picks
for itself, per job: the figure and formula work prefers `qwen2.5vl` /
`qwen3-vl`, the checks that run hundreds of times (is this row inside the table?
what is in this zone?) prefer a small vision model such as `granite3.2-vision`,
and the text questions prefer a small chat model such as `llama3.2`. Models that
only transcribe (`deepseek-ocr`, `glm-ocr`) are never picked automatically.
**Test connection** prints the model each job will actually use.

| Button | Model | What it does |
|---|---|---|
| Draft alt text | vision | figures without `/Alt` |
| Review tag roles | text | H1/P/Caption/Artifact wrong? |
| **Check heading levels** | text | levels came from font size; the model reads the wording and the order |
| **Describe weak links** | text | "click here" / a bare URL → a description that works on its own |
| **Label form fields** | text | a tooltip (`/TU`) from the text printed around the field |
| **Summarise tables** | text | one sentence for the table, written to its Summary |
| **Read formulas aloud** | vision | spoken maths, LaTeX, and "is this image decorative?" |

**Joining what a page break split (V4.1)**
Select the list at the end of page 3 and the list at the top of page 4 — or two
tables, or two paragraphs — and press **Join these …** in Properties. One tag
then holds the whole thing, keeping both pages (a tag may own boxes on several
pages). A table takes the other's rows and turns a repeated column header into a
running table heading (artifact); a list takes its items; a paragraph takes its
text. With AI switched on the model is asked whether the two really are one
thing and whether the second part repeats the header — the answer is written
into the tag's note and never cancels what you asked for.

**The model is asked only when it can change the outcome.** A join by rule takes
about 10 ms; a model on a CPU takes most of a minute, so the tick box is off by
default. Joining a list or a paragraph never calls it (the rules decide), and a
table only calls it when the repeated header is a half-match — identical or
clearly different headers are settled in code. The button says what it is doing
while it runs, and an interactive AI call gives up after 60 s rather than
hanging the editor.

**A drawn Table zone reads the layout, not just the ruling (V4.4)**
A sound-spelling card rules only the outside of its word block, with a divider
down the middle and a line under the headings; inside, the words simply sit in
columns. The grid finder then returns one big cell per block, and twelve words
land in a single cell. When the person has drawn the area and said *this is a
table*, the text layout is taken as the grid: rows from the lines that share a
band, column edges from where the lines start — measured on the fullest rows, so
a heading centred over three columns does not invent a column of its own, and
that heading spans the three columns it covers. The result is the grid the
publisher tags by hand: `Initial Position` and `Final Position` spanning three
columns each, every word a cell. A column that runs out of words half way down
keeps its empty cells: only a row whose items are already wider than a column (a
centred heading) is stretched across the columns beside it — a short row of
ordinary words is not.

This happens for a zone the person drew (and for **Read table again**), not in
the automatic page sweep: a panel ruled around a block of words is not always
meant to be a grid, and the sweep should not turn one into a 47-cell table on
its own. It also only applies to a coarse grid — at most six cells, one of which
holds six or more short items in two or more columns. A table that already has a
cell per value is left exactly as it is.

**Reading a drawn zone properly (V4.1)**
Drawing a **Table** or **L** zone, or stretching one, reads that area again:
every grid strategy is tried and the one that fits the marked area best wins
(staying inside it and filling it, with rows of equal width — not simply the one
with the most cells). That part is code and costs nothing, so it is on by
default. Ticking **+ ask the AI** also shows the area to the vision model, which
says how many rows, columns and header rows it sees; the header rows are applied
and a disagreement about the grid is written into the note. Properties has
**Read table again** (rules) and **+ AI** next to it for any time afterwards.

**A drawn P zone: paragraphs, lines, or one block (V4.2)**
A zone drawn over half a page usually covers several paragraphs. Marking the
area says *where* the text is, not that it is one block, so the lines inside are
grouped exactly as the automatic analysis groups them (column order, line
spacing, indents, a full stop on a short last line) and each group gets its own
tag and its own zone. The draw popover has three choices:

- **one tag per paragraph** (default) — the lines are grouped as the automatic
  analysis groups them
- **one tag per line** — every printed line becomes its own `P`, which is what a
  numbered reading passage needs: the line numbers printed down the margin only
  line up when each line is its own block in the reading order. **The number
  printed in the margin stays with its line** (V4.3): a fluency passage prints a
  word count ("__73") out at the right edge of every line, and the layout order
  reads that column on its own — the text went into one tag and the number into
  another, two tags for one printed row. On each row the leftmost piece is the
  line and a narrow piece beside it (a number, a score box, a tick) joins it. A
  second column of real text is not narrow, so a zone drawn over two columns
  still splits into its own lines
- **one tag for the whole zone** — the old behaviour, one block

**Group a table by its headings (V4.4)**
A sound-spelling card sets its sample words in columns under "Initial Position",
"Medial Position", "Final Position". Read across the printed rows that is
"ace acorn make break away play" — six unrelated words. What the card teaches is
each heading and then its own words. Select the table (or any cell in it) and
press **Group by heading** in the Table box:

```
TR > TH  Initial Position
TR > ace, age, ape, ate, acorn, aid, aim, aimless
TR > TH  Medial Position
TR > make, face, paid, wait, break, great, steak
TR > TH  Final Position
TR > away, clay, day, may, play, say, stay, way
```

Each heading keeps the tags printed under it — down the first column of the
group, then down the next. Where one group has fewer words than another the row
is filled up so every row still has the same number of columns (an irregular
table is a PAC failure); each filler is a real box at the end of the row, read
once as "Blank cell". Two ways to keep the words:

- **keep the tags as they are** (default) — the `P` tags are moved into the row
  unchanged, which is the shape the publisher asks for on these cards; the
  checker will call the table irregular, which is the price of that shape
- **words become cells (TD)** — each word becomes a cell, so the rows stay even
  and PAC and veraPDF are happy with it

Whichever you pick is remembered, so a workbook tagged the same way all through
is one click per table.

It works whether the words are already cells or still `P` tags from a grid the
finder only half read, and a table with no `TH` says so rather than guessing.

**Letters set apart are read as the words they spell (V4.4, `join_spaced_letters`)**
A publisher's logo line is tracked out letter by letter —
`B e n c h m a r k   e d u c a t i o n   c o m p a n y` — and a screen reader
says it one letter at a time. Such a line now carries an `/ActualText` of the
words it spells, **Benchmark education company**, written at analysis and shown
by **Suggest from the page**; the page itself is not touched.

The words come from the page, not from a dictionary: the gaps put the letters
back together (inside a word the tracking is even, a word break is a gap half
again as wide), and where the line is tracked so evenly that the gaps say
nothing, the words the page uses elsewhere do — the logo line is the company
name, and the company name is printed in full in the copyright line under it.
The result is only used when **every** piece of it is a word the page itself
uses, so a phonics card's row of single letters (`a b c d e f`) is left alone
and still reads "a, b, c, d, e and f".

**Read as — /ActualText for one tag or a hundred (V4.4)**
A sound-spelling card prints "cat" with the a in red, or the pattern "u_e" with
the letters red and the underscore black. Read as it stands a screen reader says
"cat" and "u e", and the card loses the very thing it is teaching. Properties now
has a **Read as (/ActualText)** box, for the selected tag and for any number of
selected tags at once:

- type one sentence and **Apply to all** — every selected tag is read that way
- **Suggest from the page** — the sentence is built from the glyphs themselves:
  the letters spelled out, and the ones printed in colour named. `cat` becomes
  *"c, a and t, a highlighted in red"*, `ta-ble` becomes *"t, a, hyphen b, l, e,
  b, l, e, are highlighted in red"*, `oo` becomes *"o, o, are highlighted in
  red"*. Every line is listed with its own box and can be edited before
  **Apply these**
- **Clear** — the tags are read as they are printed again

Nothing is guessed: the characters and the colour each one is set in are read off
the page (`core/actualtext.py`). **Colour is the highlight**, not whichever colour
happens to be commonest: a card of syllables prints half of every word in red
("muf-fin", "num-ber"), so red is the commonest colour on that page and the black
hyphen was being announced as the highlighted part. Where a tag holds both
coloured and plain black glyphs the coloured ones are the highlight; where every
glyph in the tag is coloured, the page's own text colour decides, and the colour is named as a person would name it (red, blue, dark
green …). Every character is its own item, separated by commas, so a reader pauses
between them, and a symbol is said with the letter it belongs to ("hyphen b",
"underscore e") because that is how the pattern is taught. Plain letters get "and"
before the last when there are three or more ("c, a and t") and a plain comma when
there are two ("o, o"); once a symbol is in the mix the list stays commas all
through. A tag whose
glyphs cannot be read (a word drawn as a picture) falls back to spelling its text.

**Page numbers announced (V4.1)**
Select the folios — the tag-type filter plus **Select all shown** picks up all of
them in two clicks — and press **These are all page numbers**: each becomes a
`P` read as "Page 5" (its `/ActualText`), and a folio that was an artifact
becomes a real tag.

**Structure**
- **Tables continued over pages → ONE `Table`** (`merge_continued_tables`): when the column header
  is printed again on the next page(s), the pages are joined. The header row is `TH` on the first
  page only; the repeated header rows on the following pages become `Pagination/Header` artifacts
  (running table heading), and the repeated page title (`running_title_artifact`) is an artifact too,
  so only the first title stays a heading. A different title (or any other content) between two pages
  starts a new table. Example: a 56-page correlation file → 4 tables (pp. 1-7, 8-24, 25-45, 46-56),
  4 × H1, 5 × TH per table.
- **How the join is decided** (`merge_continued_tables`): each page pair gets a score from the page
  format, column positions, header text, the title above the table (any text, a heading is *not*
  needed — "Level 1" → "Level 2" or "Group A" → "Group B" starts a new table), text after the table
  and how far down the page the table reached. Clear cases are decided by the rules. **Doubtful
  ones** are asked to the AI (Settings → AI, option `ai_table_merge`; CLI `--ai --ai-tables`) with the last
  rows of page N and the title/header/first rows of page N+1; `ai_table_merge_vision` also sends the
  two page crops to the vision model. Without AI, doubtful joins follow the score and the table is
  outlined amber with a note "check: …" in the editor. Decisions are listed in `model.info["ai_table_merge"]`;
  if the AI server is unreachable the rules are used and a warning is recorded.
- **Mixed page formats**: body size and heading ranks are measured per page-size group (7 pt Letter
  pages and 12 pt tabloid pages in one file no longer turn a footer date into a heading).
- **Numbered items that were tagged separately join one list** (V4.2): a rubric whose first item sits
  at the margin while the rest are indented ("1 and 2: …" then "3: …"), a question list where only
  item 1 has a write-in blank, a wrapped last line that landed beside its item — each left a run of
  neighbouring tags that are plainly one list. After everything else on the page is decided, a run of
  neighbours with the same kind of label and rising numbers is put back together as one `L`, the
  label is split off into `Lbl`, and an unlabelled tail line joins the item above it. Labels may end
  in a colon and may name a range ("1 and 2:", "4–5:"). On the 147-page file this took `H6` from 398
  to 120 and list items from 618 to 636.
- **A large-print page is measured against its own text** (V4.3): a workbook sets the teacher pages
  at 11 pt and the student pages at 18 pt on the same sheet. Measured against the document body, every
  question on those pages reads as a heading — so "1. What might you see at night?" became an `H6`,
  never a list item, and its number never became a `Lbl`. A page that sets most of its own text at one
  bigger size (at least three lines, 60 characters and more than half the page) now has that size as
  its running text: the headings on it have to beat *that*, and the numbered questions come out as one
  `L` with `Lbl` + `LBody` per item. A chapter opener with two big title lines does not qualify, so its
  title stays a heading. The pages this applied to are listed in `info["large_print_pages"]`.
- **A wrapped line indented past the number joins its item** (V4.3): "3. Did Tim and Jim like Mom's
  story?" / "How can you tell?" — the second line is indented a little past the number but not as far
  as the item text, which used to end the item. Anything indented past the label now belongs to the
  item; a line back at the label margin still starts something new.
- **Write-in blanks inside list items** (V4.3): "1. Tim and Jim kicked the ______" keeps its blank as
  a Span read "Blank Line" now that the question is a list item.
- **A list item keeps its number in a `Lbl`** (V4.2): whichever pass built the item, an `LI` whose
  `LBody` still starts with "1." / "a)" / "3:" gets a real `Lbl` — the number is read as the item's
  label instead of running into the sentence. The `Lbl` box sits inside the body box, so the glyphs
  of the number go to it and the rest to the body. The editor lists any that are left under Issues
  with a **Fix list labels** button. The button no longer cares how large the item is set (the tag
  already says it is a list item — only the wording decides), and when no page line lines up with the
  item — a hand-drawn zone, an item built by joining tags — the number's characters are measured
  inside the item itself. Anything it still cannot find is reported in the message rather than
  silently left ("… could not be read off the page").
- **Running text is not a heading, however large it is set** (V4.2): a reading passage set at 13 pt in
  a 10 pt book used to become an `H6`. More than one sentence (a full stop, then a capital) and more
  than eight words means prose, not a heading.
- **A page of equally styled short blocks is a grid of items, not headings** (V4): word cards, letter
  grids and spelling lists set every item in one large size, and one by one each looks like a heading.
  A same-size group of 8 or more short blocks that fills the page goes back to `P`. On a 147-page
  phonics assessment this took the file from 1 140 `H6` + 163 `H1` down to 398 + 7, and the numbered
  answer lists ("1. pet / 2. cat …") are now real lists (294 → 618 list items), because a label set a
  little larger than the running text still counts as a list label.
- **A page opens with its own heading** (V4.3, `page_title_first`): a workbook prints the unit title
  as a tab in the outer margin, turned on its side. By position it comes last, so the page read
  "Blend Words", the table, *then* "Unit 1 Short a" — the page's H1 arrived third. The heading that
  names a page now moves to the front of it, so the page opens with its title and the `Sect` built at
  the end wraps the page's content under the right heading. `margin` (default) moves the first
  heading set on its side out at the edge of the sheet — a unit tab, a rotated running title (tall,
  narrow, in the outer fifth of the page, so a short word that happens to sit beside the text is not
  mistaken for one) — whatever its level; `first`
  moves the page's top-level heading wherever it is, unless a heading of the same or a better level
  comes before it, so a numbered run of headings keeps its order; `off` leaves the order alone. The
  editor's **Fix reading order** applies the same rule.
- **Every table keeps its rows in THead / TBody** (V4.3, `table_thead_tbody`, now **on**): the header
  rows of a table go in a `THead` and the data rows in a `TBody`. A table with no header gets no
  `THead` — but it still gets the `TBody`, which is what PAC and the readers expect. Stray rows left
  directly under a `Table` (a hand-built table, a zone drawn by hand, an edit) are put back in the
  body group, in the analysis and after every table edit in the editor. A job or profile saved before
  this is migrated the first time it is opened, so the setting does not stay off from an old save.
  **A header with nothing under it is not a header**: a one-row table whose only row was called the
  header announced columns with no data — those cells become `TD` in the `TBody`.
- **A grid with no header gets no header** (V4.3): a phonics workbook sets every cell of its word
  grid the same way — one word, one size, all bold. The first row and the first column were read as
  headers, so a grid of words announced column headings that are not there. A header now has to
  *look* different from the body (weight, size, length of the text, judged by medians so one odd cell
  does not decide it); when the first row reads like every other row, the table has no header and
  every cell is `TD`. The old "tables need a header, flag it for review" fallback no longer applies to
  such a grid, nor to a table whose first row is empty. `Table without header cells` is a **warning**
  now, not an error — PAC and Adobe leave it to a person too.
- **The same worksheet printed again is not a continued table** (V4.3): this workbook prints each
  sheet four times, once per type size, so page after page has the same columns, the same title and
  the same first row — which the "table continued over pages" rule read as a continuation. It then
  turned the second copy's first row into a *repeated header* artifact and the page title with it, so
  pages 6, 7, 8 … lost their first row. When the new page repeats the **rows** of the previous one
  (60% of them or more), it is a repeated page, not a continuation, and each page keeps its own
  table, its own heading and its own first row.
- **Empty sliver rows and columns are dropped** (V4.3): a rule drawn a few points away from the row
  above adds a hairline row to the grid — an empty strip along the top border that then has to be
  tagged and, worse, reads as the table's header row. A band far thinner than the rest with nothing
  printed in it belongs to the row beside it.
- **Sentences boxed under a grid belong to the table** (V4.3): a worksheet rules its word grid and
  the sentence rows under it as separate boxes with a visible gap between them. Those rows fell out
  of the table — printed inside the ruling, read after it. The gap is now stepped over (up to 26 pt),
  and such a box becomes a row spanning every column. **An empty ruled row counts too**: the answer
  row between the grid and the sentence used to end the table there (no text, so the chain stopped);
  a band with side rules is a row of the table whatever is printed in it, and a blank row that spans
  the whole grid stays **one** cell, read once as "Blank cell", instead of being cut into one blank
  per column. Drawing a **Table** zone over the lot does the
  same from the other end: any line inside the marked area that no cell covers becomes a full-width
  row in its place in the reading order, because the person has said that area is one table.
- **A drawn Table zone prefers the ruled grid** (V4.3): "deep read" tries every strategy, and the
  text strategy likes to put a row in each gap between the printed rows. A grid with something
  printed in every row now beats one that merely covers more of the marked area.
- **Overlapping table cells are trimmed** (V4): a ruled form can give the grid two cells for one area;
  the row then counts a column more than its neighbours (PAC "irregular table row") and one of the two
  is left with no content. The overlap is trimmed, holes are refilled, and every row covers the grid.
- **An empty cell is never a header** (V4): a `TH` with nothing in it announces a column that is not
  there. Empty header cells become `TD` (scope and Headers cleared) at analysis, at every table edit
  in the editor, and once more *after* tagging — which also catches a header that lost its text to an
  overlapping cell. Checked on the 147-page file: 301 `TH` in the output, **0 of them empty**.
- **Cells left empty after tagging get a second pass** (V4): when two cells fight over the same glyphs
  the loser used to end up with nothing (PAC "not associated with content"). Those cells are found
  after the first tagging pass, given the invisible cell text, and their pages are tagged again.
- **Empty review marks** (`empty_markup_annots = remove`): tiny markup annotations (Square, Circle,
  Highlight …) with no comment text are removed at build time (PDF/UA 7.18.1); reported under
  "Automatic fixes". Set to `keep` to leave them.
- Reading order by recursive XY-cut: columns, sidebars, full-width figures
- Headings H1–H6 from font-style ranking, confirmed by existing bookmarks and
  1.2.3 numbering; levels normalised so they never skip, first heading is H1
- Lists with `Lbl` / `LBody`, nested by indent, `ListNumbering` attribute
- Tables with merged cells (`RowSpan` / `ColSpan`), header rows and header
  columns, `Scope`, and `Headers`/`ID` association for complex tables; holes in
  the grid are filled so every table is regular
- Figures: raster images (touching tiles merged) and vector charts (drawing
  clusters with their labels); captions detected
- Decorative images → artifacts: tiny, full-page backgrounds, repeated logos
- Running heads / folios → `Pagination` artifacts with `Header` / `Footer`
  subtype (repeated text *and* reused header/footer positions — handles
  odd/even running heads)
- Footnotes → `Note` with unique `ID`
- Tables of contents (dot leaders) → nested `TOC` / `TOCI`
- Link annotations → `Link` elements containing the link text and an `OBJR`;
  multi-line links via QuadPoints
- Form fields → `Form` elements; tooltip drafted from the nearest label
  (right-hand label for checkboxes/radios)
- Other annotations → `Annot`
- Paragraphs split across columns/pages joined into one `P`
- Other-script passages (Tamil, Hindi, Greek, Arabic, CJK …) get `/Lang`
- Title from metadata, or the first H1 when the metadata title is a file name

**Screen-reader (NVDA / JAWS) readiness**
- Headings, lists (`list with N items`), tables with header association,
  link text, alt text, per-passage `/Lang`, "Blank" for empty cells
- **Preview** shows the announcements in NVDA-like phrasing, in reading order

**Colour contrast (WCAG 1.4.3 AA / 1.4.6 AAA)**
- Every text run measured against its *real* background: each page is rendered
  twice, once with all text made invisible, so flat fills, gradients and photos
  behind text are all accounted for (10th-percentile ratio catches text that is
  unreadable over part of a photo)
- Large-text rule applied (18 pt, or 14 pt bold → 3:1 at AA)
- Each failure gets a suggested colour: same hue, lightness moved just enough
  (+ margin) — verified against the darkest and lightest parts of the background
- Optional fix on build (`fix_contrast`: selected / all / off, default *selected*
  = only what you tick). The colour is set right before the affected text
  operator and restored right after it, in the same colour space
  (Gray / RGB / CMYK / ICC — CMYK is solved through MuPDF's colour management),
  so nothing else on the page changes. Fixed runs are re-measured on the output
- Text over photos where no single colour works is flagged "fix in the design";
  spot colours and patterns are reported, not changed
- Editor **Contrast** tab (swatches, ratio, suggestion, tick to fix, AA/AAA,
  re-measure), page overlay, report sheet, batch/CLI column

**Content-stream tagging (glyph level)**
- Own content-stream interpreter with real font metrics (simple, Type0/CID,
  Type3, non-embedded base-14) computes every glyph's position
- Ownership decided per glyph; `Tj`/`TJ` operators are **split mid-string**
  where a link or label starts, without changing rendering
- Vector paths and images inside figures belong to the figure; all other
  graphics become artifacts; clipping paths never sit outside marked content
- Optional-content (`/OC`) markers preserved; `/ActualText` spans kept and nested
- Import mode keeps each original MCID with its original element

**A `Sect` around every top-level heading (V4.3)**
In the built file the `Document` element no longer holds a flat run of
headings and paragraphs: each `H1` opens a `Sect` that holds that `H1` and
everything under it — `H2`, `H3`, paragraphs, lists, tables, figures — and the
next `H1` closes it and opens the next `Sect`. So the tag tree reads

```
Document
  Sect > H1 , P , H2 , P , L , H3 , P , Table …
  Sect > H1 , P , H2 …
```

- Option `section_wrap` (Build group, also in the editor's Build dialog):
  `h1` (default) one `Sect` per top-level heading, `all` a nested `Sect` at
  every heading level (`H2` inside its `H1`'s section, and so on), `off` the
  old flat tree.
- "Top level" is the highest heading level the file actually has, so a file
  whose chapters are `H2` is grouped on its `H2`s.
- A file that already has its own sections (`Sect` / `Part` / `Art` holding
  headings — an imported tagged PDF, for instance) is left exactly as it is.
- Anything printed before the first heading (a cover, a half-title) stays a
  direct child of `Document`, outside the sections.

**Document level**
- `/Lang`, title (Info + XMP `dc:title`), `DisplayDocTitle`, `MarkInfo`,
  `Suspects=false`, `/Tabs /S`, XMP `pdfuaid:part=1`
- Link `/Contents` from the visible link text (or URI), field `/TU` from labels
- Bookmarks rebuilt from headings (depth configurable)
- Optional-content config names, embedded-file `/F` + `/UF`, explicit `CIDToGIDMap`
- Empty tags pruned — except table cells: empty `TD`/`TH` are kept (tables stay
  regular) and announced as **"Blank cell"** (setting `empty_cell_text`; per cell
  override in Properties). Default `empty_cell_method=text` puts invisible
  (Tr 3) real text + `/ActualText` in each empty cell, so every cell is
  **associated with content** (PAC 2024 check) and NVDA reads it; the page looks
  unchanged. `actualtext` writes `/ActualText` only — PAC then fails
  "Associated with content" for those cells. The checker's UA-47 mirrors the
  PAC rule (elements with no MCID/MCR/OBJR below them). Old jobs saved with
  `actualtext` are migrated to `text` automatically.

---

### Worksheets: write-in lines, panels, page-number badges
- **Numbered write-in lines** (`blank_lines_as_list`, default on):
  `L` (ListNumbering) > `LI` > `Lbl` "1." + `LBody` containing the drawn line
  with ActualText **"Blank Line"** (+ invisible text). Several lines for one
  question are ONE blank holding every line drawing, read **"Multiple Blank
  Line"** (`multi_blank_line_text`); a single line reads "Blank Line".
  "4. I can ____ ____ ." → `Lbl` "4." + `LBody` ["I can", `Span` (both lines,
  ActualText "Blank Line"), "."] — the "." is LBody text, not a tag. Numbers are kept in number
  order even when two questions share a row. **Make list** in the editor
  pairs the same way: a number-only tag becomes `Lbl`, the tag(s) after it the
  `LBody`; an empty tag there becomes a write-in line.
- **Underscore blanks** ("Number Accurate _______ of 26"): the words stay in
  the `P`, the underscores become a `Span` with ActualText **"Blank Line"**
  (read "Number Accurate Blank Line of 26"). "A ___ | B ___" on one line is
  split into two `P`, the "|" divider becomes an artifact.
- **Empty merged cells** (`split_empty_merged_cells`, default on): an empty
  area spanning several columns/rows (e.g. the blank header over "1 | c")
  becomes one blank cell per column/row, each read "Blank cell". Full-width
  rows at the table edge without a rule line (the panel the table sits on)
  are dropped, so text under the table is not swallowed as a row.
- **Write-in lines** ("1. ________", list option off): each drawn answer line becomes its own
  `P` containing the rule, announced as **"Blank Line"** (`blank_line_text`;
  invisible text + `/ActualText`, like empty cells). The question number /
  sentence start before it is a separate `P` (never a heading or list).
  Extra answer lines stacked under a question are detected too. In the editor
  any empty tag can be marked "Write-in line" in Properties.
- **Tables on panels**: white/tinted panels behind a table used to become a
  giant cell around the whole grid (PAC "Irregular table row", veraPDF
  7.2-43). Such container cells and full-width panels below the grid are now
  dropped; the checker's UA-31 counts columns like veraPDF (and flags a
  RowSpan that runs past the last row).
- **Contrast like PAC**: `contrast_large_text=pac` (default) treats only 18pt+
  as large text, as PAC 2024 does. Artifacts (page numbers, running heads)
  count as errors. Light text on a solid coloured box (e.g. white page number
  on a red badge) is fixed by darkening **the box** just enough, keeping the
  text colour. `fix_contrast` now defaults to `all`.
- **Fix now** (Contrast tab): "Fix all now" or "Fix" on one item changes the
  colours in the working PDF immediately (background job), re-measures those
  pages and redraws the page. "Undo colour fixes" restores every original
  colour. Items fixed this way are not touched again at Build.
- **Hidden backgrounds** (`normalize_backgrounds`, default on): InDesign
  layouts paint a white panel and then coloured cells on top. White text on
  the coloured cell renders at e.g. 8.9:1, but a checker that infers the
  background from vector fills (PAC 2024) can measure it against the hidden
  white panel and report white-on-white. Build removes the covered part of
  such panels (inset 1pt; renders pixel-identical — tested with MuPDF and
  poppler). The checker's UA-C2 warns when text still sits over a hidden
  fill that would fail. Invisible "Blank cell"/"Blank Line" text gets black or
  white, whichever contrasts with what is behind it.
- Table panels: full-height strips at the table edge (panel margin next to a
  page badge) and sub-6pt sliver columns/rows are dropped too; the checker
  warns (UA-35) when a cell spans every row — PAC's "Table structure is
  possibly incomplete due to a missing TH or TD element".

### Files tagged somewhere else (V4.5, `core/repair.py`)
A file that already carries tags is imported with its structure kept. What
arrives is usually sound on the page and wrong in four ways PAC reports, none of
them about what is printed — so all four are now put right on import, and each
also has a button in **Issues** for a file you are editing:

| What arrives | What the tool does |
|---|---|
| A contents entry inside a paragraph, a list item at section level, a cell outside its row (PAC *"Structure elements"*) | every tag goes back under a parent PDF/UA allows — wrapped where it stands, or joined to the container just before it, never moved in the reading order (**Fix structure**) |
| 41 `TOCI` and not one `Link` (PAC *"TOCI contains Link"*) | the cross-reference search now runs for imported files too, so contents entries become clickable; printed page numbers are matched through page labels and the folio printed on the sheet (**Links** tab) |
| "1. A \_\_\_\_\_\_ flew out of its nest" read as "A flew out of its nest" | each underscore run becomes a `Span` read **"Blank Line"**, inside the tag that holds it — a paragraph, a list item's body or a table cell. In an imported file the glyphs already belong to the tag they were imported into, so the new Span's geometry wins, the way a zone drawn by hand does (**Read write-in lines as Blank Line**) |
| A figure with no `/Alt` but a caption printed with it | the caption is offered as the alt text, for one figure or all of them at once (**Alt text from the caption**) |
| Headings that skip a level (H1 → H3) | levels are closed up and the first heading is H1 (**Fix heading levels**) |

Nothing here changes a glyph on the page; only how it is read. A rebuild of a
140-page imported workbook put 1 `LI` and 7 `TOCI` back where PDF/UA allows
them, read 62 write-in lines as "Blank Line", and created 42 links (41 contents
entries + 1 web address) — leaving no `TOCI` without a `Link`.

**Rows with nothing in them.** A ruled worksheet can be found twice: once as the
row the ruling draws, once as an answer band stacked under the row above. Both
are real tables, the glyphs can only belong to one of them, and the loser ends up
as rows and cells with no content at all — PAC's *"Structure elements not
associated with content"* (UA-47). After tagging, a row (and a table left with
nothing but such rows) that owns no content is removed; a cell that merely
overlaps another is given its invisible "Blank cell" text in the part of the cell
no other cell covers, so it keeps its own glyphs. On the 150-page workbook that
closed the last 8 of these.

### Splitting a two-page spread (V4.6, `core/pages.py`)
A book exported as spreads prints pages 12 and 13 side by side on one sheet. Read
as one page, a screen reader goes across both halves line by line, and a
contents entry for page 13 can only land on "the sheet with 12 and 13 on it".
**Split page…** (page toolbar) makes the left half this page and the right half
the next page:

- **Nothing printed changes.** The sheet is copied and each copy shows only its
  own half (its MediaBox and CropBox are that half; the other half stays in the
  content stream outside the page and is marked as an artifact at Build).
- **Every tag goes with its half** and keeps its place on the paper; every later
  page moves up by one; the left page is read before the right page. In a file
  whose tags were imported, the marked-content keys of the right half follow it
  to the new page. A link annotation on the right half moves with it.
- **Contents links are searched again**, so "page 13" now lands on its own sheet.
- The dialog says whether the page looks like a spread (about twice as wide as
  the file's ordinary pages) and offers **Split every double page (n)** for a book
  full of them, cut at the middle or at 45% / 55%.
- **Put the last split back** restores the sheet, the tags and the automatic
  proposal as they were. Colours are measured again afterwards, and Undo starts
  afresh from the split.

### Lists: one list, with its note and its sub-points
Three rules that keep a list the shape the page shows (`join_split_lists`,
default **on**; also in **Issues → Tidy the lists**):

- **A note in the middle of a list** — "3. Point to the first nonsense word…"
  followed by a paragraph with no number of its own, then "4. Record the
  student's responses…" — used to end the list and start a second one counting
  from 4. The paragraph now goes into the item it belongs to (`LBody` > `P`) and
  the list carries on: one list of four, the third with a note.
- **Points set further in than the item above them** — "– If the student has
  successfully completed the easier skill set…" under a bulleted item — become
  an `L` inside that item's `LBody`. Only a *different* mark makes a level (a
  dash under a bullet, letters under numbers): numbers that simply carry on stay
  one flat list however they are indented, so a rubric that sets "1 and 2:" at
  the margin and "3:" under it is not split into levels.
- **A wrapped line that became an item of its own** goes back into its item (as
  before).
- **An item that runs on to the next page** — its paragraphs and its own list of
  points at the top of the next page, then "5." — is one list across the pages,
  with what ran on inside item 4.
- **Two columns read out of order** — the left column's bullets put between the
  right column's items 3 and 4 — no longer split the right column's list: the
  other column's tags are moved out of the way (before the list when they are to
  its left), and the list is joined.
- **A doubled dash** ("–– If the student has successfully completed…") is a list
  mark like "–", so those points become the list inside the bullet above them.
- **A sentence cut before a number** ("…go on to Skill Set" / "2: Letter Sounds.")
  is not a list item: a number at the start of a line carries on the sentence
  above when that line stops mid-sentence at the same margin.
- **A list carries on into the next column only from the top of that column** — a
  bulleted list further down it, under a heading of its own, is a new list.
- **Read list again** grows the list's zone down over points that sit just under
  it, set in past the item marks — the last dash point a hand-drawn zone left out
  is read into the list, and the separate tag it had is removed.
- **A value printed beside its item** — "• Task A:   9/10 correct" with the score
  at a tab stop on the item's own row — belongs to that item: "Task A: 9/10
  correct". The scores used to be read as a column of their own and ended up as
  one paragraph ("9/10 correct 19/20 correct") inside the last item. A value is
  short, sits on one of the item's rows, starts to the right of its text, and
  each item takes one value per row. **Tidy the lists** puts the values of a file
  tagged before this rule on their items, reading the page again.

### A grid of words is not a paragraph run over columns
A task form sets its words in columns — "discount / confide / portable /
nonsense", then "index / station / fastest", then "return / madness / careless".
Every word ends without a full stop and the next starts with a small letter,
which is just what a sentence carried over a column break looks like, so the
words were joined into one paragraph ("nonsense index station fastest return
madness careless"). Joining across a column or page now needs a real piece of
sentence on each side — at least four words before the break, and more than a
single word after it unless the part before is a long one — and never touches an
item of a grid. Each word stays a `P` of its own, read **down the first column,
then down the next**.

For a file tagged before this rule, **Issues → Separate words joined into one
paragraph (n)** gives each word its own `P` again, in that order, with the text
printed in its zone; and any text tag with several zones has **Split into one tag
per zone** in Properties.

### A list over a page break, in a file tagged before
A list started on page 6 and carried on page 7 by hand — its zone stretched over
the page break — can still be wrong inside: a paragraph straight in the list
("Scoring vowels: …"), a paragraph straight in an item, item 4's number inside
item 3, and the part on page 7 (the rest of item 4, its bullets, item 5) never
read into items at all. Both fixes now handle it:

- **Read list again** on a list with zones on two pages reads **every zone**, page
  by page, and puts the pieces back together with the automatic rules: what runs
  on goes inside item 4, the bullets become its list, item 5 joins — one list,
  1 to 5, across the pages.
- **Issues → Tidy the lists** does the same for every such list, and also gives
  a list items only and an item its label and body: a paragraph in the list goes
  into the item above, a paragraph in an item into its body, and a second number
  in one item starts the item it is the number of.

### Page numbers read as P (`page_numbers`)
A printed page number is pagination — an artifact, the PDF/UA default. Some
publishers want it read: with **Page numbers = P** (Options, per profile) every
page number becomes a `P` where it is printed — first on its page when it sits
at the top, last when it sits at the bottom — read as **"Page 9"**
(`page_number_text`, the same reading as the editor's "These are all page
numbers"). It never joins a paragraph that runs across the page break, is never
taken for a contents entry, and still tells the link search which sheet prints
which number. For a file already analysed, the **Document** panel (nothing
selected) has **Page numbers: Read as P / Leave out (artifact)** for every page
number at once.

### A rule to write on, however short
A fluency passage counts its lines in the margin with a two-underscore tick
(`__6`, `__11`, `__73`). Printed and read as nothing until now, so
they are tagged too: the tick becomes a `Span` read as **"Underscore"**
(`underscore_tick_text`), and the count stays in the paragraph ("Tim and Jim
play ball. They Underscore 6"). A longer rule (`_______`) is still a line to
write on and reads "Blank Line". Two
underscores only count where a word starts: `snake_case` and `x__y` are words,
not rules. **Issues → Read write-in lines as Blank Line** finds these in a file
tagged before the rule.

### An empty tag, and tidying a list made by hand
**Add an empty tag** (Properties) makes a tag for something the page does not
print — a row one cell short, a label a list item never had, a line left for an
answer. Pick the type, say whether it goes before, after or inside the selected
tag, and optionally what it should be **read as**: that wording is written into
the file as invisible text, the way an empty cell is, so the tag really is there
for a screen reader. It gets a small zone at the edge of the tag it was added
to — drag it on the page to place it. (A tag with no content and nothing to read
is still dropped at Build, unless it is a cell.)

**Issues → Tidy the lists** fixes what a conversion done by hand leaves behind:

- a **wrapped line that became an item of its own** ("… for the level" / "of the
  passage.") goes back into the item it belongs to — its words keep their own
  zone, so nothing moves on the page; the list counts the items the page shows
- **characters that are not really printed** — a symbol font's private-use
  bullet, a soft hyphen, a replacement character — are taken out of the tag text,
  the alt text and the Read-as wording. Converting to a list and drawing a tag
  clean them straight away.

### Drawing a tag inside a table or a list
A box drawn over the text in a cell is meant to tag what is printed there, not
to replace the cell. A hand-drawn `P` or `L` whose area sits inside a cell (or a
list item's body, or a section) is now added **inside** that tag, at the place
in its reading order where it is printed — the cell keeps its place in the
table, and the table keeps its shape. Drawing the lead-in line as a `P` and then
the items as an `L` gives `TD` > `P` + `L`, in that order.

Where that already went wrong — `TR` > `P`, `TR` > `L`, content printed in a row
but sitting outside every cell — **Fix structure** puts the whole run into one
`TD` where the cell was, so nothing moves in the reading order. A row may hold
cells and nothing else, and the Issues list says so for each such tag.

### A list printed inside a table cell
A fluency rubric prints its score key inside the cell under the grid:

```
Rubric Score Key
1 and 2: Student has not achieved an appropriate level of fluency for the
         level of the passage.
3: Fluent reading is being refined at the level of the passage.
4: Fluent reading has been achieved for the level at which the passage is written.
```

As one block of text a screen reader gives no sense that these are three graded
items. The cell keeps its place in the table; inside it the lead-in line becomes
a `P` and the items an `L` > `LI` > `Lbl` + `LBody` (`lists_in_cells`, default
**on**). Only a cell whose lines really are labelled items is changed — same
kind of label, same margin, numbers going up, one size — so "1  2  3  4" in a
score cell stays a plain cell. Labels include `1.` `a)` `•` and the rubric form
`1 and 2:` / `3:`.

For a file that is already tagged, **Issues → Tag the list inside a table cell**
does the same for every such cell, and Properties has **Tag the list inside this
cell** for the selected cell alone.

### A line that runs across several cells
A letter grid or a word grid is often printed row by row — "A B C D E" is one
line of text, even though each letter sits in its own ruled cell. Read line by
line, the whole row went into the cell its middle happened to fall in: "M N O"
in the N cell, and the M and O cells read **"Blank cell"** over letters plainly
printed there. A line is now cut at the cell borders — word by word, never
inside a word, and only when the words really do land in different cells — so
each letter is tagged in the cell it is printed in.

For a file tagged before this rule, nothing needs re-analysing: **Issues →
Re-read the text in table cells**, or **Re-read cells from the page** in the
Table box for one table, gives every cell the words printed inside it. A cell
that already holds tags of its own (a list inside a cell) is left alone.

### Reading order safety
- Changing a tag's type never moves it. An artifact turned into a real tag
  keeps its place; artifacts imported from the original file are listed at
  their position on their own page (they used to be listed at the end of the
  document, so "Artifact -> P" jumped to the last reading position).
- Build PDF checks the order first: tags read out of page order (e.g. a page-2
  tag after page-30 content) are listed with **Fix reading order**, which puts
  them back on their page by position. The Issues tab has the same button, and
  the checker verifies the built PDF (UA-O1).
- Import drops tag branches that belong to pages no longer in the file (PDFs
  cut out of a bigger book keep the old tag tree).

### Tables: full-width rows and the AI check
- A full-width row at a table's edge is only treated as a background panel
  when the rule detector can see this table's own row lines and none is drawn
  at that edge (or the row is far taller than the others). Tables whose lines
  the detector cannot read keep all their rows ("I see Sam." / "Sam sat." /
  "Go, Sam, go!" stay rows of the table). Lines drawn as transformed boxes
  (quads) are recognised.
- Full-width boxed rows drawn as a separate stack just under a table grid
  (a few points apart) are joined to the table as rows spanning every column
  — MuPDF does not report a one-column stack of boxes as a table.
- The row joiner merges rule lines drawn in pieces and uses row boxes
  (filled or stroked) as edges; one side line may be partly missing.
- Manual fix, whatever the drawing: select the Table **and** the tags under
  it (Ctrl+click in the tree) → **Add to table as rows**: each tag becomes a
  row with one cell spanning all columns.
- **Export page** (editor toolbar) downloads a zip with that page as PDF
  (original + working), the detected drawings/tables/text and the page's
  tags — send it when a page is tagged wrong.
- `ai_table_check` (AI group, off by default): for every full-width edge row
  the cleaner wants to drop, the vision model sees the table and the row's
  text and answers inside/outside; "inside" rows are kept.

### A zone drawn by hand, then a key (V4.7)
Draw tag (`D`), drag over the content, then **press a key** — no clicking
needed: `T` Table · `1`–`6` H1–H6 · `P` paragraph · `L` list · `F` figure ·
`C` caption · `N` note · `M` formula · `Q` block quote · `K` code ·
`A` artifact · `I` or `Enter` **Auto** (the tool decides) · `Esc` cancel.
The keys are shown on the buttons of the *Tag this area as* box.

With **use the AI** ticked (on by default when the AI is on in Settings):

* **Table** — always read deeply. The vision model counts the rows and columns
  it sees; of the grids that fit the zone, the one with that shape is used, and
  the model's header rows / header column are applied.
* **L** — read deeply; the model says whether the list is numbered.
* **Figure** — the vision model drafts the alt text (or says it looks
  decorative). **Formula** — the model reads it aloud (and gives LaTeX).
* **Auto** — the vision model says what the zone is (heading and its level,
  paragraph, list, table, figure, caption, formula, note, quote, code,
  artifact), told the font size, the body size and the heading before it.
  Without the AI the rules decide: a ruled grid → Table, numbered lines → L,
  a picture → Figure, an equation → Formula, a page number in the margin →
  Artifact, a large or bold short line → the heading level the document already
  uses for text set like that, otherwise P. The tag's note says why.

The model for each job is picked from what is installed (Settings → *Model per
job*): the best vision model (qwen2.5vl, qwen3-vl, gemma3, llama3.2-vision …)
for zones and pictures, a solid text model (qwen2.5, qwen3, llama3.1 …) for the
reference lists. A model that cannot be reached never stops the zone: the tag
is made by the rules and the note says the AI was unavailable.

### Reference lists: one entry, one BibEntry (V4.7, `core/biblio.py`)
The blocks under a *References / Bibliography / Works Cited / Sources /
Further reading* heading are read again line by line, and each entry becomes
**L > LI > [Lbl] + LBody > BibEntry** (option `bibliography_as`: `list`, or
`entries` for one BibEntry each straight in the section). Entries are found
from the print, not from where the paragraph breaks fell:

* a **hanging indent** (first line at the margin, the rest set in) or a
  first-line indent;
* a **number** — `1.`, `[1]`, `(1)` — which becomes the item's Lbl;
* the **space between entries**;
* the **author pattern** (“Surname, A. B.” after a line that ended an entry);
* and, when none of these settles it and the AI is on, the **text model**.

So *Kame’enui, … & Coyne, M. D.* — whose first line ends with a full stop and
was cut into three paragraphs — is one entry again. The analysis does this by
itself (option `detect_bibliography`); a file tagged somewhere else gets it on
import; the editor's Issues tab has **Tag the reference list** for a file
analysed before.

Citations are then linked to their entry (`link_references`, now on by
default): “(Hasbrouck, 2010)”, “Treiman et al., 2016”, “Graham, Harris, &
Chorzempa, 2002” (first author), “Smith and Jones (2019)”, and a citation broken
over two lines (“(Moats,” / “2005/2006)”), whose link gets a zone on each line.
An organisation as author (“National Institute … (2000)”) matches on its words.
An entry never links to itself.

### /ActualText on a Span inside the tag — every client (V4.15, all clients V4.16)
Wherever a tag carries /ActualText — a write-in line, a blank cell, a spelled sound card, a
page number, a letter-spaced logo line, anything set by hand in the editor — the file gets
`tag > Span` and the /ActualText is on the **Span**, which holds the tag's content (and any
inner tags / link annotations). The tag itself (P, H1, TD …) carries none. Figure and Formula
keep it on the tag (it is their description; a Figure without /Alt or /ActualText fails PAC),
a tag with no content (an empty cell) has nothing to wrap, and a tag whose children have to
stay where they are (a list item's Lbl + LBody, a table's rows, a contents entry) is left
alone.

* Option **ActualText on a Span** (`actualtext_span`, Build) — **on for every profile** since
  V4.16 (it was the Academain profile only); also a checkbox in the Build PDF dialog.
* **A file whose /ActualText still sits on the tag** — one tagged elsewhere and imported, one
  built with the option off, or a tag you have just typed a *Read as* sentence on — is listed
  in the editor's **Issues** tab with a **Put /ActualText on a Span (n)** button. It makes the
  Span in the tag tree itself: the tag keeps its place and its zone, the Span inside it takes
  the content, the words, the /ActualText and the /E, and the links printed inside go with it.
  *Fix everything the tool can fix* runs it too, and **Ctrl+Z** takes it back.
* The output checker reports what is left as **UA-58** (“/ActualText on the tag instead of on
  a Span inside it”, a warning, fixable) and passes UA-58 when every one sits on a Span.
* Checked by `tests/actualtext_span_test.py` (built with the option on, built with it off, and
  the one-click fix: the tree, the built file, UA-58 before and after).

### Two engines: V4 (Academain, every profile) and the client house styles (V4.13, `core/compat.py`)
The tool's own tagging — the **V4 structure**, the one the Academain client's
files are made with, and what every profile without a house style gives
(Default, Best quality, Book, Journal article, Word export, Scanned, Batch, and
every profile you saved) — runs exactly as it did in V4.9, before the client
house styles came in. Everything V4.10–4.12 added for Kriyadocs runs **only**
in a job whose options name a house style (*Client house style* = kriyadocs,
which the Kriyadocs profile sets):

* how lines are read (a space kerned away, a justified gap, raised marks and
  accents put back on their line), narrow gutters, the journal paragraph
  breaks (caps heading, note marks after a full stop, first-line indent),
  "Table 1 outlines …" as text, margin download stamps;
* arrows as list bullets, a glued "⇒Few …" as a label;
* the inner tag (a Link) winning its glyphs inside its parent's zone;
* every zone of a tag as a link owner, a link found again over two lines,
  citations inside a reference entry left alone, the note after the mark,
  "km²" not a note, note signs in table cells, a Note's number from its Lbl;
* bare-number reference lists and the wider label margin;
* Note > Lbl kept by the structure fixer and the checker, empty TDs reported
  as a warning;
* links placed in their cell when a table is drawn again.

The switch is per job (the analysis and build processes, and each editor
request, take it from the job's options; a request always starts on V4), so a
Kriyadocs job and an Academain job open side by side never mix. Checked by
running V4.9 and V4.13 on the same files with every built-in profile (the
test corpus, the Academain-type books X56811, X91690, X159504 and print_3, the
two journal articles): the tag tree, the built file's tree, its annotations and
the checker's findings are identical; the editor's drawn zones and tag
conversions give the same trees too (V4.9 failed with an error on one drawn
zone over a link — V4.13 does not).

`python tests/engine_split_test.py`

### Client profile: Kriyadocs (V4.10–4.12, `core/house.py`, `core/tablegrid.py`)
Pick **Kriyadocs** in the Profile list and press **Upload and analyse**: a BMJ
journal article (BMJ Open, BMJ Global Health …) comes out tagged the way
Kriyadocs tags it (their sample: *bmjgh-2024-018847*). On that sample, analysed
again from its pages, the tool's tree matches the client's own tree element for
element (the same sequence of tags, the same counts of every tag type; 682
content items against the client's 687).

```
Document > Sect (one, the whole article)
  P "Open access" · P "Original research"   printed on top of page 1, a P each
  Figure /Alt "BMJ Open"           masthead logo, named after the journal
  H1 title
  P authors > Figure "Orcid icon", Reference > Link (ORCID, affiliation marks)
  P To cite … · P Handling editor … · P supplemental (Reference > Link doi)
  P Received … · P Accepted … · P > Reference > Link "Check for updates"
  P © … · Note > Lbl "1" per affiliation · P Correspondence to … (Reference > Link e-mail)
  H2 ABSTRACT, P per section · H3 box heading + L (Disc) > LI > Lbl + LBody
  H2 / H3 sections, P > Reference > Link for every citation and link
  Figure "BMJ Group Logo" + P > Link     (page 1 footer, after page 1's text)
  Figure + Caption > P · Caption + Table > TR > TH/TD (no THead/TBody)
  Note > Lbl "*" (table notes) · P abbreviations · Formula /ActualText · P "where:" + a P per term
  H3 Author affiliations + Note … · P Acknowledgements … · H3 ORCID iDs + P per name
  H2 REFERENCES + L > LI > Lbl "1" + LBody
```

* **Profile** (built in, like *Book*): house style `kriyadocs`, journal tables,
  no THead/TBody, one Sect, reference list as L > LI > Lbl + LBody, every
  cross-reference as Reference > Link, write-in-line detection off, AI alt text
  on. Every option can still be changed in *Options for this upload*.
* **Journal tables** (`table_strategy = journal`): InDesign rules a table with
  horizontal lines or shaded bands only, so MuPDF found the rows but no columns.
  Now the rows come from the rules (and from where a column's text starts again
  inside a band), the columns from the gaps that run down the whole table; a
  group label beside its rows (“<60, male”) spans them; “Gender” over
  “Male | Female” spans both; the caption on top and the notes below are kept
  out of the table. A table ruled with lines only (no fills) is found too.
  “Table 3 Continued” in the next column or on the next page joins Table 3; the
  header printed again is a running heading.
* **Page 1**: the sidebar is read after the authors (it was read before the
  title); “► Prepublication history …” is one P (the arrow is decoration);
  the dates are a P each; Check for updates is P > Link (the badge is
  decoration, the link says “Check for updates”); one Note per affiliation;
  “Correspondence to” + name + e-mail is one P. The box (“STRENGTHS AND
  LIMITATIONS…”, “WHAT IS ALREADY KNOWN…”) follows the abstract.
* **Running text**: a paragraph cut by a column, a page or a figure at the top
  of the next page is one P again (the figure and the table notes come after
  it); the download stamp up the margin (“Downloaded from … Protected by
  copyright”) is an artifact and its link is read at the end of its page.
* **References**: numbered with bare numbers (“6 Steward DL …”, right-aligned
  digits) are split per entry even across a page; the links inside an entry
  stay in its LBody; “Accra, Ghana, 2020.” inside an entry is not a citation.
* **V4.11 — the rest of the client's sample, in the Kriyadocs profile only**
  (other profiles are unchanged):
  * **No empty content items.** InDesign sets a tab, an em space or a soft
    hyphen as a space glyph in a text object of its own (with an /ActualText
    span). Such white space is no content item of its own any more — it goes
    with the text beside it, or is an artifact (as Acrobat does in the
    client's file) — so Acrobat's Tags panel shows no empty item inside the H1,
    a P, a Note or a cell. Option *One content item per tag* (Build): one content
    item for a tag across the text objects it runs over.
  * **“Open access” and “Original research”** on top of page 1: a P each.
  * **Note > Lbl**: each affiliation is a Note with its number in a Lbl
    (“1” + “Department of …”); each table note a Note with its mark in a Lbl
    (“*” + “T-test.”), as the client tags them. ISO 32000-1 names Lbl as a child
    of LI only, so PAC may list it — the client asks for it; the tool's own
    checker accepts it.
  * **Formula in words**: every Formula carries the equation spoken, as
    /ActualText (like the client) and /Alt, read from the layout — a stacked
    fraction over a drawn bar is “one-fifth” or “the fraction … over …”, a
    lowered small i is “sub i”: “CCI equals one-fifth times open bracket FP
    plus the fraction ANC4 plus SBA over 2, …”, “ln open bracket output close
    bracket sub i equals …”.
  * **Links**: no /Alt on the Link tags (the client has none); the
    description (“ORCID iD”, “Check for updates”, “BMJ Group”, the citation
    “1”) is the annotation's /Contents, which PDF/UA asks for.
  * **The download stamp's link** up the margin (“Downloaded from
    http://bmjopen.bmj.com/ …”) is taken out of the file — no stray Reference /
    Link at the end of each page any more.
  * **Empty table cells stay empty** (no “Blank cell” text), like the sample;
    the checker reports them as a warning. Merged cells keep their row/column
    spans (Option *Table spans* = pad gives a full grid instead).
  * **PDF version 1.6** at least (the client's files are 1.6; a 1.5 file is
    raised, a 1.7 file kept).
* **V4.12 — Kriyadocs, the next round** (other profiles unchanged):
  * **Note links.** Each author's affiliation mark (“Yudie Yan,¹”) is
    Reference > Link to its affiliation Note, and each mark in a table cell
    (“<0.001*”, “0.080†”) Reference > Link to the table's own note — as in the
    client's file, where they are links too. The link goes to the note itself
    (not just its page) and says where it goes: “Affiliation 1: Department of
    Ultrasound, …”, “Table note †: T-test.” (a screen reader would only say
    “1” or “dagger”). The link search reads the number of a Note from its Lbl.
  * **The title is one content item.** InDesign often sets the last line of a
    title and the authors in one text object; the text object is now split
    where the tag changes (the positioning replayed exactly, so the page
    renders to the pixel), and the H1 is one content item, as in the sample.
  * **Empty cells**: checked against the client's file — it has a TD tag for
    every empty cell (63 in bmjgh-2024-018847, no content inside), so the
    Kriyadocs output keeps the empty TD tags the same way.
  * **Formula in words, as a sentence**: “CCI equals one-fifth times the sum of
    FP, the average of ANC4 and SBA, the average of BCG plus two DPT3 plus MSL,
    LLIN, and TB.” (the client's own reading, word for word but for
    “measles”); “The natural log of output sub i equals the natural log of HF
    sub i plus …”. A fraction whose top is a sum of n terms over n is “the
    average of”; a factor times a bracketed sum is “times the sum of”; ln, log,
    sin … are said as words; a subscript after the bracket of one name goes
    with the name.
  * **Drawn and converted tags (the editor) follow the client's structure**: a
    Table drawn over a journal table is read with the journal grid, its
    caption and notes come out as Caption before it and Note > Lbl after it,
    its rows stay straight under the Table (no THead/TBody — any table edit
    that grouped rows is undone), and the citations and note marks in it are
    linked again (only inside what was drawn); a Note drawn or converted gets
    its Lbl; a drawn or converted Formula is said in words; a bulleted list is
    Disc; every link is Reference > Link; the Notes are numbered NOTE 1, 2 …
    again.

**Viewer zoom (every client, V4.12):** *Fit page* and *Fit width* in the zoom
list (and 50 %–400 %); **Ctrl +** / **Ctrl −** zoom in and out, **Ctrl 0** fits
the page; **Ctrl + mouse wheel** or a **touchpad pinch** zooms at the pointer;
**dragging with the right mouse button** zooms (up: in, down: out); the
**middle button** drags the page around. The browser itself does not zoom, the
page keeps the point under the pointer in place, and the zoom (or fit) is
remembered for the next file.

Fixes for every profile: drawing a zone over a tag tree that holds a link
directly no longer fails with an error; splitting a spread keeps every link
tag on its annotation.

Reading fixes that came with it — **Kriyadocs engine only** (V4.13: the V4
engine, Academain's, reads exactly as before; see *Two engines*):
* a link inside a drawn Table or list stays in the cell or item it is printed
  in (it went after the table);
* a line MuPDF broke at a glyph with an accent drawn over it (X̄), at a raised
  note mark (“… programmes.⁵”, “health services.⁶ ⁷ Ghana …”) or at a wide
  justified word gap (“…tab_1)). This”) is one line again; a space kerned away
  to nothing (“cross- sectional”, “zhenshcc@ 163. com”) is no space;
* a narrow gutter (7 pt) between two columns of real lines is a gutter;
* a journal's first-line indent after a full line starts a paragraph, and
  note marks after a full stop do not hide the end of the sentence;
* “Table 1 outlines the variables …” is running text, not a caption;
* “1070 per km²” is a unit, not a footnote mark;
* a tag's own child (a Link over its words) gets the glyphs it covers.

`python tests/kriyadocs_test.py`

### "Name: ____  Date: ____" — one P per prompt (V4.9)
* **Printed text, not ActualText.** A file tagged before (by this tool or
  another) carries /ActualText on its write-in lines (“Blank Line”) and ticks
  (“Underscore”); MuPDF put that text in place of the glyphs, so “Name: ____”
  was read as “Name:Blank Line” and no blank — not even in a zone drawn again —
  could be found. Every place the tool reads the page now reads the glyphs
  (`core/textflags.py`); the tag's /ActualText is kept separately.
* **One row, several prompts.** A printed row with several labels and
  write-in lines — “Name: ____ Date: ____”, “Correct Words per Minute ____
  National Norm of Words per Minute ____” — becomes one P per prompt, each with
  its own blank-line Span. A sentence with two blanks (“The ____ ran to the
  ____.”) and a blank with words after it (“____ of 26”) stay one P. The same
  happens in analysis, in a zone drawn by hand, and on import.
* **The label's P holds only the label.** “Name: ________” is tagged
  P “Name:” > Span “________” (read “Blank Line”): the underscores belong to the
  Span, so the P's own text is just “Name:” — the line is not shown, or read,
  a second time. A line inside a sentence (“The ____ ran”) keeps its place in
  the sentence. Files opened before are tidied by the same Issues button.
* **The label keeps its colon.** A glyph printed inside a zone now belongs to
  it before a neighbour that only reaches it with the margin: the “:” of
  “Date:” sits 1 pt left of the write-in line and used to go into the blank's
  Span (read “Date Blank Line Blank Line”).
* **Imported files are rebuilt.** A row tagged as one P, or a blank that took
  its label's colon, is rebuilt from the page on import; the Issues tab has
  **Split “Name: ____ Date: ____” rows** for a file opened before, and Fix
  everything runs it. An imported tag's text is now taken character by
  character from the glyphs it owns (it was taken word by word, so “Name:” went
  into the underscores' Span).
* **Contents group titles** (“Placement”, “Progress Monitoring” — no page
  number) link to where their first entry goes, or to a heading of that name
  when it comes no later; no “nothing to click” warning for them. A **Link tag
  with no link annotation** (PAC: Link element without a link annotation) keeps
  its words as a Span and the search makes it a real link.

### A drawn tag is read where it is printed (V4.9)
A file tagged elsewhere often holds the whole book in one Sect. A tag drawn by
hand on such a file — when nothing was replaced, or after re-drawing over tags
that had already gone astray — went to the end of the document, after that
Sect, and was read last (reading position 1209 of 1212). Now a drawn tag goes
into the section that holds its page's blocks, at its place on the page (page
first, then top to bottom); it never lands inside a paragraph or a span. A
block that is read after a section running past its page (a page-30 paragraph
after a Sect that ends on page 140) is reported as a reading-order issue, and
**Fix reading order** puts it back inside the section, on its page.
`python tests/zone_order_test.py`

### Footnotes and captions, linked both ways (V4.8)
* **Footnotes** (`link_footnotes`): a footnote mark in the text — a digit (or
  `*` `†`) set small and raised after a word — jumps to its note: the note with
  that mark on the same page, or for endnotes the next note with that mark (a
  *Notes* section's numbered entries are tagged Note). **Back** (`link_back`):
  the note's own mark jumps back to where the text refers to it. The link
  descriptions say “Go to note 3” and “Back to the text of note 3”.
* **Captions**: “see Figure 3” in the text jumps to the figure
  (`link_figures_tables`, now on by default), and the caption's own
  “Figure 3” jumps back to the first place the text mentions it (“Back to
  where Figure 3 is mentioned”). A list of figures is not counted as the text.
* Links the tool makes inside one tag are kept in printed order, line by line,
  so “decoding 3, 4” reads 3 before 4.

### The index: Index > L > LI, every page number a link (V4.8, `core/backmatter.py`)
The entries under an *Index* heading become **Index > L > LI > LBody**, one
item per entry. A sub-entry (set in one step) is a list inside its entry's
LBody; a line that only wraps (“of spelling, 3,” / “4”) stays with its entry;
letter dividers stay as they were (a letter tagged as a heading stays a
heading, so H jumps letter to letter). Two-column index pages are read column
by column. Every page number — the wrapped ones too — links to that printed
page (`link_index`, now on by default). Option `detect_index`; the Issues tab
has **Tag the index** for a file analysed before; imported files get it too.

### Reading order, checked by the AI (V4.8)
Issues tab → **Check this page** / **Check every page**: the vision model gets
the page with every reading block boxed and numbered in reading order (the
same blocks as the editor's numbers) and says whether a reader would go
through it that way. Where it would not, the page is listed with the AI's
order and the reason; **Show** opens the page, **Apply** puts the blocks in
that order — each block only moves among its own siblings, so a section, list
or table keeps what it holds — and Undo takes it back. The answer is checked
(every block once) before it is offered. On a CPU a page takes about a minute;
**Stop** ends a whole-book check.

### Fix everything the tool can fix
The Issues tab's first button runs every one-click fix in the order that
works — structure, lists, the reference list, heading levels, reading order,
list labels, empty headers, write-in lines, lists in cells, page numbers, alt
text from captions, the index and endnotes, and then every link again
(contents, footnotes and captions both ways, index page numbers) — and says
what each one changed. Every
step is undoable.

### Best quality (AI, slower) profile
Every AI check on (alt text drafts, table headers, table
merges looking at the pages, doubtful table rows, reference lists) and every
kind of cross-reference linked. A big book takes longer — the tagging is what
counts. The AI options do nothing when the AI is off in Settings.

### Re-analyse the whole file
Editor toolbar **Re-analyse file…** (or **Re-analyse** in the file list) runs
the full upload analysis again on every page, with a choice of existing-tags
mode and AI checks. All tags are replaced; the previous tags are kept as
`model.before_reanalyse.json` in the job folder.

### Speed (multi-core)
Settings → **Speed**: *Files at the same time* and *CPU cores per file*
(0 = automatic: half the cores' worth of files, the rest split per file).
Changes apply without a restart. Inside one file, layout analysis and the
contrast measurement run page chunks in parallel processes
(`core/parallel.py`); the result is identical to a single-process run.
`PDFUA_PAGE_PROCS` sets the cores per file for the CLI.
Updates do not ship `config.json`, so your Settings survive replacing the folder.

## The editor

| Area | What you can do |
|---|---|
| **Tag tree** | collapse/expand, search, filter (this page, issues, headings, figures, tables, lists, links, low confidence, artifacts), multi-select with Ctrl/Shift, drag rows to reorder, **Shift+drop to nest** |
| **Page view** | numbered boxes in reading order, colour by tag type; click a box ↔ tree; double-click selects the parent; toggle boxes / numbers / labels / built result |
| **Tag zones** | select a tag → drag its box to move it, drag a handle to resize; the tag then owns exactly the content inside (text is re-read) |
| **Reading positions** | Acrobat-style: page-level blocks are numbered 1, 2, 3… (a table or list is one block); nested tags are 5.1, 5.2 … Click a number on the page, click the number of the selected tree row, or use the Properties field, type the new position |
| **Auto-tag page…** | replace the tags on one page or a range (`5`, `5-8,12`) with the automatic proposal; other pages untouched; optional re-analysis with current options; undoable |
| **Draw tag** (`D`) | drag a rectangle → press a key (`T` table, `1`–`6` headings, `P`, `L`, `F`, `C`, `N`, `M` formula, `Q`, `K`, `A`, `I`/`Enter` auto) or click. Tags inside are replaced (links survive). Table and L rebuild full structure from the area; **use the AI (Ollama) for this zone** starts unticked (V4.16) — tick it and the model reads tables, lists, figures, formulas and Auto zones, in seconds and without holding up the rest of the editor |
| **Reading order** (`O`) | click boxes in the order they should be read → Apply |
| **Read as (/ActualText)** | the sentence a sound-spelling card should be read as, built from the glyphs on the page (“c, a and t, a highlighted in red”) — suggested for one tag or for a whole selection, applied one-per-tag or the same to all, and cleared the same way |
| **Add an empty tag** | a tag for something the page does not print (a missing cell, a Lbl, an answer line), placed before, after or inside the selected tag, with what it should be read as |
| **Properties** | tag type, alt text with image preview + AI draft + "decorative", artifact type/subtype, TH/TD toggle, Scope, row/column span, list numbering, link/field description, language, actual text, `/E` expansion, element ID, move/merge/remove |
| **Table tools** | header rows, header columns, association mode (auto / Scope / IDs+Headers), **Group by heading**, **Re-read cells from the page** |
| **Multi-select** | set type, wrap in Sect/Div/Part/Art/BlockQuote/L/TOC/…, make list, merge, mark as artifacts, remove |
| **Issues** | live PDF/UA problems in the tag tree; click to jump; `.` = next issue; **Fix everything the tool can fix**, **Check reading order with the AI** (this page / every page, Apply per page), and one-click fixes for what an imported or auto-tagged file usually needs — **Tag the reference list**, **Tag the index**, **Fix heading levels**, **Fix structure**, **Read write-in lines as Blank Line**, **Tidy the lists**, **Re-read the text in table cells**, **Tag the list inside a table cell**, **Alt text from the caption**, **Put /ActualText on a Span** — each showing how many it would change |
| **Tag-type filter** | a second filter listing every tag type in the file with its count (`TD (3 700)`, `Artifact:Footer (307)`), and **Select all shown** to select the whole filtered set at once |
| **Links** | every cross-reference found in the text, the target it matched, and a tick box per kind (web, contents, chapter/section, figure/table, bibliography, index) + **Apply and search again** |
| **Checks** | before/after scores, veraPDF verdict, findings, fixes applied, preflight log, downloads |
| **Preview** | what a screen reader announces, in reading order |
| **Undo / redo** | every edit (60 steps) |

**After drawing a zone:** `T` table · `1`–`6` · `P` · `L` · `F` · `C` · `N` · `M` · `Q` · `K` · `A` · `I`/`Enter` auto · `Esc`.

**Keyboard:** `↑/↓` select · `Alt+↑/↓` move · `Alt+←/→` out of / into parent ·
`Del` remove tag · `1`–`6` H1–H6 · `P` paragraph · `F` figure · `A` artifact ·
`C` caption · `N` note · `H`/`T` TH/TD · `D` draw · `O` reading order ·
`[` `]` page · `.` next issue · `Ctrl+Z` / `Ctrl+Shift+Z` · `Esc`

---

## Options and profiles

Every option is on the upload form (grouped Preflight / Analysis / Build / AI /
Client / Mode) and can be saved as a named profile. Built-in profiles: **Smart AI (all
samples)** (the default, V4.25), Best quality (AI, slower), Default, Journal
article (2-column), Book, Word / report export, Scanned document (OCR), Batch
auto (no review), and the client profile **Kriyadocs** (BMJ journal articles in
the client's own tag structure — see *Client profile: Kriyadocs*).
`python cli.py --list-options` prints them all.

---

## Batch and CLI

Web: upload several files, tick **Build without review**, download the batch
summary workbook.

```bash
python cli.py incoming/ -o out/ --profile Book --set lang=en-GB --set bookmark_levels=2
python cli.py tagged.pdf -o out/ --set existing_tags=import
python cli.py incoming/ -o out/ --set fix_contrast=all --set contrast_level=AA
python cli.py out/*.pdf --check            # checker only, non-zero exit on errors
python cli.py incoming/ -o out/ --verapdf /opt/verapdf/verapdf --ai
```

Output per file: `<name>_accessible.pdf`, `<name>_accessibility_report.xlsx`,
plus `batch_summary.xlsx`.

---

## Report (Excel)

Summary (scores, veraPDF, build stats, preflight log, automatic fixes, items to
review) · Findings after · Findings before · veraPDF · Tag tree · Figures (alt
text status) · Tables · Links & fields · Removed empty tags · Unmatched content ·
Manual checklist (with Pass/Fail/N/A dropdown) · Options used.

---

## Verification (tests/)

```bash
python tests/make_corpus.py      # 2-column article, book with TOC/bookmarks, forms,
                                 # scanned page, form XObjects + OC layer, Tamil/Greek text
python tests/run_suite.py        # every file: pixel-identical rendering, qpdf --check,
                                 # parent-tree integrity, balanced marked content,
                                 # rotated pages, import round trip
python tests/multipage_table_test.py   # a table over several pages -> one Table
python tests/ai_table_merge_test.py    # doubtful page joins, answered by a fake Ollama
python tests/ai_helpers_test.py        # V4 AI helpers (headings, links, fields, tables, maths)
python tests/xref_test.py              # V4 cross-reference links, end to end
python tests/zone_merge_test.py        # joining split tags, deep zone read, page numbers
python tests/list_run_test.py          # numbered items split across tags, zone -> paragraphs
python tests/grid_table_test.py        # word grids: no invented header, repeated pages, boxed rows
python tests/actualtext_test.py        # Read as: spelled letters, the ones printed in colour
python tests/actualtext_span_test.py   # /ActualText on a Span inside the tag, and the one-click fix
python tests/ai_speed_test.py          # the zone AI: asked once, capped, and never while the job is locked
python tests/table_groups_test.py      # a table grouped by its headings, column by column
python tests/repair_test.py            # imported files: misplaced tags, TOC links,
                                       # write-in lines, alt from the caption, phantom rows
python tests/list_shapes_test.py       # nested dash lists, a note inside an item, an item
                                       # across pages, a wrapped "2:" that is no item
python tests/pages_test.py             # splitting a two-page spread, tagged and imported
python tests/biblio_test.py            # reference lists -> BibEntry, citations linked
python tests/zone_keys_test.py         # drawn zone + key, Auto, AI on / AI unreachable
python tests/twoway_test.py            # footnotes and captions linked both ways
python tests/index_test.py             # Index > L > LI, sub-entries, page-number links
python tests/order_ai_test.py          # the AI's reading order, checked and applied
python tests/prompt_rows_test.py       # "Name: ____ Date: ____": analysed, imported, drawn
python tests/zone_order_test.py        # a drawn tag is read on its page, inside the Sect
python tests/kriyadocs_test.py         # Kriyadocs profile: a BMJ article tagged the client's way
python tests/engine_split_test.py      # the V4 engine (Academain, every profile) untouched by house styles
python tests/links_stage_test.py       # V4.18 Links stage: set by hand, the file's links, check + fix
python tests/table_edit_test.py        # V4.18 Edit table: rows/columns, cells, lines moved on the page
python tests/table_template_test.py    # V4.19 table parts, zones, split/join tables, table templates
python tests/list_edit_test.py         # V4.19 Edit list: split/join/nest items, zones, list templates
python tests/admin_test.py             # V4.19 sign-in, roles, allocation, work status, the log
python tests/marks_test.py             # V4.19.1 Ctrl+N marks a table / list, the next ones get its structure
python tests/cell_figure_test.py       # V4.19.2 a picture in a cell tagged once; no empty TR after merging
python tests/zone_delete_test.py       # V4.19.3 a zone + Delete: the tags wholly inside it go
python tests/symbol_bullets_test.py    # V4.19.4 Wingdings/Symbol bullets read right, made the item's Lbl
python tests/samples_test.py           # V4.20 sample files teach the tool a client's tagging; a file's profile changed
python tests/learning_test.py          # V4.21 approved files become samples; people's fixes; AI with samples; captions
python tests/edit_advanced_test.py     # V4.21 Edit table (tidy, headers, empty cells, paste, summary) and Edit list options
python tests/wordgrid_test.py          # V4.21.2 a wide word grid reads row by row; two-column list items; Lbl from a box
python tests/table_order_test.py       # V4.21.3 Edit table: reading numbers, columns moved, the tags inside a cell ordered
python tests/order_select_test.py      # V4.21.4 reading order check: out of printed order, order as printed, order by clicking; list items many at once
python tests/list_columns_test.py      # V4.21.4 list items with a second text in a column of its own: every engine, list zones over both columns / the words only
python tests/tag_levels_test.py        # V4.21.4 several tags a level in / out, up / down, heading levels up / down
python tests/tab_session_test.py       # V4.21.4 (server running) a sign-in ends when every tab is closed; reload / new tab / sign out in one tab
python tests/inline_zone_test.py       # V4.21.4 a Span / Quote drawn inside a paragraph or heading: inside it, in reading order, built in printed order
python tests/pages_tags_test.py        # V4.22.0 remove the tags of a page / pages / the whole file; re-analyse only some pages
python tests/workflow_test.py          # V4.23.0 Production → QA → Final QA → Delivery: roles, PAC report, rework rounds, comments, read only
python tests/teachers_guide_test.py    # V4.24.0 Academain Teacher's Guide: H1–H5 as the client's book, boxes H5 + P (no table), grid words, "Blank line", "Rr", "Page N", one Sect
python tests/brain_test.py             # V4.25.0 the self-learning AI brain: samples studied, its own model trained, every client's samples, feedback, trust, learning curve
python tests/selflearn_test.py         # V4.26.0 learned code written and checked, memory, one fix → the rest, inventory + coverage, privacy guard, rule book
python tests/selfimprove_test.py       # V4.27.0 honest probabilities (calibration, cross-validation), one file one convention, tighter rules, newest memory wins
python tests/zonelearn_test.py         # V4.28.0 where zones are cut, learned from what people change: per client, for everybody, the parallel analysis decides the same
python tests/zonelearn_app_test.py     # V4.28.0 one cut by hand → the same cut offered elsewhere in the file → learned live by a learner process → approved → the next file is right
python tests/multiuser_test.py         # V4.28.0 many people at once: the lock, the fair queue, config.json, who else is in the file, no lost change (PDFUA_LOCK_AUDIT=1)
python tests/filelock_test.py          # V4.29.0 one person at a time in a file: the others look, every change is refused (423), take over, let go, expiry, the switch
python app.py & python tests/ui_front_test.py    # V4.29.0 the front page in a browser: the demo, the moving parts and their pause, light / dark, a phone, signing in (V4.32.0: and the new parts — the teacher's story, the preview, the checks, the questions)
python app.py & python tests/ui_lock_test.py     # V4.29.0 two people, one file, in two browsers: view only, take over, let go, leaving
python tests/patch_test.py             # V4.30.0 what an edit changed is written, told and shown: the patches equal a new load after 26 kinds of change, the file is exactly the tags, 304 / gzip, the lists of files
python tests/perf_test.py              # V4.30.0 speed: a file of 4 000 tags edited in tens of milliseconds, the heartbeat answers during edits, 1 500 files listed in a blink and with a 304
python tests/worktime_test.py          # V4.30.0 working time: only active seconds, Working and QC apart, never more than has passed, the idle limit, the lists, the report, the CSV
python tests/rights_test.py            # V4.30.0 rights: role defaults, Allow / Deny, refused on the server with the reason, see all, take over, who spent the time
python app.py & python tests/ui_admin_test.py    # V4.30.0 Admin in a browser: filters and chips, Reset, views kept, the working time, the Rights dialog, Time, Workflow, Activity
python app.py & python tests/ui_users_test.py    # V4.30.0 the Users page: filters, chips and Reset, rows kept between draws, 150 rows at a time, the upload chips, the person's time
python tests/priority_test.py        # V4.33.0 the worker that analyses or builds a file runs below the web server's priority (a person editing comes first)
python app.py & python tests/ui_tree_test.py $OUT $PDFUA_JOBS   # V4.33.0 the tag tree of a file of ~6 000 tags in a browser: a window of rows, the scroll bar of the whole list, no picture held up, keys, search, select all, a click on the page, collapse/expand
python tests/model_integrity_test.py # V4.32.2 Build PDF never stops on a table or a row that is already gone; a fuzz of the editor's operations: after each one the tags are still a tree, and a file built from the result builds
python tests/front_facts_test.py     # V4.32.0 the front page tells only what the tool can show: its numbers are counted in the tool, the before → after table is measured again, nothing leaves the network, the page is built as the PDFs it asks for (V4.32.1: no product name of the model service on the page)
python tests/teacher_test.py           # V4.31.0 Ollama teaches Dez AI: lessons, answers alone, proven students and audits, people outrank the teacher, Ollama off, the Admin API
python app.py & python tests/ui_teacher_test.py   # V4.31.0 Admin → Dez AI → Ollama teaches Dez AI in a browser: the card, Study now, levels, switches, lessons, forget
python app.py & python tests/ui_zone_test.py     # V4.28.0 a zone cut by hand → the offer “Cut all N”, Admin → System and Dez AI
                                                 # (the browser tests take a Chromium you already have from PW_CHROMIUM=/path/to/chromium)
python app.py & python tests/api_test.py && python tests/ui_test.py /tmp
```

Current results: every corpus file renders **pixel-identical** after tagging;
structure integrity and qpdf checks clean; machine-check errors go to **0** on
every file once figures have alt text; the import round trip reproduces the
identical structure; 300 pages process in under a minute and an edit round-trip
on a 300-page book takes ~0.35 s.

---

## Architecture

```
app.py              web app: job queue/worker, editor API, undo/redo, settings, profiles
cli.py              batch command line
core/options.py     options, labels, built-in profiles
core/model.py       StructNode / DocModel / Finding
core/pdftext.py     content-stream interpreter + font metrics (glyph positions)
core/preflight.py   form unpacking, font embedding, ToUnicode repair, OCR
core/analyzer.py    layout → proposed tag tree
core/importer.py    existing tags → editable model (keeps original MCIDs)
core/tagger.py      two-pass content rewrite with operator splitting
core/structure.py   StructTreeRoot, ParentTree, OBJR, IDTree, attributes
core/remediate.py   document-level fixes, bookmarks, XMP
core/build.py       tag + structure + remediate, one save
core/checker.py     ISO 14289-1 machine checks, veraPDF runner
core/lint.py        live issues for the editor
core/edit.py        editor operations (draw, drop, wrap, list, merge, table headers)
core/report.py      Excel report and batch summary
core/ai.py          Ollama client
core/learn.py       sample files: what a client's finished files teach
core/brain.py       the self-learning AI brain (V4.25) — honest probabilities, one convention per file (V4.27)
core/codegen.py     the learning engine writes its own rules as Python (V4.26) — one tight rule per look (V4.27)
core/memory.py      alt texts and readings people gave, used again (V4.26)
core/zonelearn.py   where a zone is cut or joined, learned from what people change (V4.28)
core/livelearn.py   the learner process: what people changed in a file, learned a few seconds later (V4.28)
core/locks.py       the one-at-a-time lock that serves the waiting in the order they came (V4.28)
core/fairqueue.py   the work queue that serves each person in turn (V4.28)
core/editlock.py    one person at a time in a file — the others look (V4.29)
core/modelsync.py   what an edit changed: a copy of the tags, the difference, the file written from kept pieces (V4.30)
core/jobindex.py    every file's meta, read once and kept — a list of files opens nothing (V4.30)
core/worktime.py    how long people really work on a file: active seconds, Working and QC (V4.30)
core/rights.py      what a person may do on top of the role (V4.30)
core/teacher.py     Ollama teaches Dez AI: the gate, the lessons, the students, the audits (V4.31)
core/inventory.py   everything in the PDF, and what no tag holds (V4.26)
core/kb.py          the offline rule book (V4.26)
core/netguard.py    the privacy guard: nothing leaves the server (V4.26)
core/pipeline.py    prepare / finish / process_file
```

---

## Honest limits

- **Not a conformance certificate.** The built-in checker covers what software
  can decide. Confirm delivery files in **PAC 2024** and/or **veraPDF**. Six
  checkpoints are manual by design (contrast of text *inside images*, alt-text
  quality, reading order, colour-only meaning, tag semantics, decorative content).
- **Contrast fixes change the design.** They are off unless ticked (or
  `fix_contrast=all`). Get the client's agreement; for print files the CMYK
  change is computed on screen colour management, so proof it.
- **Created links are a change to the file.** Only web/e-mail addresses and
  contents entries are linked by default; the rest are found, listed and left
  alone until you switch them on in the editor's Links tab. Check a sample in
  Acrobat before delivery — a wrong destination is worse than none.
- **Spoken maths is exact for expressions set as text** (`a² + b² = c²`). A
  stacked fraction, a big integral or anything drawn as graphics has no text to
  read, so it needs the vision model or your own wording.
- **A handful of cells can still end up empty** in a ruled form whose grid
  genuinely overlaps: on the 147-page phonics file, 5 cells out of ~3 700.
  They are listed by the checker (UA-47) so they can be fixed by hand.
- **veraPDF** integration is implemented against its JSON report format but was
  not run against the real validator here (it could not be downloaded in the
  build environment) — test it once on your server.
- **Font embedding** covers the standard 14 fonts and their Arial / Times New
  Roman / Courier New equivalents using metric-compatible URW fonts. Other
  non-embedded fonts must be embedded at source. If the file's declared widths
  differ from the substitute, preflight warns (veraPDF 7.21.5).
- **ToUnicode repair** only writes mappings it can derive with certainty
  (TrueType cmaps, glyph names). CFF CID-keyed fonts without names are reported,
  not guessed.
- **Form XObjects** with transparency groups using blend modes/soft masks,
  patterns or optional content stay packed; their text is tagged as one unit.
- **Tables** come from ruling lines (and optionally whitespace). Borderless or
  nested tables usually need the Draw → Table tool or manual edits.
- **Layout analysis is heuristic.** Unusual designs (text on paths, heavy
  overlaps, rotated text blocks) need review — that is what the editor is for.
- **AI output is a draft.** A fluent but wrong alt text is worse than none
  because it looks finished. Read every one.

- **Empty header cells:** a header cell (TH) with no content is always a data cell (TD).
  Analysis and build convert them automatically; in the editor an empty TH shows the error
  "Empty header cell (TH)" with a **Fix empty headers → TD** button that converts all of them
  at once. The output checker reports leftover empty TH as UA-36.


---

## V4.14 — Kriyadocs: Note = Lbl + P

**Kriyadocs engine only** (Client house style = kriyadocs; Academain and every other client stay on the V4 structure, unchanged).

Every Note now holds two tags and nothing loose: `Note > Lbl` (the number or mark — `1`, `*`, `†`) and `Note > P` (the text). Before, the words sat directly in the Note beside the Lbl.

- Links / references printed in the note's words go inside the P; the link over the mark itself (the way back to the text) goes inside the Lbl.
- Runs after every analysis and after every editor edit of a Kriyadocs job (a Note drawn by hand gets Lbl + P too).
- A Note with no number or mark to split off (no Lbl) is left as it is.
- Cross-references: a word found in a Note's Lbl or P counts as a word of the Note (a note's own number is not linked as a mention; its way-back link starts at the mark).
- Test: `tests/kriyadocs_test.py` checks every Note in the built file is exactly Lbl + P with no loose text, and that a non-Kriyadocs job gets no P in its Notes.

---

## V4.16 — /ActualText on a Span for every client · the zone AI, quick and out of the way

**Every client** (no house style needed, the V4 and the Kriyadocs engines alike).

**1 · /ActualText only ever on a Span.** The option *ActualText on a Span* is now on in every
profile, so every client's build writes `tag > Span` with the /ActualText on the Span and none
on the P, H or TD (see */ActualText on a Span inside the tag* above). Where one is still on a
tag — a file tagged elsewhere, a job built with the option off, a *Read as* sentence you just
typed — the editor's **Issues** tab offers **Put /ActualText on a Span (n)**, which makes the
Span in the tag tree; the output checker reports the rest as **UA-58**. Figure and Formula keep
theirs (it is their description), and a tag with nothing in it has nothing to wrap.

**2 · “use the AI (Ollama) for this zone” starts unticked.** Draw a zone and the tool tags it
from the page at once; tick the box only for a zone you want the model to look at, and it then
stays ticked for the next zone. *Deep read for Table / list* is still on, so a drawn table is
read with every grid strategy as before — without the AI.

**3 · And when you do tick it, it is quick.** What used to take a minute and freeze the whole
editor:

* **The model stays in memory.** Every request now says `keep_alive` (Settings → *Keep the
  model loaded*, 30 minutes by default). Loading a vision model from disk again for each
  question was most of that minute.
* **The picture is no bigger than it needs to be.** A zone is sent at most 1100 px on its
  longest side (Settings → *Picture size (px)*); a whole-page crop at 2.2× was 1300 × 1900 px,
  and a vision model turns every tile of it into tokens for no more insight.
* **The answer is capped** (`num_predict`), and a **thinking model is told not to think**
  about a one-line question (qwen3, deepseek-r1, gpt-oss … — the server is asked again without
  it if it does not understand).
* **A drawn zone takes the quick model** of the family, as the other short jobs do (it took the
  biggest installed one). Preference order starts at `qwen3-vl`, then `qwen2.5vl`, `gemma3`,
  `moondream`, `llava-phi3`; the text jobs start at `qwen3`, then `qwen2.5`, `llama3.2`. A
  model you set yourself in Settings (per job, too) still wins.
* **What a model can do is asked once.** `/api/show` was asked for every installed model every
  minute; the answer never changes, so it is kept.
* **The editor no longer freezes.** The model is asked *before* the job is locked for the edit
  (`edit.zone_ai_answers`), and the edit itself asks it nothing — so the pages, the tag tree,
  the checks and every other job keep answering while the model reads the picture. This covers
  the zone's role (*Auto*), a table's or list's rows and columns, and a drawn Figure's alt text
  or Formula's words.
* Settings → *Use the recommended settings* sets all of it; `tests/ai_speed_test.py` checks it.

---

## V4.17 — Kriyadocs: empty header cells, web links, citation numbers

**Kriyadocs engine only** (Client house style = kriyadocs). Academain and every other
client stay on the V4 structure, unchanged — checked: the same Academain book built
with V4.16 and V4.17 gives the identical tag tree and renders pixel-identical.

1. **An empty cell in a header row stays TH** (Scope Column), as in the client's
   files — not turned into a TD. The analysis, the editor's table edits and the build
   all keep it; the Issues tab does not flag it, and the output checker counts it with
   the empty cells the client leaves silent (UA-47, a warning) instead of UA-36.
2. **Web and e-mail links are a plain Link tag** — no Reference around them: the
   article's DOI, the ORCID and Check for updates links, the e-mail address, the
   licence, “online supplemental file 1”, the DOI links in the reference list. Every
   cross-reference inside the article (citation numbers, note marks, “table 1”) stays
   Reference > Link.
3. **Citation numbers linked as one get a zone and a link each.** InDesign often puts
   one link annotation over “2 3”, “12 22”, “24 25” or “35,36,37”: the Link tag over the
   first number keeps the file's annotation (its rectangle cut down to that number at
   build), and each further number gets its own Reference > Link with its own
   annotation that goes to its own entry in the reference list (“Reference 3”). A range
   — “4–6”, “38–40”, “10–17” — stays **one zone and one link**. The full stop a citation
   is set after (“.7–9”) is no longer inside the link's zone. A number with no entry in
   the reference list leaves the link as it was. Searching the text for links again, or
   an edit, makes the same split again.

`python tests/kriyadocs_test.py` checks all three (an empty header cell, the web and
e-mail links, “2 3” and “1-3” under the file's own annotations).


## V4.18 — Links stage, Edit table, a fresh look, a dashboard with details

Every client. The analysis is unchanged: the same Academain-style files built with
V4.17 and V4.18 give the identical tag tree and render pixel-identical (article, book,
tables of the corpus checked). What is new lives in the editor, the build only acts on
what is changed there.

**1. The Links stage** (editor header: **Tags | Links**, or `G` then `L`; `/job/<id>#links`
opens it directly). The left panel lists every link — its words, where it goes, where it
came from (*file*: a link annotation of the PDF; *found*: made by the search; *hand*: set
here; *dead*: a Link tag with nothing behind it) — with filters (problems, this page, web
& e-mail, set by hand …), a search box and a health bar. On the page every link is a box
coloured by its check (green OK, amber to check, red error); the selected link shows where
it lands (a dashed red line and the tag it lands on, also as a picture in the right panel).

* **New link** (`N`, or just drag a box on the page): 1 drag a box over the words
  (`Shift`+drag adds a second box — a link over two lines), 2 where it goes — *a place
  here* (click a tag or a spot on any page; turn pages with ‹ › or `[` `]`), *a page*, *a
  web address* or *an e-mail address*, 3 one-way or **two-way** (the target links back:
  from words you mark there, or the target's first words — a note: its number). More
  options: a description for screen readers, Reference > Link or Link only, replace a link
  of the file on the same words.
* **A link selected**: point it elsewhere (place / page / web / e-mail; a link of the file
  keeps its annotation and gets a new action at build), make it two-way, draw its zone
  again, change its description, take it out (its words stay; a link of the file leaves
  the built file; a link the search made **stays out** the next time the text is searched
  — “Let it find them again” undoes that).
* **Find links automatically**: the cross-reference search with its kinds, in the stage.
  Links set by hand are kept by every new search.
* **Check links** — does every link go where its words say? Errors: a dead Link tag, a link
  that goes nowhere or starts a program, an empty or broken address. To check: “Figure 3”,
  “Table 2”, “Section 4.1” or a citation number that lands somewhere else than the
  figure / table / section / reference entry of that name, a link landing on empty paper
  or on itself, a printed address that differs from the one opened, an e-mail address
  without `mailto:`, words in an artifact, a link with no words, two links on the same
  words, a two-way link that lost its way back. Each comes with its **Fix** (point it at
  figure 3, point it at the nearest tag, add mailto:, open what is printed, make the way
  back again …); **Fix all** applies every fix that does not take a link out.
* Keys: `.` next problem, `G` go to where it lands, `Del` take the link out, `↑` `↓` the
  list, `Esc` cancel.

Kriyadocs: a cross-reference set by hand is Reference > Link, a web or e-mail link a plain
Link (as V4.17); the check also knows the client's captions (Caption > P).

**2. Edit table** (select a table or a cell → **▦ Edit table…**). The table as a grid
with row numbers and column letters: click a cell, `Shift`+click / `Shift`+arrows for a
block, a row number or column letter for the whole row / column; double-click or `Enter`
edits a cell's words.

* Rows: add above / below, **merge with the next** (the words of each cell joined),
  delete an empty row, **make a row an artifact** (a “continued” line: out of the table,
  not read). Columns: add left / right, merge with the next, delete an empty one.
* Cells: **merge** a block into one (row / column span), **split** a merged cell again,
  TH / TD with a scope; header rows and columns as before.
* **Adjust lines on the page**: the table's row and column lines are drawn on the page —
  drag a line to move it, `Shift`+click between two printed lines to cut a row there
  (both halves read their own words), `Alt`+click for a new column line, double-click a
  line to take it out. Cells read their words again whenever their zone changes (cut at
  the cell borders word by word, never inside a word); IDs + Headers are written again.

**3. A fresh look**: gradient primary buttons and stage switch, softer focus rings,
dialogs and toasts that ease in, panels that slide in when the stage changes, pulsing
markers for the selected link and the step to do now, count-up numbers. Everything
follows the system's dark mode, and turns off for “reduce motion”.

**4. Dashboard**: five tiles (files and pages, built and how many with 0 errors, errors
fixed before → after, files to review / working now, failed), the last two weeks day by
day (uploaded / built), files by status and by profile. The file list has search, status
(working now, to review, built, built with 0 errors, failed), profile and sort
(newest, oldest, name, pages, errors left), a profile column, and **▸ details** for each
file: the checker before → after as rings, how many tags, headings, figures (and without
alt text), tables, lists, links (and set by hand), notes, formulas, artifacts; profile,
mode, title, language, dates, file sizes, contrast; what is still open in the editor; what
the build did; and buttons to open the editor or straight its Links stage.

**Also** (Kriyadocs): citation numbers printed raised and *not* linked in the file are
found by the search — “35 37” two links, “38–40” one link, each to its reference entry.

`python tests/links_stage_test.py` and `python tests/table_edit_test.py` check the new
stages in the V4 engine and in the Kriyadocs house style, through to a built file with no
checker error.

**V4.18.1 — Kriyadocs: no “Fix empty headers → TD”.** A Kriyadocs job keeps an empty
cell of a header row as TH, and now nothing in the editor offers to change it: the
Issues tab has no “Fix empty headers → TD” button, “Fix everything” skips that step, and
the server leaves the cells as they are even if asked. The cause: the Issues list was
checked on the V4 engine for every job — now every request about a job (Issues, tags,
tables, links …) runs on that job's own engine from the start. Academain and every other
client: unchanged (an empty header cell is still a TD there).

## V4.19 — sign-in, admin and users, Edit list, templates, Edit table with its picture

**After the update everybody signs in.** The first start makes the admin account
**Kathir** (with the password you gave). Users live in `users.json` next to `config.json`
(`PDFUA_USERS` to keep it elsewhere; passwords are kept only as salted hashes); the work log is
`activity.log` beside it. Neither is in the zip, so an update never overwrites them.
Scripts can use HTTP Basic auth with any account.

**1. Admin and users.** There are two roles. A *user* uploads files, sees only the files they
uploaded or were given, works on them and sends them for checking. The *admin* has the Admin page:
* **Overview** — files, pages, the pool (files not given to anyone), open / approved files and
  what is running now; plus a card per user (files, pages, open, approved, work status, last activity).
* **Files** — every file with who has it, its work status, priority, due date, pages, tool
  status, errors and uploader. Search, filter (user, pool, work, tool status, folder,
  priority) and sort. Tick files, then:
  * **Allocate** to a user. A file that was already given moves to the new user (reallocate).
  * **Share out** among several users: in turn, or so everyone has about the same number of pages.
  * **Unassign** (back to the pool), or **Revert** (puts back the last allocation change).
  * **Approve**, or **Send back** with a note.
  * Set the priority, the due date or a note.
  * **Build** or **Re-analyse**.
  * **Download** the built PDFs and reports as one zip, kept in their folders.
  * **Delete**.

  Each file's notes and history open under it. **Export** gives every file as a .csv.
* **Users** — create users (user name, name, e-mail, password, role, note), edit them, set a
  new password, switch them off / on, or delete them (their files go to another user or the pool).
  The last admin cannot be switched off or deleted.
* **Bulk upload (folder)** — admin only. Choose or drop a whole folder: every PDF in it and in
  its sub-folders is uploaded (in small parts, with progress) and analysed with the chosen
  profile. For files that already have tags, the tool decides instead of asking about each one.
  The files can go to nobody yet, to one user, or be shared out at once.
* **Activity** — who did what and when (sign-ins, uploads, allocations, user changes, sent
  for checking, approved …), by user and searchable.
* **Templates** — the table and list templates, with delete.

Work status of a file: given → in progress → sent for checking → approved (or sent back).
The editor's header shows who has the file and its status, with a red border when it is late.
Click it for the next step and the notes. A user gets Start and Send for checking; the admin
gets Approve, Send back and Open again.

Other rules:
* Only the admin changes Settings and profiles.
* A user cannot delete a file the admin gave them.
* Another user's file, editor and downloads are closed (403).

**2. Edit table — with the table's picture beside the grid.** The left half shows the table
as printed, with a tab per page / part and its cells drawn on it. You can:
* Click a cell there or in the grid; the two follow each other.
* Drag a line to move it.
* `Shift`+click for a new row line, `Alt`+click for a new column line.
* Double-click a line to take it out.
* Zoom ±.

Also new in the dialog:
* Move rows ↑ ↓, and re-read the words of cells.
* A check of the table: rows of different width, no TH, empty TH, cells with words but no zone.
* **More**:
  * Row groups THead / TBody / TFoot.
  * **Split the table** at a row.
  * **Join the next table** — a table continued in the next column or page. Its first rows
    that repeat the header become an artifact.
  * **Not a table** — a paragraph per row or per cell.
  * **Read the table again** with N columns, with rows made a way you choose.
  * Templates, and **download as CSV**.

**3. Lines only over the table.** "Edit on the page" draws each printed part of a table on
its own. “Table 3” in the left column and “Table 3 continued” in the right column are two parts,
and no line runs across the paragraphs between or beside them. A line you move or add acts on its own part only.

**4. Zones on the page.** In "Edit on the page":
* **＋ Rows from a zone** — each printed line in the box becomes a row, cut at the table's
  column lines and put where it is printed.
* **＋ One row**.
* **＋ Column from a zone** — cut at the row lines.
* **− Take out (zone)** — whole rows or columns leave the table as paragraphs.

Tags printed wholly inside a new zone give way to the new cells.

**5. Edit list** (select a list or anything in it → **☰ Edit list…**). The list is shown beside its
picture: items with label and words, indented by level. You can:
* Nest (`Tab`) / un-nest (`Shift`+`Tab`) and move items.
* Join an item with the next, or split it (`Shift`+click in the picture between two lines).
* Move the line between two items (drag).
* Find the label again.
* Take an item out (it becomes a paragraph) or make it an artifact.
* Change a label or the words (double-click).
* Set the numbering of each list, split the list, join the next list, or mark it not a list.

**Edit on the page** has the same lines, plus **＋ Items from a zone** (a new item at each
line that starts with a label), **＋ One item** and **− Take out (zone)**.

**6. Templates — fix one, give the rest the same structure.** In Edit table (More → Template)
or Edit list, **Save as template #N** with a number of your own (or the next free one) and a name.
Then use **Apply to this table / list**, or **Apply to tables (lists) I click**: click each table on
the page (turn pages with [ ]), or drag a box over words nobody tagged to make a new table /
list there. The inspector has the same "Template… Apply" for the selected table or list.
* A table template keeps:
  * the number of columns and where their lines sit (as shares of the width);
  * how rows are made: at the ruled lines, one per printed line, or a new row where the first
    column starts;
  * header rows and columns, merged header cells, THead/TBody and the header association.

  When a table template is applied, the column lines go onto the drawn rules or the nearest
  white space, the rows are cut, the words are read and the headers are set. If a continued
  part's first row repeats the header, that row becomes an artifact.
* A list template keeps each level's label shape ("1.", "a)", "•", "(i)" …) and numbering, the
  nesting, and how many lines an item has.

Templates are shared by every user and every file (config.json).

**7. A fresh working page.**
* Softer panels, gradient headers and a sliding tab underline.
* Rows and boxes that ease in, selections that pulse once, mode bars that slide down, and a
  page that settles in.
* Nicer scrollbars and focus rings.
* A sign-in page with a moving background, and the same look on the admin page.

Everything follows dark mode and stops for "reduce motion".

The analysis (the V4 engine and the Kriyadocs house style) is unchanged: the corpus tag trees are
identical to v4.18.1 and render pixel-identical.

**V4.19.1 — marks instead of saved templates.** Select a table (or a cell in it) and press
**Ctrl+4** — any number, Ctrl+1 then Ctrl+2 quickly is #12. The table is marked **#4** and its
tagging structure is read (columns and where their lines sit, how rows are cut, header rows and
columns, merged header cells, THead/TBody, header association). The marked table gets a #4 badge.
Then click the next tables, on any page: each one is read again from the page and tagged
the same way, and gets a green ✓ #4 badge. A table the analysis never tagged gets the same
treatment if you drag a box over it, and the tag tree works too — click a table there.
**Esc** releases the mark. Lists work the same way: a list (or an item in it) with Ctrl+N gets
the list format — labels, levels, numbering.

Chrome, Edge and Firefox keep **Ctrl+1 … Ctrl+9** for switching browser tabs, and a web page
cannot take those keys over. **Alt+1 … Alt+9** (or Ctrl+Shift / Cmd + the number) do the same
in the tool. Marks belong to the file (marks.json in the job folder), and Undo never removes them.
The "Save as template" buttons are gone from the editor.

**V4.19.2 — pictures in table cells, merged rows.**
* **A picture printed in a table cell is tagged once**, as a Figure inside its cell. Before
  this, the analysis also tagged the same picture a second time outside the table.
  * A new analysis no longer makes the second Figure.
  * In a file analysed before, "Fix structure" (also part of "Fix everything", and of every
    build) takes the outside one out. Its alt text moves onto the Figure in the cell if that
    one has none.
  * Any change in Edit table does the same for that table.
* **No empty TR after merging cells.** A merge that covers a whole row now takes that row out,
  and the merged cell spans one row less; a column only merged cells cross goes the same way.
  An empty row already left by an earlier merge goes with "Fix structure", the build, or the
  next change in Edit table.

Other files are unchanged: the corpus tag trees are identical and render pixel-identical.

**V4.19.3 — delete the tags in a zone.**
1. Choose Draw tag (`D`) and drag a box over part of the page.
2. Press **Delete** (or Backspace), or click **🗑 Delete tags in zone** in the popover.

Every tag printed wholly inside the box is deleted with what it holds — paragraphs, headings,
figures, artifacts, and a whole list or table when all of it lies in the box (only the cells or
items in the box when the rest is outside).
* A tag that reaches outside the box, or goes on to another page, stays.
* Containers left empty go too.
* A table that lost cells has its rows put right, so no empty TR is left.
* **Ctrl+Z** brings everything back.
* The popover now always fits on the screen.

**V4.19.4 — bullets and signs set in symbol fonts read right.**
Word, PowerPoint and InDesign set bullets in **Wingdings**, Wingdings 2/3, **Symbol** or
**ZapfDingbats**: there the code "q" prints ❑, "Ø" prints ➢, "ü" prints ✓, "·" (Symbol) prints •.
Many PDFs name those glyphs wrong, so the text layer — and NVDA — read "�", "", "q" or "¾".
* **Preflight** gives every such font a ToUnicode with the real characters (Unicode's own mapping
  of these fonts; box, arrowhead, tick, diamond, …), keeping every entry that was already right.
  Found from the font's name and, for CID fonts, from the font's own symbol cmap. The log says
  which fonts were fixed. The built PDF reads ❑ / ➢ / ✓ in NVDA, Acrobat and PAC.
* **A bullet printed apart from its text** (bullet, tab, text — it came out as a line of its own)
  is joined to the text on its right, so the item is one LI with the bullet as its **Lbl** and the
  text as its **LBody**; a line that wraps (“… peripheral nervous / system (PNS).”) stays in its item.
  The bullet must be the first thing on its row and the text at the hanging indent; dashes,
  asterisks and dots beside a table cell are left alone.
* ❑ ❒ ⬧ ⧫ ⬥ ◻ ◼ ⮚ ✗ ☐ ☑ ☒ ⇨ ❖ ➔ ▶ … are now list bullets too.
* A glyph that still cannot be named (an unknown font) becomes “•” for the Lbl.

Files analysed before this version keep their old text: **Re-analyse** them (or upload again).
The corpus tag trees are identical.

**V4.20 — sample files, the profile of a file, a new admin overview.**

*Sample files (Admin → Sample files).* Upload finished, correctly tagged PDFs for a client — an
existing profile, a **new client** (a profile is made for it from a profile you pick), or
**Common** (every client). Each sample is read in a worker process and stored in a database
(`sample_files/samples.db`, next to the app; `PDFUA_SAMPLES` moves it):
* every text tag (P, H1–H6, Caption, Note, BlockQuote …) with how it looks — size against the body
  text, bold, italic, lines, words, where it sits, how it starts and ends
* the wording of short tags and the tag each got (“Key messages” → H2)
* the running heads and feet (text outside the tag tree at the top / foot of the page)
* which tag holds which (Sect > H2, Table > Caption …) and the client's own tag names (RoleMap)

When a file is analysed, the samples of its client (the profile's name, or the profile's
*Samples client* option) and the common samples are used:
* a tag the analysis was **not sure of** (a heading found by size, a short bold line, a caption, a
  note) takes the tag the most similar sample texts have, when they agree (≥ 70 %)
* wording the client's samples always tag one way gets that tag; a short paragraph that looks
  exactly like the client's headings becomes one (≥ 90 %, the client's own samples only)
* a paragraph printing one of the samples' running heads / feet (digits ignored) becomes an artifact
* heading levels never jump; a caption is made only next to a Figure / Table; hand edits stay
* every change has the note `samples: …`; with no samples nothing changes
* the editor's Properties show **what the samples say** about the selected tag — one click takes it
* **Add to samples** (Admin → Files) adds finished files of the tool as samples; samples can be
  switched off, learned again or deleted; *What the tool learned* shows it per client
* option *Learn from sample files* (profile → Client) switches it off for a profile

*The profile of a file.* The profile chip in the editor's header, and in the file list, opens
**Profile of this file**: pick another profile, analysed again with it (recommended; a copy of
the tags is kept) or only from the next build. Users change their own files; an admin changes
many at once (Admin → Files → *Change profile…*). The change is logged and kept with the file.

*Admin → Files* has a **Profile** column and filter. *Admin → Overview* is new: eight tiles
(click one to see those files — pool, to check, approved, overdue, failed …), the last 14 days
(uploaded / built / approved), work status with the share approved, due soon and overdue,
who has what, files per profile (with errors left and sample files learned), latest activity,
what is running, and the quality (checker errors before → after). It refreshes every 20 s.

The corpus tag trees are identical (no sample files there).

**V4.21 — approved files teach the tool; AI with the samples; more for the admin, tables and lists; motion.**

*Approved files become samples by themselves.* When an admin approves a file (in the editor or in
Admin → Files, one or many), it is added to its client's samples: the PDF, the **tag model** (stored
compressed in the database — it stays even if the file is deleted), **what the tool proposed** and
**what people changed** in it (P → H2, a caption that was a paragraph …), and its **details**
(profile, client, pages, who worked on it and who approved it, checker errors before and after,
hand edits, notes). Sent back or reopened: its sample is switched off; approved again: brought up to date.
* The fixes people make weigh most when the next file of the client is converted — they are
  exactly the cases the analysis got wrong.
* Admin → Sample files → **Learning**: switch the samples, "approved files teach the tool", the AI
  and the client structure on or off; the database in numbers; **⤓ Database backup**; **Learn all again**.
* Each sample has a **Details** drawer: where it came from, who approved it, its tags, what people
  changed, its running heads, the tag model and what the tool proposed as .json.
* *What the tool learned* also shows **what people fix most** and **where captions go**.

*AI with the samples.* What the samples cannot settle (they do not agree enough) is put to the local AI
(Ollama, when it is on) together with the client's own examples from the sample files and the fixes
people made, so the model answers the way this client tags (option *AI with the samples*, profile →
Client; one question per file, at most 25 tags). Changes carry the note `samples + AI: …`.

*The client's structure.* Where at least three sample captions agree (≥ 80 %), a table's or figure's
caption is put where the client puts it: inside first, inside last, before or after (option
*Client structure from the samples*).

*Editor → Client tab.* The file against its client's samples: tags the client never uses, tags its
files always have, captions put elsewhere, running heads still read, and every tag the samples tag
otherwise — click to see it, **Take** one or **Take all** (Ctrl+Z takes it back).

*Admin.* **System** tab (version, files and disk, what is running, AI and learning settings,
**clean up old files** — approved / failed / every file not touched for N days, counted first).
Users: a **Details** drawer per user (files, pages, approved, errors left, active days, what they
did lately, **give their open files** to another user or the pool). Files: the detail row shows the
facts (mode, tags, language, errors, changed by samples, approved, sample, profile changes) with
*Change profile*, *Add to samples*, *Build*. Activity: filter by action, counts per action,
**Export (.csv)**. Bulk approve now logs "approved" (the overview's last 14 days count it).

*Edit table — More:* **Tidy the table** (every row as wide as the table, empty rows and columns
out, empty headers → TD) · **Row → header / Column → header / → data** · **Empty cells read as …**
· **words from a spreadsheet**: Ctrl+C copies the selected cells, Ctrl+V pastes Excel cells into
the grid · a table **Summary** (written as /Summary; *Draft with AI*) · keys M merge, S split,
H TH, D TD.

*Edit list — Whole list:* **Join lines with no label** (a wrapped line taken for an item goes back
to its item) · **Items at the labels** · **One item per line** · **Nest by labels** (1. / a) / i. …,
by indent where there is no label) · **Flatten** · **Numbering from labels**.

*Motion.* A thin bar at the top while the server works; a soft pulse where a button is pressed;
toasts stack and leave; the page shimmers while it loads and fades in; its boxes come in one after
another; the picked tag's marker slides in; the editor's panes and popovers fade in; the analysing
page shows its steps; pages change with a short fade (browsers with view transitions). Everything
stops for who asks the system for reduced motion.

The corpus tag trees are identical.

**V4.21.1 — zoom up to 800 %.** The page zooms from 25 % to **800 %**: the zoom list has 500 %,
600 %, 700 % and 800 %, and Ctrl + / Ctrl − / Ctrl + wheel / a pinch / right-drag go that far too.
The page picture is drawn at the zoom asked (sharp, not stretched); a very big page (a poster) is
drawn at most ~60 million pixels and stretched from there.

**V4.21.2 — word grids row by row, two texts in a list item, a label from a box.**
* *A word grid wider than it is tall reads row by row* (Academain and every client on the V4
  engine): "litch  mudge  vux  quam  cep" / "gen  knaz  wrop  saten  qui" is read litch, mudge,
  vux, quam, cep, gen, knaz … — each word its own P — and what is printed under the grid comes
  after it. A grid is three or more short words (three words at most each) per row, set apart by
  1.5 em or more, rows aligned. A tall grid — words set in columns, "discount / confide / portable
  / nonsense" — still reads down each column (V4.13). Kriyadocs keeps its own order.
* *An item set in two columns keeps its two texts apart* (every client):
  "1. duck      The duck is swimming on the pond." is LI > Lbl "1." + LBody > P "duck" +
  P "The duck is swimming on the pond." The second text starts at a tab stop (a gap of 1.5 em or
  more) and is three words or more; a short value ("Task A:   9/10 correct") stays with its item.
  The first item of such a list now joins the list too (its sentence was read as a flow of its
  own and the list stopped there).
* *Edit list → on the page → **Lbl Label (zone)**:* drag a box over the label of an item (a
  number, a letter, a bullet the analysis missed) — or down a whole column of labels: what is
  printed in the box becomes each item's Lbl, the rest its LBody. The list's numbering follows.

The corpus tag trees are identical.

**V4.21.3 — Edit table: the reading order.**
A table is read row by row, left to right (that is how PDF/UA tables are read); Edit table now
shows and sets that order:
* **reading order** (box over the picture): every cell shows its reading number, in the grid and
  on the printed table.
* **Columns ← →** move a column left or right — its cells are read in the new place in every row
  (rows already move with ↑ ↓). A column moved this way draws no line through the printed table.
* **The tags inside a cell** (a cell holding several paragraphs, a list, a figure …): pick the cell
  and its tags are listed in the order they are read — ↑ ↓ or drag to change it, **By position**
  to read them top to bottom, left to right as printed. More → Reading order → **Order every
  cell's tags by position** does it for the whole table.
* Ctrl+Z undoes each change.

**V4.21.4 — Reading order: check and adjust by hand; Shift / Ctrl selection.**
* **Edit table → Reading order → ☰ Check & adjust…** (or the key **R**) opens a panel over the
  grid:
  * **Check** walks through the cells in the order a screen reader reads them: ⏮ ◀ ▶ ⏭ (or the
    arrow keys, Home / End), **▶ Play** goes through by itself (Space), **speak** says each cell's
    words. The cell being read glows on the grid and on the printed table, with its number, text,
    role and whether it is where it is printed ("✓ as printed" / "⚠ printed as #5").
  * The panel says whether every cell is read as printed; cells read out of their printed place
    are outlined. **Next mismatch ⚠** jumps to the next one, **Order as printed** puts the cells
    in the printed order (rows on the page, left to right).
  * **Set by clicking**: click the cells — in the grid or on the picture — one after another in
    the order they should be read; each gets its number (click it again to take it out,
    Backspace takes back the last click). **Apply this order** (Enter): the cells are read in that
    order, the cells not clicked follow in their present order. Needs a table without merged
    cells and with rows of one width (split them, or Tidy the table first).
  * Esc closes the panel (not the dialog); Ctrl+Z undoes each change.
* **Edit table keys:** Shift+arrows grow the block one cell at a time from where it started,
  Ctrl+arrows jump to the edge of the table, Ctrl+Shift+arrows select to the edge, Home / End the
  start / end of the row (Ctrl+Home / End: the first / last cell), Shift+Space the whole row(s),
  Ctrl+Space the whole column(s), Ctrl+A every cell, Ctrl+click adds or drops one cell. The keys
  keep working after a click on the picture.
* **Edit list: several items at once.** Shift+click a run of items, Ctrl+click to add or drop
  one; Shift+↑ ↓ select more, Ctrl+↑ ↓ (or Home / End) the first / last item, Ctrl+Shift+↑ ↓ select
  to the first / last, Ctrl+A all, Ctrl+Space adds or drops the item in focus, Esc back to one
  item. The item buttons work on every selected item in one change (one Undo): → Nest / ← Un-nest
  (Tab / Shift+Tab), ↑ ↓ move the block, **Join selected** (items side by side, key J), Find the
  label, Take out (Del), Make artifact. Selected items are shown on the printed list too.
* **List items with a second text in a column of its own** (a spelling form: "1. city" on the
  left, "Traffic is heavy in the city." at a tab stop on the right, printed as a text column of its
  own — every client): each item is Lbl "1." + LBody > P "city" + P "Traffic is heavy in the
  city.". Before, only the first sentence went into its item, the others stayed paragraphs after
  the list (two sentences sometimes in one paragraph — and a sample could make one a heading).
  Also: a sentence that wraps onto a second line stays one P in its item; a list cut in pieces by
  those sentences (7. · sentence · 8. …) is one list again when the numbers go on and the labels
  stand one under the other; a first item read as a heading ("1. band" H4 over "2. …") goes back
  into its list; a long first word with less room before the tab stop is split as well. Only for
  short items (eight words at most), when most items show the same tab stop on their own row and
  every line moved sits on an item's row (or wraps right under it) in the body size — a
  paragraph in the next column of the page is left alone. Checked on the Academain spelling
  forms (pages 71–88 of X56811: every one of the 330 items).
* **Tag levels for several tags at once.** Select tags in the tag tree (Shift+click / Ctrl+click,
  Shift+↑ ↓): the inspector shows **Tag level** — ⇤ Level out (out of their parent, right after
  it), ⇥ Level in (into the tag just before them, together, in order), ↑ Up, ↓ Down — and, when
  headings are among them or inside them, **H− level up** (H3 → H2) / **H+ level down** (H2 → H3),
  kept between H1 and H6. Keys: Alt+arrows work on every selected tag, Shift+Alt+↑ ↓ change the
  heading levels (also for one heading, or every heading inside a selected Sect). Tags side by
  side move as one block; a tag inside another selected tag moves with it; one Undo.
* **🔊 Reader tab — what NVDA reads, out loud.** The tags read the way NVDA reads a PDF in browse
  mode: "heading level 2, …", "list with 10 items" … "out of list", "table with 3 rows and 4
  columns", "row 2, Count column 2, 38" (the column / row header named), "graphic, <alt>",
  "link, …", the /ActualText and blank-line wording, artifacts skipped.
  * **▶ Play** says all, from this page on into the next pages (or the whole file, or from the
    selected tag), with the browser's voice; the words light up as they are spoken (in a big
    speech viewer) and the tag being read glows on the page, which follows along. Without a voice
    (or with "speak out loud" off) the words still run at reading speed.
  * ⏮ ⏭ / ↑ ↓ step, speed 0.5–2.5×, voice, "say tag types" or words only, sounds (a tone for a
    heading, list, table, graphic, link, ⚠), say page numbers, a voice per /Lang, headings in a
    higher voice.
  * NVDA quick keys in the tab: H / L / I / T / G / K / F next heading, list, item, table, graphic,
    link, form field (Shift: back), 1–6 a heading level, W the next problem, Space play / pause,
    Esc or Ctrl stop. An **elements list** (headings, lists, tables, graphics, links, fields,
    problems) to jump to one.
  * **Problems a listener would hear**: no alt text, alt text that starts "image …", an empty
    heading, a skipped heading level, a table with no header cells, a data cell with no header, an
    empty link or "click here", an empty list / item / tag. Counted, marked ⚠ (with a warning
    tone), "only problems" in the transcript.
  * **Transcript**: every line read, click one to read from it, double-click to show the tag; Copy
    / Save .txt; the number of words and the listening time.
* **Sign-in page**, new: the product at a glance beside the form (a page being tagged, the four
  main features), icons in the fields, show / hide password, a Caps Lock warning, the user name
  remembered on this computer (never the password), a "Signing in…" state; light and dark, and a
  single column on a phone. Credit: application created by Kathir, Founder & CEO, Dezquare Tech —
  info@dezquare.co.in · www.dezquare.co.in (also in the user menu inside the app).
* **Reading speed.** Edit table → Reading order (Check & adjust) has a speed slider (🐢 0.3× … 2×
  🐇) by "speak"; Play now says each cell to the end before the next one (no cell cut short).
  Edit list has **🔊 Read the list**: the items read in order from the picked one ("list with 3
  items", "1. …", "nested list, level 2", "out of list"), each lit as it is read, at the same speed.
  One speed for the whole app (remembered on this computer; the Reader tab uses it too).
* **Sign-in page: the story of the studio.** Beside the form, seven short animated stories turn by
  themselves (hover holds them, the bars pick one, ← → too): Upload — it tags itself · Tables and
  lists, fixed visually · Every link in one place (the Links stage) · Alt text, written by AI ·
  Hear it the way NVDA reads it · Checked before it ships · Private, learning, made for teams; and
  a running line of the advantages (PDF/UA-1, WCAG 2.1 AA, batch auto-tagging, AI alt text
  generator, Links stage, local AI, files stay on your server, Excel report, NVDA-style check,
  client house styles, team roles). No third-party product names on the page. Calm (no motion)
  for who asks for none.
* **A tag inside a tag, drawn by hand.** Draw a zone over a few words of a paragraph (a heading,
  a caption, a cell, a list body, a Span …) and pick **Span** (key S), **Quote** (O), **Lbl** (B),
  **Reference** (R), Code, Note, Formula or Figure: the new tag goes *inside* that tag —
  "P > Span" — instead of beside it. The pop-up says which tag it goes into ("inside P “Traffic
  is heavy …”"); untick it for the old way (a tag of its own). Its place among the tags already
  inside follows the reading order (line by line, left to right), and in the built file the
  paragraph reads "The quick " · Span "brown fox" · " jumps over …" in printed order. A zone over
  the whole paragraph still replaces it. The Reader tab and the Preview read such a paragraph
  once, whole (an inner tag's /ActualText in its place).
* **A sign-in ends when the app is closed.** Close every tab of the app and open it again: the
  sign-in page ("The app was closed, so you were signed out"), the session ended on the server
  too. A reload, a link inside a tab, or a second tab opened while another tab of the app is open
  stay signed in; a tab the browser brings back after a restart asks again; Sign out in one tab
  signs out the others. The session cookie no longer outlives the browser (`static/tabguard.js`:
  a per-tab key in sessionStorage, and the open tabs answer a new one over a BroadcastChannel).
  * **Auto-tag page** gives the new structure (a proposal made by an older version is brought up
    to date first, without analysing the file again).
  * **A list zone** drawn over both columns, or over the numbers and words only, gives the same
    items; drawn again over an existing list's labels, it takes that list over. Text of a tag the
    zone takes over that is printed outside the zone is kept as a paragraph (it was dropped
    before) — and goes into its item when it is the item's second text.

The corpus tag trees are identical.


**V4.22.0 — Tags on pages; the Reader's text view.**
* **Tags on pages** — the toolbar's **Auto-tag page…** and new **Remove tags…** open one dialog:
  pick **this page**, **from page … to …**, a list of pages ("5, 8-12") or **the whole file**,
  then:
  * **⚡ Auto-tag** — the tags of those pages replaced by the proposal made at upload (quick);
  * **🔍 Re-analyse** — those pages read again with the current options (only those pages are
    analysed; the rest of the file still gives the body size, running heads …), then tagged;
  * **🗑 Remove tags** — every tag of those pages taken off (the whole PDF with "the whole file"),
    to tag them by hand; a tag printed on other pages too keeps those pages; groups left empty
    go too. The printed pages never change, and Undo brings every tag back.
* **Reader — the text view**: the words being said stay in a box of their own (no more text
  running over the buttons on a narrow screen), the box scrolls with the voice, long passages
  get a smaller type, a progress line shows how far through, and "next ›" shows what comes after.
  **Captions on the page** (on by default, Voice and options): while it plays, the words run in a
  big caption bar at the foot of the screen with the word being said lit.
* **Reader — what is read for a tag**: /ActualText first, then /Alt (a Figure's or Formula's, and
  /Alt on any other tag), then /E (the expansion of an abbreviation), else the printed words —
  for a tag inside a paragraph too (P > Span, Link, Quote, Figure …): the paragraph is read once,
  with each inner tag's words in their place ("Traffic is VERY HEAVY in the city."), and a figure
  inside the line is read where it stands.


**V4.23.0 — The production workflow: Production → QA → Final QA → Delivery.**
* **Users by role** (Admin → Users → New user / Edit): **Production**, **QA**, **Final QA** and
  **Admin** (a user of an earlier version is Production).
  * **Production** uploads, analyses and tags files, and sends each to QA.
  * **QA** and **Final QA** never upload, analyse, re-analyse, auto-tag or change a profile (the
    server refuses it; the buttons are gone): they **correct and check** — every tag tool, Edit
    table / list, Remove tags, Build — comment, send to rework or approve. They download the
    **input PDF**, the **output PDF**, the report and the PAC reports of every file they check.
* **Every file has a step** — shown on every file list (a four-dot strip Production · QA · Final QA
  · Delivery with the step lit, "Rework 2" when it went back twice), in the editor's header and in
  the new **💬 Review** tab:
  `Production · given / in progress / rework` → `QA · waiting / checking` → `Final QA · waiting /
  checking` → `Delivery · ready` → `Delivered` (and `On hold`). Production's first change starts the
  file (in progress) by itself; a QA user's first change to a file of the queue makes it theirs.
* **Move to QA needs the PAC report**: the button opens a dialog — drop the PAC report (PDF, HTML,
  XML, image, zip; 40 MB at most), say passed / failures (and how many), a note. It goes to QA only
  when the accessible PDF is built after the last change and every comment of a rework is marked
  fixed; the dialog lists what is still missing. A refused report is not kept. Every round's
  report stays with the file (Review → Files, the admin's zip).
* **QA**: **Take the next file ▸** (urgent first, then the due date, then who waited longest) — the
  file becomes theirs and leaves the other QA users' queue. **Review comments** with a category
  (Tagging, Reading order, Headings, Alt text, Tables, Lists, Links, Metadata, Contrast, Other), a
  severity (Minor / Major / Critical), the page and the selected tag pinned (a click on the comment
  goes there). Who wrote a comment **edits** it — marked **edited**, the old text kept in its tooltip
  — or **deletes** it. A **checklist** (ok / n/a / ✗ per line; ✗ starts a comment).
  * **↩ Rework stage-1, stage-2 …** sends the file back to its production user with the open
    comments (a note is enough when there is no comment).
  * **✓ Approve → Final QA** (an open comment stops it; a change QA made needs a build first, so the
    approved file is always the built one). Comments marked fixed are closed — verified.
* **Production in a rework**: the comments with the page / tag, **✓ Mark fixed** (with what was
  changed), Reply; Move to QA again with a new PAC report — the file goes back to **the same QA
  user**. A rework Final QA asked for goes straight back to **Final QA**.
* **Final QA**: the same check; **Approve → Delivery**, then **📦 Mark delivered**.
* **Read only**: a file at another step opens read only — a banner says why ("The file is with
  QA"), the build and tag tools are hidden and the server refuses any change.
* **The header's 📥 My queue** shows how many files wait for you (rework, given, in your QA queue,
  being checked, to deliver); the file list gets step tabs (All · Waiting for me · Production ·
  Rework · QA · Final QA · Delivery · Delivered · On hold) and a button for what each file asks of
  you (Start, Fix the rework, Check ▸, Continue checking, Deliver). QA's home page is their queue.
* **Admin → Workflow**: a **board** of every step (drag a file to another step; to Production from
  a check = rework with a note), tiles (in production / rework, in QA, in Final QA, ready,
  files with a rework, **% first pass**), per production user files · rework files · rounds ·
  comments · done · first pass, per checker approved / sent to rework, **what QA finds** (comments by
  category) and the **rules**:
  PAC report before QA · PAC report must pass · built after the last change · 0 errors in the tool's
  check · every rework comment fixed · the Final QA step (off: QA approval → Delivery) · rework goes
  back to who sent it · checklist before approval · **give checks out by themselves** (a file
  reaching QA / Final QA goes to the checker with the fewest pages waiting).
  The admin also moves a file to any step, chooses its QA / Final QA checker, puts it on hold, and
  in Files: **Move to step…**, **Rework…** (from QA, Final QA, Delivery or Delivered), Approve.
* **History**: every step with who, when, the note, the PAC report and the comments it took; time
  spent in each step. The Activity log, the CSV export (step, rework rounds, QA, Final QA, comments,
  last PAC) and the download zip (the PAC report of every round, `&original=1` the input PDF) carry
  it too. An approved file still teaches the tool (Sample files); sent back or to a check again, it
  stops until it is approved again.


**V4.24.0 — Academain Teacher's Guide structure (`core/tguide.py`, option *Teacher's Guide structure*).**
The client's complete, tagged Teacher's Guide X159397 (Benchmark Phonics Intervention, Level 1,
Unit 15 — 44 pages) was read tag by tag; a Teacher's Guide is now tagged the way that book is.
* **When**: *Teacher's Guide structure* (group Client) — `off` · `auto` · `on`. The **Academain
  profile** is `auto`: the pass runs when the book is a Teacher's Guide ("Teacher's Guide" on its
  first pages and lessons headed "Lesson N (20–30 MIN.)"). Every other book — the test corpus, the
  client's other books X56811, X159504, print_3 — is not one and is tagged exactly as before; the
  Default profile and every other client never run it.
* **The structure** (Document > **one Sect** for the whole book):
  * cover — Figure (programme logo) · P unit name · P level · P "UNIT 15 Rr" · **H1** Teacher's
    Guide · Figure (cover photo, publisher logo)
  * contents — **H2** Table of Contents · TOC > TOCI > Link
  * each lesson — **H2** "Lesson 1 (20–30 MIN.)"; **H3** the parts in the slab serif (Materials,
    Warm-Up Review and Repetition, Multimodal Mini-Lessons, Vocabulary Booster, Apply to Reading and
    Writing, Additional Supports …); **H4** the bold condensed sub-heads (Additional Materials,
    Reread to Build Fluency, Blend Words, Read Aloud: "…"); **H5** the head of each column of a
    **ruled box** — the boxes are **not tables**: each column is H5 + P, read column after column
  * **P** every paragraph; **each word of a blending / word grid its own P**, row by row; a
    sentence one P; **L > LI > Lbl + LBody** the bullet and numbered lists; Figure the pictures,
    icons and badges
  * the **page number** a P read **"Page 2"**, read last on its page; the running foot
    ("© Benchmark …", "Unit 15 • Rr"), the lesson tab on the page edge and the printer's marks are
    artifacts
* **/ActualText** as the client's book: "_____" → **"Blank line"** · "Rr" → **"uppercase R,
  lowercase r"** · "R R R r r r" → "Three Uppercase R, Three Lowercase r" · "B H P b h p" →
  "uppercase B, uppercase H, …" · the ISBN and the product code digit by digit.
* **How**: the V4 analysis runs first (its figures, table of contents, artifacts and reading order
  stay); the pass then makes the text structure again from the printed lines and their type styles
  (FiraSans-Bold 42 → H1, 30–34 → H2; Rockwell Bold → H3; Fira condensed extra-bold → H4; Fira
  extra-condensed bold → H5 in a ruled box, H4 outside it), finds the ruled boxes, the grids and
  the background pictures, and orders each page as the client's book reads (lesson head, the margin
  column, the main column, the boxes column by column, the page number last). The build keeps the
  whole guide in one Sect; the pages print exactly as before.
* **Checked against the client's own tagged X159397** (the same file, untagged, through the tool
  with the Academain profile): **99.0 %** of the text gets the client's tag, **98.7 %** is read in
  the client's order; render difference 0, the structure whole. Still for the operator: the alt
  text of the figures (AI alt text or by hand) and a blank page's note.


**V4.25.0 — The self-learning AI brain (`core/brain.py`) · Smart AI is the upload default.**

The sample files (V4.20) taught the tool by example. V4.25 turns that database into a brain
that learns by itself, decides by itself, and gets better with every file.

*1. Smart AI (all samples) — the new upload default.* No client's own profile is the default
any more. A file uploaded with no profile chosen (the form, a script, a folder) is tagged by
the PDF/UA rules, the local AI and **every client's sample files**: the files most like this
one — the same kind of document, the same words and type styles — count most (option
*Samples to learn from* = `all`; a profile keeps `client` for its own client's samples). A
Teacher's Guide is still found (`teachers_guide = auto`), and every AI check of Best quality
is on. An installation whose default was Default, Best quality or Academain is moved to Smart
AI once; Settings can pick any other profile afterwards.

*2. A stronger database — the AI studies every sample.* Each sample file (uploaded, or an
approved file) is now also stored as:
* a **document**: its type (journal article, book, workbook, Teacher's Guide, assessment,
  report, manual, form, presentation …) by rule — and by the local AI — and a fingerprint of
  its words and type styles, so a new file finds the samples it is most like;
* a **style map**: which font, size, weight and capitals the client sets each tag in
  (“helvetica 1.0× CAPS → H2”, “Rockwell bold 1.4× → H3”) — the fonts are stored with every
  learned tag now;
* a **memory** (RAG): its short texts and their tags as vectors — hashed n-grams always, and
  the vectors of the Ollama embedding model when one is installed (`nomic-embed-text`,
  `mxbai-embed-large`, `bge-m3` …) — so the nearest finished examples of any new text can be
  looked up;
* **insights**: the local AI reads a digest of the sample (its tags, their looks, examples,
  which tag holds which, captions, running heads, what people corrected) and writes the
  client's **tagging rules** in plain words and the **document type**. The rules go into every
  later question to the AI about that client's files, and are shown on the admin's brain page.

*3. Its own trained model.* A role classifier (softmax regression over the layout features,
the words, the shape of the first word, the font, the client and the document type; numpy
only, no GPU) is trained on every tag of every sample and every fix people made — again by
itself after each new sample, after builds that taught it something (at most every 10
minutes), or with **Train now**. Its accuracy is measured on **sample files it was not trained
on** (precision / recall per tag too), so the number says how it does on a file it has never
seen. 30 000 examples train in about 10 s.

*4. Its own decisions.* With *AI brain* on (Smart AI, Best quality), every text tag of a new
file hears six opinions — the analysis' rules, the nearest sample tags, the trained model,
the style map, the wording memory and (for what stays doubtful) the local AI — each weighed
by **how often that source was right before**. The brain changes a tag when the weighed vote is
clear and at least two learned sources agree (*AI brain: how bold* — careful · balanced ·
bold). What stays doubtful goes to the local AI with the PDF/UA rules, the client's insights,
the heading before the block, the brain's own vote and the **three most similar finished
texts** (at most *AI brain: tags asked per file*, 45 by default, 15 per question). With
nothing learned at all, a tag the analysis doubts still goes to the AI.
Guard rails keep it to what PDF/UA allows: a heading is never a paragraph of sentences, never
longer than 22 words or three lines, never smaller than the body text or in the foot of the
page unless the client's style map says so; a caption is always next to its figure or table;
heading levels never jump; hand edits, tables, lists, figures, a Teacher's Guide and a client
house style are never touched. Learned voices fade when nothing like the tag was ever seen.
Every change has a note **“brain: 78 % — model, samples, style”** or **“brain + AI: …”**;
a tag the brain is still unsure of is outlined amber for review. The file keeps the brain's
summary: its **tagging confidence**, the document type, the samples it learned most from,
what it changed and what it asked.

*5. It learns from the outcome.*
* **Every built file teaches** (Learning → *Every built file teaches*): the tags people changed
  in the tool's proposal are remembered at each build (they weigh less than an approved
  file's, until it is approved).
* **Every approved file** becomes a sample (as before) — and is compared with what the brain
  decided: each source's **trust** goes up or down, so the brain's own decisions get better by
  themselves; and the **first-pass quality** — the share of text tags the tool got right
  before anybody changed them — is kept per file. That is the learning curve: it should rise
  as the brain learns.
* Samples learned before V4.25 are read again once, by themselves, when the app starts (their
  fonts, document and memory), then studied by the AI and trained on.

*6. Where to see it.*
* **Admin → 🧠 AI Brain**: model accuracy (held-out), right first time (first vs latest
  approved files), examples learned, memory (with AI vectors), style patterns, fixes learned;
  the **learning curve**; **whom the brain trusts** (each source, % right, its weight); the
  style map; the model tag by tag; **what the AI learned** from each sample (its rules and
  document type); the files it learned from. Switches: *AI brain decides the tags*, *Every
  built file teaches*, *AI studies each sample*. Buttons: **Train now**, **AI study of every
  sample**.
* **Editor → Client tab**: the brain's card — tagging confidence, document type, what it
  changed and asked, the samples it learned most from — and **🧠 Think again** (the brain
  decides the text tags again with everything learned since; queued, Ctrl+Z puts them back).
  A tag's properties show what each source says (“🧠 Brain: H2 78% · P 22% — rules P · model H2 ·
  style H2”).
* **Command line**: `python cli.py --brain-status`, `python cli.py --brain-train`;
  `python -m core.brain train | study [--all] | status`. The CLI's default profile is Smart AI
  too, and each file's line says what the brain decided.

*Options* (profile → AI / Client): `samples_scope` (client | all), `ai_brain`, `ai_brain_max`,
`brain_strength` (careful | balanced | bold). *Learning settings*: `brain`, `feedback`,
`ai_study`. New requirement: **numpy**. The database is the same `sample_files/samples.db`
(new tables: docs, memory, insights, brain_models, feedback, decisions, trust, history);
**⤓ Database backup** includes them.

*Honest limits.* The brain decides **text tags** (headings H1–H6, paragraphs, captions, block
quotes) — tables, lists, figures, links and reading order still come from the analysis and its
AI checks (all on in Smart AI). Its accuracy is only as good as the samples: a handful of
finished files of each kind of document is the start; every approved file adds to it. The AI
study and the doubtful-tag questions take about a minute each on a CPU — the answers are drafts
the brain weighs, never the last word on their own.

Checked: every test suite passes as in V4.24 (the corpus trees identical, every client's
engine unchanged when the brain is off); `tests/brain_test.py` runs the whole loop with a
stand-in Ollama — samples studied and trained on (100 % on held-out files), another client's
file tagged from them, the brain alone and the AI with the nearest examples, the popover,
Think again, feedback at build, trust and first-pass quality at approval, Train now, the
switches and the command line.


**V4.26.0 — The learning engine writes its own code · nothing in the PDF is missed · nothing leaves the server.**

*1. Learned code (`core/codegen.py`).* From the fixes people make — in the editor (a few seconds after
the edit), at every build and in every approved file — the learning engine finds the pattern behind
each kind of fix and writes it down as a small **Python rule**, a module of its own:

```python
"""Learned rule R0001 (version 2) — written by the learning engine …
What it does: a tag the tool proposed as P becomes H2 when: in capitals, not bold, size × body 0.94–1.06,
words <= 4, font == helvetica.  Learned from: 14 fix(es) people made (4 in approved files).
Checked on: 18 tag(s) it matches — 100.0 % of them are H2."""
def match(f):
    return (f['caps'] >= 0.8 and f['bold'] == 0 and 0.94 <= f['size_rel'] <= 1.06
            and f['words'] <= 4 and f['font'] == 'helvetica')
```

* **Mined** by rule induction over everything in the database (a few hundredths of a second for
  40 000 tags, numpy): the conditions that pick out the fixed tags and as few others as possible —
  by their *look* first (font, size, weight, capitals, length, place), by their wording only when the
  look cannot tell; then specialised to the evidence, so a rule never reaches further than what
  people fixed. When induction fails and the AI is on, the local AI proposes the conditions — **as
  data**; a condition on anything but a known feature is dropped.
* **Written** by the engine's own code generator (every value through repr()), never by a model.
* **Checked** before it may run: the source is parsed and every node must be on a short whitelist —
  comparisons, and/or/not, `f["feature"]`, constants, `starts()/ends()/has()`. No import, no
  attribute, no loop, no call to anything else, no builtins at run time. The engine also checks that
  the compiled code does exactly what the tested conditions do.
* **Tested** on every finished tag and fix in the database; **active** only at ≥ 93 % precision on
  enough verified tags, otherwise a **candidate**.
* **Used** by the brain as a voice of its own ("learned code"), weighed by its precision and its
  trust; an artifact rule turns a running head or a stamp into an artifact directly. Every use is
  recorded with the rule's id.
* **Kept honest**: every build and approval says whether each rule was right; a rule people keep
  undoing is **retired by itself**. Admin → 🧠 AI Brain → *Learned code*: every rule, what it does in
  words, what it learned from, how it tested, how it does in real files, its **Python code**, switch
  on / off, delete, **Write rules now**. The code is kept in the sample database and, to read, in
  `sample_files/learned_code/R0001.py …`.

*2. Learning from people, at once.*
* **One fix → the rest**: change a tag in the editor (P → H2) and the brain finds the other tags of
  the file that look the same (same font, size, weight, capitals, still the old tag) and offers
  **Make all N H2** — one Undo.
* **Live learning**: a fix is remembered a few seconds after the edit, not only at the build, so the
  next file anybody uploads benefits (and the brain trains again — at most every 10 minutes).
* **Memory** (`core/memory.py`): a picture described once (a logo, an icon — recognised by a digest
  of its pixels) gets the same alt text in the next file, before the AI is asked, so it costs no
  question; a reading set once ("Rr" → "uppercase R, lowercase r") is used again. Built files teach,
  approved files confirm; another client's memory only when confirmed.
* Fixes that turn a tag into an **artifact** are now learned too (they were left out before).

*3. Nothing in the PDF is missed (`core/inventory.py`).* At upload the whole file is read, back end
and front end, in one quick pass (300 pages in under a second): version, objects, encryption, the
structure tree and RoleMap, /Lang, MarkInfo, DisplayDocTitle, XMP (title, pdfuaid), bookmarks, page
labels, layers, embedded files, JavaScript, form fields and XFA; every font (embedded, subset,
ToUnicode, how many characters it prints); every picture (size, colour space, a digest of its
pixels, every place it is drawn); every page (characters, words, lines, sizes, fonts, colours,
invisible text, unreadable characters, scripts, write-in blanks, text on its side, pictures and how
much of the page they cover, vector drawings, links, annotations, form fields, scans). Then the
**coverage**: every printed line, picture, link and field matched against the tag tree.
* Editor → **🔎 PDF data**: the coverage (“98.4 % of the 1 240 printed lines are in a tag”), each line
  no tag holds — outlined on the page — with **P / Auto / A** for one or **Tag all as P** (lines printed
  one under the other become one paragraph), large pictures left as decoration (**Make Figure**), what
  the file itself says (fonts not embedded, unreadable characters, scans, XFA …), the facts of the file
  (and what it was when it came in), fonts and pictures. **Check again** after edits.

*4. Nothing leaves the server (`core/netguard.py`).* Every process of the tool (web app, workers,
learner, brain, command line) refuses any connection outside this machine and the private network
— before a byte is sent — and logs the attempt; the Ollama server set in Settings is allowed, and an
admin can allow another host (Admin → 🧠 AI Brain → *Privacy guard*). The pages carry a
Content-Security-Policy (they may only talk to this server), no referrer is sent, and the Reader and
every spoken check use **only voices that run on the computer** (an online browser voice would send
the words away).

*5. In doubt, the tool looks it up — offline (`core/kb.py`).* An online search would have to send the
doubtful text out, so the tool has its own **rule book**: short summaries of the PDF/UA-1
requirements (ISO 14289-1 clauses), the WCAG success criteria and the tagging practice, plus the
rules the AI learned from each client's samples and **house rules** the admin adds. The AI's
questions about doubtful tags carry the relevant lines; the editor's Client tab and the admin page
have a search box.

New tables in `sample_files/samples.db`: code_rules, rule_hits, alt_memory, say_memory, kb_custom
(feedback gets a node column) — all in **⤓ Database backup**. `python -m core.brain code` writes the
rules from the command line; `train` now trains the model and writes the rules.

*Honest limits.* The engine writes **rules** in a deliberately small, checked part of Python — it does
not rewrite the tool's own program (that would let one bad answer break every file). Rules decide
text tags and artifacts; tables, lists, figures and reading order still come from the analysis, its AI
checks and people. The privacy guard covers the tool's own processes and pages; veraPDF and Tesseract
are local programs that do not go online. The rule book summarises the standards — the clause numbers
are there to look up the text itself.

Checked: every test suite of V4.25 passes; the Default / Academain / Kriyadocs / Book tag trees of the
corpus are identical to V4.24 (the learning only acts through the brain, which those profiles do not
use); `tests/selflearn_test.py` runs the whole loop.


**V4.27.0 — The self-learning measures itself honestly, keeps one convention per file and writes tighter rules.**

V4.25 and V4.26 gave the tool a brain that learns from samples and from people's fixes. V4.27 improves how it
*judges itself*, how it *decides inside one file* and how it *writes its rules*. No new option; a profile that does
not use the brain is untouched.

*1. Honest numbers (`core/brain.py`).*
* **Cross-validation over files.** The model's accuracy used to be measured on one held-out fifth of the files —
  with a handful of sample files that can be three files, and the number then says more about *which* three than
  about the brain. Now (up to 12 000 examples) every file is held out exactly once, in up to 4 rounds, so the
  accuracy, the precision / recall per tag and the macro-F1 are measured on **all** the examples; the same data
  always gives the same rounds. A big database (over 12 000 examples) keeps one held-out fifth, so training never
  takes longer there.
* **Calibration.** A model trained for many rounds on few examples is too sure of itself; the brain weighs the
  model's voice against the other sources, so "99 %" that is right 70 % of the time would outvote them for no
  reason. The held-out predictions now fix one number — a *temperature* — that makes "78 % sure" mean right 78 %
  of the time; it is saved **inside the model** (a model saved by V4.26 loads with temperature 1, nothing to
  convert) and used by the brain, the editor's popover and Think again. On synthetic noisy data the calibration
  error (ECE) fell by a quarter to two thirds (e.g. 8.8 % → 3.1 %). Admin → 🧠 AI Brain → *Model accuracy* shows the rounds and
  the calibration error; `python cli.py --brain-status` prints them with the temperature and the log-loss.
* **Recent examples count a little more.** A client's style moves on; an example's weight falls from 1.0 (today)
  towards 0.6 with a half-life of a year — never to nothing.
* **Trust forgets.** The trust of a source (rules, samples, model, style, wording, code, AI) used to be the share
  right over its whole life. It now counts about its **last 600 decisions**: when a count passes the window, old and
  new are scaled back together, so a source that has got better is no longer held back by what it got wrong long ago.

*2. One file, one convention (`layout_voices`, a new source "the same look in this file").* A document keeps its
conventions: the bold 14 pt lines are all section heads, or none of them is. Every tag was decided on its own text,
place and what the samples say. Now the verdicts the learned sources are **sure of** (the brain's own bar: enough
share, enough sources agreeing) are pooled over the tags of the *same look* in the file — font, size, weight,
capitals — leaving the tag's own out, and heard as one more voice:
* a tag with **moderate** evidence follows the sure ones (a file whose heads all look alike is no longer tagged
  half and half because half of them have unusual words);
* **unsure verdicts are not shared**, and a look needs at least three sure others before it speaks;
* the voice is **as loud as the look is consistent**: silent when the sure others split (65 % and less), in full
  when they agree (95 % and more); only four or more sure others that agree 80 % count as an agreeing source;
* it is recorded with the other opinions, so its trust is learned like everyone else's (Admin → *Whom the brain
  trusts*, "The same look in this file"); notes read “brain: 62 % — model, layout”.
In a simulation of the decision rule (sources 55–90 % accurate, looks 60–97 % consistent, 40 files each) pooling
changed the accuracy by between −0.1 and +0.8 points — never a real loss where looks are mixed, a gain where they
are consistent. (A simulation, not your files: the numbers say the rule is safe, not what it gains on your work.)
* **Same look, same level.** A heading the brain made, and was not sure of its level (< 85 %), takes the level the
  file already gives the others of that look when at least three headings of it agree (≥ 75 %): no H2 here and H3
  there for the very same type style.
* **Check these first.** Editor → Client tab: the doubtful tags, ordered by what their answer teaches (doubt × how
  many other tags look the same); a click selects the tag on the page, and a fix there offers the others (“Make all
  N …”, V4.26).

*3. Learned code that is tighter and seen to work (`core/codegen.py`).*
* **One rule for each look.** When the fixes of one kind come in several looks — the chapter titles *and* the section
  heads of a book both become H2, in a bold slab serif and in plain capitals — V4.26 wrote one broad rule that
  stretched over everything between them (“size 0.94–1.89 ×, at most 5 words” matches every short line). Now each
  look (font, weight, capitals) gets a rule of its own, as tight as its own fixes (“font == rockwell, bold, size
  1.71–1.89 ×…”). The fixes of the other looks are left out of its tests; a broad rule written by an older version is
  **retired** when the narrower ones replace it (*Admin → Learned code* says so).
* **Seen to work beyond one place.** A rule is switched on only when its fixes come from **two files** (or ten in one
  file) and in **no file where it fires three times or more is it right less than 75 %** of the time — precision on
  average can hide a file where the rule is plain wrong. A rule that waits says why in its *note* (“learned from one
  file only — waits for a second file”); the table shows how many files it was learned from. A rule that is already
  on and unchanged keeps working — the new checks are for new rules.

*4. Memory.* When two files describe the same picture (or read the same text) differently, the description people
**confirmed last** wins — not the one written most often; a confirmed one still beats a newer unconfirmed one, and the
client's own memory still comes first.

*5. It does not forget the last fixes.* A training asked for within ten minutes of the last one was *dropped*; the
fixes made in the last minutes of an editing session were learned only if somebody fixed or built something after the
pause. Now one training is made when the pause is over (it does nothing when there is nothing new). The editor's
popover keeps the brain's context while nothing new was learned (it took a couple of seconds for every tag you
clicked on a big database).

New columns (added by themselves, in **⤓ Database backup**): `brain_models` — `logloss`, `ece`, `temperature`,
`folds`; `code_rules` — `files`, `min_file_precision`. `python -m core.brain train` prints the calibration.

*Honest limits.* Calibration is fitted and measured on the same held-out predictions (one number is fitted, so the
optimism is small, but it is not a separate test set). The "same look" voice is only as good as the verdicts it
pools, and a look that is a heading in one place and a label in another is deliberately silent. Recency weights and
the 600-decision window are heuristics, not fitted. Waiting for a second file can delay a good rule by one file.
Reading order, tables, lists and figures are still decided by the analysis and its AI checks, as before.

Checked: every test suite that runs without a server passes — the 45 of V4.26 as they were, and the new
`tests/selfimprove_test.py` (calibration, cross-validation, recency, the trust window, the layout voice with and
without it on the same file, levels, the review order, one rule for each look, the one-file and wrong-in-one-file
checks, newest memory, an older database, the delayed training); `api_test.py` and `ui_test.py` run through against
a running server, and the new Client-tab list and the admin numbers were rendered in a browser.


**V4.28.0 — The tool learns where people cut and join zones, live, and many people can work at the same time.**

When a publisher sets its paragraphs apart by a small gap, the analysis takes a page of paragraphs for **one zone** — and
people draw a zone round each paragraph, file after file, because the rule that decides "same zone or a new one?" never
learned. V4.25 – V4.27 learned *which tag* a zone gets (P → H2), the alt texts and the readings; V4.28 learns **where a zone
begins and ends** — from what people change, while they work — and uses it for the next files of that client and of
clients that work the same way. The editor, too, now serves many people at once.

*1. What it learns (`core/zonelearn.py`).*
* **The pairs.** In every analysis, for each two lines of running text one under the other, the tool decides "same zone"
  or "a new zone". It now writes down each such pair — 27 numbers (the gap and the line pitch in em, the indent, whether
  the line above is short and how it ends, how the line below begins, a change of size, weight, italic or font, the words
  in each line …), the answer of its rule and what it decided — in `zone_pairs.npz` in the file's folder.
* **The corrections.** When the tags change (a zone drawn, cut, joined, removed), the tags are read against the pairs: one
  text tag holds both lines → "same zone"; two tags → "a new zone"; a line in a table, a list or no tag → "cannot tell"
  and left out. Where the tags say something else than the tool decided, a person corrected it. A zone cut or joined by hand
  weighs 4; a pair on a page a person corrected, left as it was, 0.5; a pair of an approved file 1.0; what a reviewer
  (QA, Final QA, admin) did ×1.5; the other clients' examples ×0.25 in a client's own model.
* **A small model beside the rule.** For each client, and one for everybody: a logistic model that **moves the rule's
  answer** by at most ±2.5 log-odds — it overrules the rule only where it is at least 65 % (or at most 35 %) sure, and
  where the pairs look like nothing it has seen it stays with the rule. Plain numpy, trained in a fraction of a second.
* **It must earn its place.** A model is used only when, on files it was **not** trained on (held out file by file), it
  fixes at least 3 pairs and at least twice as many as it spoils plus one — with at least 4 corrections by hand (6 when
  they come from one file only: "waits for a second file or 6"). The model for everybody waits until two clients have
  corrected, and is then tested client by client. A model that has not earned its place is listed with the reason.
* **Which model.** The client's own; for a client without one, the model of the most similar client (the pairs look
  alike: ≥ 0.7, from ≥ 2 files); otherwise the model for everybody. In an option (*Learn where zones are cut*, on by default) and in
  Admin → 🧠 AI Brain → Learning (off for everybody). The client house styles (Kriyadocs …) keep their own rules.
* **Where it is used.** In the analysis of every page (the page-parallel analysis decides exactly as the single-process one)
  and in a zone drawn by hand that covers several paragraphs.

*2. Live.* A few seconds after the **last edit** (`PDFUA_LIVE_DELAY`, 4 s; a person who never pauses is learned from every
45 s) the tool starts a **learner process** (`core/livelearn.py`) for the file. It records the tags that changed (V4.25),
the alt texts and readings (V4.26) and the zones (V4.28), and trains the zone models again when something was corrected —
so the next file anybody uploads, even while you are still working, is already right. It does not wait for the build.
Build and approval make the file a sample, and its zone rows replace the live ones; a tagged file uploaded as a sample
teaches too (its tags are the answers). Undo the cut and the next run forgets it.

*3. The same cut elsewhere in the same file.* After you cut (or join) a zone by hand, the editor looks in the file for places
that look the same — the same space between the lines, the same indent, a word split at the end of the line, a lower-case
start — and are still one zone, and offers **Cut all 9** (“🧠 Learned from your zone fix”, with the start of each line;
*Show them* selects the first). One click, one Ctrl+Z. The links and spans inside a zone stay with the text they are printed
in, and a cut never goes through one of them.

*4. Many people at the same time.*
* **The one-at-a-time lock.** MuPDF is not thread-safe, so a request that opens a PDF takes the MuPDF lock; before, *every*
  request about a file did. Now reading and changing tags (a role, a move, the summary, the lint, the status, who is
  here, a picture already drawn) never waits for it, and the requests that need it are served **in the order they came**
  (`core/locks.py`) — none waits for ever while others pass it. A page picture is drawn once, for everybody, and kept next to
  the file; a browser that has it gets a 304. The page geometry is read once, not at every load. Two people's requests
  about one file are put in order, one file's work never waits for another file's.
* **Learning outside the editor.** The learner runs in its own process (at most `PDFUA_LEARNERS`, 2, at a time; one for each
  file, the edits made meanwhile learned right after) — it never takes the editor's lock.
* **A fair queue** (`core/fairqueue.py`). The files waiting to be analysed or built are served **in turns, one file of each
  person** — somebody who puts in forty files does not keep the others waiting; an urgent (or high) file goes first, and one
  made urgent moves up at once; a build somebody waits for goes before a new upload of the same priority; the brain's
  background training goes last. The queue position shown is the real one.
* **Who else is in the file.** The editor asks every 6 s who else has the file open and whether the tags changed since it
  loaded them: a chip “👥 Anna” in the header; when somebody else changed the tags and you are not typing, their changes are
  shown by themselves, otherwise a bar “Anna changed the tags in this file — *Show their changes*”. Undo asks first when
  somebody else made the last change in the last 15 minutes (the history belongs to the file).
* **The settings file.** Every change of `config.json` is one step (read, change, write under a lock, through a temporary
  file of its own), so two admins saving at the same moment no longer lose one's change — and **a saved setting no longer
  deletes the table / list templates and the privacy settings** (until V4.27, any save of the settings, of a profile or of the
  learning switches dropped them).
* **The server.** `python app.py` uses **waitress** when it is installed, with `PDFUA_THREADS` (24) threads — its own
  default of 4 would let four slow requests block everybody; the tags of the 24 files used last stay in memory
  (`PDFUA_MODEL_CACHE`), not every file ever opened; the pictures kept for a file are limited. Admin → System shows how many are in
  the editor now, who waits in the queue, how often and how long a request waited for the PDF lock, and the learner.
* Measured on one 4-core machine with small files (clients in the same process, so a floor for what a real network costs):
  16 people at the same time — requests per second 98 → 249; opening the tags 123 → 35 ms (median); a page picture 134 → 7 ms;
  changing a role 120 → 49 ms; summary 88 → 32 ms. With one person nothing is slower (28 → 33 requests per second).

*Honest limits.* The zone model decides one thing — two lines of running text one under the other: the same zone or a new
one. Columns, tables, lists, headings and the reading order are still decided by the analysis and its checks. It needs
corrections to learn from — at least 40 pairs and 4 zones corrected by hand (6 from one file) — so a client's first file is
only helped by the cut-the-same-places offer, and by the model of a similar client or for everybody if there is one; it
learns the habit it is shown, so a client whose files differ from one another gets less out of it. Nobody is locked out
of a file: when two people change the same tag at the same moment the later change wins (both are in the history and the
banner says who changed what). There is still **one server process** — the queue and the caches live in it — and one
PDF is opened at a time by the requests that must open it; twenty people editing tags is smooth, a hundred drawing zones
at the same moment would queue. A learner process takes ~100–200 MB while it runs.

New: option `learn_zones`; Admin → 🧠 AI Brain → *Zones* (models, why, **Train zones now**) and the *Learn where zones are
cut* switch; `python cli.py --brain-status` and `python -m core.zonelearn [train --force]`; `GET /api/job/<id>/presence`,
`GET /api/job/<id>/zones/similar`, `POST /api/job/<id>/zones/similar/apply`, `POST /api/admin/zones/train`. New tables in
`sample_files/samples.db`: `zone_rows`, `zone_models` (in **⤓ Database backup**); `zone_pairs.npz` in each file's folder, and
`<sample>.zone.npz` next to a sample file. Environment: `PDFUA_THREADS`, `PDFUA_SERVER=flask`, `PDFUA_LEARNERS`,
`PDFUA_LIVE_DELAY`, `PDFUA_MODEL_CACHE`, `PDFUA_LOCK_AUDIT=1` (tests: a request that opens a PDF without the lock fails).

Checked: every test suite of V4.27 passes as it was (the live-learning wait of `selflearn_test.py` now waits for the learner
process instead of a fixed 6 s); new `tests/zonelearn_test.py` (a client's four files corrected → the next file has every
paragraph in a zone of its own while another client's is untouched; the other way round for a double-spaced manuscript, where
every line is "far" from the next one; a second client wakes the model for everybody and a client never seen before is right;
the parallel and the single-process analysis log the same pairs), `tests/zonelearn_app_test.py` (a cut by hand → the offer →
applied, undone, redone → learned by the learner process → built, the tags sound, approved → the next upload is right; the
admin switch) and `tests/multiuser_test.py` (the lock, the queue, `config.json`, ten people at once with two in one file and no
lost change, the lock audit, who is in the file, undo of somebody else's change). `api_test.py` and `ui_test.py` run through
against a running server (waitress), and the chip, the bar, the offer and Admin → System were rendered in a browser.


**V4.29.0 — One person at a time in a file, a front page that shows what the studio can do, and Dez AI.**

*1. One person at a time in a file (`core/editlock.py`).* While somebody works in a file nobody else may change it. The others
can open it and **look** — the tags, the pages, the issues, the Reader, the PDF data — and every change is refused.
* **Who has the file.** Whoever opens a file in the editor (or presses *Edit this file* in a file they were only looking at).
  The header shows **🔒 You are editing**; for everybody else **👁 View only · Anna**, a bar says “Anna is working on this file
  now — you can look at it, not change it.”, the tools that change something (Build, Undo, Draw, Re-analyse, the AI helpers,
  the inspector's fields …) are gone or dimmed, and a hotkey that would change a tag says why it does nothing. The editor of
  somebody who only looks shows the other person's changes by itself, a moment after they are made.
* **Enforced by the server**, not only by the page: every request that changes a file (a role, a move, undo, a build, a zone, an
  AI draft, re-analyse, a new profile, delete …) is answered **423 Locked** with `{"locked": true, "by": "Anna"}` for anybody
  else — the editor, a script, an admin. Reading, the pictures, the lint, the Reader and the PDF data are never held back.
* **How long it lasts.** It is kept while the editor is open (the editor says “I am still here” with every look at who is in
  the file, every 6 s). It ends when the person **leaves** (the editor lets go — also when the tab is closed), when the editor
  has said nothing for **40 s** (a closed laptop, a lost connection), when the person has **done nothing in the file for 10
  minutes** (a tab left open over lunch — *Edit this file* takes it again), when the file **moves on in the workflow** (sent to
  QA, approved …), or when an **admin** takes it (**Take over**, with a question first; the person who had it is told and
  can look) or unlocks it (Admin → Files: the 🔒 badge of a file — click it).
* **Looking only.** The file list opens a file somebody has as a view (`/job/<id>?view=1`, which takes nothing); so does a link
  with `?view=1`. A person who opens a file normally has it, so that the one who comes to work is not overwritten.
* **Scripts** that never open the editor take no lock (and are refused while somebody else has the file). A refused change is
  HTTP 423, a lock that is simply free is taken by the editor of a person who works on.
* **Admin → Workflow → “One person at a time in a file”** (on by default) switches it off for everybody; environment
  `PDFUA_LOCK_IDLE_MIN` (10) and `PDFUA_LOCK_TTL` (40). Admin → System shows how many files are held and by whom.
* The file list and Admin → Files show who has a file. Undo still asks first when somebody else made the last change.

*2. A front page that shows what the studio can do.* The sign-in page is now the studio's front door: the sign-in beside a
headline; **an animated demo** in which Dez AI tags a page (a beam runs down it, every block gets its tag, the tag tree builds
up, the console says what it finds, the PDF/UA check ends with 0 errors); **“Meet Dez AI”**, its orb, its speech bubble and what
it does; **the zone-learning story** (cut a zone once → “9 more places look the same” → the next file is right); the numbers; eight
reasons to choose the studio; how it works in four steps; the teams (the lane from Production to Delivery, and the lock passing from
person to person); privacy; a last call. It is all drawn by `static/landing.js` and `static/landing.css`: a net of tags drifting in
the background, words that turn, numbers that count up, parts that come up as they are scrolled to, cards the pointer lights, a
**light / dark** button (and the computer's own choice), and a **pause** button that stops everything that moves and shows how
each picture ends (kept for the next visit; the computer's “less motion” setting does the same). Nothing is loaded from the
internet (the pages' Content-Security-Policy is unchanged). Accessible, as the studio should be: one `h1`, landmarks, a skip
link, labels on every field, the moving pictures are hidden from screen readers and described once in words, text on
coloured backgrounds keeps its contrast, and on a phone the sign-in comes first. The form is the old one (user name, password with
its eye, Caps Lock notice, the remembered user name — never the password — and the tab guard of V4.21.4).

*3. Dez AI.* The studio's own AI now has a name: **Dez AI** — a local language and vision model (Ollama, on your own server)
together with the self-learning brain that is trained on your approved files and learns from every correction, live. It proposes
the tags, drafts the alt texts, shows how sure it is and why, learns each client's way, and sends nothing out of your network.
The name is used where the AI is: the editor's **Dez AI** button and its helpers, Admin → **🧠 Dez AI**, Settings, the file
list (“Dez AI: on”) — where a **tip strip** gives one useful thing at a time. (Option names, the engine's notes in the tags and the
files are unchanged.)

Also: the app has an inline icon (no more request for a `favicon.ico` that does not exist).

*Honest limits.* The lock keeps people from overwriting each other in the tags and builds of one file; it is **not** a login
or an access right (who may see a file is still decided by roles and assignments), and the admin's file management — priority,
assignment, due date — and the workflow's own steps keep their own rules. It lives in the server's memory (one process): after a
restart the editors that are open take their files again as soon as their person does something. A person who opens a file to
work and then leaves the tab open holds it for up to 10 minutes of idleness — the *view only* link and the admin's *Take over*
are there for that. The front page's numbers (“300 pages in under a minute”) are the figure this README gives for its multi-core
test (“Current results”), not a promise for every machine; the demo and the zone story are illustrations, not a recording.
Dez AI is only as good as the local model installed and the files it has seen.

Checked: every test suite of V4.28 passes as it was; new `tests/filelock_test.py` (the lock, and through the app: the view, the
423s on eleven kinds of change, reading never held back, take over, let go, the editor's and the file list's information, the
workflow letting go, the idle and silent ends, the switch), `tests/ui_front_test.py` (the page whole, the demo reaching its end, the
zone story, the lane, the numbers, the words, the pause kept, light / dark, less motion, a phone, signing in right and wrong, nothing
from another server) and `tests/ui_lock_test.py` (two admins in two browsers: view only, the refused hotkey, the file list, take over
seen by the other, the change of the one who works shown to the one who looks, let go, leaving). `api_test.py` and `ui_test.py` run
through against a running server.


**V4.30.0 — A big file stays quick, the working time of every file, rights, and filters you can clear.**

*1. Why a changed tag took seconds — and what is different now.* Measured on a 300-page book of 5 087 tags (headless Chromium
on a 4-core machine, which is slower than yours): a **role change by hotkey took 8.9 s** to show, a **delete 9.4 s**, an undo
2.1 s, clicking a tag in the tree 0.3 s, a page turn 0.45 s. The causes were in the editor and the server, not in the PDF:

| What happened at every change | What happens now |
|---|---|
| the editor asked for the whole file (3 MB), the issues, the colours and the cross-references, **one after the other**, and drew every one of the 5 000 rows of the tag tree again (the browser then laid the whole page out twice: ~0.7 s) | the answer to a change **carries what it changed** (a patch of 1–2 KB); the editor changes those tags and **those rows** — a row is made again only when what it shows changed — and asks for the issues a moment later, in the background |
| the server wrote **the whole file** again (80 ms for 5 000 tags) | a copy of the tags (core/modelsync.py) is compared with the live tags (dictionary against dictionary, in C): only the tags that changed are written again — the text of every other tag is kept and joined (~10 ms) |
| every list of files — the Users page, the **queue badge each open tab asks for every 45 s**, Admin — opened and parsed the meta.json of **every file** (200 ms for 2 000 files, on one Python thread that everybody's edits share) | every file's meta is read once and kept (core/jobindex.py); a list costs a look at the folder at most every 4 s, and the writer says which file it changed. 2 000 files: 200 ms → 20 ms; the answers carry an ETag and are 304 (1 ms) when nothing changed |
| the panels' reads (colours, cross-references, page numbers, marks, tidy lists …) waited for the **one-at-a-time PDF lock** that a page being drawn or a zone being cut holds | they read the tags and never open the PDF: they wait for nothing but their own file |
| passing over tags with the arrow keys asked the server about each of them — and every answer **held the PDF for everybody** | the question is asked when the person stays on a tag (0.3 s), and not at all for a tag asked about before; the “learned from your fix” offer after a pause in the changes |
| the zone offer (“Cut all N”) read the file's proposal from disk and compared every pair of lines one by one (400 ms, on the file's lock) | the proposal is kept, the comparison is one step per page (numpy), the answer is kept for each version of the file (400 → ~70 ms, then 1 ms) |
| the learner (live learning) competed with the web server for the processor | it runs below the server's priority |

After (same file, same machine): a **role change shows in 0.13 s** (0.32 s until even the issues are fresh), a **delete 0.27 s**, an
undo 0.32 s, clicking a tag 0.04 s, a page turn 0.006 s, opening the editor 1.4 s instead of 2.5 s. In-process, a change of one tag
in a file of 4 000 tags answers in ~55 ms (before: ~120 ms for the write alone, then ~450 ms of asking again) and the answer is
1.5 KB; the editor's heartbeat answers in ~30 ms **while** somebody edits; a server of 1 500 files lists them in 5 ms the second time.

How it works, for whoever reads the code:
* `mutate()` (every change of the tags goes through it) takes a snapshot for undo — now a **hard link** to the file, not a copy — runs the
  edit, compares the tags with the copy, writes the file from the kept pieces, and leaves the patch in `g.patch`; an `after_request`
  hook adds `"patch"` to the JSON answer. A patch is `{base, rev, nodes, removed, derived, top, can_undo, can_redo, geom}`; the
  editor applies it only when its `rev` is the patch's `base` (anything else — another person's change, a page split — and it asks
  for the whole file, as before). More than 400 changed tags (a whole page re-tagged) is told as `reload`.
* The reading numbers shown in the tree are the one global thing an edit can move (a delete renumbers everything after it): the patch
  lists the ones that moved, and a leaf tag that only changes its role does not renumber anything. The reading *order* is the walk
  through the tree — the editor works it out itself.
* `GET /model`, `/lint` and the zone offer are built once for each version of the file and compressed once; `rev` is the file's date
  and a count of changes, so two saves in the same instant cannot look the same. Pages, answers and scripts travel **gzip** to a
  browser that accepts it (a 3.4 MB list of 5 000 tags is ~230 KB on the wire — what people on Wi-Fi or a VPN feel), and scripts and styles
  (`?v=`) are kept by the browser for a year instead of being asked about at every page.
* Writing `meta.json` (“last edited by”) for every change is at most once in 2 s while the file is already marked changed.
* **Nothing about the files changed**: `model.json`, the history, the PDFs, the reports are the same; an older browser tab simply
  asks for the whole file.

*2. Working time — Working and QC, break time left out (`core/worktime.py`).* The editor of a person who works counts the seconds in
which something was done in it — a key, a click, a drag, the wheel, a move of the mouse. A pause between two such moments shorter
than the **break limit** (Admin → Workflow → *Break after*, 2 minutes by default, 30 – 900 s) is thinking and counts; a longer one
is a break and counts for nothing — and a hidden tab counts for nothing. The seconds go with the editor's heartbeat (`/presence?…&w=`);
the server never counts more than has passed since the person's last report. They count for the person **working in the file** (the
holder of the lock; with the lock off, whoever may change it) — not for somebody who only looks. **Working** is the time while the
file is in production (and rework), **QC** while it is with QA or Final QA.
* The editor shows **⏱ 12 min** (⏸ while paused) beside the lock chip; the Users page shows the person's own time on every row (and
  the team's total to an admin or whoever has the right); **Admin → Files** has a **Time** column (Working · QC, who spent it in the
  tooltip and in the file's details) and the CSV has *working time (min)*, *QC time (min)* and *time by*.
* **Admin → Time**: today / 7 / 14 / 30 / 90 days, one person or everybody — the tiles, day by day, who, a table (average per active
  day), and an export (.csv) of person × day. The Overview has a tile “worked today”.
* Kept in the file's meta.json (`time`, so the lists have it for nothing) and, day by day, in `worktime.json` next to it; written at
  most every 30 s per file and when the server stops (a crash loses up to 30 s). The pages show what is counted and not yet written.

*3. Rights (`core/rights.py`).* The role says what a person does in the workflow; a **right** is one thing on top of it that an admin
gives or takes away: **upload**, **analyse again / change the profile / auto-tag**, **delete files**, **build**, **download**, **use the
AI helpers**, **see every file (look only)**, **take over a file** from whoever has it, **see who spent the working time**. Each has the
role's default (so nothing changes until an admin makes an exception); a person's own **Allow / Deny** wins; an admin always may.
Admin → Users → *Rights* shows every right with the default of the role, the person's setting and what is in force; the Users table
says “role defaults” or “N custom”. The server refuses what a person has no right to (403 with the reason — “You have not been given
the right to: delete files. Ask an admin.”), the pages do not offer it (no Build button, no download links, no upload box), and it is
logged. *See every file* lets a person open any file and read it; changing it stays with whoever has it at its step.

*4. Admin → Files: filters you can see and clear.* **↺ Reset filters** (disabled while nothing is on; it also puts the sort back) and
the empty state's own reset; every filter in use is a **chip** with its own ✕; **⚙ More filters**: uploaded / due dates, overdue only,
pages from – to, errors left, working time ≥ and QC ≥ minutes, worked on by, nobody has worked on it, QA / Final QA, being edited
now, open comments, in rework, built / not built / changed since the build; **saved views** (a set of filters under a name) and the
filters of the last visit are kept in the browser; sorts by edited lately, most / least working time and most errors; **Export these**
(the list as shown, with the times); `/` goes to the search, Esc clears it; **200 rows are drawn at a time** (“Show 200 more”) and “all N
that match are selected” says so when more match than are drawn. The Users page's file list has the same chips, **↺ Reset**, the empty
state, and 150 rows at a time; Admin → Activity has a Reset.

*5. Calmer, livelier pages.* The Users page keeps its rows between draws (a row is made again only when its file changed, and only
the bar of a file being analysed moves — before, the whole list **and its entrance** were drawn again every second and a half, and
the dashboard replayed its count-up); the upload card shows the chosen files as chips (each can be taken out) with a bobbing arrow when
a file is dragged over it; the filter bars stick under the header with a shadow when the list is scrolled; cards slide in as they come
into view; chips pop; panels unfold; the tabs of Admin fade in; a progress bar flows; the Admin tab Time draws its days. Everything
moves with `transform` / `opacity` only, and **nothing moves** when the computer's “reduce motion” is on. Admin → System shows what
keeps it quick (files known and how often they were read, copies of tags in memory, answers kept).

*Honest limits.* The first change after the server starts (or after a file has been out of the memory's 24 files) pays for making the
copy of the tags (~0.2 s for 5 000 tags; it is made while the editor opens, so usually nobody waits; it is made afresh every 500 changes as a safety net); an **undo** still reads the
older file (~0.15 s for 5 000 tags — then it is a patch like the rest); a person who only **looks** reloads the whole file when it
changes (it is built once for everybody and a 304 for the rest). The numbers above are one machine's: they show where the time
went, not what yours will be. Working time is what the **editor** reports — time spent reading the PDF in another program, or on the
phone, is not work in the file, and a person who reads for longer than the break limit without touching anything is, by that rule,
on a break; it is a measure of activity, not a time sheet to pay by. Rights narrow or widen what a role may do; they are not a
security boundary against somebody with access to the server's files. A list of files reads the folder every 4 s at most, so a file
copied into the folder by hand shows within a few seconds.

Checked: every test suite of V4.29 passes as it was; new `tests/patch_test.py` (the copy finds a changed field, a list or a dictionary
changed in place, a tag added, a tag gone, the file's own fields; the file written from the pieces is the file `DocModel.to_json()`
writes; then **26 kinds of change** — role, language, alt text, the title, move, indent, bulk, undo, redo, a zone, levels, delete with
and without children, a list, a table, a zone delete — through the app, each time the patched tags equal a new load, field by field;
a change made outside `mutate()` is found by the next one; 304, gzip, versioned scripts, the lists of files), `tests/perf_test.py`
(budgets: a file of 4 000 tags edited in tens of milliseconds with a 1.5 KB answer, the heartbeat while edits go on, 1 500 files
listed again for nothing, the 304s), `tests/worktime_test.py`, `tests/rights_test.py`, `tests/ui_admin_test.py` and
`tests/ui_users_test.py` (in a browser: the filters, chips, Reset, views kept, rows kept between draws, 150 at a time, the upload chips,
the Rights dialog, Time, Workflow, Activity). `api_test.py` and `ui_test.py` run through against a running server.

---

**V4.31.0 — Ollama teaches Dez AI: every answer is a lesson, and Dez AI answers by itself what it has learned.**

Two AIs work in this tool, and they are not the same thing.

* **Dez AI** is the studio's own AI — the part that lives in the program and learns: the brain, the memory, the learned code, the zone
  models. It needs no model server; it works offline, in milliseconds.
* **Ollama** is its teacher: a large language / vision model on a server. It can answer almost anything, but it takes seconds to minutes
  on a CPU, and it forgets every answer the moment it has given it.

V4.31 makes the teacher help the student, so Dez AI needs the teacher less every week and still has it when something is new.

*1. The gate.* Every question the tool puts to Ollama — 19 kinds, from “is row 1 a header?” to “which line starts a reference entry?” — now
goes through a gate (`core/teacher.py`, wrapped around the task functions of `core/ai.py`, so the analysis, the editor and the worker
processes all use it). The gate

1. asks **Dez AI first**: has it been taught this? A question it has been asked before is answered from its lessons; a *new* question of a
   kind it has learned well is answered by a small trained model of its own, a **student** — but only a student that has *proven* itself;
2. otherwise asks **Ollama**, as before, and **keeps the answer as a lesson**: the question in a compact form, the answer, the model that
   gave it, the file and the client it was about. The next question of that kind may not need Ollama at all;
3. now and then (1 in 12 by default) asks Ollama a question Dez AI *could* have answered alone, and compares the two — the honest measure
   of the student.

*2. What Dez AI learns from Ollama's answers.*

| Ollama is asked … | Dez AI learns |
|---|---|
| is row 1 of a table a header? · does a table go on over the page break? · are these two parts really one? | a **student** (yes / no, or choice) from the shape of the rows, the titles, the header rows, the layout hints |
| which line starts a reference entry? · which line starts a list item? | a **student for lines**: from the first words, the punctuation, the line before and after |
| the label of a form field | a **rule** found in the lessons: “for a text field with text to its left, the label is that text” (per kind of field, with its record) |
| what to say for a link · a one-sentence table summary · a cross-reference | the **answer for exactly that question** (the same link text and address, the same caption and header …) |
| alt text · a formula read aloud · a drawn zone · the reading order of a page | the answer for **the same picture** — also at another size, by a look-alike fingerprint (128 brightness steps and a 4 × 4 grid; a flat block of one colour has none) |
| the tags of the brain's doubtful text blocks | each tag Ollama chose, **with the layout of the block**, trains the brain's own model — with a low weight (0.35 × how sure Ollama was) until a person has had the say |

*3. Proven, not trusted.* Each kind of question has a **level**, chosen from the evidence (or fixed by an admin):
**watch** (Ollama answers, the answer is only kept) → **recall** (also answers what it has been asked before) → **solo** (also answers *new*
questions with its student). A student reaches *solo* only when, in cross-validation **over the lessons it was not trained on**, its answers
agree with Ollama's ≥ 95 % of the time at the confidence it needs to speak (≥ 90 % sure by default), with at least 30 lessons — and it
loses the duty by itself when its **audits** (1 in 12 answers asked to Ollama as well) fall below that. A student is trained again when its
lessons have grown by a tenth, in a process of its own below the web server's priority (a person's next click is never slower because Dez
AI is studying).

*4. People outrank the teacher.* What people leave in a built or approved file — a table's header rows, a link's description, a table's
summary — is a **human lesson** (weight 4 against Ollama's 1): the lessons Ollama gave for the same question are marked *confirmed* (the
person kept the answer) or *corrected* (the person changed it — it is never used again), which also gives the honest **“Ollama was right
N %”** per kind of question. An untouched answer in a *built* file is silence, not consent; an *approved* file confirms. **📚 Learn from the
files here** reads the finished files of the server once and learns what people decided in them. Ollama's tags for the brain are judged the
same way: a tag a person changed stops training the model, a tag in an approved file is confirmed (and then the sample rows say it better).

*5. Dez AI helps Ollama, too.* For the questions that need the studio's wording (a link's description, a form field's label, a table's
summary), the question to Ollama carries the **answers people approved for the most similar items** — so what Ollama writes follows how this
studio words it, and a person has less to change.

*6. Ollama off, or out of reach.* The Settings box is now “**Ask Ollama (the teacher)**”. Switched off, nothing new is asked — and Dez AI
still answers what it learned (the analysis passes it on to the workers, the editor's helpers use it, reference lists are split by its
student); what it has not learned stays unanswered, as it always did, with a clear message. (Admin can switch this off: *also when Ollama is
off*.)

*7. Admin → 🧠 Dez AI → **Ollama teaches Dez AI**.* One card: **Dez AI answers alone** (% of the questions of the last 7 days), **lessons
kept** (how many from people), **Ollama questions saved** (and about how much waiting that was — measured on Ollama's own answering times
here), **students proven**, **Ollama was right** (the tags, judged by approved files), **tags taught**; four switches (lessons, answers
alone, also when Ollama is off, examples for Ollama); the audit rate, the agreement a student must reach, how sure it must be, the lessons
it needs; and **a row for every kind of question** — what Dez AI may do (Automatic, Off, Watch, Recall, Solo), the lessons (how many from
people, ✓ confirmed, ✗ changed, “Ollama right N %”), who answered in the last 7 days, the student's marks (right on unseen questions,
coverage, proven or learning) and its audits against Ollama. A row opens to show its lessons (who taught it, which model, when); a task's
lessons can be forgotten (all, Ollama's only, or the ones people changed). **⚙ Study now** trains the students; **📚 Learn from the files
here** reads the finished files. The Users page says “Dez AI: learning from Ollama” (or “offline”), the editor's AI helpers say who
answered (“Dez AI answered 5 by itself, from what it has learned · Ollama answered 2 — Dez AI keeps them as lessons”). Command line:
`python cli.py --teacher-status`, `--teacher-train`; `python -m core.teacher status | train | harvest | forget <task>`.

*8. Where it lives.* In the sample database (`sample_files/samples.db`, in **⤓ Database backup**): `lessons`, `teach_days` (counts per
day), `teach_audit`, `students`, `teacher_roles`. Nothing leaves the server; the lessons hold the same kind of text the sample files do,
and only admins can read them. Writers are many (every worker process, every request): a lesson is one short write, never waited for for
more than 3 s, never worth a stall; the daily counts are buffered and written every 3 s.

*Honest limits.* A lesson is only as good as Ollama's answer until a person has judged it — which is why the students must prove
themselves against Ollama *and* people's decisions weigh more; a student agrees with Ollama on the kind of question it saw, so it also
repeats Ollama's habits where nobody has corrected them. The picture memory answers a picture it has seen (also smaller or larger), not
one that merely looks similar to a human eye; a flat block of colour is never remembered. Generating text (link descriptions, summaries,
alt text) is remembered, not generalised: Dez AI does not write new sentences, it reuses Ollama's and people's. “Time saved” is
Ollama's measured answering time × the questions Dez AI answered (a guess of 6 s / 20 s until Ollama has been timed here). The brain's tag
decisions stay as in V4.25–4.27: Ollama's tags only add low-weight training rows for it.

Checked: every test suite of V4.30 passes (the AI tests now keep their lessons in a folder of their own); new `tests/teacher_test.py`
(a stand-in Ollama whose answers follow rules the students can find: a link answered once and never again, even with other words around
it; the switches and levels; a header-row student proven on 70 lessons and then answering 40 new questions alone with at most 2 asked, 39
or 40 right; audits 1 in 2 and a student taken off duty when they disagree, back when they agree; the form-field rule; reference lists
split alone; “the table goes on” alone; the same picture described once, also at twice the size, another picture and a non-picture
passed on; Ollama off; a person's words used and Ollama's lessons confirmed / changed; the brain's tags with little weight, judged by a
built and an approved file; the examples in Ollama's question; four processes writing 160 lessons at once; the Admin API, refused for
a production user; the editor's helpers saying who answered), `tests/ui_teacher_test.py` (the card in a browser: a row for every kind of
question, the tiles, Study now → a proven student, a level held back and kept after a reload, the switches and numbers, a task's lessons
opened and forgotten).


---

**V4.32.0 — A front page that shows everything the studio can do — and can prove every word of it.**

The sign-in page is what a client sees first, so it now answers the questions a client asks before they start: *why this studio, what
exactly does it do, how sure can we be, what do we get, what does it cost us in time, where do our files go?* Each answer is a part of
the page, in this order: the sign-in and the demo → **Why clients choose us** → Dez AI and the zone story → **Ollama teaches, Dez AI
remembers** (V4.32.1: the page says “Online AI”) → the numbers → the features → **every kind of content** → **the screen-reader preview** → **the standards** → **what
lands on your client's desk** → how it works → the teams → **what changes** (a table) → privacy → **the questions clients ask** →
a last call. The menu has eight entries and lights the part on the screen; a thin bar shows how far down the page you are, and a
button takes you back up.

* **Why clients choose us.** Six reasons, each with its proof: right the first time (Dez AI proposes the whole structure, the doubtful
  tags are listed first) · checked on every build (the machine checks, veraPDF when installed, the report says what is left for a
  person) · fast for one, fair for many (300 pages in under a minute on a multi-core server; the queue serves each person in turn) ·
  your files never leave (the guard refuses every connection outside the network, and logs it) · it learns each client's house style
  · proof for the client (the PDF, the Excel report, the history of who did what and for how long).
* **Ollama teaches. Dez AI remembers.** (V4.32.1: **Online AI teaches. Dez AI remembers.**) A small story that moves when it is scrolled to: a question goes to Ollama, the answer comes
  back and travels to Dez AI as a lesson, the share of questions Dez AI answers alone climbs (0 → 78 %), the lessons kept and the
  waiting time saved grow. Beside it: *proven, not trusted* (a student answers alone only after it agrees with its teacher on questions
  it has not seen, and is checked now and then) · *people outrank both* · *your studio's own words* (the examples Dez AI shows the
  teacher). The page says it is an illustration; the real numbers are on Admin → Dez AI.
* **Every kind of content** — sixteen tiles: headings, columns and reading order, lists, tables, figures and alt text, captions and
  footnotes, links and cross-references, contents and index, reference lists, mathematics, forms, running heads and page numbers,
  worksheets, scanned pages, many languages, colour contrast. Each is something the README describes and a test shows.
* **The screen-reader preview.** A page is read block by block and what a screen reader would announce appears beside it (heading level
  1, the text, the picture's description, a table with 3 columns, a column header, a list with 3 items, a link with its description).
* **The standards.** *{N} machine checks* — the number is **counted in the tool**: every `UA-…` rule of `core/checker.py` and
  `core/pipeline.py` (`app.site_facts()`, also the options, the built-in profiles and the undo steps the page quotes), so the page cannot
  say 58 when the tool has 60. Fourteen things the checks look at tick one by one. **Checker errors on our reference files, before and
  after the automatic pass** — six files, drawn as bars: the journal article 9 → 1, the book 6 → 0, the form 9 → 0, the attendance register
  6 → 0, Tamil and Greek text 6 → 0, rotated pages 9 → 1 (the one left is a picture whose description a person approves). They are the
  numbers `tests/run_suite.py` prints. And **an honest note**: a machine check is not a conformance certificate — confirm in PAC 2024
  and veraPDF.
* **What lands on the client's desk**: the accessible PDF (its pages pixel-identical to the original), the Excel report, the veraPDF and
  PAC verdict, the story of the file. **What changes** compares tagging by hand with the studio in eight rows (a real table, with a
  caption and row and column headers). **Nine questions** (certified? where do the files go? what do we need — is a GPU required? can
  the AI learn something wrong? many people at once? which PDFs and languages? house styles? batches and scripts?), answered with the
  limits, one open at a time (`<details>`, so the keyboard works without a script).
* **Every claim has its proof.** Nothing on the page is a testimonial, a logo, a certificate or a promise: the numbers come from the
  tool, the README or a measurement, and the sentences that were bigger than their proof were made smaller (“300 pages in under a
  minute”, as this README says; “a file of 4,000 tags”, as `tests/perf_test.py` builds; no talk of prices; no “certified”).
* **Built as the PDFs it asks for.** One `h1`, no heading level skipped, every part named by its heading (`aria-labelledby`), a skip
  link, a real table, nothing written into the HTML as a handler, nothing loaded from another server (the Content-Security-Policy is
  unchanged). The new moving pictures follow the same rules as the old ones: the **pause** button and the computer's “less motion” stop
  them and show how they end; they start only when seen and stop when they leave; on a phone the sign-in still comes first.

*Honest limits.* The page is an illustration of what the studio does, not a recording; its “300 pages in under a minute” is the figure
this README gives for a multi-core test machine, not a promise for every server; “10 built-in profiles” and “100+ options” are counts
of the tool today; the before → after numbers are for the reference files of `tests/corpus`, not for yours.

Checked: every test suite of V4.31 passes as it was; new `tests/front_facts_test.py` (no server needed: the machine checks, options,
profiles and undo steps on the page are the tool's own; the four workflow steps, the zoom, the password hashing, “300 pages in under a
minute” and “4,000 tags” are what the code, this README and `tests/perf_test.py` say; a connection to a public address is refused before
a byte is sent and the guard logs it, and the page's Content-Security-Policy lets it talk to its own server only; the six reference files
are run through the studio again and must give the numbers and bars on the page, each being what its label says; one `h1`, no skipped
level, every link finds its part, no id twice, every `aria-labelledby` finds its heading, a real table, no handler in the HTML, nothing
loaded from another server, every id the script looks for is on the page; no testimonial, certificate or guarantee), and a longer
`tests/ui_front_test.py` (in a browser: the six reasons, the teacher's story moving and growing, the preview reading line after line and
lighting its block, the checks ticking and the bars filling, the table, the questions — one open at a time, also by keyboard — the
progress bar and the way back up, paused and with less motion: everything shown as it ends, a phone: nothing wider than the screen).


---

**V4.32.1 — The front page calls the model service “the Online AI”.**

On the sign-in page the AI model service that Dez AI asks and learns from (the Ollama server of Admin → Settings → AI) is now named
only **“the Online AI”** — in the page, in the page with a wrong-password message, in `landing.js` and in `landing.css` (the
comments are served too). Admin, the editor, the command line and this README keep the real name; nothing but the front page changed.

* **Where it appears:** the heading **“Online AI teaches. Dez AI remembers.”** and the teacher in its little story, “Meet Dez AI” (taught
  by the Online AI, drafts alt text from the Online AI's vision model), the reading-order chip, the table of what changes, the privacy
  list and three of the questions (what do we need to run it — is a GPU required; do my files leave my server).
* **Said plainly where it runs.** “Online” must not be read as “in the cloud”, which would contradict what the page promises, so the
  first thing the page says about it is that **it runs on your own network** (“Dez AI and the Online AI that teaches it both run on your
  own network”), every mention of it stays clear of “cloud” and “internet”, and the question *Do my files leave my server?* adds the
  limit the guard really has: every connection outside the network is refused — and **if the Online AI should live elsewhere, the admin has
  to name that host in the Privacy guard first** (Admin → Privacy guard).
* `tests/front_facts_test.py` keeps it so: no product name of the model service in the page, its error variant, its script or its
  style (including `style.css`, which the page loads), the Online AI spoken of at least ten times, the first mention saying where it
  runs, and none of its mentions putting it in the cloud or on the internet.


---

**V4.32.2 — Build PDF no longer stops with “list.index(x): x not in list”.**

*What was reported.* Build PDF ended with `The build failed. Your tags are unchanged` and, in the details,
`ValueError: list.index(x): x not in list` — raised in `core/build.py` (`eff.detach(tb.id)`) through `core/model.py` (`detach`).

*Why.* Two cleanups of the build both look for tables and rows that hold nothing, and both used `DocModel.detach`, which takes a
tag out of the tree but leaves it in the model. `repair.drop_duplicate_rows` (run first) took a **table with no rows** out of the
tree; the cleanup after the tagging (it runs when it has dropped at least one empty row) found the same table in the model, and
detached it **a second time** — and `list.index` has nothing to find in a list that no longer holds it. It needs a table without rows
(all its rows deleted by hand, or a zone that found none) *and* a row that nothing is printed in, so it showed in some files only.
A blank row printed on top of a real one failed the same way, one line earlier.

*What changed.*
* `detach` is idempotent: a tag that is out of the tree already answers `-1` instead of raising (so does one whose parent is gone);
  `remove` of such a tag takes it and what hangs from it out of the model; new `DocModel.attached_ids()`.
* `drop_duplicate_rows` and the cleanup of the build look only at tags that are in the tree, and **take what they drop out of the
  model too** (`remove`), so no later step can meet a dropped tag again. A **caption or a note held by a table with no rows stays**,
  where the table was (before, it left the tree together with the table).
* Found on the way, with a fuzz of the editor's operations: **“Group by headings”** (`convert_table_columns`) on a table whose
  headings and words hang straight from it stopped with the same error, and could hang a tag twice; and **Reading position** of an
  artifact stopped with `KeyError: None` — now it says an artifact is not read, so it has no position.

Checked: new `tests/model_integrity_test.py` — the model's own operations; Build PDF with the reported case (a table with no rows next
to a row that is dropped), with a blank row on top of a real one, with a table that has a caption and no rows, with a table that is in
no list — each builds and passes qpdf, the structure checks and the balance of marked content; the heading grouping; and a **fuzz** of 25
operations of the editor (roles, delete, wrap, lists, rows, merge, move, drop, levels, empty tags, remove tags, group by headings,
zones drawn and deleted, geometry, split, rebuild, reading order …) over four files: after each, the tags are a tree (none out of it,
none hung twice, every parent right), nothing stops, and a file built from the result builds. It fails on V4.32.1 and passes now;
every other suite passes as it did.


---

**V4.33.0 — The editor stays quick in a big file: scrolling the tag tree, creating a tag.**

*What was reported.* “The tool is getting heavier — even scrolling is slow, like it is hanging, and sometimes creating a tag keeps loading.”

*What was measured* (a 300-page test book of **15 775 tags**, Chromium; the numbers are from a machine without a graphics card, so
what counts is how many times slower, not the seconds):

| | before | now |
|---|---|---|
| elements in the page (the tag tree) | 80 284 | 1 417 |
| scrolling the tree with the wheel, a picture takes | **167 ms** (6 a second; every one over 100 ms) | **16.7 ms** (60 a second; none over 20 ms) |
| the browser's layout work during 40 wheel steps | 2 997 ms | 88 ms |
| the editor shows its first tag after | 1.7 s | 0.4 s |

*Why.* The tag tree was **every tag of the file in the page** — 15 775 rows, five elements each. At every wheel step the browser laid
the rows out again (each time a few more came into view, and every row's height changed the height of all of them), tested which one
the pointer was over, and kept an intersection observer for each (`content-visibility`, added in V4.30, which hid the cost of the
painting but not of the layout). A file of 3 000 tags was already at 50 ms a picture. Nothing in the script was slow: it was the size of
the page.

*What changed.*
* **The tag tree is a window** (`renderWindow` in `static/editor.js`). Every row is exactly 30 px high, so where a row is, is a sum:
  only the rows near the screen (and 24 above and below) are in the page, between two spacers that keep the scroll bar as long as
  the whole list. A list of 250 rows or fewer is shown whole, as before. The rows are made while you scroll (one picture per
  frame, nothing while the screen is well inside the rows that are there). What reaches for a row that is not in the page —
  the arrow keys, a click on a box of the page, the issues, *Select all shown*, the search — goes by the list (`flat`), not by the
  page: the tree scrolls to the row and the row is made. Screen readers hear the place in the whole tree (`aria-posinset`,
  `aria-setsize`).
* **Analysis and build run below the web server's priority** (`core/jobrunner.py`, `PDFUA_JOB_NICE`, default 8, 0 leaves it): the
  worker that reads or builds a file — and the page processes it starts — no longer take the CPU from a person creating a tag or
  clicking while five files are read on four cores. On an idle machine nothing is slower. (Windows: below normal priority.)
* A long **Issues list** is drawn 300 at a time (“Show 300 more”).
* Measured and found fine: the page picture at 100–400 % (60 pictures a second), the Issues list of 1 900 issues, the Links list
  (400 at a time), the idle editor (0 % of a core), a file's tags in the first request (1 MB with gzip for 15 775 tags).

*Honest limits.* Creating a tag in the 15 775-tag file takes 0.5 s on the server (the page's lines are read again, the reading numbers
are made again); creating a **table, list or figure with the AI** waits for the language model — on a machine without a graphics card
that is tens of seconds, and it is the model, not the editor, that takes the time. A file of 100 000 tags would still need its
first request (10 MB of tags) to be cut up.

Checked: new `tests/ui_tree_test.py` (a file of ~6 000 tags in a browser: a few dozen rows and elements in the page, the scroll bar of
the whole list, wheel scrolling with no held-up picture, the middle and the end of the list, 70 × ↓ and 30 × ↑ with the row always on the
screen, a search for one tag in 6 000, *Select all shown*, a click on a box far down the list, collapse and expand, a change of type
in its row — it hangs on V4.32.2 at *Select all shown*) and `tests/priority_test.py`; every other suite passes as it did.
