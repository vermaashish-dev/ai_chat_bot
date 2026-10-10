# VANI Seller Persona API (PS04 Persona Design)

> Submission overview: [SUBMISSION.md](SUBMISSION.md). Build journey: [skills.md](skills.md). One slide: [slide/one_slide.pdf](slide/one_slide.pdf).

A local FastAPI service. For each IndiaMART seller it builds a VANI voice persona from that seller's data:
- the voice, language and tone to use, with the reason and the past-call evidence for each choice
- a conversation plan
- rules for adapting the persona mid-call
- a standalone voice-agent system prompt

It also serves the per-seller variables that the Sarvam agent fetches before each call, and it stores the mid-call switch log.

## What the data contains (and doesn't)

The source is the PS07 dataset in `data/` (see `data/PS07_DATA_DICTIONARY.md`).

| File(s) | Ingested as | Rows |
|---|---|---|
| `ps07_seller_files_part1/2.csv` | `suppliers` (1 row per GLID, every column kept in `profile_json`) | 149,363 |
| `ps07_bot_calls_*.csv` | `supplier_conversations` (channel `bot_call`) | 269,455 |
| `ps07_recording_sample_5000.csv` | same calls; all 5,000 detected as duplicates | 0 new |
| `ps07_call_turns.csv` | `conversation_turns` (second transcripts kept as `transcript_variant` 1) | 139,545 |
| `ps07_executive_calls.csv` | `supplier_conversations` (channel `executive_call`, employee ID dropped) | 67,151 |

Things to know about the data:
- **The seller file has no business-name column.** Names are taken from bot call summaries ("asking if the lead was from X") and from the bot's opening greetings. They are stored in `supplier_aliases` with their source and an occurrence count. This makes 64,034 sellers findable by name. 4,173 names are shared by more than one GLID, and those lookups return HTTP 409. **GLID is the reliable key.**
- **The data has no package, pricing or renewal information.** The past calls aimed to fix a 15–20 minute meeting with a sales executive, so that is the call objective.
- **Packages and prices** come from IndiaMART's public [paid services page](https://help.indiamart.com/knowledge-base/paid-services-of-indiamart/), read on 2026-10-10. The catalogue is in `app/services/packages.py`:
  - MDC, TrustSEAL Pro and Maximiser Pro have listed 1, 2 and 3-year prices.
  - IM Star Pro, IM Leader Pro and IM Industry Leader are quote-only.
  - The page doesn't say whether GST is included, so the agent says the executive confirms the final price. It never offers discounts.
- **Package suggestion by turnover** is our own rule (the page sets no eligibility), so it is labelled as a suggestion with its reason:

  | Turnover | Suggested package |
  |---|---|
  | 0–40 L | MDC |
  | 40 L–1.5 Cr | TrustSEAL Pro |
  | 1.5–5 Cr | Maximiser Pro |
  | 5–25 Cr | IM Star Pro |
  | 25–100 Cr | IM Leader Pro |
  | 100 Cr and above | IM Industry Leader |
  | Unknown | MDC, low confidence |

  The suggestion is part of the persona spec, and the Sarvam pre-call tool returns it as `recommended_package` and `package_pitch`.
- **Evaluator output files** (`ps07_evaluator_outputs_*.csv`) are listed in the data dictionary but are **not in the folder**. If you add them, they are reported as `unrecognized` until a loader is written.
- **No Markdown transcripts were provided.** The Markdown loader works on any `*.md` file in `CONVERSATION_DATA_DIR`. It reads front matter (`glid`, `business_name`, `date`). A conversation that can't be matched to exactly one seller is stored with `link_status = needs_review`.
- **Executive call logs cover August and September 2026 only.**

## Setup (Windows PowerShell)

```powershell
cd "C:\ashish\AI projects\IM_Haka_2026_v1"
py -3.10 -m venv .venv            # Python 3.10+ works; 3.11+ recommended
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env       # then edit .env (the LLM key is optional)
```

