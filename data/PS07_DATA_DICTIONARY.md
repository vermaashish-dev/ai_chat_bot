# 07 · Seller-Fit Adaptive Voice Persona: Dataset

**Window:** 1 Apr to 30 Sep 2026 · **Sources:** IndiaMART warehouse (`im_dwh_rpt`), voice-eval DB, pg-main, via Redash
**Join keys:** `fk_glusr_usr_id` (seller GLID) links everything to a seller; `attempt_id` links a bot call across files.
Not masked: summaries, transcripts and evaluator quotes contain real seller and company names. Internal use only.

## Files

**How the files are split:** bot calls are `ps07_bot_calls_2026-04_to_06.csv`, then one file each for Jul, Aug, Sep. Evaluator outputs are `ps07_evaluator_outputs_2026-04_to_06.csv`, then Jul (2 parts), Aug (3 parts), Sep. The seller file is in 2 parts (`ps07_seller_files_part1/2.csv`). Parts of the same file have identical columns; stack them to get the whole file.

| File | One row per | Rows |
|---|---|---|
| `ps07_seller_files.csv` | seller (the **input** for persona generation) | 149,363 |
| `ps07_bot_calls_YYYY-MM.csv` | answered bot call | 269,455 |
| `ps07_evaluator_outputs_YYYY-MM.csv` | evaluator result for a bot call (JSON) | 278,332 |
| `ps07_call_turns.csv` | turn of a bot call (who spoke, when, what) | 139,555 turns · 12,788 calls |
| `ps07_recording_sample_5000.csv` | bot call chosen for audio analysis | 5,000 |
| `ps07_executive_calls.csv` | executive call to one of these sellers | 67,151 · 6,933 sellers |

---

## 1. `ps07_seller_files.csv` (seller persona input)

**Profile:** `seller_city`, `seller_district`, `seller_state`, `seller_pincode`, `seller_locality`, `business_type`, `annual_turnover`, `nature_of_business`, `nature_of_business_secondary`, `gst_registration_year`, `top_category_1/2/3`, `top_parent_category`, `top_category_group`, `num_categories`

**Engagement history** (prefix `eng_`, from voice-eval `seller_engagement`, about 64% of sellers): activity in the last 30 days; calls attempted, answered, long and short (90 days); pickup ratio and callbacks (90 days and 1 year); meetings and hot meetings (1 year); enquiries received and replied, PNS calls received and answered (90 days and 1 year); not-interested and not-picked counts (1 year); product count and products edited; `eng_cqs` (catalog quality score).

**Bot call history (Apr–Sep):**
| Column | Meaning |
|---|---|
| `bot_attempts`, `bot_answered`, `bot_answer_rate` | Bot dial attempts, answered attempts, answer rate |
| `calls_meeting_fixed`, `calls_not_interested`, `calls_general`, `calls_call_later` | Answered calls by outcome |
| `avg_answered_call_sec` | Average answered call length |
| `past_objections` | Top 3 objection types raised, with counts (e.g. "Seller Unavailability & Scheduling Constraints (2)") |
| `past_questions_asked` | Top 3 question types the seller asked (Meeting Visit, Identity Confirmation, Pricing Payment…) |
| `asked_if_talking_to_bot` | 1 if the seller asked whether they were talking to a bot |
| `showed_frustration` | 1 if the seller showed frustration on a past call |
| `past_dispositions_detailed` | Top 3 detailed outcomes from the disposition evaluator (call_dropped, callback_requested, not_interested, meeting_fixed, wrong_number, already_in_touch_with_im…) |
| `already_in_touch_with_executive` | 1 if the seller said they're already in touch with an IndiaMART executive |
| `do_not_call_requested` | 1 if the seller asked not to be called |

## 2. `ps07_bot_calls_YYYY-MM.csv` (answered bot calls)

`attempt_id`, `fk_glusr_usr_id`, `redis_bucket` (hot-lead type), `lead_bot_version` (bot style / campaign: `main_vani`, `main`, `last_met_d…`, `personalis…` = personalised-script trial, `base_vani`, `summary_va…`, `arrowhead`), `call_attempt_count`, `call_start_time` (IST), `lead_call_duration`, `disposition_label` (Meeting Fixed / Not Interested / General / Call Later / Busy), `meeting_fixed`, `lead_call_summary` (96% filled), `call_recording_url`, `has_turn_transcript`, `has_evaluator_output`

**Recordings:** public MP3 links; 60 of 60 tested opened without login. Arrowhead calls have no recording links.

## 3. `ps07_evaluator_outputs_YYYY-MM.csv`

`attempt_id`, `fk_glusr_usr_id`, `eval_agent`, `agent_output` (JSON). Evaluators:

| `eval_agent` | Calls | What the JSON holds |
|---|---|---|
| `disposition` | 150,821 | Detailed outcome, reason, next action, follow-up timing, confidence |
| `seller_questions` | 24,694 | Each question: category, the seller's words, the bot's answer |
| `incorrect_mf` | 19,617 | Whether a "meeting fixed" was wrong; seller's initial reaction and final stance |
| `execdrop` | 18,768 | Who ended the call and why |
| `se_contact` | 17,716 | Whether the seller is already in touch with / met an executive, with quote |
| `location_confirmation` | 15,640 | Location discussed / confirmed |
| `latency_repetition` | 13,681 | **Turn-by-turn transcript with timestamps**, plus bot lag / repetition |
| `topics` | 7,179 | Topic-by-topic flow of the call with quoted evidence |
| `objections` | 5,233 | Each objection: category, reasons, quotes |
| `ivr` | 4,983 | Whether a human answered or an IVR / voicemail |

## 4. `ps07_call_turns.csv` (for live adaptation)

`attempt_id`, `fk_glusr_usr_id`, `turn_no`, `speaker` (bot / seller), `start_sec`, `end_sec`, `text`. Extracted from the `latency_repetition` evaluator. Mostly Hindi / Hinglish. Use it to find where sellers get frustrated, confused, interested or rushed, and how the bot responded.

## 5. `ps07_recording_sample_5000.csv`

Same columns as the bot calls files. Balanced across the 4 answered outcomes and the bot versions (it includes all 452 recorded calls from the personalised-script trial). 2,494 of the 5,000 also have turn transcripts, for checking speech-to-text against them. No Arrowhead calls.

## 6. `ps07_executive_calls.csv`

Outgoing calls by IndiaMART executives to the sellers in the seller file, from pg-main `click_to_call` (Airtel telephony). Modules: DSR (58k), MERP (7k), plus work orders, tele-dashboard, customer tickets and service. Recruitment, internal and bot calls are excluded.

Columns: `click_to_call_id`, `fk_glusr_usr_id`, `fk_employeeid`, `module`, `status` (Answered / NotAnswered), `call_start_time`, `call_duration`, `call_duration_customer`, `ring_duration`. Recording links are not included (they need Airtel authentication and won't play outside IndiaMART's systems).

**Limits:**
- pg-main keeps only recent call logs, so this covers **August and September 2026** only. 7 Sep and 18 Sep are missing; those days timed out repeatedly and were dropped.

## Removed as low-value
Empty or near-empty engagement fields (notification clicks, similar enquiries / PNS / buy-leads, BLNI, wrong-number, verified meetings), 1-year versions identical to the 90-day ones (answered, long and short calls), 90-day meeting / not-interested / not-picked counts (almost all zero), seller coordinates (city, district and pincode already cover location), first / last bot call dates, `lead_tbro_time`, the recording-sample flag (the sample is its own file), and placeholder summaries ("INFO_NA").
