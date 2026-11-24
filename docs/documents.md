# Working on documents

FreeHand can work on your Word documents, PowerPoints, PDFs, Excel files
and Markdown or text files: read them, check and fix their accessibility,
edit them, take out comments and editor's notes, combine them, and make new
ones.

## Setting it up, once

FreeHand works in one folder you share with it. In FreeHand's `.env` file,
add:

```
FREEHAND_WORK_DIR=C:\Users\you\Documents\FreeHand work
```

Then restart FreeHand. Put the documents you want help with in that folder,
or in folders inside it.

**Your originals are never changed.** Every result is saved as a new file
beside the original, for example `Report (edited).docx`, `Report
(clean).docx` or `Report (accessible).docx`. If that name is taken,
FreeHand adds (2), (3) and so on. It never deletes anything.

## What you can ask

- **"What's in my work folder?"**
- **"Read Chapter 3 and summarise it."** Word, PowerPoint, PDF, Excel,
  Markdown and text.
- **"Check Report.docx for accessibility."** For Word, PowerPoint and PDF,
  FreeHand lists the problems, why each matters, and how to fix it:
  - pictures without alt text, or with only a file name;
  - missing or skipped headings, and bold text that looks like a heading;
  - slides without titles;
  - tables without a header row;
  - no title or language;
  - empty paragraphs used for spacing;
  - links that say "click here";
  - comments left in;
  - PDFs that aren't tagged or are only scanned pictures.
- **"Make an accessible copy."** FreeHand sets the title and language,
  gives tables a header row, and writes alt text for pictures. Your AI model
  looks at each picture to do this, so it uses your AI. Then it says what's
  still to do.
- **"Change every 'client' to 'customer'", "Make paragraph 12 a heading",
  "Give slide 4 the title 'Our results'".** The formatting is kept.
- **"Clean up Draft.docx."** FreeHand takes out comments, accepts tracked
  changes, and removes editor's notes in square brackets, like "[Editor:
  check this]", "[TK]", "[TODO]" or "[?]". It lists every note it removed,
  so you can check none were meant for readers.
- **"Combine chapter 1, chapter 2 and the appendix into one document."**
  Word files keep their formatting and pictures. PDF, Markdown, text and
  Excel files come in as text, headings and tables.
- **"Make a PowerPoint from these notes"** or **"Write this up as a Word
  document."** FreeHand uses real headings and slide titles, so the result is
  accessible from the start.

## Good to know

- When combining Word files, the text of links is kept but not their web
  address, and numbered lists may restart their numbers. Check the result.
- PDF text can come out with odd line breaks, so it may need tidying.
- Alt text written by AI is a draft: check it says what matters about each
  picture.
