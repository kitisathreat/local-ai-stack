"""
title: Dictionary & Thesaurus
author: local-ai-stack
description: Look up definitions, pronunciation, etymology, synonyms, and antonyms using the Free Dictionary API and Datamuse. No API key required.
required_open_webui_version: 0.4.0
requirements: httpx
version: 1.0.0
licence: MIT
"""

import httpx
from pydantic import BaseModel, Field
from typing import Callable, Any, Optional


DICT_API     = "https://api.dictionaryapi.dev/api/v2/entries/en"
DATAMUSE_API = "https://api.datamuse.com/words"


class Tools:
    class Valves(BaseModel):
        pass

    def __init__(self):
        self.valves = self.Valves()

    async def define(
        self,
        word: str,
        __event_emitter__: Callable[[dict], Any] = None,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Get the definition, pronunciation, part of speech, and example sentences for a word.
        :param word: English word to look up (e.g. "ephemeral", "serendipity", "algorithm")
        :return: All definitions, pronunciations, and usage examples
        """
        word = word.strip().lower()

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(f"{DICT_API}/{word}")
                if resp.status_code == 404:
                    return f"Word not found: '{word}'. Check spelling or try a related word."
                resp.raise_for_status()
                data = resp.json()

            if not data:
                return f"No definition found for: {word}"

            entry = data[0]
            phonetics = entry.get("phonetics", [])
            phone_text = next((p.get("text", "") for p in phonetics if p.get("text")), "")
            audio_url = next((p.get("audio", "") for p in phonetics if p.get("audio")), "")

            lines = [f"## {entry.get('word', word).title()}"]
            if phone_text:
                lines.append(f"*{phone_text}*")
            lines.append("")

            for meaning in entry.get("meanings", []):
                pos = meaning.get("partOfSpeech", "")
                lines.append(f"**{pos}**")
                for i, defn in enumerate(meaning.get("definitions", [])[:3], 1):
                    definition = defn.get("definition", "")
                    example = defn.get("example", "")
                    synonyms = defn.get("synonyms", [])[:4]
                    antonyms = defn.get("antonyms", [])[:3]

                    lines.append(f"{i}. {definition}")
                    if example:
                        lines.append(f"   *\"{example}\"*")
                    if synonyms:
                        lines.append(f"   Synonyms: {', '.join(synonyms)}")
                    if antonyms:
                        lines.append(f"   Antonyms: {', '.join(antonyms)}")
                lines.append("")

            if audio_url:
                lines.append(f"🔊 Pronunciation: {audio_url}")

            # Etymology if available
            etymology = entry.get("origin", "")
            if etymology:
                lines.append(f"\n**Etymology:** {etymology}")

            return "\n".join(lines)

        except httpx.ConnectError:
            return "Cannot reach Dictionary API. Check internet connection."
        except Exception as e:
            return f"Dictionary error: {str(e)}"

    async def synonyms(
        self,
        word: str,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Get synonyms, related words, and words with similar meaning.
        :param word: Word to find synonyms for (e.g. "happy", "fast", "important")
        :return: Synonyms grouped by strength of similarity with scores
        """
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                # Synonyms (means like)
                syn_resp = await client.get(
                    DATAMUSE_API,
                    params={"ml": word, "max": 15, "md": "f"},
                )
                # Sounds like
                rhy_resp = await client.get(
                    DATAMUSE_API,
                    params={"rel_syn": word, "max": 10},
                )
                syn_resp.raise_for_status()
                synonyms = syn_resp.json()
                related = rhy_resp.json() if rhy_resp.status_code == 200 else []

            if not synonyms and not related:
                return f"No synonyms found for: {word}"

            lines = [f"## Synonyms for: {word}\n"]
            if synonyms:
                lines.append("**Most similar words:**")
                lines.append(", ".join(w.get("word", "") for w in synonyms[:12]))
            if related:
                lines.append("\n**Direct synonyms:**")
                lines.append(", ".join(w.get("word", "") for w in related[:8]))
            return "\n".join(lines)

        except Exception as e:
            return f"Synonyms error: {str(e)}"

    async def rhymes(
        self,
        word: str,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Find words that rhyme with a given word.
        :param word: Word to find rhymes for (e.g. "orange", "silver", "cat")
        :return: List of rhyming words
        """
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(DATAMUSE_API, params={"rel_rhy": word, "max": 20})
                resp.raise_for_status()
                results = resp.json()

            if not results:
                # Try near-rhymes
                async with httpx.AsyncClient(timeout=10) as client:
                    resp = await client.get(DATAMUSE_API, params={"rel_nry": word, "max": 20})
                    results = resp.json() if resp.status_code == 200 else []
                if not results:
                    return f"No rhymes found for: '{word}' (some words have no perfect rhymes)"

            rhyme_words = [r.get("word", "") for r in results if r.get("word")]
            return f"## Words that rhyme with '{word}':\n" + ", ".join(rhyme_words)

        except Exception as e:
            return f"Rhymes error: {str(e)}"

    async def word_associations(
        self,
        word: str,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Find words commonly associated with or following a given word.
        :param word: Word or phrase to find associations for (e.g. "coffee", "machine learning", "ocean")
        :return: Words frequently associated with the input
        """
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                # Triggered by / associated with
                assoc = await client.get(DATAMUSE_API, params={"rel_trg": word, "max": 15})
                # Frequently follows
                follows = await client.get(DATAMUSE_API, params={"lc": word, "max": 10})

                assoc_words = [w.get("word", "") for w in (assoc.json() if assoc.status_code == 200 else [])]
                follow_words = [w.get("word", "") for w in (follows.json() if follows.status_code == 200 else [])]

            lines = [f"## Word Associations: {word}\n"]
            if assoc_words:
                lines.append(f"**Triggered by '{word}':** {', '.join(assoc_words)}")
            if follow_words:
                lines.append(f"**Words that often follow '{word}':** {', '.join(follow_words)}")
            if not assoc_words and not follow_words:
                return f"No associations found for: {word}"
            return "\n".join(lines)

        except Exception as e:
            return f"Word associations error: {str(e)}"
