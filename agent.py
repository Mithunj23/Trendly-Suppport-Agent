"""
agent.py
--------
The orchestration layer. This is a real tool-calling ReAct loop:
  1. Send conversation + tool schemas to the LLM.
  2. If the LLM emits tool_calls, execute them against tools.py (server-side,
     with customer_id injected from session state — never from the LLM).
  3. Feed tool results back as `tool` messages.
  4. Repeat until the LLM returns a plain text answer.
  5. Cap iterations so a confused loop can't run forever; on cap, escalate.

Uses Groq's OpenAI-compatible /chat/completions endpoint (free tier,
llama-3.3-70b-versatile, which supports function calling). Swapping to
OpenAI or Anthropic later only means changing the client + model name —
the tool schemas and loop are already OpenAI-tool-call-shaped.
"""

import os
import json
import traceback
from pathlib import Path
from dotenv import load_dotenv
from groq import Groq
load_dotenv()

import tools

MODEL = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")
MAX_TOOL_ITERATIONS = 6

_client = None


def get_client():
    global _client
    if _client is None:
        load_dotenv()

        api_key = os.environ.get("GROQ_API_KEY")
        if not api_key:
            raise RuntimeError(
                "GROQ_API_KEY is not set. Get a free key at https://console.groq.com/keys "
                "and set it as an environment variable."
            )

        _client = Groq(api_key=api_key)

    return _client


POLICY_TEXT = (Path(__file__).parent / "trendly_policy.md").read_text()

SYSTEM_PROMPT = f"""You are Trendly's support assistant, embedded in the chat widget on trendly.com.

# Who you are talking to
You do NOT know who the customer is until they are identified. Before looking up
any order, you must call `identify_customer` with whatever they give you (email,
phone, or order/customer ID they claim). If identification fails, ask them to
double check what they typed — do not guess, and do not proceed to order lookups.
Once identified, you don't need to re-identify them again this conversation.

# Ground rules (non-negotiable)
- Policy questions must be answered ONLY from the policy document below. If the
  document does not cover something, say you don't know and offer a human agent.
  Never invent a policy, a number, or an exception.
- Never state whether a return/exchange is eligible from your own reasoning.
  Always call `check_return_eligibility` and report exactly what it returns.
- Never call `initiate_return` or `initiate_exchange` without having just
  confirmed eligibility in this turn (the tools re-check anyway, but you should
  never promise an outcome before calling them).
- Never offer a discount, coupon, waiver, or goodwill credit that isn't in the
  policy document.
- Never ask for or accept bank account numbers, card numbers, or CVV in chat.
  If a COD refund needs bank details, tell the customer a human agent will
  collect them securely, and escalate.
- Never discuss or confirm an order that does not belong to the identified
  customer, even if the customer insists it's theirs or gives you the right-
  looking order ID. Rely on `get_order`'s own authorization check.
- Lost-parcel claims are not returns — always escalate them to a human, per
  policy 1.6. Do not attempt to resolve them yourself.
- If a customer sounds upset (delay, damage, lost item), briefly acknowledge
  that before reciting policy. A flat policy quote to an angry customer reads
  as robotic and erodes trust.
- When you escalate, write a `summary` a human agent could act on in five
  seconds: what happened, what the customer wants, what you already checked.
- If you're not confident, say so and offer to escalate rather than guessing.

# When to escalate
- Lost-parcel claims (policy 1.6)
- COD refunds needing bank details (policy 3.3)
- A second exchange on the same item (policy 4.4)
- Anything the policy document doesn't cover
- The customer explicitly asks for a human
- Damaged/wrong items outside your ability to verify (still gather details
  first: photos are policy-required within 48 hours, but a human processes it)
- Any situation where you're not confident the policy clearly resolves it

# Tone
Plain, warm, concise. No corporate filler. Use rupee amounts and business-day
figures exactly as stated in the policy — don't round or approximate.

# Policy document (verbatim — this is your only source of truth for policy)
---
{POLICY_TEXT}
---
"""

TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "identify_customer",
            "description": "Resolve a customer's identity from an email, phone number, or customer ID they provide. Must be called before any order lookup.",
            "parameters": {
                "type": "object",
                "properties": {
                    "identifier": {"type": "string", "description": "Email, phone, or customer ID as given by the user."}
                },
                "required": ["identifier"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_order",
            "description": "Look up an order's status and details by order ID. Only returns data if the order belongs to the currently identified customer.",
            "parameters": {
                "type": "object",
                "properties": {
                    "order_id": {"type": "string", "description": "e.g. TR-4521"}
                },
                "required": ["order_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_return_eligibility",
            "description": "Check whether a specific item in an order is eligible for a return (refund) or exchange (size only), per policy. Always call this before telling the customer whether something is eligible.",
            "parameters": {
                "type": "object",
                "properties": {
                    "order_id": {"type": "string"},
                    "sku": {"type": "string", "description": "SKU of the item in question, from the order's items list."},
                    "action": {"type": "string", "enum": ["return", "exchange"]},
                },
                "required": ["order_id", "sku", "action"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "initiate_return",
            "description": "Create a return record for an eligible item. Will refuse if the item is not actually eligible, even if you believe it is.",
            "parameters": {
                "type": "object",
                "properties": {
                    "order_id": {"type": "string"},
                    "sku": {"type": "string"},
                    "reason": {"type": "string", "enum": ["change_of_mind", "wrong_item", "damaged", "defective", "other"]},
                },
                "required": ["order_id", "sku", "reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "initiate_exchange",
            "description": "Create a size-exchange record for an eligible item. Will refuse if not actually eligible (e.g. already exchanged once).",
            "parameters": {
                "type": "object",
                "properties": {
                    "order_id": {"type": "string"},
                    "sku": {"type": "string"},
                    "new_size": {"type": "string"},
                },
                "required": ["order_id", "sku", "new_size"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "escalate_to_human",
            "description": "Hand off to a human support agent with a structured, actionable summary.",
            "parameters": {
                "type": "object",
                "properties": {
                    "reason_category": {
                        "type": "string",
                        "enum": [
                            "lost_parcel",
                            "cod_bank_details",
                            "second_exchange_approval",
                            "damaged_or_wrong_item",
                            "out_of_policy_scope",
                            "customer_requested_human",
                            "other",
                        ],
                    },
                    "summary": {"type": "string", "description": "What happened, what the customer wants, and what you already checked. Written for a human to act on immediately."},
                    "order_id": {"type": "string", "description": "Optional, if relevant."},
                    "priority": {"type": "string", "enum": ["normal", "high"]},
                },
                "required": ["reason_category", "summary"],
            },
        },
    },
]


class Session:
    """Holds per-conversation state: message history + authenticated identity."""

    def __init__(self):
        self.messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        self.customer_id = None
        self.customer_name = None

    def to_dict(self):
        return {
            "messages": self.messages,
            "customer_id": self.customer_id,
            "customer_name": self.customer_name,
        }

    @classmethod
    def from_dict(cls, d):
        s = cls()
        s.messages = d.get("messages") or s.messages
        s.customer_id = d.get("customer_id")
        s.customer_name = d.get("customer_name")
        return s


def _dispatch_tool(session: Session, name: str, args: dict):
    """
    Executes a tool call. customer_id is ALWAYS taken from session state,
    never from `args`, even if the model included one — this is the
    server-side authorization boundary described in tools.py.
    """
    try:
        if name == "identify_customer":
            result = tools.identify_customer(args["identifier"])
            if result.get("found"):
                session.customer_id = result["customer_id"]
                session.customer_name = result["name"]
            return result

        if name == "get_order":
            return tools.get_order(args["order_id"], customer_id=session.customer_id)

        if name == "check_return_eligibility":
            return tools.check_return_eligibility(
                args["order_id"], args["sku"], args["action"], customer_id=session.customer_id
            )

        if name == "initiate_return":
            return tools.initiate_return(
                args["order_id"], args["sku"], args["reason"], customer_id=session.customer_id
            )

        if name == "initiate_exchange":
            return tools.initiate_exchange(
                args["order_id"], args["sku"], args["new_size"], customer_id=session.customer_id
            )

        if name == "escalate_to_human":
            return tools.escalate_to_human(
                args["reason_category"],
                args["summary"],
                customer_id=session.customer_id,
                order_id=args.get("order_id"),
                priority=args.get("priority", "normal"),
            )

        return {"error": f"unknown_tool:{name}"}
    except Exception as e:
        traceback.print_exc()
        return {"error": "tool_execution_failed", "detail": str(e)}


def run_turn(session: Session, user_message: str) -> dict:
    """
    Runs one user turn through the tool-calling loop. Returns
    {"reply": str, "tool_trace": [...]} for the caller (API/UI) to render.
    """
    client = get_client()
    session.messages.append({"role": "user", "content": user_message})
    tool_trace = []

    for _ in range(MAX_TOOL_ITERATIONS):
        response = client.chat.completions.create(
            model=MODEL,
            messages=session.messages,
            tools=TOOL_SCHEMAS,
            tool_choice="auto",
            temperature=0.2,
        )
        choice = response.choices[0]
        msg = choice.message

        if not msg.tool_calls:
            session.messages.append({"role": "assistant", "content": msg.content or ""})
            return {"reply": msg.content or "", "tool_trace": tool_trace}

        # The assistant wants to call one or more tools.
        session.messages.append(
            {
                "role": "assistant",
                "content": msg.content or None,
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                    }
                    for tc in msg.tool_calls
                ],
            }
        )

        for tc in msg.tool_calls:
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            result = _dispatch_tool(session, tc.function.name, args)
            tool_trace.append({"tool": tc.function.name, "args": args, "result": result})
            session.messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": json.dumps(result),
                }
            )

    # Safety valve: if we somehow loop MAX_TOOL_ITERATIONS times without a
    # final answer, don't leave the user hanging — escalate and say so.
    fallback = tools.escalate_to_human(
        "other",
        "Assistant could not resolve this after multiple tool calls; needs human review.",
        customer_id=session.customer_id,
    )
    reply = (
        "I'm having trouble resolving this on my own — I've escalated it to our "
        f"support team (ticket {fallback['ticket_id']}). They'll follow up shortly."
    )
    session.messages.append({"role": "assistant", "content": reply})
    return {"reply": reply, "tool_trace": tool_trace}
