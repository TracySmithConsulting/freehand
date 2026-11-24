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
- **"Combine these three PowerPoints into one."** Slides keep their text,
  pictures and speaker notes, and Markdown notes become slides. Charts,
  videos and embedded objects don't come across: FreeHand says which.
- **"How does Report.docx look?"** or **"Check slides 3 to 5 look right."**
  FreeHand turns up to six pages or slides into pictures, and your AI model
  says what looks wrong: text cut off or overlapping, text that's too small
  or faint, pictures stretched or covering text, near-empty pages. It's one
  AI call for all of them. You can ask about something in particular too.
- **"Save Report.docx as a PDF."** An accessible, tagged PDF beside it, and
  FreeHand says whether it has a title and language.
- **"Make a picture of a cosy reading corner and put it on slide 2"**, or
  **"Put my blanket photo after paragraph 4."** FreeHand makes the picture
  with your image service, or uses a picture you put in the work folder,
  and saves it there. Your AI model writes the alt text by looking at the
  actual picture, unless you give it. Making a picture costs a little, like
  an AI message.

## Pictures: which service

FreeHand makes pictures with whichever of your AI services can, using the
keys you set up under AI model: Agnes first (its image model may be free),
then MiniMax, then OpenRouter. If one can't (no credit, for example), it
tries the next, and says why if none could.

## Good to know

- When combining Word files, the text of links is kept but not their web
  address, and numbered lists may restart their numbers. Check the result.
- PDF text can come out with odd line breaks, so it may need tidying.
- Alt text written by AI is a draft: check it says what matters about each
  picture.
