# Vera submission

This is a small, deterministic composer for the magicpin Vera challenge. It
keeps the useful part of the brief in one place: every outbound message is
grounded in the category, merchant, trigger, and (when present) customer
context that the judge pushed to the bot.

The copy is written by trigger family rather than by one generic template.
That lets a research note sound like a peer-to-peer note, a recall reminder
use a real slot, and a performance dip lead with the number that changed.
The fallback is intentionally conservative: if an injected trigger does not
have enough detail, the bot asks to turn the supplied signal into a next step
instead of inventing an offer, source, or result.

There is no API key or network dependency. The server stores contexts in
memory, replaces them only when a higher version arrives, and keeps a small
conversation state for auto-reply detection, clear intent handoffs, graceful
stops, and de-duplication.

Run it with:

```text
python bot.py
```

The optional `conversation_handlers.py` module exposes the same reply router
for an offline integration. The generated `submission.jsonl` is built from
the deterministic pairs produced by `dataset/generate_dataset.py`.

The only missing production detail is the public URL and team metadata; those
belong to the deployment environment, not the message composer.
