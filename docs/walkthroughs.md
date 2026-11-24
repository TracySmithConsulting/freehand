# Screen reader walkthroughs

A walkthrough is a recording of you using someone's app or website with a
screen reader, talking through what happens, for a client. FreeHand helps
while you record, and drafts the report afterwards.

## Setting it up, once

FreeHand needs one folder for your walkthroughs, where your recordings are
saved and where it puts the reports. In FreeHand's `.env` file, add:

```
FREEHAND_WALKTHROUGHS_DIR=C:\Users\you\Documents\Walkthroughs
```

Then restart FreeHand. This is the only folder outside its own vault that
FreeHand may write to, and it only ever adds new files there. It never
changes or deletes anything.

Set your recording program (for example OBS) to save recordings in that
folder, or in a folder inside it.

## While you record

Two ways to mark problems, and you can use both:

- **Say "Problem:" out loud,** then what's wrong. FreeHand finds it in the
  recording afterwards, at the exact time.
- **Send FreeHand a note,** for example from Telegram on your phone:
  - "start walkthrough" (or "start walkthrough for Acme's app") when you
    start recording;
  - "note: the Stop button has no name" whenever you notice something;
  - "stop walkthrough" when you've finished.

Notes are handled straight away, without AI, so they cost nothing. On
Telegram the replies are silent, so your phone doesn't make a sound on the
recording. Notes are timed from "start walkthrough", and FreeHand moves
them onto the recording's own times afterwards.

## Afterwards

Ask FreeHand to "write the walkthrough report". You can also give the app's
name, the client and the tasks you agreed. FreeHand takes the newest
recording in your folder, or the one you name, and in the background:

1. turns its speech into text, on your computer;
2. finds your "Problem:" moments and your notes;
3. asks your AI model once for a draft of the five biggest problems;
4. saves three new files beside the recording:
   - `... report (draft).docx`: the report, as an accessible Word document
     with real headings;
   - `... transcript.txt`: everything said, with times;
   - `... captions.srt`: captions, for example for YouTube.

It tells you when the report is ready, on FreeHand's page and in the chat
you asked from. A half-hour recording usually takes a few minutes.

The report is a draft. Check every problem against the recording, fill in
the details in square brackets, and delete the "Draft by FreeHand" line
before you send it.
