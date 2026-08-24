# SPDX-License-Identifier: AGPL-3.0-or-later
"""The set the semantic cache's per-model similarity thresholds were measured on.

Kept in the repo so the numbers in embeddings.EMBEDDING_MODELS can be re-derived rather
than trusted, and so a future model added to the registry is calibrated the same way
rather than by guess. tests/test_cache_calibration.py re-runs the derivation.

Written before any measurement, so the thresholds fit the questions rather than the
questions fitting a threshold.

Three classes, because they carry different costs:
  PARAPHRASE — same question, different words. A hit here saves a model call.
  HARD_NEG   — same shape, different answer (negation, entity swap, inverse operation).
               A hit here serves a WRONG answer. This class sets the floor in every
               model measured.
  EASY_NEG   — different topics entirely. Any usable model separates these.
"""

PARAPHRASE = [
    ("how do I create a symbolic link", "what's the command to make a symlink"),
    ("what is the pythagorean theorem", "explain pythagoras' theorem"),
    ("how do I enable TLS on redis", "how to turn on TLS for my redis server"),
    ("what does the fork() syscall do", "explain what fork() does in C"),
    ("how do I list running docker containers", "command to show docker containers that are running"),
    ("what is the capital of France", "which city is France's capital"),
    ("how do I reset my password", "what's the process for resetting a password"),
    ("what is BM25 scoring", "explain how BM25 ranking works"),
    ("how much annual leave do I get", "what is my yearly holiday allowance"),
    ("what are the office opening hours", "when is the office open"),
    ("how do I claim expenses", "what's the expense claim process"),
    ("what is a vector database", "explain what vector databases are"),
    ("how do I rotate an API key", "what's the procedure for rotating api keys"),
    ("what causes a memory leak", "why do memory leaks happen"),
    ("how do I configure nginx as a reverse proxy", "nginx reverse proxy setup"),
    ("what is the difference between TCP and UDP", "TCP vs UDP explained"),
    ("how do I back up a postgres database", "postgres backup command"),
    ("what is OAuth used for", "explain the purpose of OAuth"),
    ("how do I cancel my subscription", "what's the way to cancel a subscription"),
    ("what is the return policy", "how do returns work"),
    ("who do I contact about payroll", "which team handles payroll questions"),
    ("how do I install the CLI", "what are the CLI installation steps"),
    ("what is the maximum upload size", "how large a file can I upload"),
    ("how do I enable two factor authentication", "steps to turn on 2FA"),
    ("what does a 503 error mean", "explain HTTP status 503"),
    ("how do I export my data", "what's the data export procedure"),
    ("what is the SLA for support tickets", "how quickly does support respond"),
    ("how do I change my email address", "steps to update my email"),
    ("what is idempotency in APIs", "explain idempotent API requests"),
    ("how do I run the test suite", "command to execute the tests"),
]

HARD_NEG = [
    ("how do I create a symbolic link", "how do I create a hard link"),
    ("how do I enable TLS on redis", "how do I disable TLS on redis"),
    ("what is the capital of France", "what is the capital of Germany"),
    ("how do I back up a postgres database", "how do I restore a postgres database"),
    ("what is TCP", "what is UDP"),
    ("how much annual leave do I get", "how much sick leave do I get"),
    ("what is BM25 scoring", "what is TF-IDF scoring"),
    ("how do I rotate an API key", "how do I revoke an API key"),
    ("what is the pythagorean theorem", "what is fermat's last theorem"),
    ("how do I list running docker containers", "how do I list docker images"),
    ("how do I enable two factor authentication", "how do I disable two factor authentication"),
    ("how do I export my data", "how do I import my data"),
    ("what is the maximum upload size", "what is the maximum download size"),
    ("how do I install the CLI", "how do I uninstall the CLI"),
    ("what does a 503 error mean", "what does a 403 error mean"),
    ("how do I change my email address", "how do I change my postal address"),
    ("what is the SLA for support tickets", "what is the SLA for billing tickets"),
    ("how do I cancel my subscription", "how do I upgrade my subscription"),
    ("who do I contact about payroll", "who do I contact about recruitment"),
    ("how do I run the test suite", "how do I run the linter"),
    ("show me sales for Q1", "show me sales for Q2"),
    ("what is the price in euros", "what is the price in dollars"),
    ("how do I add a user", "how do I remove a user"),
    ("what changed in version 2.1", "what changed in version 3.1"),
    ("is the service available in the UK", "is the service available in the US"),
]

EASY_NEG = [
    ("what is the capital of France", "how do I back up a postgres database"),
    ("how do I claim expenses", "what is a vector database"),
    ("what causes a memory leak", "what are the office opening hours"),
    ("how do I reset my password", "what is BM25 scoring"),
    ("what is OAuth used for", "how do I cancel my subscription"),
    ("nginx reverse proxy setup", "how much annual leave do I get"),
    ("what does the fork() syscall do", "what is the return policy"),
    ("explain what vector databases are", "when is the office open"),
    ("how do I configure nginx as a reverse proxy", "which city is France's capital"),
    ("what is the difference between TCP and UDP", "what's the expense claim process"),
    ("how do I install the CLI", "what is the SLA for support tickets"),
    ("what does a 503 error mean", "who do I contact about payroll"),
    ("how do I export my data", "what is idempotency in APIs"),
    ("what is the maximum upload size", "explain pythagoras' theorem"),
    ("how do I run the test suite", "what is my yearly holiday allowance"),
]
