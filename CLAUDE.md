# Spanish Voice Tutor

A voice-first Spanish tutor for English speakers. The tutor speaks Spanish; the learner replies by voice or text in Spanish, English, or a mix. It also runs the reverse: English prompts, Spanish answers. The full plan, rationale and roadmap are in `docs/plan.md`. Read it before starting a new phase. How the current system works (modules, client↔agent contract, data model, known limits) is in `docs/implementation.md`; keep it current when the implementation changes.

## Current status

- Current phase: **Phase 5 – Polish and ship** (Phases 0–4 done; Phase 5 steps 1–6 implemented, deployment pending)
- Update this line whenever a phase is finished.

## Architecture (cascaded voice pipeline)

```
Browser (Next.js + LiveKit client)
  ⇄ LiveKit Cloud room (WebRTC audio + data channel)
  ⇄ Tutor agent (Python, LiveKit Agents):
       turn detection (Silero VAD + turn model)
       → STT: Deepgram Nova-3, language=multi (EN/ES code-switching)
       → LLM: Claude Sonnet (tutor reply, streamed sentence by sentence)
       → TTS: Cartesia Sonic 3.5 (Spanish + English)
       ↳ Feedback extractor: Claude Haiku, runs in parallel
         → corrections/vocab sent to the UI over the data channel
  Postgres (Supabase): profile, vocab (FSRS), error log, sessions
  Azure Pronunciation Assessment: drill modes only
```

We deliberately chose a cascade over a speech-to-speech model. We need an exact learner transcript for corrections, a separate feedback channel, and per-language control of voice and speed. Do not switch to a realtime speech-to-speech model unless asked.

`record_feedback` is Haiku structured output (`agent/feedback.py`), not a tool call on the tutor LLM: a tool call would sit inside the spoken turn. It is published on the `tutor.feedback` text stream; the session summary is the `session_summary` RPC (Sonnet).

Learner model (Phase 3): magic-link login (Supabase Auth); the token route verifies the Supabase JWT and sets the LiveKit identity to the user id. The agent loads the learner by that identity (`main.py open_learner`; non-UUID identities, e.g. evals, run without memory). Layers: `main.py` → `learner_model.py` (FSRS, matching, lesson) → `learner_store.py` (only module with SQL, asyncpg over the Supabase session pooler). Schema in `supabase/migrations/`. The session opener (due words + recent errors) goes into the prompt, Deepgram keyterms and the `lesson_context` RPC.

Lesson modes (Phase 4): `set_lesson_mode` RPC → `mode_controller.py` swaps the session's Agent (LiveKit handoff): `Tutor` / `RolePlay` (`tutor_agents.py`, LLM) or a drill (`drills.py`). Drills are code-driven: Haiku generates items and judges translations (`drill_content.py`); repeat-after-me tees the STT input audio (`speech_capture.py`) to Azure (`pronunciation.py`, the only Azure SDK module, es-ES, continuous recognition + reference alignment). Drills speak verdicts built in code, raise `StopResponse` so the LLM never replies, publish on `tutor.drill`, and store `drill_attempts` + per-word `word_scores`. Typed drill answers come through a custom `text_input_cb` (LiveKit's default skips `on_user_turn_completed`). The feedback channel pauses during drills.

Polish (Phase 5): `costs.py` meters every session (LiveKit session usage + direct Claude/Azure calls, price table with sources) → `session_cost` RPC, `sessions.cost`, `uv run python costs.py`. Listening support RPCs: `replay_slow` (second Cartesia TTS at 0.7×, audio-only `say`) and `translate_line` (Haiku, cached). Web: one top bar that wraps on phones, side panel becomes a drawer below `xl`, per-line Slow/EN buttons via `AgentMessageActionsContext` in the transcript, installable (app/manifest.ts, icons; no service worker). `RECORD_TURNS=1` saves spoken turns to `agent/evals/recordings/` (git-ignored) for the recorded-turn eval. Agent image: `agent/Dockerfile` (models baked in).

## Repo layout (target)

```
agent/        Python LiveKit agent (tutor pipeline, prompts, tools)
web/          Next.js client (transcript, feedback panel, mic controls)
docs/         notes and plan.md
```

## Tutor behaviour rules (keep these in the system prompt)

- Speak Spanish at the learner's CEFR level; use English only as a lifeline.
- If the learner answers in English, recast it in Spanish and invite them to repeat.
- If the learner mixes languages, supply the missing Spanish word and log it as a vocab gap.
- In conversation, use implicit recasts only, at most one per turn. Explicit corrections go to the side panel via the `record_feedback` tool.
- Keep spoken replies to 1–3 sentences and usually end with a question.
- The feedback extraction must never delay the spoken reply.

## Conventions

