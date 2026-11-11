# Spanish Voice Tutor: current implementation

State as of 2026-11-09, after Phase 5. Covers how the system works today: components, data flow, the
contract between client and agent, the data model, and the known limits. The roadmap and its reasons
are in `plan.md`. Commands and conventions are in `CLAUDE.md`.

## 1. System overview

```text
Phone / desktop browser                       LiveKit Cloud (EU)
┌──────────────────────────────┐   WebRTC    ┌──────────────────────────────────────────────┐
│ Next.js app (Vercel)         │◄──audio────►│ Room                                         │
│  login (Supabase Auth)       │◄──text─────►│  ▲                                           │
│  transcript, feedback/drill  │◄──RPC──────►│  │ Tutor agent (Python, LiveKit Agents)      │
│  panels, mode picker, cost   │             │  │ eu-central, image from agent/Dockerfile   │
└──────────┬───────────────────┘             └──┼───────────────────────────────────────────┘
           │ POST /api/token (Supabase JWT)     │
           ▼                                    │  Deepgram (EU) · Claude · Cartesia · Azure
  token route verifies JWT ──► LiveKit token    │
  identity = Supabase user id                   ▼
                                     Supabase Postgres (Frankfurt, session pooler)
                                     learners · sessions · vocab · errors · drill_attempts · word_scores
```

- **Client:** `web/` (git submodule, fork `bliep/agent-starter-react`, branch `tutor`), hosted on
  Vercel at https://espanol-tutor.vercel.app.
- **Agent:** `agent/`, one Python process per worker. It handles audio, LLM calls, scoring and
  persistence. It is hosted on LiveKit Cloud (`eu-central`), agent id in `agent/livekit.toml`,
  dispatch name `tutor`.
- **Data and auth:** Supabase. Postgres holds the learner model, and Supabase Auth handles accounts
  and magic links.

The browser never talks to the database or to the model providers. Everything goes through the room:
audio both ways, text streams from agent to client, and RPCs from client to agent.

## 2. Repository layout

### Agent (`agent/`)

