"""Ask the user, with choices (Nov 2026).

When something is genuinely the user's to decide, the model can call
``ask_user`` with one to four questions, each with a few options. The
turn ends there, and the questions are the reply:

- in the console they're shown as radio buttons (pick one) or checkboxes
  (pick any), plus a "Something else" box, and a button that sends the
  answers as the next message;
- in WhatsApp, Telegram and Slack, and for anyone reading the text, the
  same questions come as a numbered list with lettered options, and the
  person just replies ("1b, 2a and c").

Nothing is kept waiting: the questions go into the chat history like any
reply, so the next message is read with them in view.
"""
from __future__ import annotations

from typing import Optional

ASK_USER = "ask_user"
MAX_QUESTIONS = 4
MAX_OPTIONS = 6
LETTERS = "abcdef"


def schema() -> dict:
    return {
        "type": "function",
        "function": {
            "name": ASK_USER,
            "description": (
                "Ask the user to choose, when a decision is theirs to make and you can't sensibly "
                "pick for them. Give 1 to 4 short questions, each with 2 to 6 options. The user can "
                "always answer in their own words too. Don't use it for things you can find out or "
                "for a simple yes or no."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "intro": {"type": "string", "description": "Optional one sentence before the questions"},
                    "questions": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": MAX_QUESTIONS,
                        "items": {
                            "type": "object",
                            "properties": {
                                "question": {"type": "string", "description": "The question, ending with ?"},
                                "options": {"type": "array", "items": {"type": "string"},
                                            "minItems": 2, "maxItems": MAX_OPTIONS},
                                "multiple": {"type": "boolean",
                                             "description": "True if the user may choose more than one"},
                            },
                            "required": ["question", "options"],
                        },
                    },
                },
                "required": ["questions"],
            },
        },
    }


def _text(value, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def clean(args: Optional[dict]) -> Optional[dict]:
    """Checked, tidied questions, or None when there's nothing usable.

    Small models get the shape slightly wrong (a single question instead of
    a list, options as one string), so take what's meant where it's clear.
    """
    args = args if isinstance(args, dict) else {}
    raw = args.get("questions")
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, list) and args.get("question"):
        raw = [args]
    questions = []
    for q in raw or []:
        if not isinstance(q, dict):
            continue
        text = _text(q.get("question"), 300)
        options = q.get("options")
        if isinstance(options, str):
            options = [o for o in options.replace(";", ",").split(",")]
        seen, opts = set(), []
        for o in options or []:
            o = _text(o.get("label") if isinstance(o, dict) else o, 120)
            if o and o.lower() not in seen:
                seen.add(o.lower())
                opts.append(o)
        if text and len(opts) >= 2:
            questions.append({"question": text, "options": opts[:MAX_OPTIONS],
                              "multiple": bool(q.get("multiple"))})
        if len(questions) == MAX_QUESTIONS:
            break
    if not questions:
        return None
    return {"intro": _text(args.get("intro"), 300), "questions": questions}


def as_text(asked: dict) -> str:
    """The questions as plain text: for chat apps, the history, and voice."""
    lines = [asked["intro"]] if asked.get("intro") else []
    many = len(asked["questions"]) > 1
    for n, q in enumerate(asked["questions"], 1):
        how = "choose any" if q["multiple"] else "choose one"
        lines.append(f"{f'{n}. ' if many else ''}{q['question']} ({how})")
        lines.extend(f"   {LETTERS[i]}) {o}" for i, o in enumerate(q["options"]))
    lines.append("Reply with your choices" + (", for example 1a, 2b" if many else ", for example a")
                 + ", or in your own words.")
    return "\n".join(lines)


def refusal() -> str:
    return ("That ask_user call had no usable questions: each needs a question and at least two "
            "options. Ask again properly, or just ask in plain words.")
