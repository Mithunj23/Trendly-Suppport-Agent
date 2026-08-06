# SOLUTION.md — Trendly Support Assistant

## Architecture

A FastAPI backend runs a ReAct-style tool-calling loop against Groq's free
`llama-3.3-70b-versatile` (native OpenAI-compatible function calling). Six
tools are exposed: `identify_customer`, `get_order`, `check_return_eligibility`,
`initiate_return`, `initiate_exchange`, `escalate_to_human`. The loop calls
the model, executes whatever tools it requests, feeds results back, and
repeats until the model returns plain text (capped at 6 iterations, with an
automatic escalation as a safety valve if it never converges).

The key design decision: **the model orchestrates, but never decides.**
Every fact that matters — whether an item is inside the return window,
whether a category is returnable, whether this is the customer's order —
is computed in plain Python (`tools.py`) from the policy document's actual
rules and the order data, and handed to the model as a structured verdict.
The model's job is to gather the right inputs, call the right tool, and
phrase the output well — not to do policy arithmetic in its head. Early
testing showed the model reliably catches the "obvious" rule (30-day window)
but silently drops secondary rules (jewellery is non-returnable regardless
of window) when asked to juggle several at once — see `PROMPTS.md` v2 for
the specific failure and fix.

The same logic extends to authorization: `customer_id` is never a model-
supplied argument. It's resolved once via `identify_customer` and then
injected server-side into every subsequent tool call from session state.
A prompt instruction alone ("don't discuss other customers' orders") proved
insufficient under adversarial testing; moving the check into code closed
it completely (`tests/test_tools.py::test_get_order_wrong_customer_refused`).

State is a per-session in-memory object (message history + authenticated
`customer_id` + exchange counters), keyed by a client-generated
`session_id`. Good enough for a demo / single instance; noted as a
limitation below.

## Key trade-offs

- **Policy doc inlined in the system prompt, not RAG.** At ~1,500 words it
  fits entirely in context, and inlining removes retrieval-failure risk
  (wrong chunk → confidently wrong answer) for a single small document.
  Doesn't scale to a multi-document policy library — see limitations.
- **Fixed "as of" date (`AS_OF_DATE`, default 2026-08-05) instead of the
  wall clock.** The dataset's own `_note_for_designers` hints are written
  relative to a specific day (e.g. "well outside the 30-day window"). Using
  `datetime.now()` would make those notes drift true/false as real time
  passes past the two-week grading window. Pinning the date keeps the fixed
  dataset's intended behavior stable indefinitely, at the cost of the demo
  not reflecting "today" if run much later — documented and overridable via
  env var.
- **Eligibility logic in code, not in the prompt.** Costs some flexibility
  (a genuinely novel policy exception needs a code change, not just a prompt
  edit) in exchange for auditability and determinism — the thing actually
  being evaluated here.
- **Single LLM call per tool-loop step, not a separate planner model.** A
  dedicated planning step (plan → execute → replan) would handle more
  complex multi-order requests more robustly, but adds latency and cost for
  a workload that's 70% simple, single-order lookups. Chose the simpler
  ReAct loop and let the model re-plan naturally on each iteration.

## Known limitations

- **In-memory session store.** Restarts lose all sessions; doesn't work
  across multiple server instances without a shared store (Redis, etc.).
- **No footwear-return test case in the fixed dataset.** All footwear orders
  (TR-4525) are undelivered, so the ₹300 no-box-deduction path is
  implemented and prompted for but not exercised by a real delivered
  footwear order in `orders.json`.
- **Free-tier LLM rate limits.** Groq's free tier is generous but not
  unlimited; a burst of concurrent demo traffic could hit a 429. No retry/
  backoff is implemented beyond what the loop naturally allows.
- **No conversation persistence across page reloads beyond the browser's
  localStorage session_id** — clearing storage starts a fresh, unauthenticated
  session (this is arguably correct behavior for a support widget, but worth
  naming as a choice).
- **Business-day math for dispatch/delay windows (`_business_days_between`)
  is a simple Mon–Fri count** — it doesn't account for public holidays,
  which policy 1.1 explicitly mentions. Would need a holiday calendar for
  full correctness.
- **The model can still phrase things inconsistently across runs** (it's an
  LLM) even though the underlying decision is deterministic. The tool-result
  JSON is the ground truth; the prose wrapping it is not guaranteed
  byte-identical run to run — expected and generally fine for a chat
  interface, but worth flagging for anyone building strict conformance tests
  on the text output rather than the tool calls.

## Five discovery questions for Trendly's ops team

1. **What does "escalate to a human" actually connect to?** A ticket queue
   (Zendesk/Freshdesk), a live handoff to a human agent mid-chat, or both
   depending on urgency? This changes whether `escalate_to_human` needs to
   also pause/end the bot conversation or can keep chatting while a ticket
   works in the background.
2. **How is customer identity verified today** — order ID, email, phone,
   OTP, logged-in session from the storefront? I assumed email/phone/
   customer-ID matching for this exercise, but a real deployment probably
   has an existing auth session to hook into instead of re-asking.
3. **What's the actual return/exchange fulfillment system** — is there an
   API this agent should call to genuinely create pickup requests and
   refunds, or does "initiate_return" here just need to hand off a
   structured request to an existing ops workflow?
4. **How often does the policy document change, and who owns edits?** If
   it's revised often, I'd want a versioned/cached fetch (with a "policy
   last updated" check) rather than a file baked into the deployment, so
   the agent doesn't answer from a stale copy.
5. **What's the actual language/locale mix of the 2,000 daily chats?** This
   build assumes English; if a meaningful share is in Hindi or other
   regional languages, that changes both the prompt and which model/tier
   handles it best.