The LLM is optional and **currently disabled** (`LLM_ENABLED=false`, no key). The API then never calls an LLM and returns the rule-based persona. The provider is OpenAI by default (`LLM_PROVIDER=openai`, model `gpt-4.1-mini`); Anthropic (`LLM_PROVIDER=anthropic`, `claude-haiku-5-5`) is also supported. To enable it, set `LLM_ENABLED=true` and the selected provider's key (`OPENAI_API_KEY`, or `ANTHROPIC_API_KEY` plus `ANTHROPIC_WORKSPACE_ID` for a key not scoped to a workspace), then restart the server. `/health` reports `llm_configured`.

Copy the PS07 CSV files into `data\` (the `SUPPLIER_DATA_DIR`). Put optional Markdown transcripts in `data\conversations\`. Files attached in a chat are **not** visible to the app; they must be in these folders.

## Ingest

```powershell
python -m scripts.ingest          # about 75 s the first time; unchanged files are skipped by hash (under 1 s)
python -m scripts.ingest --force  # re-read every file (rows still deduplicate by hash)
```

The script creates `persona.db`, then prints per-file counts (imported, updated, unchanged or duplicate, failed) and any missing inputs. Finally it computes the past-call evidence and caches it in `evidence_cache.json`.

## Run

```powershell
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Swagger UI is at http://127.0.0.1:8000/docs.

```powershell
$body = @{ business_name = "Wide Digital Systems" } | ConvertTo-Json   # unique name; "HB Industries" returns 409 (2 sellers)
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/api/v1/supplier/persona -ContentType "application/json" -Body $body

# By GLID (recommended), rule-based only (no LLM call):
$body = @{ glid = 1494419; history_limit = 5; use_llm = $false } | ConvertTo-Json
(Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/api/v1/supplier/persona -ContentType "application/json" -Body $body).voice_agent_system_prompt
```

| Endpoint | Purpose |
|---|---|
| `POST /api/v1/supplier/persona` | `{business_name | glid, history_limit?, use_llm?}`. Returns supplier, conversation history, persona (narrative + spec), call strategy, voice-agent system prompt, metadata. Errors: 404 (with suggestions), 409 (with candidates), 422 |
| `POST /api/v1/sarvam/pre-call` | Sarvam on_start tool: `{glid}` → `{found, call_ref, variables:{...}}`. Always returns 200. Unknown sellers get the cautious variables; do-not-call sellers get stop variables |
| `POST /api/v1/campaign/dial-check` | Pre-dial gate: `{glids:[...]}` → `allowed` / `blocked` (do-not-call) / `unknown` (not on record, so unchecked). Dial only `allowed` |
| `POST /api/v1/sarvam/switch-log` | Records one mid-call switch: `{glid, call_ref, signal, change_made, reason, seller_quote?}`. Repeats of the same switch in a call are dropped |
| `POST /api/v1/sarvam/post-call` | Sarvam on_end tool: stores the outcome variables for the `call_ref` |
| `GET /api/v1/call-outcomes?glid=` | Lists stored outcomes |
| `GET /api/v1/switch-log?glid=&call_ref=` | Lists the switch log |
| `GET /api/v1/calls?glid=` | Lists calls. Each pre-call lookup issues a unique `call_ref` |
| `GET /api/v1/calls/{call_ref}` | One call end to end: the persona variables VANI received, its live switch log and its post-call outcome |
| `GET /api/v1/evidence` | Past-call evidence behind the persona rules |
| `POST /api/v1/signals/detect` | Hinglish cue detector for frustration, confusion, rush, interest, language switch, bot suspicion and opt-out |
| `POST /api/v1/admin/ingest?force=&wait=` | Runs incremental ingestion in the background (202); `wait=true` blocks. Local only, or with `ADMIN_TOKEN` |
| `GET /api/v1/admin/ingestion-status` | File-level results |
| `GET /health` | Database health and counts; no secrets |

