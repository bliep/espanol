"""Spots learner turns transcribed in a non-Latin script.

Deepgram's language=multi recognises ~55 languages. Noise, breathing or mumbling is
sometimes transcribed as another language, e.g. Arabic ("مرحبا"). The learner only
speaks Spanish and English, which always contain Latin letters, so such a turn is noise.

  "Ayer fui al mercado"  → keep        "مرحبا كيف حالك" → ignore
  "Sí" / "¿Qué?"         → keep        "Привет"          → ignore
"""

import unicodedata

# Unicode names of Latin letters start with this, e.g. "LATIN SMALL LETTER N WITH TILDE".
LATIN = "LATIN"


def is_foreign_script(text: str) -> bool:
    """True if the text has letters but none of them are Latin."""
    letters = [ch for ch in text if ch.isalpha()]
    if not letters:
        return False  # punctuation or digits only: nothing to judge
    return not any(unicodedata.name(ch, "").startswith(LATIN) for ch in letters)