- Python 3.12, `uv` for deps, type hints, `ruff` for lint/format.
- TypeScript strict mode in `web/`; use `@livekit/components-react`.
- Secrets live in `.env` (never commit). Expected keys: `LIVEKIT_URL`, `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET`, `DEEPGRAM_API_KEY`, `ANTHROPIC_API_KEY`, `CARTESIA_API_KEY`, and later `SUPABASE_URL`, `SUPABASE_PUBLISHABLE_KEY` (browser), `SUPABASE_SECRET_KEY` (server/evals only), `SUPABASE_JWKS_URL` (token route), `SUPABASE_DB_URL` (agent; session pooler, not the IPv6-only direct host), `AZURE_SPEECH_KEY`, `AZURE_SPEECH_REGION` (`northeurope`; West Europe was unavailable).
- Target variety: Spain Spanish (es-ES): Castilian TTS voice, es-ES pronunciation locale.
- Use EU regions/endpoints where a provider offers them (user is in the Netherlands).
- Log per-stage latency (end of user speech → STT final → LLM first token → TTS first audio). Target: under ~800 ms voice-to-voice.
- On learner audio, disable STT smart formatting so learner mistakes aren't auto-corrected away.

## How to work in this repo

- For each new phase, read `plan.md`, propose a step-by-step plan, and wait for approval before writing code.
- Check current SDK docs for LiveKit Agents and provider plugins rather than relying on memory; these APIs change often.
- Keep changes small and runnable. After each step, say how to run/test it.
- When a decision changes (e.g. a different TTS provider), update this file and `plan.md`.

## Commands (fill in as they exist)

- Agent console (terminal mic, no web): `cd agent && uv run python main.py console`
- Agent dev: `cd agent && uv run python main.py dev`
- Agent eval (language policy, ~$0.02/run): `cd agent && uv run python evals/recast.py --runs 3`. Run after any prompt or LLM change.
- Agent feedback eval (extractor accuracy + latency, <$0.01/run): `cd agent && uv run python evals/feedback.py --runs 3`. Run after any feedback prompt change.
- Agent long chat (30 typed turns vs live agent: reply + feedback latency, summary; needs `main.py dev` running): `cd agent && uv run python evals/long_chat.py`
- Agent tests (learner model; uses the Supabase DB with a throwaway user): `cd agent && uv run pytest -q`
- DB migrations (applies new `supabase/migrations/*.sql` once each): `cd agent && uv run python migrate.py`
- Agent memory e2e (login → token route → two sessions, 2nd opens with due words + errors; needs `main.py dev` and web dev running, ~2 min): `cd agent && uv run python evals/memory.py`
- Agent pronunciation eval (Azure on Cartesia-synthesised audio, <$0.01): `cd agent && uv run python evals/pronunciation.py`
- Agent translation eval (drill judge + item generator, ~$0.01/run): `cd agent && uv run python evals/translation.py --runs 3`. Run after drill prompt changes.
- Agent lesson modes e2e (all modes, slow replay, translation, cost vs live agent, incl. spoken repeat via a synthetic mic track; needs `main.py dev` and web dev, ~2 min): `cd agent && uv run python evals/lesson_modes.py`
- Agent voice latency (drives spoken turns, prints session cost; needs both servers): `cd agent && uv run python evals/voice_latency.py`. Read the real latency from the agent's `tutor.latency - e2e` log lines, not the eval's client-side estimate.
- Deploy agent (LiveKit Cloud, eu-central): `cd agent && lk agent deploy`; status/logs: `lk agent status`, `lk agent logs`. Secrets: `lk agent update-secrets` (only the agent's keys; LIVEKIT_* are injected). A local `main.py dev` worker also registers as `tutor` in the same project, so dispatches may go to either.
- Recorded turns (WER of production STT + expected corrections; uses `agent/evals/recordings/`, synthetic turns if empty): `cd agent && uv run python evals/recorded.py`. Record with `RECORD_TURNS=1 uv run python main.py dev`, then fix each JSON's `reference` and `expected_corrections`.
- Costs over stored sessions: `cd agent && uv run python costs.py --days 30`
- Web hosting: Vercel project `espanol-tutor` (https://espanol-tutor.vercel.app), deploys `tutor` on push. Env vars: LIVEKIT_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET, AGENT_NAME, SUPABASE_URL, SUPABASE_PUBLISHABLE_KEY, SUPABASE_JWKS_URL. Don't run `vercel link`/`env pull` in `web/` without care: `web/.env.local` is a symlink to the root `.env` and the CLI writes into it; deploy via git push, not `vercel deploy` from the local folder.
- E2E evals against the hosted app: prefix with `WEB_URL=https://espanol-tutor.vercel.app` (stop any local `main.py dev` first).
- Don't run `next build` while `next dev` is running: it corrupts `.next` (restart dev afterwards).
- Recast eval in role-play: add `--scenario restaurant` (or doctor, job_interview, hotel, directions).
- Agent lint: `cd agent && uv run ruff check . && uv run ruff format .`
- `web/` is a git submodule: fork `bliep/agent-starter-react`, branch `tutor` (our changes); remote `upstream` = `livekit-examples/agent-starter-react`. Use pnpm with its lockfile (npm resolves newer deps that break the build). GitHub account: `bliep`.
- Web setup: `git submodule update --init && ln -sf ../.env web/.env.local && cd web && npx pnpm@9.15.9 install --frozen-lockfile`
- Web dev: `cd web && npx pnpm@9.15.9 dev` (http://localhost:3000)
- Web check: `cd web && npx pnpm@9.15.9 exec tsc --noEmit && npx pnpm@9.15.9 build`
- Bump upstream: `cd web && git fetch upstream && git merge upstream/main` (on `tutor`)