**LLM vs rules.** Every response contains a rule-based persona spec, built from the seller's data and the evidence, with no LLM call. When the LLM is enabled and its key is set, the low-cost model of the selected provider (`gpt-4.1-mini` for OpenAI, `claude-haiku-5-5` for Anthropic; override with `LLM_MODEL`) adds the narrative, the history insights and the strategy, and writes the system prompt. The call uses structured outputs (OpenAI `chat.completions.parse` or Anthropic `messages.parse`, with the `LLMPersonaOutput` Pydantic model), so the reply is constrained to the schema and validated. Refusals and truncated replies also fall back to rules. A prompt that leaks internal terms (GLID, database, …) or makes up offers ("90% discount", "guaranteed leads") is rejected, and the rule-based prompt is used instead. Timeouts, rate limits and provider errors also fall back to rules, with a warning in `metadata.warnings`. Raw provider errors and keys are never returned. For sellers marked do-not-call, the LLM is skipped and the strategy is "Do not call".

## How the persona is built

`app/services/persona_rules.py` maps the seller's fields to the persona settings:

| Setting | Driven by |
|---|---|
| Opening language and switch-ready language | `seller_state` |
| Hinglish mix and formality | `annual_turnover`, `business_type` |
| Pace | `avg_answered_call_sec`, `past_objections`, `past_questions_asked`, `showed_frustration` |
| Tone (warmth, empathy, assertiveness) | Not-interested count, callback history, pricing questions |
| Opening | The callback the seller requested on a dated call, an earlier meeting, or `already_in_touch_with_executive` |
| Objection playbook | The seller's own `past_objections` |
| Call eligibility | `do_not_call_requested` |

Every setting carries a reason, the fields it used, the past-call evidence (with sample size and a strength label) and a confidence. When fields are missing, the confidence is lowered and the missing fields are listed.

Main evidence findings (from `GET /api/v1/evidence`):
- After a rush, confusion, frustration or interest signal, calls where VANI's next turn still asked a clear question fixed 5 to 17 points more meetings. This is why every adapted turn ends with a question.
- Sellers who requested a callback before (n = 14,655) have a 7.0% meeting share, against an 11.5% baseline.
- Sellers already in touch with an executive have an 8.1% meeting share.
- Calls placed between 11:00 and 12:00 do worst (6 to 8%, against about 13.5% at 09:00 and 15:00–19:00).
- The personalised-script trial (n = 452) fixed 14.2% meetings, against 11.6% for `main_vani`.

All of these are observational associations, and `caveats` in the evidence output lists the confounds.

To export persona specs (JSON and Markdown) for review or submission:

```powershell
python -m scripts.export_personas --demo            # one seller per contrasting segment
python -m scripts.export_personas --glid 1494419    # a specific seller
python -m scripts.export_switch_log                 # live switch log -> outputs/switch_log.md and .json
```

## Sarvam agent `sulabh_im` (`sulabh-im-80199a3e-82ea`)

Configured and connected:
- the female voice "Sarika – Conversational"
- the greeting `{{opening_greeting}}`, built by the pre-call API: *Hello, kya aap <business name> se bol rahe hain?* (in Hindi) when the name is on record, otherwise *Hello, this is VANI calling from IndiaMART about your business listing. Am I speaking with the business owner?* VANI introduces itself after the seller confirms
- a Hindi opening, with 11 languages and automatic language detection
- the prompt from `sarvam/agent_prompt.md`
- the voicemail message and the "Meeting fixed" goal
- three API tools that reach this API through a public HTTPS tunnel

The latest committed version is 12. The exact definitions are in `sarvam/agent_spec.json`.

| Tool | When | Endpoint | What it does |
|---|---|---|---|
| `fetch_seller_persona` | Before the first turn (on_start) | `POST /api/v1/sarvam/pre-call` | Sends `{{glid}}` and fills the 18 per-seller variables plus `call_ref` |
| `log_persona_switch` | Mid-call (run) | `POST /api/v1/sarvam/switch-log` | Logs each delivery switch with `call_ref`, signal, change, reason and the seller's words |
| `send_call_outcome` | After the call (on_end) | `POST /api/v1/sarvam/post-call` | Sends the outcome variables below, with `call_ref` |

**Input variables:**
- `glid` comes from the campaign or test call.
- The pre-call tool fills: `seller_found`, `call_allowed` (`yes`, `no` or `unknown`), `business_name`, `opening_greeting`, `seller_city`, `seller_category`, `opening_language`, `switch_language`, `language_style`, `delivery_style`, `call_objective`, `opening_plan`, `seller_facts`, `previous_commitment`, `objection_guidance`, `frustration_threshold`, `recommended_package`, `package_pitch` and `call_ref`.
- Every variable defaults to the cautious unchecked-seller values, which apply if the API can't be reached.

