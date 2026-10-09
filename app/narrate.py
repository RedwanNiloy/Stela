"""Turn collected facts into a spoken-style story.

The LLM only ever sees the facts that geo.py collected. If the LLM fails for any
reason, we return a plain templated summary built in code (mode "facts-only"),
so the user never sees an error screen.
"""
import json
import os
import re

import httpx

LLM_TIMEOUT = float(os.environ.get("LLM_TIMEOUT", "30"))  # seconds; raise it only for slow local CPUs
TEMPERATURE = 0.3
MAX_TOKENS = 400

# The grounding rules come first and are the same for every voice.
GROUNDING_RULES = """STRICT RULES (these override everything else):
1. Use ONLY the facts in the JSON below. Never add species, dates, places, people, cultures or events that are not in it.
2. If a section is an empty list or empty, say plainly "nothing recorded nearby". Never fill the gap.
3. If a section is null, that data source could not be reached. Say it could not be checked right now. Do not guess.
4. Fossils show only what has been FOUND near here, not everything that lived here. Say so.
5. The setting (sea, land, swamp) is inferred from the rock type. Always say "probably".
6. Do not claim anything about any culture (Egyptian or otherwise) unless it appears in the facts.
7. Write species names exactly as given. Never translate them into common names, and never add anything about
   who built, used or lived at a place beyond what its summary says.
8. Animal records are NOT proof that an animal lives or is alive. Never say the animals are alive, live here,
   roam, wander, can be seen or inhabit the area. Say only that records or sightings exist. If some records are
   marked dead or are museum specimens, say so. Always add that a record shows where an animal was observed,
   not necessarily where it lives.
9. The "scopes" section says how far from the walker each section's facts really come from. Whenever a scope's
   level is not "exact_area", say so in plain words using its note (for example "these are about Dhaka, the
   nearest city") and describe that wider place or distance. NEVER claim those facts happened at, or are under,
   the walker's exact spot. For level "none", say "nothing recorded". For a coarser map, say the rock is a broader grouping.
10. Copy numbers, distances and ages exactly as written in the facts (say "1,658 times", not "sixteen thousand times").
    Never convert units or round them differently.
"""

VOICE = """VOICE (tone only; the rules above still apply exactly):
You are a calm stone scribe speaking aloud to a walker standing on this spot. Begin by asking them to
stand still, for example "Stand still. The ground remembers..." Plain, warm, easy to hear. No lists,
no headings, no markdown."""

MAIN_TASK = """TASK: In under 150 words, tell the walker: 1) the rock under their feet and how old it is
(compare with humans using time_ruler), 2) what this place probably was then, 3) human history (or the wider place it is about, if the scope says so),
4) rare animals RECORDED nearby (records only; mention the caveat). Do not mention fossils; they are shown separately."""

FOSSIL_TASK = """TASK: In 2 to 4 short sentences, continue the story for the same walker (do not greet them again).
Say how far away the fossils were found, using the fossils scope note (never say they were found "here" or "under your
feet" unless the level is exact_area), then name the classes given, and remind them this is only what has been found
so far, not everything that lived there. If the fossils scope level is "none", just say nothing is recorded."""


def _rare_for_llm(r):
    out = {"species": r["species"], "records": r["records"]}
    if r.get("datasets"):
        out["dataset"] = r["datasets"][0]
    for k in ("marked_dead", "specimen_records"):
        if r.get(k):
            out[k] = r[k]
    return out


def llm_view(facts):
    """A smaller copy of the facts for the model: fewer tokens, so faster and less to get wrong."""
    view = {k: v for k, v in facts.items() if v != "pending" and k != "scope"}
    if facts.get("scope"):  # how far from the walker each section's facts come from
        view["scopes"] = {k: {"level": v["level"], "note": v["note"]} for k, v in facts["scope"].items() if v}
    if isinstance(view.get("rock"), dict):
        view["rock"] = {k: v for k, v in view["rock"].items() if v not in (None, "") and k != "descrip"}
    if view.get("human_history_nearby"):
        view["human_history_nearby"] = [{**h, "summary": h["summary"][:240]} for h in view["human_history_nearby"]]
    if view.get("rare_animals_recorded_nearby"):
        view["rare_animals_recorded_nearby"] = [_rare_for_llm(r) for r in view["rare_animals_recorded_nearby"]]
    return view


def build_prompt(facts, part):
    task = MAIN_TASK if part == "main" else FOSSIL_TASK
    facts_json = json.dumps(llm_view(facts), ensure_ascii=False, separators=(",", ":"))
    return f"{GROUNDING_RULES}\n{VOICE}\n\n{task}\n\nFACTS:\n{facts_json}\n"


# ---------- LLM call ----------
def llm_config():
    base = os.environ.get("LLM_BASE_URL", "").strip()
    if base:
        return {"mode": "online", "base": base, "key": os.environ.get("LLM_API_KEY", ""),
                "model": os.environ.get("LLM_MODEL", "")}
    return {"mode": "offline", "base": os.environ.get("OLLAMA_URL", "http://localhost:11434"),
            "model": os.environ.get("OLLAMA_MODEL", "gemma3:4b")}


