"""Drill agents: code-driven rounds of items, scored without the tutor LLM.

  on_enter ──► intro + generate a round (Haiku) ──► present item 1
  learner turn ──► on_user_turn_completed:
      score (Haiku judge or Azure) ──► publish result (tutor.drill) ──► store attempt
      speak "verdict + next item", or "verdict + recap" after the last one
      raise StopResponse: the tutor LLM never answers in a drill
  any turn after the recap ──► next round
  typed answers ──► answer() via the mode controller (LiveKit's text input skips the hook)

Spoken verdicts are built here from the scores, so they are short and predictable.
"""

import asyncio
import logging
import time
from collections.abc import AsyncIterable, Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum

import fsrs
from livekit import rtc
from livekit.agents import Agent, ModelSettings, StopResponse, llm
from pydantic import BaseModel

from drill_content import DrillItem, DrillItemGenerator, TranslationJudge
from feedback import VocabItem
from learner_model import LearnerModel, LessonContext, WeakWord
from learner_store import DrillAttempt
from modes import LessonMode
from prompts import Level
from pronunciation import PronunciationAssessor, WordError, WordScore
from speech_capture import SpeechCapture
from transcript_filter import is_foreign_script

log = logging.getLogger("tutor.drill")

Publish = Callable[[str], Awaitable[None]]

ROUND_SIZE = 8
GOOD = 80  # score at or above: correct
FAIR = 50  # score at or above: partly right
PRON_GOOD = 85
PRON_FAIR = 60
WORDS_TO_MENTION = 2  # weakest words named in a pronunciation verdict
MEANING_WEIGHT = 0.6  # translate_en score = 60% meaning + 40% form

# The LLM is never called in a drill; instructions only describe the role.
DRILL_INSTRUCTIONS = "Eres Lucía, tutora de español. Ahora diriges un ejercicio."

_INTRO: dict[LessonMode, str] = {
    LessonMode.TRANSLATE_EN: (
        "Vamos a traducir. Te digo una frase en inglés y tú la dices en español."
    ),
    LessonMode.TRANSLATE_ES: (
        "Vamos a traducir. Te digo una frase en español y tú me dices qué significa en inglés."
    ),
    LessonMode.REPEAT: "Vamos a practicar la pronunciación. Repite cada frase después de mí.",
}

_ASK: dict[LessonMode, str] = {
    LessonMode.TRANSLATE_EN: "Número {n}. ¿Cómo se dice: {english}?",
    LessonMode.TRANSLATE_ES: "Número {n}. ¿Qué significa: {spanish}?",
    LessonMode.REPEAT: "Número {n}. Repite: {spanish}",
}

SAY_IT_ALOUD = "Dilo en voz alta, por favor: {spanish}"
RECAP = "Fin de la ronda: {score} de cien. Cuando quieras, dime algo y hacemos otra ronda."


class DrillEventKind(StrEnum):
    ITEM = "item"
    RESULT = "result"
    RECAP = "recap"


class DrillEvent(BaseModel):
    """Wire format on the tutor.drill text stream (web/lib/tutor-drill.ts)."""

    kind: DrillEventKind
    mode: LessonMode
    index: int = 0  # 0-based item index
    total: int = 0
    prompt: str = ""  # what the learner is asked (item); what they were asked (result)
    source: str = ""  # the sentence to translate or repeat
    answer: str = ""
    expected: str = ""
    score: float = 0  # 0–100
    words: list[WordScore] = []  # per-word scores (result)
    correction: str = ""
    note: str = ""
    weakest: list[WeakWord] = []  # recap


@dataclass
class DrillDeps:
    level: Level
    lesson: LessonContext
    learner: LearnerModel | None
    publish: Publish
    generator: DrillItemGenerator = field(default_factory=DrillItemGenerator)
    judge: TranslationJudge = field(default_factory=TranslationJudge)
    assessor: PronunciationAssessor | None = None  # repeat mode only


@dataclass
class Scored:
    """One answered item: what to show, say and store."""

    score: float
    verdict: str  # spoken before the next item
    words: list[WordScore]
    details: dict
    correction: str = ""
    note: str = ""
    review: tuple[VocabItem, fsrs.Rating] | None = None