**Output variables** (extracted after the call):

| Variable | Contents |
|---|---|
| `call_outcome` | `meeting_fixed`, `callback_requested`, `not_interested`, `already_in_touch`, `complaint`, `wrong_person`, `do_not_call`, `language_barrier` or `other` |
| `meeting_time`, `callback_time` | The agreed times |
| `objections_raised` | Objections raised on the call |
| `persona_switches` | The switch log as summarised after the call |
| `call_summary` | A 1–2 line summary |

**Connecting it.** Sarvam can't reach `127.0.0.1`, so the API needs a public HTTPS address:
1. Set `API_TOKEN` in `.env` to a random, API-only value and start uvicorn.
2. Run `cloudflared tunnel --url http://127.0.0.1:8000`.
3. A quick tunnel's address changes every time cloudflared restarts. When it does, update the URL in all three tools and save a new version. A named tunnel or a hosted deployment avoids this.

Platform limits:
- **The token is stored as plain text in the tools.** Sarvam only attaches stored secrets to URLs on its own allow-list, so the bearer token sits as a static header in the tool config, visible to workspace members. Use a dedicated token and rotate it.
- **The live switch log can miss switches.** This agent was created with mid-call variable updates turned off, and that can't be changed. The live log therefore comes from `log_persona_switch`, with the post-call `persona_switches` filling any gaps. A language switch handled by Sarvam's own language tool can use up the turn, so it may only appear post-call.
- **Pace is per agent, not per seller.** Per-seller pace is carried by turn length and wording. For an audibly different pace in a demo, clone the agent and change `speech_settings.pace`.
- **Per-seller variables outweigh general prompt rules.** That's why safety behaviour for do-not-call and unchecked sellers is written into those sellers' variables themselves, not left to a rule in the prompt.

### Prompt and agent spec (single source of truth)

- **`sarvam/agent_prompt.md` is the prompt deployed to the Sarvam agent.** The API's `voice_agent_system_prompt` is the same file with the seller's variables filled in, so the two can't drift apart.
- **Numbers the agent speaks are generated in code, in words.** That covers prices ("thirty five thousand rupees for one year") and the customer care number ("nine six nine six…"). On live calls the LLM misread digits, so it never converts numbers itself.
- **`python -m scripts.sarvam_spec`** generates `sarvam/agent_spec.json` from the code: variables with their fallback defaults, the three tools, voicemail, intro and goal. Use `--check` to fail if the spec is stale or the prompt uses an undeclared variable. The token and URL stay as `${API_TOKEN}` and `${PUBLIC_BASE_URL}` placeholders.
- **Changing the agent:** edit `agent_prompt.md`, run the spec script and the tests, push the prompt to Sarvam, then run the regression suite.
- **Regression suite** "VANI seller persona regression" (8 scenarios on Sarvam): package price accuracy, the complaint hard stop with the customer care number, the frustration limit, consent for an unchecked seller, the do-not-call seller, a rushed seller, the AI-question answer and the meeting happy path.

### Do-not-call safety

There are two layers:

1. **Before dialling (the main control).** Filter every campaign contact list first:

   ```powershell
   python -m scripts.build_dial_list contacts.csv   # needs a 'glid' column
   ```

   This writes `contacts_dialable.csv`, which is what you upload to Sarvam, and `contacts_excluded.csv`, with a reason for each removed row: do-not-call, GLID not on record, invalid or duplicate. Against the current data: 148,185 allowed and 1,178 blocked.

2. **During the call (the fallback).** When the seller can't be looked up, `call_allowed` is `"unknown"`, never `"yes"`. That happens when the GLID is unknown or the API can't be reached; in the second case the Sarvam variable defaults apply. VANI then:
   - shares no seller details and suggests no package
   - asks permission first and continues only on a clear yes
   - closes at the first sign of frustration

## Tests

```powershell
python -m pytest -q
```

