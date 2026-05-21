"""
headline_translate.py

Translate GDELT story headlines regardless of source language.
The translator auto-detects the headline language.

Install:
    pip install deep-translator

Usage:
    from headline_translate import translate_story_headlines

    translated = translate_story_headlines(stories)

    for s in translated:
        print(s.title)
        print("->", s.translated_title)
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Iterable

from deep_translator import GoogleTranslator


@dataclass
class TranslatedHeadline:
    original_title: str
    translated_title: str
    detected_language: str


def detect_and_translate(
    text: str,
    target_language: str = "en",
) -> TranslatedHeadline:
    """
    Auto-detect the source language and translate to target_language.
    """
    if not text.strip():
        return TranslatedHeadline(
            original_title=text,
            translated_title=text,
            detected_language="unknown",
        )

    try:
        translator = GoogleTranslator(source="auto", target=target_language)
        translated = translator.translate(text)
    except Exception:
        return TranslatedHeadline(
            original_title=text,
            translated_title=text,
            detected_language="unknown",
        )

    # Language detection is best-effort — recent deep-translator releases
    # have made GoogleTranslator().detect() unreliable, so a failure here
    # must not throw away the successful translation above.
    detected = "unknown"
    try:
        result = GoogleTranslator().detect(text)
        if isinstance(result, (list, tuple)) and result:
            detected = str(result[0])
        elif result:
            detected = str(result)
    except Exception:
        pass

    # If the translator returned nothing useful, fall back to the original
    # so the caller can still render something.
    if not translated or not str(translated).strip():
        translated = text

    return TranslatedHeadline(
        original_title=text,
        translated_title=str(translated),
        detected_language=detected,
    )


def translate_story_headlines(
    stories: Iterable,
    target_language: str = "en",
    delay: float = 0.25,
):
    """
    Adds translated_title + detected_language fields to stories.

    Works with your existing Story objects.
    """

    translated_stories = []

    for story in stories:
        result = detect_and_translate(
            story.title,
            target_language=target_language,
        )

        # Attach fields dynamically
        story.translated_title = result.translated_title
        story.detected_language = result.detected_language

        translated_stories.append(story)

        # avoid hammering translation service
        time.sleep(delay)

    return translated_stories