class Drill(Agent):
    mode: LessonMode

    def __init__(self, deps: DrillDeps) -> None:
        super().__init__(instructions=DRILL_INSTRUCTIONS)
        self._deps = deps
        self._items: list[DrillItem] = []
        self._index = 0
        self._scores: list[float] = []
        self._round_over = False
        self._busy = asyncio.Lock()  # one answer at a time (voice and typing can overlap)
        self._active = False  # False once the learner switched mode: no more speaking

    async def on_enter(self) -> None:
        self._active = True
        self.session.say(_INTRO[self.mode])
        await self._new_round()

    async def on_exit(self) -> None:
        self._active = False

    async def on_user_turn_completed(
        self, turn_ctx: llm.ChatContext, new_message: llm.ChatMessage
    ) -> None:
        await self.answer(new_message.text_content or "")
        raise StopResponse()  # the drill replied; the tutor LLM must not

    async def answer(self, text: str) -> None:
        """Scores the learner's answer to the current item, then moves on."""
        async with self._busy:
            await self._answer(text.strip())

    async def _answer(self, answer: str) -> None:
        if self._round_over:
            await self._new_round()
            return
        if not self._items or not answer:
            log.info("%s: answer ignored (round not ready)", self.mode)
            return  # round still generating, or nothing said
        if is_foreign_script(answer):
            log.info("%s: ignored answer in a non-Latin script: %s", self.mode, answer)
            return  # noise transcribed as another language; the item stays open

        item = self._items[self._index]
        start = time.perf_counter()
        scored = await self._score(item, answer)
        log.info(
            "%s item %d scored in %.0f ms",
            self.mode,
            self._index,
            (time.perf_counter() - start) * 1000,
        )
        if scored is None:
            return  # subclass already asked again (e.g. typed in repeat mode)

        await self._publish(
            DrillEventKind.RESULT,
            prompt=self._ask(item),
            source=self._source(item),
            answer=answer,
            expected=item.spanish if self.mode != LessonMode.TRANSLATE_ES else item.english,
            score=scored.score,
            words=scored.words,
            correction=scored.correction,
            note=scored.note,
        )
        await self._record(item, answer, scored)
        if not self._active:
            return  # mode switched while scoring: stored, but the new mode has the floor

        self._scores.append(scored.score)
        self._index += 1
        if self._index < len(self._items):
            await self._present(f"{scored.verdict} ")
        else:
            await self._recap(scored.verdict)

    async def _score(self, item: DrillItem, answer: str) -> Scored | None:
        raise NotImplementedError

    async def _new_round(self) -> None:
        lesson = self._deps.lesson
        errors = [e.category.value for e in lesson.recent_errors]
        self._items = await self._deps.generator.generate(
            self.mode, self._deps.level, ROUND_SIZE, lesson.due_words, errors
        )
        self._index, self._scores, self._round_over = 0, [], False
        log.info("%s round: %d items", self.mode, len(self._items))
        if self._active:
            await self._present()

    async def _present(self, prefix: str = "") -> None:
        item = self._items[self._index]
        await self._publish(DrillEventKind.ITEM, prompt=self._ask(item), source=self._source(item))
        self.session.say(prefix + self._ask(item))

    async def _recap(self, verdict: str) -> None:
        score = round(sum(self._scores) / len(self._scores))
        weakest = await self._deps.learner.weakest_words() if self._deps.learner else []
        await self._publish(DrillEventKind.RECAP, score=score, weakest=weakest)
        self.session.say(f"{verdict} {RECAP.format(score=score)}")
        self._round_over = True

    def _source(self, item: DrillItem) -> str:
        return item.english if self.mode == LessonMode.TRANSLATE_EN else item.spanish

    def _ask(self, item: DrillItem) -> str:
        # "¿Qué significa: Voy al trabajo?" not "...trabajo.?"
        english, spanish = item.english.rstrip(".!?"), item.spanish.rstrip(".!?")
        return _ASK[self.mode].format(n=self._index + 1, english=english, spanish=spanish)

    async def _publish(self, kind: DrillEventKind, **fields: object) -> None:
        event = DrillEvent(kind=kind, mode=self.mode, index=self._index, total=len(self._items))
        try:
            await self._deps.publish(event.model_copy(update=fields).model_dump_json())
        except Exception:
            log.exception("publishing drill %s failed", kind)

    async def _record(self, item: DrillItem, answer: str, scored: Scored) -> None:
        learner = self._deps.learner
        if not learner:
            return

        expected = item.english if self.mode == LessonMode.TRANSLATE_ES else item.spanish
        attempt = DrillAttempt(
            self.mode.value, self._ask(item), expected, answer, scored.score, scored.details
        )
        words = [(w.word, w.score) for w in scored.words]
        try:
            await learner.record_drill(attempt, words, scored.review)
        except Exception:
            log.exception("recording drill attempt failed")


