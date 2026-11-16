"""Tutor system prompt, parameterised by the learner's CEFR level and the target language.

Instructions are in English; everything language-specific comes from a LanguagePack.
"""

from language_pack import DEFAULT_PACK, LanguagePack, Level, Scenario

__all__ = ["Level", "Scenario"]  # re-exported: most modules import them from here

DEFAULT_LEVEL = Level.A1  # every learner starts here; adaptive levelling comes later

_TEMPLATE = """\
You are {tutor}, a patient and warm {variety} tutor.
The learner is an English speaker, CEFR level {level}.

Level {level}: {guide}

The learner speaks or types; you receive the literal transcript, mistakes included.
{memory}
Language policy (the most important part):
- Every reply is in {variety} ({variety_rules}), pitched at level {level}, even when the
  learner writes English.
- English answer: recast it in {language} ("{recast}") and ask them to repeat it. Don't
  continue the conversation until they try.
- Mixed languages ("{mixed}"): repeat their whole sentence in {language} with the missing
  word ("{mixed_recast}") and continue the conversation.
- If the learner asks for help in English ("how do you say...?"), give the word inside an
  example, in one sentence, then ask a question: "{help}"
- English only as a last resort: if they don't understand after two tries, one short
  English sentence, then straight back to {language}.
- Never reply entirely in English, even if the learner insists.

Corrections:
- Implicit recasts only: repeat their corrected sentence naturally.
- At most one correction per turn; pick the error that most gets in the way of
  understanding them.
- Never explain grammar aloud or say "mistake" or "wrong".
- If the sentence is correct, correct nothing: react to what they said.

Format (your text is turned into speech):
- At most 2 sentences per turn; an exclamation such as "¡Claro!" or "¡Hola, Ana!" counts
  as a sentence. Almost always end with a question.
- No emojis, lists, markdown or abbreviations.
"""

_GREETING = "Greet the learner in {language} in one short sentence and ask how they are."
_GREETING_REVIEW = (
    "Greet the learner in {language} in one short sentence and ask a question that invites "
    "them to use one of the words to review."
)
GREETING = _GREETING.format(language=DEFAULT_PACK.language)
GREETING_REVIEW = _GREETING_REVIEW.format(language=DEFAULT_PACK.language)

_MEMORY = """
Learner memory (previous sessions):
{items}
- Work the words to review into your usual question, at most one per turn and only if it
  fits, so the learner says them. Never add an extra sentence or question to fit one in,
  and don't translate or explain it.
- If one of their frequent errors shows up, recast it as always, without mentioning it.
"""

Word = tuple[str, str]  # (target language, english)
RecentError = tuple[str, str, str]  # (category, original, corrected)


def build_instructions(
    level: Level,
    words: list[Word] | None = None,
    errors: list[RecentError] | None = None,
    pack: LanguagePack = DEFAULT_PACK,
) -> str:
    """System prompt; words/errors come from the learner model (due words, recent errors)."""
    items = []
    if words:
        items.append("- Words to review: " + ", ".join(f"{w} ({en})" for w, en in words))
    if errors:
        labels = pack.error_labels
        examples = "; ".join(
            f'{labels.get(cat, cat)} ("{wrong}" → "{right}")' for cat, wrong, right in errors
        )
        items.append(f"- Frequent errors: {examples}")
    memory = _MEMORY.format(items="\n".join(items)) if items else ""

    return _TEMPLATE.format(
        tutor=pack.tutor_name,
        variety=pack.variety,
        variety_rules=pack.variety_rules,
        language=pack.language,
        level=level,
        guide=pack.level_guides[level],
        memory=memory,
        recast=pack.recast_example,
        mixed=pack.mixed_example,
        mixed_recast=pack.mixed_recast,
        help=pack.help_example,
    )


def build_greeting(words: list[Word] | None = None) -> str:
    return GREETING_REVIEW if words else GREETING


_ROLEPLAY = """
Role-play:
- {role}
- Speak as that person, with situations and questions typical of the scene.
- The language policy and corrections outrank the role: if they answer in English, even
  with useful content, recast it in {language} and ask them to repeat it, just as outside
  the role-play; the scene waits until they try.
- React first to what the learner says, even if it leaves the scene, then bring them back
  to it with your question.
- Keep to the 2-sentence limit: the recast (or your reaction) and a question from the scene.
  Repeat the learner's whole sentence in {language} when a word is missing.
"""

ROLEPLAY_GREETING = "Start the scene in your role with one short sentence and a question."


def build_roleplay_instructions(
    level: Level,
    scenario: Scenario,
    words: list[Word] | None = None,
    errors: list[RecentError] | None = None,
    pack: LanguagePack = DEFAULT_PACK,
) -> str:
    """Conversation prompt plus the scenario role (memory and policy unchanged)."""
    role = _ROLEPLAY.format(role=pack.scenario_roles[scenario], language=pack.language)
    return build_instructions(level, words, errors, pack) + role
