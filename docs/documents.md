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
  - lists typed with "•" or "-" instead of real bullets, and list-styled
    lines with no bullet showing;
  - links that say "click here";
  - comments left in;
  - PDFs that aren't tagged or are only scanned pictures.
- **"Make an accessible copy."** FreeHand sets the title and language,
  gives tables a header row, and writes alt text for pictures. Your AI model
  looks at each picture to do this, so it uses your AI. Then it says what's
  still to do.
- **"Change every 'client' to 'customer'", "Make paragraph 12 a heading",
  "Give slide 4 the title 'Our results'".** The formatting is kept.
- **"Make paragraphs 18 to 22 real bullets."** FreeHand makes them real
  Word bullets, or a numbered list, and takes out any typed "•". A screen
  reader then says it's a list and how many items it has.
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
  with your image service, or uses a picture you put in the work folder.
  A made picture is saved beside its document, named after it. Your AI
  model always writes a made picture's alt text by looking at it, and says
  if any words in it are garbled, misspelled or in another language. For
  your own photo, you can give the alt text. Making a picture costs a
  little, like an AI message.
- **"Replace the picture in paragraph 24 with a new one"** or **"Take out
  the picture at the end."** FreeHand swaps or removes it, so you don't end
  up with two.

## A big job, like a book

- **Your editor's comments:** ask "What did my editor say in Chapter 3?".
  FreeHand lists each comment with who wrote it, what it says, the words
  it's on, and its paragraph. Once a comment is dealt with, FreeHand can
  remove just that one.
- **Tracked changes:** ask "Read me the tracked changes in Chapter 3".
  FreeHand lists each insertion and deletion with its text, who made it
  and where. Then you can accept or reject them one by one ("accept 1 to
  4, reject 5"), or all at once.
- **Long documents** are read in parts of about 30,000 characters, and
  FreeHand reads on until it's done.
- **More steps:** document work can take up to 15 steps per message
  before FreeHand reports back. A long job may still need "carry on".
- **It carries on where it stopped.** FreeHand keeps short notes of what
  it read and saved with each reply, so "Go ahead" or "Make the changes"
  picks up from there instead of reading everything again. If it runs out
  of steps, it says exactly which changes are left.
- **Whole-chapter changes in one go:** "Make paragraphs 26, 32 and 54
  Heading 2", "Put the section titles in sentence case, keeping
  DeafBlind", or "Take the full stops off the bullets in 5.2". Sentence
  case, title case and capitals keep the words you name as written.

## Putting a book together

- **Combine the parts in order:** "Combine the cover, foreword, chapters 1
  to 8, glossary and references into one document." Each part keeps its
  formatting, pictures, bullets, numbered lists and footnotes, and the
  footnotes are numbered through the whole book.
- **A contents page:** "Add a contents page after the title page."
  FreeHand adds a Contents heading and a real Word contents list built
  from the headings. When you open the file in Word, it asks to update the
  fields: choose Yes to add the page numbers.
- **Footnotes:** first ask "Suggest footnotes for Chapter 3 from the
  references". FreeHand lists each sentence it would footnote and the
  reference it would cite. Only once you've agreed the list does it add
  them, as real Word footnotes that a screen reader announces. Ask "List
  the footnotes" to hear them in order. Only footnote a sentence that
  really comes from a source a reader could find.

**Work chapter by chapter, then combine.** Each step sends the
conversation so far to your AI model, so smaller pieces cost less and are
easier to check. A good order for each chapter: read the comments and
tracked changes, decide, make one edited copy with all the changes, clean
out the notes, check accessibility, then add pictures. When every chapter
is done, combine them into the book.

## Pictures: which service

FreeHand makes pictures with whichever of your AI services can, using the
keys you set up under AI model: Agnes first (its image model may be free),
then MiniMax, then OpenRouter. If one can't (no credit, for example), it
tries the next, and says why if none could.

## Good to know

- When combining Word files, the text of links is kept but not their web
  address. Check the result.
- PDF text can come out with odd line breaks, so it may need tidying.
- Alt text written by AI is a draft: check it says what matters about each
  picture.
- Image services are bad at drawing words: they come out garbled or in the
  wrong language. Pictures without words work better, and the words can go
  in the text or the caption.
