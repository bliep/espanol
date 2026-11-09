# Spanish Voice Tutor

A voice-first Spanish tutor for English speakers. The tutor speaks Spanish; the learner replies by voice or text in Spanish, English, or a mix. It also runs the reverse: English prompts, Spanish answers. The full plan, rationale and roadmap are in `docs/plan.md`. Read it before starting a new phase.

## Current status

- Current phase: **Phase 3 – Learner model** (Phases 0–2 done; Phase 3 implemented, browser login check pending)
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
- Secrets live in `.env` (never commit). Expected keys: `LIVEKIT_URL`, `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET`, `DEEPGRAM_API_KEY`, `ANTHROPIC_API_KEY`, `CARTESIA_API_KEY`, and later `SUPABASE_URL`, `SUPABASE_PUBLISHABLE_KEY` (browser), `SUPABASE_SECRET_KEY` (server/evals only), `SUPABASE_JWKS_URL` (token route), `SUPABASE_DB_URL` (agent; session pooler, not the IPv6-only direct host), and later `AZURE_SPEECH_KEY`, `AZURE_SPEECH_REGION`.
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
- Agent lint: `cd agent && uv run ruff check . && uv run ruff format .`
- `web/` is a git submodule: fork `bliep/agent-starter-react`, branch `tutor` (our changes); remote `upstream` = `livekit-examples/agent-starter-react`. Use pnpm with its lockfile (npm resolves newer deps that break the build). GitHub account: `bliep`.
- Web setup: `git submodule update --init && ln -sf ../.env web/.env.local && cd web && npx pnpm@9.15.9 install --frozen-lockfile`
- Web dev: `cd web && npx pnpm@9.15.9 dev` (http://localhost:3000)
- Web check: `cd web && npx pnpm@9.15.9 exec tsc --noEmit && npx pnpm@9.15.9 build`
- Bump upstream: `cd web && git fetch upstream && git merge upstream/main` (on `tutor`)
