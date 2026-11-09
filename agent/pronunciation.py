"""Pronunciation scoring with Azure Speech Pronunciation Assessment (es-ES).

The only module that talks to the Azure Speech SDK. Callers pass 16-bit mono PCM and
the reference sentence; they get word scores back.

  pcm ──► push stream ──► continuous recognition (one result per spoken segment)
                              │  per segment: recognized words with accuracy 0–100
                              ▼
          align recognized words to the reference (difflib), like Azure's
          continuous-assessment sample:
            "Hoy llevo el paraguas" vs heard "hoy llevo paraguas"
            → hoy 100, llevo 98, el 0 (omitted), paraguas 100

Continuous mode (not recognize_once) so a pause mid-sentence does not cut the answer off.
"""

import asyncio
import difflib
import logging
import os
import re
import threading
import unicodedata
from enum import StrEnum

import azure.cognitiveservices.speech as speechsdk
from pydantic import BaseModel

from costs import CostMeter

log = logging.getLogger("tutor.pronunciation")

LOCALE = "es-ES"  # target variety (CLAUDE.md)
SAMPLE_RATE = 16_000  # what we push; callers resample to this
BYTES_PER_SECOND = SAMPLE_RATE * 2  # 16-bit mono
RECOGNITION_TIMEOUT_S = 30.0
KEY_ENV = "AZURE_SPEECH_KEY"
REGION_ENV = "AZURE_SPEECH_REGION"


class WordError(StrEnum):
    NONE = "none"
    MISPRONOUNCED = "mispronounced"
    OMITTED = "omitted"


class WordScore(BaseModel):
    word: str  # as written in the reference, without punctuation
    score: float  # accuracy 0–100; 0 when omitted
    error: WordError


class PronunciationScore(BaseModel):
    score: float  # mean word score, omissions count as 0
    completeness: float  # % of reference words spoken
    fluency: float  # Azure fluency 0–100 (mean over segments)
    heard: str  # what Azure recognised
    words: list[WordScore]


MISPRONOUNCED_BELOW = 60  # word accuracy under this is shown as an error


class PronunciationAssessor:
    def __init__(
        self, key: str | None = None, region: str | None = None, meter: CostMeter | None = None
    ) -> None:
        self._config = speechsdk.SpeechConfig(
            subscription=key or os.environ[KEY_ENV], region=region or os.environ[REGION_ENV]
        )
        self._meter = meter

    async def assess(self, pcm: bytes, reference: str) -> PronunciationScore:
        """pcm: 16 kHz, 16-bit, mono. Runs the blocking SDK call in a worker thread."""
        if self._meter:
            self._meter.add_azure(len(pcm) / BYTES_PER_SECOND)
        segments = await asyncio.to_thread(self._recognize, pcm, reference)
        return _score(reference, segments)

    def _recognize(self, pcm: bytes, reference: str) -> list[speechsdk.SpeechRecognitionResult]:
        fmt = speechsdk.audio.AudioStreamFormat(
            samples_per_second=SAMPLE_RATE, bits_per_sample=16, channels=1
        )
        stream = speechsdk.audio.PushAudioInputStream(fmt)
        stream.write(pcm)
        stream.close()  # end of stream ends the session

        recognizer = speechsdk.SpeechRecognizer(
            self._config,
            language=LOCALE,
            audio_config=speechsdk.audio.AudioConfig(stream=stream),
        )
        # Miscue detection off: segments are aligned to the full reference below.
        speechsdk.PronunciationAssessmentConfig(
            reference_text=reference,
            grading_system=speechsdk.PronunciationAssessmentGradingSystem.HundredMark,
            granularity=speechsdk.PronunciationAssessmentGranularity.Word,
            enable_miscue=False,
        ).apply_to(recognizer)

        results: list[speechsdk.SpeechRecognitionResult] = []
        done = threading.Event()

        def on_recognized(evt: speechsdk.SpeechRecognitionEventArgs) -> None:
            if evt.result.reason == speechsdk.ResultReason.RecognizedSpeech:
                results.append(evt.result)

        def on_canceled(evt: speechsdk.SpeechRecognitionCanceledEventArgs) -> None:
            if evt.cancellation_details.reason == speechsdk.CancellationReason.Error:
                log.error("azure canceled: %s", evt.cancellation_details.error_details)
            done.set()

        recognizer.recognized.connect(on_recognized)
        recognizer.canceled.connect(on_canceled)
        recognizer.session_stopped.connect(lambda _: done.set())

        recognizer.start_continuous_recognition()
        if not done.wait(RECOGNITION_TIMEOUT_S):
            log.warning("azure recognition timed out")
        recognizer.stop_continuous_recognition()
        return results


def _score(reference: str, segments: list[speechsdk.SpeechRecognitionResult]) -> PronunciationScore:
    heard: list[tuple[str, float]] = []  # (normalized word, accuracy)
    fluency: list[float] = []
    for result in segments:
        assessment = speechsdk.PronunciationAssessmentResult(result)
        fluency.append(assessment.fluency_score)
        heard += [(_normalize(w.word), w.accuracy_score) for w in assessment.words]

    ref_words = re.findall(r"[\wáéíóúüñÁÉÍÓÚÜÑ]+", reference)
    words = _align(ref_words, heard)

    spoken = sum(w.error != WordError.OMITTED for w in words)
    return PronunciationScore(
        score=_mean([w.score for w in words]),
        completeness=100 * spoken / len(words) if words else 0,
        fluency=_mean(fluency),
        heard=" ".join(result.text for result in segments),
        words=words,
    )


def _align(ref_words: list[str], heard: list[tuple[str, float]]) -> list[WordScore]:
    """Reference words with the accuracy of the heard word matched to each."""
    ref_norm = [_normalize(w) for w in ref_words]
    matcher = difflib.SequenceMatcher(a=ref_norm, b=[w for w, _ in heard], autojunk=False)

    scores: list[WordScore] = []
    for op, a0, a1, b0, b1 in matcher.get_opcodes():
        if op == "insert":
            continue  # extra words the learner said: not scored

        for offset, ref in enumerate(ref_words[a0:a1]):
            if op == "delete" or b0 + offset >= b1:
                scores.append(WordScore(word=ref, score=0, error=WordError.OMITTED))
                continue

            # equal: same word; replace: a different word was heard in its place
            score = heard[b0 + offset][1] if op == "equal" else min(heard[b0 + offset][1], 50)
            error = WordError.MISPRONOUNCED if score < MISPRONOUNCED_BELOW else WordError.NONE
            scores.append(WordScore(word=ref, score=score, error=error))
    return scores


def _normalize(word: str) -> str:
    decomposed = unicodedata.normalize("NFD", word.lower())
    return "".join(ch for ch in decomposed if unicodedata.category(ch) != "Mn" and ch.isalnum())


def _mean(values: list[float]) -> float:
    return round(sum(values) / len(values), 1) if values else 0.0