106 tests use synthetic fixtures and mocked LLM clients; no API key is needed and nothing is charged. They cover:
- ingestion and idempotent re-runs
- duplicates, malformed rows, cp1252 files and transcript variants
- unlinked calls and Markdown linking
- supplier matching, 404 and 409
- history ordering and tie-breaks, undated calls and truncation
- index presence
- the do-not-call restriction: the dial gate, including lists beyond SQLite's parameter limit, and the stop variables
- the cautious unknown-seller fallback
- per-call `call_ref`, and switch-log de-duplication within a call
- requests never waiting for the evidence computation
- background ingestion
- numbers in words: Indian lakh and crore, prices, and the customer care number
- the prompt having no digit prices, declared variables only, and an up-to-date spec with no secrets
- prompt injection in transcripts
- LLM timeout, rate limit, auth and provider errors, refusal, truncation and schema failure (mocked OpenAI and Anthropic clients)
- provider selection, default models, and the LLM on/off switch
- prompt-leak rejection
- token and proxy access control

Lint: `ruff check app scripts tests --select F,E9,B,UP --ignore B008` (B008 is the standard FastAPI `Depends` pattern).

The real-LLM test is optional:

```powershell
$env:RUN_LLM_INTEGRATION="1"; $env:REAL_OPENAI_API_KEY="sk-..."; python -m pytest tests/test_llm_integration.py   # or REAL_ANTHROPIC_API_KEY
```

## Troubleshooting

| Problem | Fix |
|---|---|
| `/health` reports `empty` | Run `python -m scripts.ingest` |
| A "No seller file found" message | Check `SUPPLIER_DATA_DIR` |
| A persona has the warning "evidence is still being computed" | After a data change the evidence is recomputed in the background (about 30–40 s). Requests never wait for it: they use the previous evidence, marked stale, or continue without it. `scripts.ingest` pre-computes it |
| `POST /admin/ingest` returns 202 | Ingestion now runs in the background. Poll `GET /admin/ingestion-status`, or pass `?wait=true` to block |
| 403 from the API | The request came through a proxy or tunnel without `API_TOKEN`, or to an admin route without `ADMIN_TOKEN` |
| 409 on a name | The name belongs to several sellers. Use `glid` from `error.details.candidates` |
| The database is about 600 MB | Expected. It holds the full call summaries and transcripts. Delete `persona.db*` and re-ingest to rebuild |

## Layout

```
app/main.py                      FastAPI app, error handlers, background evidence load
app/config.py, db.py, models.py  settings, engine/session, ORM tables and indexes
app/schemas.py                   request/response and LLM-output models
app/api/routes.py                endpoints and access control
app/services/ingestion_service.py      CSV/Markdown ingestion, aliases
app/services/supplier_repository.py    GLID / exact / normalised lookup, suggestions
app/services/conversation_repository.py history retrieval
app/services/evidence_service.py       past-call evidence
app/services/persona_rules.py          rule-based persona spec
app/services/persona_service.py        orchestration, LLM call, validation, fallback
app/services/sarvam_service.py         Sarvam variables, call_ref, dial check, prompt rendering
app/services/packages.py               package catalogue, turnover suggestion, numbers in words
app/services/signals.py                live-adaptation cue lexicon
app/services/llm_client.py             OpenAI / Anthropic SDK wrapper (structured output, on/off switch)
app/prompts/persona_prompt.txt         LLM analysis prompt
sarvam/agent_prompt.md                 live VANI prompt (single source)
sarvam/agent_spec.json                 generated Sarvam variables and tools (no secrets)
scripts/ingest.py                      ingestion CLI
scripts/build_dial_list.py             do-not-call filter for campaign lists
scripts/sarvam_spec.py                 generate or check the Sarvam spec
scripts/export_personas.py             persona spec export
scripts/export_switch_log.py           live switch log export
scripts/package_submission.py          builds the clean submission zip
outputs/persona_specs/                 sample persona specs (5 contrasting sellers)
outputs/switch_log.md, .json           live mid-call switch log
SUBMISSION.md, skills.md, slide/       approach note, build journey, one-slide presentation
tests/                                 pytest suite
```
