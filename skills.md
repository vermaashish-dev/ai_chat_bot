# skills.md: build journey and tools used

**Problem statement:** PS04 Persona Design. VANI builds a persona for each seller before the call, then adapts it live.

## Tools used

| Tool | What we used it for |
|---|---|
| **Sarvam AI Agents platform** | The live voice agent `sulabh_im` (`sulabh-im-80199a3e-82ea`). It provides the female voice, a Hindi opening, 11 languages with automatic language detection, and Hinglish code-mixing. It also runs three API tools (before, during and after the call), test calls, chat tests and the eval suite. |
| **Sarvam Voice Agents MCP** | We configured the agent from the coding session: the prompt, the variables, the tools and their versions (v1 to v10). We also used it for test calls, free chat tests and the 8-scenario regression suite. |
| **Claude Code** | Our pair-programming agent. It analysed the data, wrote the code and tests, audited the prompt, and pushed and checked the agent through MCP. |
| **Python 3.10, FastAPI, Pydantic v2** | The persona API and the pre-call, switch-log and post-call endpoints that Sarvam calls. |
| **SQLite + SQLAlchemy 2** | We loaded the 149,363 sellers, 336,606 calls and 139,545 transcript turns from PS07 into one indexed database (WAL mode). |
| **pytest, ruff** | 106 automated tests with synthetic fixtures and mocked LLMs, plus linting. |
| **cloudflared** | A public HTTPS tunnel, so the Sarvam tools can reach the local API. |
| **LLM (optional)** | OpenAI `gpt-4.1-mini` or Anthropic `claude-haiku-5-5`, with structured outputs. It is **disabled for this submission** because we had no key or credit. Every persona comes from the rule engine, which needs no LLM. |

## Build journey

### 1. Read the brief and the data before writing code
- We read the PS04 brief, the buyer-side VANI example prompt and the PS07 data dictionary.
- **Discovery 1:** the seller file has **no business-name column**. We recovered names from bot call summaries ("asking if the lead was from X") and opening greetings. That made 64,034 sellers findable by name. Names shared by more than one seller return HTTP 409, so the **GLID** is the reliable key.
- **Discovery 2:** the data has no package or price fields, so facts come only from IndiaMART's public paid-services page.

### 2. Data layer
- Ingestion is incremental, with hash-based deduplication, so a re-run takes under a second.
- It handles duplicates, malformed rows, cp1252 files, second transcript variants and undated calls.
- Supplier resolution:
  - by GLID, or by exact or normalised name;
  - an unknown name returns 404 with fuzzy suggestions;
  - a name shared by several sellers returns 409 with the candidates.

### 3. Evidence from past calls
We mined the 269k bot calls to see which persona choices go with meetings being fixed. Examples:
- **Clear question after a signal:** after a rush, confusion or interest signal, calls where VANI's next turn still asked a clear question fixed 5 to 17 points more meetings.
- **Very short answered calls:** sellers whose answered calls average under 20 seconds fixed 0.5% meetings, against an 11.5% baseline. They get a brisk pace and short turns.
- **Earlier callback request:** these sellers fixed 7.0% meetings, so the opening honours the promised callback.

Every persona choice carries its evidence, a sample size, a strength label and a confidence.

### 4. Persona engine, one persona per seller
- **Voice:** gender, accent, pitch and pace.
- **Language:** opening language, the language to switch to, the Hinglish mix and formality.
- **Tone:** warmth, empathy and assertiveness.
- **Conversation plan:** opening, personalisation, objection playbook, escalation rules and guardrails.

Each choice has a reason, the data fields it used and a confidence. Missing fields lower the confidence (FAQ Q6). Five contrasting samples are in `outputs/persona_specs/`.

### 5. Sarvam integration

| Tool | When it runs | What it does |
|---|---|---|
| `fetch_seller_persona` | `on_start` | Fills 18 per-seller variables plus a unique `call_ref` |
| `log_persona_switch` | Mid-call | Writes each switch to the switch log |
| `send_call_outcome` | `on_end` | Stores the outcome, the meeting or callback time and a summary |

### 6. Live adaptation
- VANI watches for five signals: frustration, confusion, rush, interest and a language switch, plus bot questions and opt-outs.
- On each signal it changes delivery, never facts, and logs the switch with the seller's own words.
- **Result:** `outputs/switch_log.md` holds 26 live switches covering all five signals.

### 7. Hardening from real test calls
Every bug below was found on a real call or chat test and then fixed:

| Problem on the call | Fix |
|---|---|
| VANI kept pitching after the seller got irritated | The frustration limit became a hard stop, and after frustration VANI asks only about a callback |
| It said "15 thousand" for 35,000 and misread the customer care number | All numbers are now rendered **in words by code**; the LLM never converts digits |
| It pitched a do-not-call seller | Safety moved into the per-seller variables, because Sarvam weights those above general prompt rules. We also added a pre-dial filter (148,185 sellers allowed, 1,178 blocked) |
| It called itself a "virtual assistant" in the greeting | It introduces itself only as "VANI from IndiaMART". If asked directly, it says truthfully that it is automated, and it never claims to be human |
| Closing lines were not spoken | Only the `end_interaction` message is spoken on the closing turn, so the full closing line now goes there |

### 8. Audit and quality pass
- **Safety:**
  - a dial-check endpoint;
  - a cautious path for unchecked sellers (permission first, no seller details);
  - a token on the tunnelled API, with proxy detection.
- **Robustness:**
  - a per-call `call_ref`;
  - switch-log de-duplication;
  - evidence computation that never blocks a request;
  - background ingestion.
- **LLM safety:**
  - structured outputs;
  - rejection of prompts that leak internal terms or invent offers;
  - prompt-injection tests;
  - fallback to rules on any provider error.
- **Single source of truth:** `sarvam/agent_prompt.md` is both the deployed prompt and the API's system prompt. A generated `agent_spec.json` with a `--check` mode stops the two from drifting apart.

## Lessons
1. **Variables beat prompt rules on Sarvam.** Safety behaviour has to travel inside the per-seller variables.
2. **Never let the LLM read digits.** Pre-render every number in words.
3. **Test on real calls early.** Most fixes came from listening to calls, not from reading the prompt.
4. **Use free chat tests for single fixes** and save the paid evals and calls for regression runs.

## What we would do next
- A stable hosted URL instead of a quick tunnel, and a rotated API token.
- Per-seller speaking pace through cloned agents. Sarvam sets pace per agent, not per call.
- Turn the LLM on to enrich the persona narrative, and run the regression suite again with credits.
