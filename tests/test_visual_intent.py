# SPDX-License-Identifier: AGPL-3.0-or-later
"""Which questions are excluded from the semantic cache.

A visual request is excluded because its answer carries a generated chart that must not
be replayed for a different question. The rule that did that was a bare keyword list,
and for a Redis product "graph", "plot" and "diagram" are ordinary domain vocabulary:
measured on 20 plain questions it blocked 11 of them — "what is graph theory", "explain
knowledge graphs in Redis", "what is the plot of Hamlet" — while missing 6 of 17 real
requests. Every one of those turns paid a full LLM call it never needed to.

Both directions are cheap to get wrong and neither is catastrophic, so the table below is
the specification: a false positive costs a cache miss, a false negative risks replaying
a chart for a question that wanted a different one.
"""
import pytest

from visualweaver.cache import wants_visual

# ── things the user wants to SEE ─────────────────────────────────────────────
VISUAL = [
    "Visualize the theorem of Pythagoras",      # the query in the original report
    "plot y = x^2",
    "draw a diagram of the TCP handshake",
    "chart of CO2 emissions 1990-2020",
    "show me a bar chart of sales by region",
    "Sugar molecule in 2d and 3d",              # no verb at all
    "render f(x) = sin(x)",
    "create a pie chart of usage",
    "graph the fibonacci sequence",
    "illustrate the OSI model",
    "sketch the architecture",
    "make me a histogram of latencies",
    "visualise the dependency tree",            # British spelling
    "generate an svg of the logo",
    "animate the sorting algorithm",
    "picture of a sierpinski triangle",
    "diagram knowledge graphs in redis",        # imperative, same nouns as the question below
    "Can you draw a flowchart of the login flow?",
    "please plot the latency percentiles",
    # follow-up phrasings a real conversation uses — an independent review found the
    # first rewrite missed all of these, and a missed visual request is the direction
    # that can replay a stale chart
    "turn that into a flowchart",
    "same thing but as a pie chart",
    "what would this look like as a chart?",
    "how about a bar chart of that?",
    "an image showing the topology",
]

# ── things that merely MENTION a picture ─────────────────────────────────────
INFORMATIONAL = [
    "what is graph theory",
    "explain knowledge graphs in Redis",
    "how do I read a flame graph",
    "what is the plot of Hamlet",
    "solve y = 3x + 2 for x",
    "what is a directed acyclic graph",
    "who invented the bar chart",
    "describe the diagram conventions in UML",
    "what does f(x) mean in maths",
    "summarise the org chart changes",
    "is RedisGraph still supported",
    "what is a symbolic link",
    "explain the fork() syscall",
    "what is a hausdorff dimension",
    "are there other similar fractals",
    "how does BM25 scoring work",
    "list the supported file types",
    "when was Redis 7 released",
    "compare the pie chart and bar chart conventions",
    # idioms that borrow a drawing verb — each is a fixed collocation, not a keyword
    "draw your own conclusions",
    "draw up a plan for migrating the index",
    "plot summary of the book please",
    "render the template with these variables",
    "draw a parallel between the two designs",
]


@pytest.mark.parametrize("q", VISUAL)
def test_a_request_to_see_something_is_excluded_from_the_cache(q):
    assert wants_visual(q) is True, q


@pytest.mark.parametrize("q", INFORMATIONAL)
def test_a_question_that_merely_names_a_picture_is_cacheable(q):
    assert wants_visual(q) is False, q


def test_the_two_sets_are_separated_better_than_the_keyword_rule_managed():
    """Held as a number so a future 'small tweak' cannot quietly regress it: the keyword
    rule scored 11 false positives and 6 misses on these same sets."""
    fp = [q for q in INFORMATIONAL if wants_visual(q)]
    fn = [q for q in VISUAL if not wants_visual(q)]
    assert len(fp) == 0, fp
    assert len(fn) == 0, fn


def test_the_same_nouns_read_differently_as_a_question_and_as_an_order():
    """The distinction the keyword list could not draw."""
    assert wants_visual("diagram knowledge graphs in redis") is True
    assert wants_visual("explain knowledge graphs in Redis") is False


def test_an_empty_or_missing_query_is_not_a_drawing_request():
    for q in ("", None, "   "):
        assert wants_visual(q) is False


def test_the_rule_does_not_backtrack_catastrophically():
    """It runs on every chat turn, and the query is user input."""
    import time
    t = time.time()
    wants_visual("plot " + "a " * 25_000)
    assert time.time() - t < 1.0


def test_a_known_idiom_errs_towards_not_caching():
    """"plot a course" is not a chart. It costs one cache miss, which is the harmless
    direction — recorded so the behaviour is deliberate rather than unnoticed."""
    assert wants_visual("how do I plot a course through the docs") is True


# ── trademarks ───────────────────────────────────────────────────────────────
def test_no_imitation_of_the_redis_visual_identity_ships():
    """Redis Ltd.'s trademark policy grants community projects use of the NAME and no
    logo rights at all, and says explicitly: "Don't imitate our visual identity". The
    Settings panel carried a hand-drawn stacked cube in Redis red — not their logo, but
    near enough to evoke it, which is the part a mark protects."""
    import pathlib
    html = pathlib.Path("visualweaver/index.html").read_text(encoding="utf-8")
    i = html.index('id="tab-redis"')
    banner = html[i:i + 1600]
    assert "<svg" not in banner, "a mark came back into the Redis panel"
    assert "polygon" not in banner


def test_the_non_endorsement_notice_is_present_where_redis_is_named():
    """The community permission is conditional on making it clear the project is not
    endorsed — so the notice is part of the licence to use the name, not decoration."""
    import pathlib
    html = pathlib.Path("visualweaver/index.html").read_text(encoding="utf-8")
    i = html.index('id="tab-redis"')
    banner = html[i:i + 1600]
    assert "registered trademark of Redis Ltd" in banner
    assert "not endorsed" in banner

    readme = pathlib.Path("README.md").read_text(encoding="utf-8")
    assert "## Trademarks" in readme
    assert "not endorsed, supported, sponsored, or certified by" in readme
    for owner in ("Docker", "GitHub", "OpenAI", "Mistral", "Ollama", "Groq", "Qwen"):
        assert owner in readme.split("## Trademarks")[1].split("## License")[0], owner