class TranslateToSpanish(Drill):
    mode = LessonMode.TRANSLATE_EN

    async def _score(self, item: DrillItem, answer: str) -> Scored:
        v = await self._deps.judge.judge(
            item.english, item.spanish, item.target, answer, self._deps.level
        )
        score = round(MEANING_WEIGHT * v.meaning + (1 - MEANING_WEIGHT) * v.form)
        recalled = v.target_used and v.meaning >= GOOD

        if v.meaning >= GOOD and v.form >= GOOD:
            verdict = "¡Muy bien!"
        elif v.meaning >= FAIR and v.corrected.strip().lower() != answer.lower():
            verdict = f"Casi. Mejor: {v.corrected}."
        else:
            verdict = f"Se dice: {item.spanish}"

        target = WordScore(
            word=item.target,
            score=100 if recalled else 0,
            error=WordError.NONE if recalled else WordError.OMITTED,
        )
        rating = fsrs.Rating.Good if recalled else fsrs.Rating.Again
        return Scored(
            score=score,
            verdict=verdict,
            words=[target],
            details=v.model_dump(),
            correction=v.corrected if v.corrected.strip().lower() != answer.lower() else "",
            note=v.note,
            review=(VocabItem(spanish=item.target, english=item.target_english), rating),
        )


class TranslateToEnglish(Drill):
    mode = LessonMode.TRANSLATE_ES

    async def _score(self, item: DrillItem, answer: str) -> Scored:
        # No target word for comprehension: the judge scores meaning only.
        v = await self._deps.judge.judge(item.spanish, item.english, "", answer, self._deps.level)
        if v.meaning >= GOOD:
            verdict = "¡Correcto!"
        elif v.meaning >= FAIR:
            verdict = f"Casi. Significa: {item.english}"
        else:
            verdict = f"Significa: {item.english}"

        error = WordError.NONE if v.meaning >= FAIR else WordError.MISPRONOUNCED
        target = WordScore(word=item.target, score=v.meaning, error=error)
        return Scored(
            score=v.meaning, verdict=verdict, words=[target], details=v.model_dump(), note=v.note
        )


class RepeatAfterMe(Drill):
    """Learner audio is copied from the STT input to Azure; typed answers are not scored."""

    mode = LessonMode.REPEAT

    def __init__(self, deps: DrillDeps) -> None:
        super().__init__(deps)
        self._capture = SpeechCapture()
        self._assessor = deps.assessor or PronunciationAssessor()

    async def on_enter(self) -> None:
        self._capture.attach(self.session)
        await super().on_enter()

    async def on_exit(self) -> None:
        self._capture.detach(self.session)
        await super().on_exit()

    async def stt_node(
        self, audio: AsyncIterable[rtc.AudioFrame], model_settings: ModelSettings
    ) -> AsyncIterable:
        async def tee() -> AsyncIterable[rtc.AudioFrame]:
            async for frame in audio:
                self._capture.push(frame)
                yield frame

        async for event in Agent.default.stt_node(self, tee(), model_settings):
            yield event

    async def _score(self, item: DrillItem, answer: str) -> Scored | None:
        pcm = self._capture.take()
        if pcm is None:
            self.session.say(SAY_IT_ALOUD.format(spanish=item.spanish))
            return None

        p = await self._assessor.assess(pcm, item.spanish)
        weak = sorted((w for w in p.words if w.score < PRON_GOOD), key=lambda w: w.score)
        if p.score >= PRON_GOOD:
            verdict = "¡Muy bien!"
        elif p.score >= PRON_FAIR and weak:
            names = ", ".join(w.word for w in weak[:WORDS_TO_MENTION])
            verdict = f"Bien. Cuidado con: {names}."
        else:
            verdict = f"Escucha bien: {item.spanish}."

        return Scored(score=p.score, verdict=verdict, words=p.words, details=p.model_dump())


DRILLS: dict[LessonMode, type[Drill]] = {
    LessonMode.TRANSLATE_EN: TranslateToSpanish,
    LessonMode.TRANSLATE_ES: TranslateToEnglish,
    LessonMode.REPEAT: RepeatAfterMe,
}
