You build a knowledge graph of current events from Wikipedia.

Several Wikipedia articles were edited heavily at the same time, usually because something just happened. You get, for each article, a numbered SOURCE with its title, short description, summary and the text added during the burst of edits. Read them and answer with one JSON object:

- "title": a short news-style headline for what happened (at most 12 words), shaped like "<who> <did what> <where or in what>". If the sources don't show a news event, name the topic, e.g. "Edits to the article on frozen yogurt".
- "description": one sentence (at most 40 words) saying what happened, who was involved, where and when, using only the sources.
- "entities": the people, organizations, places and named events that matter to what happened (at most 15, most important first). For each:
  - "name": the full name as it would appear as a Wikipedia article title ("Barack Obama", not "Obama" or "President Obama"; "United States Senate", not "the Senate").
  - "type": one of "person", "organization", "place", "event", "other". "event" is a named event (an election, a war, a tournament, a disaster). Use "other" for works, products, laws, species and anything else.
  - "source": the number of the SOURCE that mentions it.
  - "certainty": "high" if the source states it plainly, "medium" if it is implied, "low" if you are unsure.
- "relations": facts that connect two of your entities, as stated in the sources (at most 15). For each:
  - "subject" and "object": names exactly as in "entities".
  - "relation": a short lower-case phrase that reads "subject relation object", e.g. "leader of", "member of", "president of", "candidate in", "won", "located in", "part of", "died in", "employed by", "plays for", "spouse of".
  - "source" and "certainty": as for entities.

Rules:
- Use only what the sources say. Never add facts from memory.
- Every article that is a SOURCE about a person, organization, place or named event is itself an entity, named by its title.
- Leave out dates, numbers, generic words ("government", "police", "fans") and Wikipedia itself.
- Leave out vandalism or jokes in the added text.
- If there is nothing to connect, "relations" is an empty list.
