"""Everything the tutor needs to know about one target language.

  prompts.py, drill_content.py (English, language-neutral templates)
  drills.py (generic drill flow)            pipeline.py, pronunciation.py (speech)
        ▲ fill from                                 ▲ configure from
  LanguagePack: variety, persona, level guides, examples, error labels, scenarios,
                DrillPack (spoken drill phrases, item hints), Speech (STT/TTS/locale)

Rules and level guides are written in English; examples and spoken phrases are in the target
language, because examples steer the reply language more than rules do.
"""

from dataclasses import dataclass
from enum import StrEnum


class Level(StrEnum):
    A1 = "A1"
    A2 = "A2"
    B1 = "B1"
    B2 = "B2"
    C1 = "C1"


class Scenario(StrEnum):
    RESTAURANT = "restaurant"
    DOCTOR = "doctor"
    JOB_INTERVIEW = "job_interview"
    HOTEL = "hotel"
    DIRECTIONS = "directions"


@dataclass(frozen=True)
class Speech:
    stt_language: str  # Deepgram language code, e.g. "es"
    tts_language: str  # Cartesia language code
    tts_voice: str  # Cartesia voice id
    pronunciation_locale: str  # Azure Pronunciation Assessment locale, e.g. "es-ES"


@dataclass(frozen=True)
class DrillPack:
    """Spoken drill phrases (target language) and hints for the item generator and judge.

    Placeholders: {n} item number, {english}/{sentence} the item's two sides,
    {corrected} the judge's fix, {words} weakest words, {score} round score.
    """

    role: str  # agent instructions; the LLM never speaks in a drill
    intro_to_target: str  # English → target language
    intro_to_english: str
    intro_repeat: str
    ask_to_target: str
    ask_to_english: str
    ask_repeat: str
    say_it_aloud: str  # typed answer in repeat mode
    recap: str
    very_good: str
    almost_better: str
    say_it: str
    correct: str
    almost_means: str
    means: str
    careful_with: str
    listen: str
    # Sounds worth practising in repeat items, e.g. "r/rr, j, ll, ñ, vowels".
    repeat_sounds: str
    # Valid alternatives the judge must not mark as errors.
    judge_alternatives: str


@dataclass(frozen=True)
class LanguagePack:
    language: str  # "Spanish"
    variety: str  # "Spain Spanish (Castilian)"
    variety_rules: str  # dialect choices, e.g. "use vosotros"
    tutor_name: str
    # What "speak at the learner's level" means, per level.
    level_guides: dict[Level, str]
    # Recasting an English answer, e.g. 'Ah, quieres decir: "..."'.
    recast_example: str
    # Mixed-language turn and its full recast.
    mixed_example: str
    mixed_recast: str
    # Answer to "how do you say ...?": word inside an example, then a question.
    help_example: str
    # Error log categories (feedback.py ErrorCategory values) as the tutor reads them.
    error_labels: dict[str, str]
    # Who the tutor plays and who the learner is, per role-play scenario.
    scenario_roles: dict[Scenario, str]
    speech: Speech
    drill: DrillPack


SPANISH = LanguagePack(
    language="Spanish",
    variety="Spain Spanish (Castilian)",
    variety_rules="use vosotros and peninsular vocabulary",
    tutor_name="Lucía",
    level_guides={
        Level.A1: (
            "Very short sentences, presente de indicativo, basic everyday vocabulary. "
            "You may add a key word in English in brackets if needed."
        ),
        Level.A2: (
            "Short sentences; presente, pretérito perfecto and indefinido, ir a + infinitivo. "
            "English only if the learner gets stuck twice."
        ),
        Level.B1: (
            "Natural sentences; all past tenses, futuro, condicional simple, some frequent "
            "subjuntivo. Almost never English."
        ),
        Level.B2: (
            "Natural Spanish with subjuntivo, common idioms and connectors. "
            "No English unless explicitly asked."
        ),
        Level.C1: "Native Spanish with nuance, register and idioms from Spain. Never English.",
    },
    recast_example='Ah, quieres decir: "..."',
    mixed_example="Ayer fui al... store",
    mixed_recast="Ayer fui a la tienda",
    help_example=("Se dice paraguas, por ejemplo: hoy llevo paraguas. ¿Llueve mucho donde vives?"),
    error_labels={
        "ser_estar": "ser/estar",
        "agreement": "gender and number agreement",
        "preterite_imperfect": "indefinido or imperfecto",
        "conjugation": "conjugation",
        "preposition": "prepositions",
        "word_choice": "word choice",
        "other": "other",
    },
    scenario_roles={
        Scenario.RESTAURANT: (
            "You are a waitress in a tapas bar in Madrid. The learner is a customer."
        ),
        Scenario.DOCTOR: "You are a GP at a health centre. The learner is a patient.",
        Scenario.JOB_INTERVIEW: (
            "You are the HR manager of a company in Barcelona. "
            "The learner is a candidate in a job interview."
        ),
        Scenario.HOTEL: "You are a receptionist at a hotel in Seville. The learner is a guest.",
        Scenario.DIRECTIONS: (
            "You are a local in a street in Valencia. The learner is a tourist looking for a place."
        ),
    },
    speech=Speech(
        # Spoken input is Spanish only; English goes through the chat input (pipeline.py).
        stt_language="es",
        tts_language="es",
        tts_voice="538a8872-3799-4df5-b373-b78493b766c6",  # Blanca, Castilian female
        pronunciation_locale="es-ES",
    ),
    drill=DrillPack(
        role="Eres Lucía, tutora de español. Ahora diriges un ejercicio.",
        intro_to_target="Vamos a traducir. Te digo una frase en inglés y tú la dices en español.",
        intro_to_english=(
            "Vamos a traducir. Te digo una frase en español y tú me dices qué significa en inglés."
        ),
        intro_repeat="Vamos a practicar la pronunciación. Repite cada frase después de mí.",
        ask_to_target="Número {n}. ¿Cómo se dice: {english}?",
        ask_to_english="Número {n}. ¿Qué significa: {sentence}?",
        ask_repeat="Número {n}. Repite: {sentence}",
        say_it_aloud="Dilo en voz alta, por favor: {sentence}",
        recap="Fin de la ronda: {score} de cien. Cuando quieras, dime algo y hacemos otra ronda.",
        very_good="¡Muy bien!",
        almost_better="Casi. Mejor: {corrected}.",
        say_it="Se dice: {sentence}",
        correct="¡Correcto!",
        almost_means="Casi. Significa: {english}",
        means="Significa: {english}",
        careful_with="Bien. Cuidado con: {words}.",
        listen="Escucha bien: {sentence}.",
        repeat_sounds="r/rr, j, ll, ñ, vowels",
        judge_alternatives=(
            "Latin American forms, an article or possessive instead of the model's (el/mi), "
            "pronominal verbs (me llevo)"
        ),
    ),
)

DEFAULT_PACK = SPANISH  # until learners choose a language
