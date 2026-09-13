---
name: Telegram recipe bot
description: Telegram bot that scrapes recipes into sqlite and answers "what can I cook with X" through a local model via Ollama; paused since April, resume by fixing the scraper
type: project
weight: medium
created: 2026-03-20
related: decisions.md, feedback_build_not_buy.md
---

A weekend project: a Telegram bot in Python that scrapes recipe pages into a sqlite table (title, ingredients, steps, source URL) and answers ingredient questions by handing the top matches to a local model through Ollama. No cloud API in the loop, by design.

What works: ingestion of about 400 recipes, ingredient search with a small TF-IDF index, the Telegram handler.

Why it is paused: the two main recipe sites changed their markup in April and the scraper returns empty bodies. Resuming means rewriting the two parsers against the new pages and adding a test fixture per site so the next markup change is caught by the suite, not by an empty answer.

Status: paused. Not abandoned.