async def call_llm(client, prompt):
    """Returns (text, mode). Raises on any failure."""
    cfg = llm_config()
    if not cfg["model"]:
        raise RuntimeError("LLM_MODEL is not set")
    messages = [{"role": "user", "content": prompt}]  # one user message: not every Gemma server accepts a system role
    if cfg["mode"] == "online":
        headers = {"Authorization": f"Bearer {cfg['key']}"} if cfg["key"] else {}
        r = await client.post(cfg["base"].rstrip("/") + "/chat/completions", headers=headers,
                              timeout=LLM_TIMEOUT,
                              json={"model": cfg["model"], "messages": messages,
                                    "temperature": TEMPERATURE, "max_tokens": MAX_TOKENS})
        r.raise_for_status()
        text = r.json()["choices"][0]["message"]["content"]
    else:
        r = await client.post(cfg["base"].rstrip("/") + "/api/chat", timeout=LLM_TIMEOUT,
                              json={"model": cfg["model"], "messages": messages, "stream": False, "think": False,  # thinking models would spend the budget reasoning
                                    "options": {"temperature": TEMPERATURE, "num_predict": MAX_TOKENS}})
        r.raise_for_status()
        text = r.json()["message"]["content"]
    text = (text or "").strip()
    if not text:
        raise RuntimeError("empty LLM reply")
    return text, cfg["mode"]


# ---------- Fallback: plain text built in code ----------
def _ma(x):
    return f"{x:g}"


def scope_note(facts, layer):
    """The plain-words scope of a layer, or '' when it describes the walker's own area."""
    sc = (facts.get("scope") or {}).get(layer)
    return sc["note"] if sc and sc["level"] != "exact_area" else ""


def facts_only_text(facts, part="main"):
    if part == "fossils":
        f = facts.get("fossils_found_nearby")
        note = ((facts.get("scope") or {}).get("fossils") or {}).get("note", "")
        if f is None:
            return "The fossil database could not be reached right now."
        if not f:
            return "Fossils: " + (note or "nothing recorded nearby.")
        classes = ", ".join(c for c, _ in f["classes"][:4]) or "various groups"
        return (f"Fossils: {f['count']} finds, mostly {classes}. {note} "
                "This shows only what has been found so far, not everything that lived here.")
    parts = []
    rock = facts.get("rock")
    if rock is None:
        parts.append("No mapped rock unit was found for this spot.")
    else:
        what = rock.get("name") or "an unnamed rock unit"
        sentence = f"The ground here is mapped as {what}"
        if rock.get("lith"):
            sentence += f" ({rock['lith']})"
        if rock.get("b_age") is not None:
            sentence += f", which began forming about {_ma(rock['b_age'])} million years ago"
            if rock.get("b_int_name"):
                sentence += f" in the {rock['b_int_name']}"
            ruler = facts.get("time_ruler")
            if ruler:
                sentence += f". That is roughly {ruler['times_older_than_humans']:,} times older than our species"
        parts.append(sentence + ".")
        if scope_note(facts, "rock"):
            parts.append(scope_note(facts, "rock"))
        s = facts.get("ancient_setting")
        if s and s.get("meaning"):
            parts.append(f"From the rock type, this place was probably {s['meaning']} (an inference, not a certainty).")
    human = facts.get("human_history_nearby")
    if human is None:
        parts.append("Human history could not be checked right now.")
    elif not human:
        parts.append("Human history: " + (scope_note(facts, "human") or "nothing recorded nearby."))
    elif scope_note(facts, "human"):  # a wider place, not the walker's spot: say so before the text
        parts.append(f"{scope_note(facts, 'human')} {human[0]['summary']}")
    else:
        parts.append("Human history nearby: " + "; ".join(f"{h['place']} ({h['distance_m']} m)" for h in human[:3]) + ".")
    rare = facts.get("rare_animals_recorded_nearby")
    if rare is None:
        parts.append("Rare animal records could not be checked right now.")
    elif not rare:
        parts.append("Rare animals recorded nearby: " + (scope_note(facts, "rare") or "nothing recorded nearby."))
    else:
        parts.append("Rare animals recorded nearby: " + ", ".join(r["species"] for r in rare[:4]) + ". "
                     + scope_note(facts, "rare") + " " + facts.get("rare_animals_note", ""))
    return " ".join(p.replace("  ", " ") for p in parts)


# Safety net for rule 8: small models sometimes ignore it. If the story claims the animals are alive or live
# here, we drop it and use the plain facts summary instead. (Fossil text is exempt: "everything that lived here".)
ALIVE_CLAIM = re.compile(r"(?<!\w)(alive|living|lives|roam\w*|wander\w*|inhabit\w*|dwell\w*|still here)(?!\w)", re.I)


# The required caveat itself says "...not necessarily where it lives", so that phrase must not trip the check.
CAVEAT_PHRASE = re.compile(r"where (?:it|they|an animal|the animal|the animals|animals) (?:lives?|may live|might live)", re.I)


def claims_animals_alive(text):
    return bool(ALIVE_CLAIM.search(CAVEAT_PHRASE.sub("", text)))


async def narrate(client, facts, part="main"):
    """Returns (text, mode); mode is 'online', 'offline' or 'facts-only'. Never raises."""
    nothing_to_say = part == "main" and facts.get("rock") is None and not facts.get("human_history_nearby") \
        and not facts.get("rare_animals_recorded_nearby")
    if nothing_to_say:  # with no facts, a template is safer than asking a model to talk
        return facts_only_text(facts, part), "facts-only"
    try:
        text, mode = await call_llm(client, build_prompt(facts, part))
        if part == "main" and claims_animals_alive(text):
            return facts_only_text(facts, part), "facts-only"
        return text, mode
    except Exception:
        return facts_only_text(facts, part), "facts-only"
