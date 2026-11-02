# Simplified Technical English

Write all prose in Simplified Technical English (STE). This covers replies to
the user, messages to peers, commit messages, comments, docs, and prompts to
workers and helpers.

- Put one topic in each sentence. An instruction has 20 words or fewer. A
  description has 25 words or fewer.
- Use the active voice and simple tenses. Write instructions in the imperative.
- Use common words. Do not use idioms, phrasal verbs or slang.
- Use one word for one meaning, and use the same word each time. Do not use
  synonyms for variety.
- Keep articles ("the", "a"). Do not stack nouns into long chains.
- Write steps as a numbered list, with one action in each step. Use a list for
  three or more items.
- Say each point once. Cut flourish and accounts of your own diligence.
- Keep technical names (functions, files, commands, finding ids) as they are.
- When you change older text, rewrite it to this standard.

The user set this rule on 2026-10-18: "everyone needs to use simplified
technical english henceforth". This file is the one source. A session hook
loads it at startup, resume, clear and compaction, and at subagent start. Do
not pass it on to other sessions; each one loads it.
