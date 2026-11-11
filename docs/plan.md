# Spanish Voice Tutor — Tech Stack & Implementation Plan

Nov 7, 2026 · @Anuj

## Summary

Build it as a **cascaded voice pipeline** (speech-to-text → LLM → text-to-speech) on **LiveKit Agents**, not as a single speech-to-speech model. A tutor needs a reliable transcript of what you said to correct it, a separate feedback channel, and per-language control of voice and speed; the cascade gives you all three and lets you pick the best part for each job.

| Layer | Pick | Why |
| --- | --- | --- |
| Client | Next.js (React, TypeScript) + LiveKit client SDK | Web first, mobile later as a PWA or React Native |
| Realtime transport + agent runtime | LiveKit (Cloud to start) + LiveKit Agents (Python) | WebRTC, turn-taking, interruptions and UI state sync solved for you |
| Speech-to-text | Deepgram Nova-3 Multilingual (`language=multi`) | Handles English and Spanish switching mid-sentence in one stream |
| Tutor brain | Claude (Sonnet 5.5 for conversation, Haiku 5.5 for fast side tasks) | Strong instruction-following for pedagogy and structured feedback |
| Text-to-speech | Cartesia Sonic 3.5 | Low, steady latency; strong Spanish naturalness; speed control |
| Pronunciation scoring | Azure Speech Pronunciation Assessment (es-ES / es-MX) | Phoneme- and word-level scores for drills |
| Data | Postgres (Supabase: DB + auth) + FSRS spaced repetition | Learner model: vocab, error log, session history |

A speech-to-speech model (OpenAI gpt-realtime-2.1 or Gemini Live) stays a valid alternative for a fast prototype; the trade-offs are in Architecture options.

## Requirements

The core requirement is bilingual, code-switching voice in both directions with sub-second replies.

- **Two-way language mixing.** The tutor speaks Spanish, you answer in Spanish, English or a mix. The reverse also works: the tutor gives an English prompt and you answer in Spanish (translation drills).
- **Text and voice in one session.** You can type or speak at any moment; the transcript is always visible.
- **Corrections without breaking the flow.** Spoken recasts during conversation, explicit corrections in a side panel.
- **Level-aware.** Vocabulary, speed and English support adapt to your CEFR level (A1 to C1).
- **Memory of you.** Words you know, mistakes you repeat, and topics you covered carry across sessions.
- **Latency.** Voice-to-voice reply under about 800 ms, so it feels like talking to a person.
- **Natural turn-taking.** You can interrupt the tutor (barge-in); pauses while you search for a word should not end your turn too early.
- **Listening support.** Slow-replay and per-sentence translation of anything the tutor said.

## Architecture options

Choose the cascade for the product; use speech-to-speech only if you want the fastest possible prototype.

