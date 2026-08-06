# Trendly Support Assistant

An agentic support assistant for Trendly (D2C fashion retailer) built for the
Yellow.ai FDE (Intern) screening assignment. Handles order status, policy
Q&A, return/exchange eligibility + processing, and clean escalation to a
human — via real tool-calling, not keyword matching.

URL:https://trendly-support-agent-605r.onrender.com/

## Architecture at a glance

```
User → FastAPI (/chat) → agent.py (ReAct tool-calling loop)
                              │
                              ├─ Groq LLM (Llama 3.3 70B, free tier, function calling)
                              │
                              └─ tools.py (deterministic policy engine)
                                    ├─ identify_customer
                                    ├─ get_order              (session-authorized)
                                    ├─ check_return_eligibility  (policy rules in code)
                                    ├─ initiate_return / initiate_exchange (re-validates before acting)
                                    └─ escalate_to_human
```

The LLM never decides eligibility, dates, or authorization itself — it calls
`tools.py` and reports back exactly what the tool returned. Policy rules
(30-day window, non-returnable categories, final-sale exchange-only, one
exchange per item, lost-parcel-is-not-a-return, etc.) live in code, not in
the prompt's hope that the model "remembers" them correctly every time.

See `SOLUTION.md` for the full design writeup, trade-offs, and discovery
questions.

## Stack

- **Backend:** Python, FastAPI
- **LLM:** Groq API, `llama-3.3-70b-versatile` (free tier, supports native
  function/tool calling — no credit card required)
- **State:** in-memory per-session (swap for Redis/DB in production, see
  SOLUTION.md)
- **Frontend:** a single static HTML/JS chat widget (no build step)

## Run it locally (one command, after setup)

```bash
git clone <this-repo-url>
cd trendly-support-agent
pip install -r requirements.txt
cp .env.example .env        # then paste in your free Groq API key
export $(cat .env | xargs)  # or use direnv / your shell's env loading
uvicorn app:app --host 0.0.0.0 --port 8000
```

Open `http://localhost:8000` — the chat widget is served directly from
`/`. Or talk to it via curl:

```bash
curl -X POST http://localhost:8000/chat \
  -H "Content-Type: application/json" \
  -d '{"message": "hi, I am ananya.rao@example.com"}'
```

### Get a free Groq API key
1. Go to https://console.groq.com/keys
2. Sign up (no card needed), create a key
3. Put it in `.env` as `GROQ_API_KEY=...`

## Run the tests

```bash
pip install pytest
pytest tests/test_tools.py -v          # 21 unit tests, no API key needed
python tests/scripted_conversations.py # end-to-end, needs GROQ_API_KEY, uses live calls
```

`test_tools.py` exercises the deterministic eligibility engine directly and
covers every edge case planted in `orders.json` (outside-window, non-returnable
category, final-sale, cancelled, lost-in-transit, exchange-limit, and the
cross-customer authorization boundary). `scripted_conversations.py` drives
the full agent through the LLM to sanity-check orchestration and refusals.

## Deploy (free tier)

### Option A — Render.com (recommended, `render.yaml` included)
1. Push this repo to GitHub.
2. On Render: New → Blueprint → point at the repo (it reads `render.yaml`).
3. Set the `GROQ_API_KEY` env var in the Render dashboard (marked `sync: false`
   so it's not committed).
4. Deploy — Render gives you a live `https://<name>.onrender.com` URL.

Free-tier note: Render's free web services spin down after inactivity and
take ~30s to wake on the next request — expected on a free tier, not a bug.

### Option B — Railway / Fly.io / any Python PaaS
`Procfile` is included (`web: uvicorn app:app --host 0.0.0.0 --port $PORT`).
Set `GROQ_API_KEY` as an environment variable on the platform and deploy.

### Option C — Run with Docker
```bash
docker build -t trendly-agent .
docker run -p 8000:8000 -e GROQ_API_KEY=your_key trendly-agent
```

## Environment variables

| Variable | Required | Default | Notes |
|---|---|---|---|
| `GROQ_API_KEY` | yes | — | free key from console.groq.com |
| `GROQ_MODEL` | no | `llama-3.3-70b-versatile` | any Groq tool-calling model |
| `AS_OF_DATE` | no | `2026-08-05` | pins "today" for return-window math so the fixed dataset's notes stay consistent regardless of when this is graded — see SOLUTION.md |

## AI-usage note

I used Claude to scaffold the FastAPI/agent structure, generate the initial
draft of `tools.py`'s eligibility rules from the policy doc, and draft the
test scenarios. I wrote/rewrote the system prompt in `agent.py` myself
through several iterations (see `PROMPTS.md` for the process), reviewed and
adjusted every eligibility rule in `tools.py` by hand against the policy
document and the `_note_for_designers` hints in `orders.json`, and ran the
21 unit tests myself to verify correctness before writing this README. I can
walk through and modify any part of this code live.

## Repo structure

```
app.py                       FastAPI server (/chat, /reset, /health, /debug/actions)
agent.py                     Tool-calling orchestration loop + system prompt
tools.py                     Deterministic policy engine (order lookup, eligibility, actions)
orders.json                  Provided fixed dataset (unmodified)
trendly_policy.md            Provided policy doc (unmodified)
static/index.html            Chat UI
tests/test_tools.py          21 unit tests against the policy engine
tests/scripted_conversations.py   End-to-end LLM conversation tests
requirements.txt
render.yaml, Procfile        Deployment configs
PROMPTS.md                   Prompt iteration log
SOLUTION.md                  Architecture, trade-offs, discovery questions
```
