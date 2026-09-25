# magicpin Vera Challenge — Project Progression

Last updated: 2026-09-25

This file is the handoff document for the project. Any future person or LLM
should read it before changing the repository.

## Goal

Build a merchant-facing AI assistant for the magicpin “Build Vera Better”
challenge. The assistant must compose concise, grounded WhatsApp messages from
four context types:

1. Category context — category voice, offers, peer statistics, and digest items.
2. Merchant context — identity, performance, offers, history, and signals.
3. Trigger context — the event that caused the message.
4. Optional customer context — relationship, preferences, and consent.

Challenge page: https://magicpin.com/vera/ai-challenge

Required deliverables are a Python bot, a 30-line JSONL submission, and a
short README. The technical testing brief defines these endpoints:

- `GET /v1/healthz`
- `GET /v1/metadata`
- `POST /v1/context`
- `POST /v1/tick`
- `POST /v1/reply`

## Current implementation

### `bot.py`

The bot is deterministic and self-contained. It does not need an API key or an
external service. Run it with:

```text
python bot.py
```

It uses Python's standard-library HTTP server so it works in the current
workspace without installing FastAPI. The public function is:

```python
compose(category, merchant, trigger, customer=None)
```

Implemented message families include research, compliance, CDE opportunities,
performance changes, renewals, festivals, bridal follow-ups, curious asks,
win-backs, IPL, reviews, milestones, planning intent, seasonal dips, supply
alerts, refills, GBP verification, competitors, performance spikes, and
dormancy.

### `conversation_handlers.py`

Small wrapper exposing the multi-turn reply handler for integrations or local
tests.

### `submission.jsonl`

Contains exactly 30 JSONL rows, ordered using the deterministic pair-selection
logic in `dataset/generate_dataset.py`. Each row has:

- `test_id`
- `body`
- `cta`
- `send_as`
- `suppression_key`
- `rationale`

### `README.md`

Explains the deterministic, context-grounded approach and local run command.

## Dataset inventory

The supplied raw data contains:

- 5 category JSON files under `dataset/categories/`
- 10 representative merchants in `dataset/merchants_seed.json`
- 15 representative customers in `dataset/customers_seed.json`
- 25 representative triggers in `dataset/triggers_seed.json`
- `dataset/generate_dataset.py`, which deterministically expands the seeds to
  50 merchants, 200 customers, 100 triggers, and `test_pairs.json`

Do not replace the seed data with invented records. The generator is the source
of truth for the canonical 30-pair order.

## Important data traps already handled

### Placeholder triggers

The generator creates 75 triggers whose payload is only:

```json
{"placeholder": true, "metric_or_topic": "..."}
```

Some of these appear in the canonical 30. The bot must not turn a placeholder
into a fake date, price, research citation, metric, appointment, or offer. It
returns an explicit no-send result and the HTTP tick endpoint skips it.

### Consent

Generated customer contexts can have `reminder_opt_in: false` or consent that
only covers promotional messages. The bot checks trigger-specific consent and
does not send recall, appointment, refill, trial, or win-back messages outside
the customer's consent scope.

### Synthetic dates

The dataset contains dates around April–November 2026 and is synthetic. Do not
recalculate relative timing from the machine's current date. Use supplied
fields such as `days_until`, `days_to_wedding`, and `due_date`.

### Grounding

Case studies contain illustrative details that are not always present in the
raw contexts. Do not copy unsupported prices, customer counts, sources,
delivery promises, or relationship details into messages.

## Validation already completed

- Python syntax check passed for `bot.py` and `conversation_handlers.py`.
- All 25 seed triggers compose without exceptions.
- All 30 generated canonical pairs compose without placeholder leakage.
- Canonical test IDs and suppression keys match the deterministic generator.
- `submission.jsonl` parses as 30 valid JSON objects with no blank bodies.
- Replay behavior was exercised for:
  - canned auto-reply → end
  - clear commitment such as “let’s do it” → action response
  - stop/hostile opt-out → end
- `git diff --check` passed.

## Working rules

- Do not commit, push, reset, or change the remote unless the user explicitly
  asks.
- Preserve the seed data and existing challenge documents.
- Keep messages human, concise, category-appropriate, and grounded in the
  supplied contexts.
- Prefer a no-send decision over invented context.
- Keep the bot deterministic; do not add an external model dependency without
  a clear reason and an offline fallback.
- After changes, run syntax validation, JSONL validation, and a focused replay
  test. Remove generated `__pycache__/` folders if they appear locally; they
  are ignored by `.gitignore`.

## Recommended next steps

1. Start the bot locally with `python bot.py`.
2. Exercise the HTTP endpoints with the supplied `judge_simulator.py` after
   configuring its local bot URL and judge provider.
3. Review the 30 messages for tone and challenge fit before submission.
4. Add deployment-specific metadata and a public URL only when the user is
   ready to deploy.
5. Let the user review the final diff and commit it themselves.

## Current Git state

The repository has an `origin` remote and no commit has been created yet. All
project files are intentionally still uncommitted for user review.