|  | Cascaded: STT → LLM → TTS (recommended) | Speech-to-speech realtime model |
| --- | --- | --- |
| Examples | Deepgram + Claude + Cartesia | OpenAI gpt-realtime-2.1 / -mini, Gemini 3.1 Flash Live |
| Transcript of the learner | First-class: it is the LLM's input, so corrections are exact | Side output; the model reasons on audio, the transcript can differ from what it "heard" |
| Structured feedback | Easy: a second LLM call or tool call per turn | Possible via tools, but competes with the live voice turn |
| Voice and speed per language | Full control (pick voices, slow down Spanish for beginners) | Limited to the model's built-in voices |
| Pronunciation nuance | Needs a separate scorer (Azure) | Model hears tone and accent natively, but gives no scores |
| Latency | About 600 to 900 ms with streaming at every stage | Lowest; one model hop |
| Cost | Lower; each part is priced separately and swappable | Higher: roughly $0.15 to $0.20 per minute for gpt-realtime-2 before caching ([source](https://rywalker.com/research/openai-realtime-api)) |
| Lock-in | Each part replaceable | Tied to one vendor's models and voices |

Because LiveKit Agents supports both shapes behind the same session, you can start with one and switch later without rewriting the client.

## Recommended tech stack

One Python agent process does all the voice work; the browser only captures and plays audio and renders the transcript and feedback.

&#91;embedded content: tutor architecture · cascaded voice pipeline\]

The same flow as plain text (kept for Markdown export):

```text
Web app (Next.js + LiveKit client)
  mic, speaker, live transcript, feedback panel, text input
        ⇅  audio + text
LiveKit Cloud room (WebRTC audio both ways + data channel)
        ⇅  your speech / tutor reply
Tutor agent (LiveKit Agents, Python)
  Turn detection (Silero VAD + turn-taking model)
    → Speech-to-text: Deepgram Nova-3, language=multi
    → Tutor LLM: Claude Sonnet 5.5 (streams sentences)
    → Text-to-speech: Cartesia Sonic 3.5 (Spanish + English) → back to room
  Tutor LLM ↳ Feedback extractor: Claude Haiku 5.5
               corrections + vocab → UI via data channel
                 ↓
               Postgres (Supabase): profile, vocab (FSRS), errors
                 → feeds next lesson's context and Deepgram keyterms
  Turn detection ↳ learner audio (drills only)
                 → Azure Pronunciation Assessment: word and phoneme scores
```

Each stage streams into the next, so the tutor starts speaking its first sentence while Claude is still writing the second. The day's target words go from Postgres into the prompt and into Deepgram as keyterms.

| Component | Pick | Alternatives | Notes |
| --- | --- | --- | --- |
| Agent framework | [LiveKit Agents](https://livekit.io/field-guides/guide/livekit-vs-pipecat) (Apache-2.0) | Pipecat (BSD-2) | LiveKit syncs agent state (listening, thinking, speaking) to the client and ships React UI components; Pipecat is a pipeline you wire yourself |
| Speech-to-text | [Deepgram Nova-3 Multilingual](https://deepgram.com/blog/nova-3-multilingual-major-wer-improvements-across-languages) | Deepgram Flux Multi, OpenAI gpt-realtime-whisper | Code-switching in one stream; the Feb 2026 update cut streaming WER by about 21%; supports keyterm prompting |
| Tutor LLM | Claude Sonnet 5.5 | GPT, Gemini | Use Haiku 5.5 for the parallel feedback extraction |
| Text-to-speech | [Cartesia Sonic 3.5](https://cartesia.ai/vs/cartesia-vs-elevenlabs) | ElevenLabs Flash v2.5 | Cartesia reports native Spanish speakers preferred it over Eleven v3, and steadier P90 latency; ElevenLabs has a far bigger voice library |
| Pronunciation | [Azure Pronunciation Assessment](https://learn.microsoft.com/azure/ai-services/speech-service/language-support) | Speechace | Spanish locales supported; streaming mode available |
| Speech-to-speech (optional) | [OpenAI gpt-realtime-2.1](https://datanorth.ai/news/openai-releases-gpt-realtime-2-1-voice-models) | [Gemini 3.1 Flash Live](https://buttondown.com/verified/archive/google-unveils-gemini-31-flash-live-the-next) | For a quick prototype or a "free talk" mode |
| Frontend | Next.js + `@livekit/components-react` | React Native / Expo for mobile |  |
| Data and auth | Supabase (Postgres + auth) | Plain Postgres + Clerk | Use the EU region |
| Spaced repetition | `py-fsrs` | SM-2 |  |
| Observability | LiveKit metrics + Langfuse |  | Log per-stage latency and every LLM call |

## Tutor design

Every tutor turn produces two outputs: a short spoken reply, and a structured feedback object the UI shows beside the transcript.

**Language policy (the heart of the product)**

- Default: the tutor speaks Spanish at your level, with English only as a lifeline.
- You reply in English → the tutor recasts it in Spanish ("Ah, quieres decir: *Fui al mercado ayer*") and invites you to repeat.
- You mix languages ("Ayer fui al... store") → the tutor supplies the missing word in context, logs it as a vocab gap.
- Reverse mode: the tutor gives an English sentence, you answer in Spanish; scored for meaning first, form second.

**Correction strategy**

- In conversation: implicit recasts only, at most one per turn, so the flow survives.
- In the side panel: explicit corrections with a one-line rule (ser/estar, gender agreement, preterite vs imperfect).
- End of session: a short summary of 3 to 5 patterns to work on, saved to your learner profile.

**Prompt and output design**

- System prompt holds: persona, your CEFR level, the language policy, today's lesson goal, and 10 to 20 target words from spaced repetition.
- Feedback via a tool call (`record_feedback`) with fields: `corrections[]` (original, corrected, rule), `new_vocab[]`, `english_gloss`, `user_language` (es / en / mixed).
- Keep spoken replies short (1 to 3 sentences) and end with a question, so you speak more than the tutor.
- A fast model (Haiku) can run the feedback extraction in parallel, so it never delays the spoken reply.

**Lesson modes**

1. Free conversation on a topic you choose.
2. Role-play scenarios (ordering food, a doctor visit, a job interview).
3. Translation drills, English ↔ Spanish.
4. Repeat-after-me pronunciation drills with scores.
5. Listening: the tutor tells a short story, then asks comprehension questions.

**Learner model**

- Vocabulary items scheduled with FSRS (`py-fsrs`); a word counts as reviewed when you use it correctly in speech.
- Error log by category; frequent errors become the next lesson goal.
- The day's target words are also sent to Deepgram as keyterms, which improves recognition of exactly the words you are practising.

**Pronunciation**

- Use Azure Pronunciation Assessment in drill modes, where a reference sentence exists; it returns accuracy, fluency and completeness down to the phoneme.
- Azure's prosody scoring is only available for en-US, so intonation feedback for Spanish has to come from the LLM's judgement, not a score ([source](https://docs.azure.cn/en-us/ai-services/speech-service/how-to-pronunciation-assessment)).
- Microsoft suggests trying both es-ES and es-MX and keeping whichever scores better for the accent you are learning ([source](https://learn.microsoft.com/en-ca/azure/ai-services/speech-service/how-to-pronunciation-assessment)).

## Implementation plan

Six phases over about 12 weeks for one developer working part-time; you have a tutor you can actually talk to after phase 1.

&#91;embedded content: implementation roadmap · 6 phases, 12 weeks\]

The same roadmap as a table (kept for Markdown export):

| Phase | Weeks | Deliverables | Done when |
| --- | --- | --- | --- |
| 0 · Spike | 1 | LiveKit agent wiring Deepgram, Claude and Cartesia; talk to it from a bare web page | Voice round-trip under 1 s, with per-stage timings logged |
| 1 · Conversation MVP | 2–3 | Level and language-policy prompt, open mic + push-to-talk, live transcript, typed input | A 10-minute Spanish chat works and English replies get recast |
| 2 · Feedback channel | 4–5 | `record_feedback` tool on Haiku, corrections side panel, end-of-session summary | Corrections show within 2 s of each turn without delaying the voice |
| 3 · Learner model | 6–7 | Supabase auth + Postgres, vocab and error log, FSRS scheduling, keyterms to Deepgram | Each session opens with your due words and recent mistakes |
| 4 · Drills and pronunciation | 8–9 | Translation drills EN ↔ ES, repeat-after-me with Azure scores, role-play scenarios | Drill scores are stored and shown per word |
| 5 · Polish and ship | 10–12 | Slow replay, sentence translation, mobile PWA, eval set of recorded turns, cost tracking | Daily use on your phone feels smooth at a known cost per minute |

Each phase ends with something you can use, so you can stop after any of them. First steps for the spike:

- [x] Create LiveKit Cloud, Deepgram, Anthropic and Cartesia accounts; put the keys in `.env`
- [x] Start from LiveKit's Python voice-agent starter and swap in the Deepgram (`nova-3`, `language=multi`), Anthropic and Cartesia plugins
- [x] Write a first tutor prompt: A2 level, Spanish-first, recast English replies, max 2 sentences per turn
- [x] Use LiveKit's React starter as the web client
- [x] Log time from end of your speech to first tutor audio, per stage

**Phase 1 status (done, 2026-11-08):**

- [x] CEFR level picker (A1–C1) drives prompt and TTS speed; sent as agent dispatch metadata
- [x] Language policy: recast English, fill gaps in mixed turns, stay in Spanish; `agent/evals/recast.py` passes 97% over 3 runs
- [x] Open mic + push-to-talk (button or space bar), switchable mid-call
- [x] Live transcript and typed input (from the LiveKit starter)
- [x] 30-turn typed chat (`agent/evals/long_chat.py`): stable, recasts correct, reply time flat
- Latency: Sonnet 5.5 runs with thinking off at low effort (default high effort cost ~2.4 s to first token). Typed reply ~1.8 s as seen by the client, ~0.85 s agent-side; accepted for now. Revisit in Phase 5.

**Phase 2 status (implemented 2026-11-08; browser check pending):**

- [x] `record_feedback` as Claude Haiku 5.5 structured output, in parallel with the reply (a tool call on the tutor LLM would delay the voice). Prompt plus a code clean-up drop no-op corrections, English gaps restated as corrections, and sentence-length vocab. `agent/evals/feedback.py`: 98–100% over 3 runs.
- [x] Published per turn on the `tutor.feedback` text stream. Live 30-turn chat: feedback 30/30, median ~1.0 s, p90 ~1.24 s (target 2 s); reply median unchanged (~1.8 s).
- [x] Corrections side panel (web, xl screens and up): original → corrected + rule, new words, gloss.
- [x] Session summary: `session_summary` RPC, Sonnet 5.5, ~7 s; 3–5 patterns with the learner's own examples, plus the session's vocab gaps. Saving it to the profile is Phase 3.

**Phase 3 status (implemented 2026-11-09; browser login check pending):**

- [x] Supabase (EU, Frankfurt): `learners`, `sessions`, `vocab` (FSRS card per word), `errors`; RLS read-own. Migrations in `supabase/migrations/`, applied with `agent/migrate.py`.
- [x] Magic-link login; token route verifies the Supabase JWT, LiveKit identity = user id. Removes the starter's insecure-route exception.
- [x] Error categories on corrections (ser/estar, agreement, preterite/imperfect, conjugation, preposition, word choice, other). Feedback eval 98–100%, now also checks categories.
- [x] FSRS (`py-fsrs`): a vocab gap reviews "Again" (new card if needed); a due word used in Spanish outside any corrected fragment reviews "Good". Plain-text matching (accents, case, articles ignored; no lemmatizer).
- [x] Session opener: 15 most overdue words + top 3 error categories (14 days) → system prompt, Deepgram keyterms, greeting, and a "Today" section in the panel (`lesson_context` RPC). Recast eval with memory (`--memory`): 100% over 2×3 runs after moving the memory block ahead of the policy rules (89% when appended at the end).
- [x] Summary saved on request and at shutdown; session `ended_at` set.
- [x] `agent/evals/memory.py`: two sessions as a throwaway logged-in learner; the 2nd opens with the due word and the error. Passes.

**Phase 4 status (implemented 2026-11-09; browser check pending):**

- [x] Lesson modes switchable mid-call (web mode picker → `set_lesson_mode` RPC → LiveKit agent handoff): conversation, role-play, translate EN→ES, translate ES→EN, repeat after me.
- [x] Role-play: 5 scenarios (tapas bar, doctor, job interview, hotel, directions); language policy and feedback panel unchanged. Recast eval `--scenario`: restaurant 92%, doctor 94% (3 runs) after making the policy override the role and keeping the 2-sentence limit.
- [x] Translation drills: 8 Haiku-generated items per round from due words and recent error types; Haiku judge scores meaning (60%) and form (40%), target word use, minimal correction. Spoken verdicts are built in code. EN→ES reviews the target word in FSRS. `evals/translation.py`: 100% (3 runs) after telling the judge that valid alternatives (me llevo, el/mi) are not form errors.
- [x] Repeat after me with Azure Pronunciation Assessment (es-ES, North Europe): learner audio teed from the STT input, ~1 s pre-roll, continuous recognition aligned to the reference, so pauses and omissions are handled. Typed answers are refused. `evals/pronunciation.py` 4/4 (correct 99, vowel changes flag the words, omissions flagged, a 2 s pause still complete). es-MX not compared: es-ES is the target variety.
- [x] Stored and shown per word: `drill_attempts`, `word_scores`; drill panel colours each word by score and lists the weakest words (`word_scores` RPC).
- [x] `evals/drills.py`: 7/7 checks against the live agent, including a spoken answer on a synthetic mic track (98.8).
- Scoring latency in drills: translation judge 1–2.5 s, Azure ~2.4 s after end of turn. Acceptable for drills (not on the conversation voice path).
- Known limit: English prompts and corrections in EN→ES / ES→EN are read by the Spanish voice.

**Phase 5 status (steps 1–6 implemented 2026-11-09; deployment pending):**

- [x] Cost tracking per session (USD): LiveKit session usage (Sonnet tokens incl. cache, Deepgram audio, Cartesia characters) + direct Haiku/Sonnet/Azure calls + LiveKit minutes. Shown live in the app, saved in `sessions.cost`, `costs.py` report. Measured: spoken conversation ≈ $0.043/min (Cartesia 43%, LiveKit 25%, Deepgram 20%, Claude 12%); a drill-heavy session ≈ $0.065/min. Azure's price is an estimate ($1/h STT + $0.30/h pronunciation; its pricing page did not render numbers).
- [x] Slow replay (0.7×, separate TTS, not added to the transcript) and per-line English translation (Haiku, cached), as buttons under each tutor line.
- [x] Phone layout: one wrapping top bar, panel as a drawer below `xl`, hold-to-talk at the bottom on phones; checked at 390×844 and 1440×900 in headless Chromium. Installable (manifest, icons); no service worker (current Chrome/iOS install without one, and the tutor needs the network).
- [x] Recorded-turn eval: `RECORD_TURNS=1` saves spoken turns locally; `evals/recorded.py` gives WER of production STT and correction recall. On synthetic turns Deepgram turned "Me gusta mucho las montañas" into "Me gustan…": the STT can hide learner errors even with smart formatting off. Real recordings will show how often.
- [ ] Latency: NOT on target. LiveKit's own e2e_latency on the deployed agent (eu-central): normal spoken turns 1.4–1.7 s (end-of-turn wait 0.58 s + Sonnet first token 0.6–0.7 s + TTS/transport); 2 of 6 turns 5–6 s, when the turn detector waited the full 5 s max delay. Preemptive LLM generation (on by default) does not visibly help. An earlier client-side measurement (0.5–0.7 s, also in commit 2e7055b) was wrong: it counted late chunks of the previous reply. The agent now logs `tutor.latency - e2e` per reply. Options: lower the 5 s max end-of-turn delay, a faster tutor LLM, preemptive TTS.
- [x] Agent deployed to LiveKit Cloud, `eu-central` (`agent/Dockerfile`, `agent/livekit.toml`; deploy with `cd agent && lk agent deploy`). Against it: `evals/lesson_modes.py` 10/10, `evals/memory.py` pass.
- [x] Web on Vercel: https://espanol-tutor.vercel.app (project `espanol-tutor`, production branch `tutor`, 7 env vars). Fully hosted: `evals/memory.py` pass, `evals/lesson_modes.py` 10/10 (`WEB_URL=https://espanol-tutor.vercel.app`).
- [ ] Supabase redirect URL for the hosted app; then daily use on the phone.

**Decision (2026-11-11): spoken input is Spanish only; English goes through the chat.**
Deepgram has no "English or Spanish" setting. `language=multi` guesses among ~55 languages: on
the learner's own recordings it turned "Me llamo Sonia" into Italian ("Mi chiamo"), Greek and
Persian in 3 of 4 turns, and `language_hint=es,en` made no difference. `language=es` got all 4
right (including a real "llama" for "llamo") and still transcribed most English and mixed test
sentences; a sentence switching language mid-way can come back empty. STT is now
`language=es`; the chat input says "Speak Spanish · type in English or Spanish". This replaces
the code-switching-by-voice requirement above.

## Costs, risks and open questions

Running costs are dominated by the models, not the framework; expect cents per minute of conversation.

- Managed hosting (LiveKit Cloud or Pipecat Cloud) runs about $0.01 per minute; the model calls are the larger line ([source](https://www.forasoft.com/blog/article/pipecat-vs-livekit-agents)).
- Speech-to-speech with gpt-realtime-2 is roughly $0.15 to $0.20 per minute before caching ([source](https://rywalker.com/research/openai-realtime-api)).
- For comparison, ElevenLabs Flash TTS works out around $0.045 per minute ([source](https://techsy.io/it/blog/migliori-api-sintesi-vocale-sviluppatori)); check Deepgram, Cartesia and Anthropic pricing pages for current rates before you budget.
- Deepgram's free signup credit ($200) covers early development ([source](https://deepgram.com/blog/nova-3-multilingual-major-wer-improvements-across-languages.md)).

**Risks and mitigations**

| Risk | Mitigation |
| --- | --- |
| STT "corrects" learner errors into proper Spanish, hiding mistakes | Disable smart formatting on learner audio; compare against Azure word-level output in drills |
| Turn ends while you hesitate mid-sentence | Longer end-of-turn silence for beginners; push-to-talk option |
| Latency creeps past 1 s | Log per-stage timings from day one; stream LLM output into TTS sentence by sentence |
| Tutor talks too much or corrects too often | Hard limits in the prompt; evaluate on a fixed set of recorded learner turns |
| Voice and transcripts are personal data (you are in the EU) | Use EU endpoints where offered (Deepgram has an EU region), set retention limits, get consent for storing audio |

**Open questions**

- Which Spanish do you want to learn: Spain (es-ES) or Latin American (es-MX)? It sets voices, vocabulary and the pronunciation locale.
- ~~Just for you, or for other users too?~~ Decided: magic-link auth per learner (Phase 3).
- Web only, or a phone app from the start?

## Sources

- [OpenAI gpt-realtime-2.1 release (DataNorth)](https://datanorth.ai/news/openai-releases-gpt-realtime-2-1-voice-models)
- [OpenAI Realtime API overview and costs (rywalker.com)](https://rywalker.com/research/openai-realtime-api)
- [Gemini 3.1 Flash Live announcement](https://buttondown.com/verified/archive/google-unveils-gemini-31-flash-live-the-next)
- [Deepgram Nova-3 Multilingual update, Feb 2026](https://deepgram.com/blog/nova-3-multilingual-major-wer-improvements-across-languages)
- [Deepgram language coverage and EU region](https://deepgram.com/languages)
- [LiveKit Agents vs Pipecat (LiveKit)](https://livekit.io/field-guides/guide/livekit-vs-pipecat)
- [Voice agent frameworks compared (Fora Soft)](https://www.forasoft.com/blog/article/pipecat-vs-livekit-agents)
- [Cartesia vs ElevenLabs (Cartesia)](https://cartesia.ai/vs/cartesia-vs-elevenlabs)
- [TTS API pricing comparison (techsy.io)](https://techsy.io/it/blog/migliori-api-sintesi-vocale-sviluppatori)
- [Azure Speech language support](https://learn.microsoft.com/azure/ai-services/speech-service/language-support)
- [Azure pronunciation assessment how-to](https://docs.azure.cn/en-us/ai-services/speech-service/how-to-pronunciation-assessment)