| Module | Responsibility |
|---|---|
| `main.py` | Entrypoint: per-session wiring (learner, pipeline, feedback, modes, RPCs, shutdown). |
| `pipeline.py` | Builds the voice pipeline: VAD, STT, LLM, TTS, turn handling; slow-replay TTS. |
| `prompts.py` | System prompt per CEFR level, learner memory block, role-play scenarios, greetings. |
| `tutor_agents.py` | LLM agents: `Tutor` (free talk) and `RolePlay`; optional turn recording. |
| `modes.py` | `LessonMode` enum and the `set_lesson_mode` payload (`ModeRequest`). |
| `mode_controller.py` | Switches agents mid-call (LiveKit handoff); routes typed drill answers. |
| `drills.py` | Code-driven drill agents: translate EN→ES, ES→EN, repeat after me. |
| `drill_content.py` | Haiku: drill item generator and translation judge. |
| `pronunciation.py` | The only Azure Speech SDK module: pronunciation assessment, word alignment. |
| `speech_capture.py` | Captures the learner's spoken answer (16 kHz PCM) from the STT input. |
| `feedback.py` | Haiku feedback extractor (`record_feedback` payload) plus clean-up rules. |
| `feedback_channel.py` | Runs the extractor beside the conversation; publishes and records results. |
| `summary.py` | Sonnet end-of-session summary (3–5 patterns, new words). |
| `session_recap.py` | Builds the summary on request or at shutdown; closes the session row. |
| `learner_model.py` | Domain logic: lesson opener, FSRS reviews, word matching, drill recording. |
| `learner_store.py` | The only module with SQL (asyncpg over the Supabase session pooler). |
| `input_modes.py` | Open mic vs push-to-talk. |
| `sentence_translator.py` | Haiku translation of a tutor line (cached). |
| `costs.py` | Cost meter and price table; CLI report over stored sessions. |
| `latency.py` | Per-turn latency logging (stage sum and LiveKit's `e2e_latency`). |
| `turn_recorder.py` | Opt-in recording of spoken turns for the eval set (`RECORD_TURNS=1`). |
| `migrate.py` | Applies `supabase/migrations/*.sql` once each. |
| `tests/`, `evals/` | pytest (learner model) and evals (see §13). |

Layering inside the agent:

```text
main.py ──► mode_controller / tutor_agents / drills ──► learner_model ──► learner_store ──► Postgres
                                  │                          (FSRS, matching)     (SQL only)
                                  └──► drill_content / feedback / summary ──► Claude
                                  └──► pronunciation ──► Azure Speech SDK
```

### Web (`web/`, our additions to the LiveKit starter)

| File | Responsibility |
|---|---|
| `app/api/token/route.ts` | Issues LiveKit tokens to logged-in learners only. |
| `lib/auth.ts`, `lib/server-auth.ts` | Supabase Auth in the browser (magic link) and on the server (JWT check). |
| `lib/tutor-rpc.ts` | Typed client for every agent RPC. |
| `lib/tutor-feedback.ts`, `lib/tutor-drill.ts`, `lib/tutor-level.ts` | Wire types mirroring the agent, plus stream subscriptions. |
| `components/app/tutor-layout.tsx` | Call screen: top bar, transcript, panels. |
| `components/app/lesson-workspace.tsx` | Feedback / drill panel (right column on wide screens, drawer on phones). |
| `components/app/feedback-panel.tsx`, `drill-panel.tsx` | Corrections, summary, "Today"; drill progress and per-word scores. |
| `components/app/mode-picker.tsx`, `push-to-talk.tsx`, `level-picker.tsx` | Controls. |
| `components/app/line-actions.tsx` | Slow / EN buttons under each tutor line. |
| `components/app/session-cost.tsx` | Live cost line. |
| `components/app/login-view.tsx` | Magic-link login. |
| `app/manifest.ts`, `public/icons/` | Installable web app. |
| `hooks/use-*.ts` | One hook per concern (auth, feedback, drill, mode, cost, RPC client…). |

The transcript component from the starter (`components/agents-ui/agent-chat-transcript.tsx`) has one
change: it renders optional per-message actions from `AgentMessageActionsContext`.

## 3. Session lifecycle

```text
browser: login ──► POST /api/token {room_config: agent "tutor", metadata {"level": "B1"}}
token route: verify Supabase JWT ──► LiveKit token (identity = user id) ──► join room
LiveKit dispatches agent "tutor" ──► main.entrypoint(ctx):

  level  = dispatch metadata (default A2)
  connect, wait for participant
  learner = LearnerModel if identity is a UUID and the DB is reachable, else None (anonymous)
  lesson  = learner.lesson()        due words + recent errors
  session = pipeline.build_session(level, keyterms = due words)
  attach: LatencyLogger, CostMeter, FeedbackChannel, SessionRecap, TurnRecorder (opt-in)
  register RPCs                      (before start: the greeting means "ready")
  session.start(agent = Tutor(level, lesson, greeting))
  ... conversation, mode switches, drills ...
  shutdown: recap.finish() (summary if new turns, ended_at, cost) ──► store.close()
```

- **Anonymous sessions:** console mode, the evals' non-UUID identities, or a missing DB still work.
  They just have no memory and save nothing.
- **Shutdown:** LiveKit runs shutdown callbacks concurrently, so recap and store close share one
  callback to keep the order.

## 4. Voice pipeline (`pipeline.py`)

| Stage | Choice | Settings |
|---|---|---|
| VAD | Silero | Loaded once per worker in `prewarm`. |
| Turn detection | LiveKit turn detector | `max_delay` 5 s (learners pause to search for words). |
| STT | Deepgram Nova-3 | `language=multi` (EN/ES), `smart_format=False`, EU endpoint, `keyterm` = due words. |
| LLM | Claude Sonnet 5.5 | Prompt caching; thinking off, effort low (first token ~0.6–0.9 s vs ~2.4 s by default). |
| TTS | Cartesia Sonic 3.5 | Castilian voice "Blanca", speed by level (A1 0.85 … B2/C1 1.0). |

LiveKit's preemptive LLM generation is on by default. Input mode is open mic or push-to-talk
(`input_modes.py`): push-to-talk switches turn detection to manual and commits the turn on release.

## 5. Tutor prompt and language policy (`prompts.py`)

The system prompt is in Spanish and has four parts:
- **Persona:** "Lucía", tutor of Spain Spanish.
- **Level guide:** grammar and vocabulary bounds per CEFR level.
- **Language policy:**
  - Recast English and ask the learner to repeat.
  - Fill gaps in mixed turns.
  - Answer "how do you say" in one example sentence.
  - English only as a last resort.
- **Correction and format rules:**
  - Implicit recasts only, at most one per turn.
  - At most 2 sentences, usually ending with a question.
  - No markdown or emoji.

Two additions are inserted *before* the policy rules, because measured adherence was higher with the
hard rules last:
- **Learner memory:** due words to weave into questions (at most one per turn, never an extra
  sentence) and recent error types.
- **Role-play:** a scene role. The policy overrides the role, and the 2-sentence limit still applies.

## 6. Feedback channel and summary

```text
conversation_item_added (user text) ──► background task ──► FeedbackExtractor (Haiku, structured output)
        │                                                     corrections[] {original, corrected, rule, category}
        │ (voice reply never waits)                           new_vocab[] · english_gloss · user_language
        ▼
publish JSON on text stream "tutor.feedback" ──► web feedback panel
record_turn() ──► learner model (vocab gaps, error log, FSRS)
history ──► SessionSummarizer (Sonnet) on request / at shutdown
```

- **Why not a tool call:** `record_feedback` is structured output, not a tool call on the tutor LLM.
  A tool call would sit inside the spoken turn.
- **Clean-up after Haiku** (`feedback._clean`), each rule measured in `evals/feedback.py`:
  - drops no-op corrections;
  - drops English gaps restated as corrections;
  - drops vocab entries that are whole sentences or basic words.
- **Error categories:** `ser_estar`, `agreement`, `preterite_imperfect`, `conjugation`,
  `preposition`, `word_choice`, `other`.
- **Paused in drills:** the feedback channel stops during drills, which score answers themselves.

## 7. Learner model (`learner_model.py`, `learner_store.py`)

- **Session opener (`lesson()`):** the 15 most overdue FSRS words and the top 3 error categories of
  the last 14 days. They go to:
  - the prompt (§5);
  - the greeting;
  - Deepgram keyterms;
  - the `lesson_context` RPC (the panel's "Today" section).
- **Per conversation turn (`record_turn()`):**
  - Each vocab gap is reviewed as FSRS **Again**, with a new card if needed. It also joins the
    session's practice set.
  - Corrections go to the error log.
  - A due word used in a Spanish turn, outside any corrected fragment, is reviewed as **Good**.
- **Word matching:** plain text, case- and accent-insensitive, with a leading article ignored ("El
  Paraguas" → `paraguas`). There is no lemmatizer, so `viajar` doesn't match `viajo`.
- **Drills (`record_drill()`):** one attempt row plus per-word scores. An EN→ES answer reviews its
  target word: Good if used correctly, Again otherwise.
- **Weak words (`weakest_words()`):** the lowest average drill score per word over 30 days.
- **Clock:** injectable, so tests can simulate "next day".

## 8. Lesson modes and drills

```text
web mode picker ──RPC set_lesson_mode {"mode", "scenario"}──► ModeController.set_mode
   conversation ──► Tutor          feedback on
   roleplay     ──► RolePlay       feedback on   (restaurant, doctor, job_interview, hotel, directions)
   translate_en ──► TranslateToSpanish ┐
   translate_es ──► TranslateToEnglish ├─ feedback paused
   repeat       ──► RepeatAfterMe      ┘
session.update_agent(new agent) ──► new agent's on_enter speaks first
```

How drills work (`drills.py`):
- **The LLM never replies.** Drills are code-driven.
- **On entry:** an intro line, then Haiku generates a round of 8 items (`spanish`, `english`,
  `target`, `target_english`) from the due words and recent error types.
- **Each answer**, in `on_user_turn_completed` for voice or `answer()` for typed input:
  1. Score the answer.
  2. Publish the result on `tutor.drill`.
  3. Store it.
  4. Speak the verdict and the next item.
  5. Raise `StopResponse`.
- **Spoken verdicts** are built in code from the scores ("¡Muy bien!", "Casi. Mejor: …",
  "Se dice: …"), so they stay short and predictable.
- **Rounds:** after the 8th item comes a recap with the average score and weakest words. Any further
  turn starts a new round.
- **Typed answers:** LiveKit's default text input calls `generate_reply` and skips the turn hook.
  The session therefore uses a custom `text_input_cb` (`ModeController.on_text_input`) that routes
  typed text to the active drill.
- **Mode switch mid-answer:** a drill that is no longer active stores the answer but stays silent.

Scoring per drill:

| Drill | Scorer | Score | Per-word score | FSRS |
|---|---|---|---|---|
| EN→ES | Haiku judge: meaning, form, target used, minimal correction | 0.6·meaning + 0.4·form | Target word 100 / 0 | Good / Again |
| ES→EN | Haiku judge (meaning only) | meaning | Target word = meaning | none |
| Repeat | Azure pronunciation assessment | Mean word accuracy (omitted = 0) | Every word | none |

## 9. Pronunciation (`pronunciation.py`, `speech_capture.py`)

```text
RepeatAfterMe.stt_node: STT input frames ──tee──► SpeechCapture
   ring buffer (~1 s) ──► on user_state "speaking": start recording with the ring (no clipped onset)
   end of turn: take() ──► 16 kHz PCM, or None if no speech (typed ──► "Dilo en voz alta")
PronunciationAssessor.assess(pcm, reference):
   push stream ──► Azure continuous recognition (es-ES, word granularity, miscue off)
   per segment: recognized words + accuracy ──► align to the reference (difflib)
   ──► words [{word, score, error: none | mispronounced | omitted}], completeness, fluency
```

- **Continuous mode** (not `recognize_once`), so a pause mid-sentence doesn't cut the answer off.
- **Spanish sound merges:** b/v and ll/y sound the same, so "lebo" for "llevo" is not an error.
- **Speed:** scoring takes about 2.4 s after the end of the turn.

## 10. Listening support

- **Slow replay (`replay_slow` RPC):** a second Cartesia TTS at 0.7× synthesizes the line, and
  `session.say("", audio=…, add_to_chat_ctx=False)` plays it. The transcript and chat history keep
  only the original line, and the session TTS speed is untouched.
- **Translation (`translate_line` RPC):** Haiku English translation, cached per line.
- **Web:** buttons under every tutor line, through `AgentMessageActionsContext`.

## 11. Client ↔ agent contract

All payloads are JSON. The agent side is the source of truth; the TypeScript mirrors live in
`web/lib/tutor-*.ts`.

**RPCs** (client → agent, registered in `main.py`):

| Method | Payload | Returns |
|---|---|---|
| `set_input_mode` | `"open"` \| `"ptt"` | `ok` |
| `ptt_start`, `ptt_end`, `ptt_cancel` | none | `ok` |
| `set_lesson_mode` | `{"mode", "scenario"}` | `ok` |
| `lesson_context` | none | `{due_words[], recent_errors[]}` |
| `session_summary` | none | `{patterns[], encouragement, new_words[]}` (timeout 30 s) |
| `word_scores` | none | `[{word, average, attempts}]` |
| `session_cost` | none | `{minutes, claude, deepgram, cartesia, azure, livekit, total, per_minute}` |
| `replay_slow` | tutor line | `ok` |
| `translate_line` | tutor line | English text |

**Text streams** (agent → client):

| Topic | Content |
|---|---|
| `tutor.feedback` | One `FeedbackMessage` per learner turn (conversation and role-play). |
| `tutor.drill` | `DrillEvent`: `item`, `result` (with per-word scores), `recap`. |
| `lk.transcription`, `lk.chat` | LiveKit's own transcript and typed input. |

**Dispatch metadata:** `{"level": "A1" … "C1"}`.

## 12. Web client

- **Auth:** the browser uses the publishable key and sends a magic link that returns to the page's
  origin. The token route then:
  1. checks `Authorization: Bearer <access token>` against `SUPABASE_JWKS_URL` (ES256, issuer
     `<SUPABASE_URL>/auth/v1`, audience `authenticated`);
  2. issues a 15-minute LiveKit token whose identity is the user id.

  Unauthenticated requests get 401. Supabase redirect URLs must include the Vercel URL.
- **Browser configuration:** `next.config.ts` maps `SUPABASE_URL` and `SUPABASE_PUBLISHABLE_KEY` to
  `NEXT_PUBLIC_*` at build time, so the same `.env` serves both.
- **Layout:**
  - One top bar: log out, input mode or level, lesson mode and cost. It wraps on phones.
  - On `xl` screens the panel is a right-hand column; below that it's a drawer with a toggle
    button, and hold-to-talk moves to the bottom.
  - Checked at 390×844 and 1440×900 in headless Chromium.
- **Agent join timeout:** 90 s (`AGENT_JOIN_TIMEOUT_MS` in `components/app/app.tsx`; the SDK
  default is 20 s). The cloud agent sleeps when idle on the free plan, and waking it takes longer
  than 20 s, so with the default the page gave up before the agent arrived.
- **Installable:** manifest and icons; no service worker, because current Chrome and iOS install
  without one and the tutor needs the network.

## 13. Cost tracking (`costs.py`)

```text
CostMeter
  pipeline: session.usage ──► Sonnet tokens (cache reads/writes split out), Deepgram audio s, Cartesia chars
  direct:   add_claude(model, usage)  feedback, summary, drill content, translation
            add_azure(seconds)        pronunciation audio
            add_tts(chars)            slow replay
  minutes ──► LiveKit agent + WebRTC minutes
breakdown() ──► session_cost RPC (live), sessions.cost (at shutdown), costs.py report
```

Prices are constants in `costs.py` with their sources, checked 2026-11-09. Azure's price is an
estimate ($1/h STT plus $0.30/h pronunciation assessment).

Measured costs:
- **Spoken conversation:** about $0.043/min. Cartesia 43%, LiveKit 25%, Deepgram 20%, Claude 12%.
- **Drill-heavy session:** about $0.065–0.073/min.

## 14. Data model (Supabase Postgres)

| Table | Key columns | Written by |
|---|---|---|
| `learners` | `id` (= `auth.users.id`), `level` | session start |
| `sessions` | `learner_id`, `level`, `started_at`, `ended_at`, `summary` jsonb, `cost` jsonb | start / summary / shutdown |
| `vocab` | (`learner_id`, `term`), `spanish`, `english`, `card` jsonb (FSRS), `due` | gaps, recall, EN→ES drills |
| `errors` | `learner_id`, `session_id`, `category`, `original`, `corrected`, `rule` | feedback |
| `drill_attempts` | `mode`, `prompt`, `expected`, `answer`, `score`, `details` jsonb | drills |
| `word_scores` | `attempt_id`, `term`, `word`, `mode`, `score` | drills |

- **Access:** the agent connects as the database owner and bypasses RLS. RLS lets a logged-in learner
  read only their own rows.
- **Deletion:** deleting an auth user cascades to all of their rows.
- **Migrations:** `supabase/migrations/`, applied with `agent/migrate.py` (tracked in
  `schema_migrations`).

## 15. Configuration and secrets

| Where | Keys |
|---|---|
| Root `.env` (local; `web/.env.local` is a symlink to it) | All of the below plus `SUPABASE_SECRET_KEY` (evals only). |
| LiveKit Cloud agent secrets | `DEEPGRAM_API_KEY`, `ANTHROPIC_API_KEY`, `CARTESIA_API_KEY`, `SUPABASE_DB_URL`, `AZURE_SPEECH_KEY`, `AZURE_SPEECH_REGION` (`LIVEKIT_*` injected by the platform). |
| Vercel env vars | `LIVEKIT_URL`, `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET`, `AGENT_NAME`, `SUPABASE_URL`, `SUPABASE_PUBLISHABLE_KEY`, `SUPABASE_JWKS_URL`. |

Optional agent flag: `RECORD_TURNS=1`.

## 16. Testing and evals

| Check | What it verifies | Needs |
|---|---|---|
| `pytest` | Learner model: matching, FSRS flow across simulated days, drill word scores | DB |
| `evals/recast.py` (`--memory`, `--scenario`) | Language policy of the tutor prompt (judge: Haiku) | API keys |
| `evals/feedback.py` | Extractor accuracy, categories, latency | API keys |
| `evals/translation.py` | Drill judge verdicts and item generator | API keys |
| `evals/pronunciation.py` | Azure scorer on synthesized audio (good, changed vowels, omissions, pause) | API keys |
| `evals/recorded.py` | WER of production STT on recorded turns, expected corrections | recordings or synthetic |
| `evals/memory.py` | Login → token route → two sessions; the 2nd opens with due word and error | agent + web |
| `evals/lesson_modes.py` | All modes, spoken repeat on a synthetic mic track, replay, translation, cost | agent + web |
| `evals/long_chat.py` | 30 typed turns: reply/feedback latency, summary | agent |
| `evals/voice_latency.py` | Drives spoken turns, prints session cost (latency: read agent logs) | agent + web |
| `evals/agent_wait.py` | Page still waits after 35 s without an agent (cold-start tolerance); Playwright | web dev with `AGENT_NAME=tutor-absent` |

- **Synthetic audio:** evals that need it use the tutor's own Cartesia voice (`evals/synth_audio.py`).
- **Hosted runs:** end-to-end evals run against the hosted app with
  `WEB_URL=https://espanol-tutor.vercel.app`.

## 17. Deployment and operations

- **Agent:** `cd agent && lk agent deploy` builds the Dockerfile on LiveKit Cloud and rolls out a new
  version.
  - The image runs `/app/.venv/bin/python main.py start`.
  - VAD and turn-detector models are downloaded at build time.
  - It includes the Azure SDK's native libraries (`libssl3`, `libasound2`).
  - Status and logs: `lk agent status`, `lk agent logs`. Roll back with `lk agent rollback`.
- **Web:** Vercel deploys `tutor` on every push (production branch set to `tutor`). Deploy through
  git; `vercel deploy` from the local folder could upload the symlinked `.env`.
- **Database:** add a migration file, then run `agent/migrate.py`.
- **Logs worth watching:**
  - `tutor.latency` (`e2e` lines are LiveKit's measured voice-to-voice latency);
  - `tutor.feedback`, `tutor.drill`, `tutor.learner`.
- **Two workers named `tutor`:** a local `main.py dev` registers in the same LiveKit project as the
  cloud agent, so dispatches may go to either.

## 18. Known limits and open issues

- **First call after idle is slow.** On LiveKit Cloud's free Build plan the agent sleeps when idle;
  the next call waits 10–20 s or more for it to wake. The paid Ship plan keeps it warm.
- **Latency is above target.** LiveKit's `e2e_latency` on the hosted agent:
  - normal spoken turns take 1.4–1.7 s (end-of-turn wait 0.58 s plus Sonnet first token 0.6–0.7 s);
  - some turns take 5–6 s, when the turn detector waits its full 5 s maximum.

  Options: a lower max delay, a faster tutor model at low levels, preemptive TTS.
- **STT can hide learner errors.** Even with smart formatting off, Deepgram turned "me gusta mucho las
  montañas" into "me gustan…" on synthetic audio. `evals/recorded.py` will quantify this on real
  recordings.
- **English through the Spanish voice:** English prompts and corrections in translation drills are
  read by the Castilian voice.
- **Word matching** has no lemmatizer, so inflected forms of a due word don't count as recall.
- **Azure pricing** in `costs.py` is an estimate.
- **Access control:** anyone can request a magic link unless sign-ups are disabled in Supabase
  (Authentication → Sign In / Providers → User Signups).
