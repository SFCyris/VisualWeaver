# SPDX-License-Identifier: AGPL-3.0-or-later
"""Measured advice for the RAG controls.

Every verdict here is derived from what this deployment actually observed — the
distribution of pre-threshold retrieval scores, and which chunk ranks answers really
cited. Nothing is compared against a table of "recommended" values, because there is no
value that is right for every corpus: a similarity threshold of 0.9 is sensible on a
corpus that scores 0.85-0.95 and switches vector search off entirely on one that scores
0.60-0.75.

Where there is no measurement, the verdict is ``unknown`` and the UI shows no colour.
A setting honestly marked "not enough data yet" is useful; one coloured from a shipped
constant is the same mistake as a hardcoded price list — authoritative-looking, and
wrong the moment the deployment differs from the author's.
"""
import logging
import math

from . import constants, state

log = logging.getLogger("visualweaver")

# Below this many queries the distribution is too thin to read a percentile off.
MIN_QUERIES = 20
# ...and below this many cited answers, "which ranks get used" is noise.
MIN_CITED_ANSWERS = 10
# A threshold recommendation aims to keep this share of queries producing a vector hit.
TARGET_PASS = 0.85
# Ranks beyond the cited working set, kept as headroom before calling top_k too high.
TOPK_HEADROOM = 2

_BUCKET = 1.0 / state.RAW_HIST_BUCKETS

# The diff modal asks the user to confirm a change; it should name the controls the way
# the panel labels them, not the way config.json spells them.
_LABELS = {
    "rag.similarity_threshold": "Similarity Threshold", "rag.top_k": "Top-K Chunks",
    "rag.hybrid_search": "Hybrid Search", "rag.rerank_candidates": "Candidates to score",
    "reranker.enabled": "Rerank retrieved chunks", "reranker.top_n": "Chunks to keep",
    "history_max_tokens": "Chat History Budget", "rag.chunk_size": "Chunk Size",
    "rag.chunk_overlap": "Chunk Overlap",
}


def _totals(stats: dict) -> dict:
    """Sum the per-instance counters into one deployment-wide view."""
    out = {"queries": 0, "hits": 0, "answers_with_citations": 0, "raw_score_sum": 0.0,
           "errors": 0, "scored": 0, "raw_sample": [],
           "raw_hist": [0] * state.RAW_HIST_BUCKETS,
           "cited_hist": [0] * state.CITED_HIST_RANKS}
    # A snapshot: these counters are mutated from executor threads while this runs, and
    # iterating the live dict raises "dictionary changed size during iteration".
    for s in list((stats or {}).values()):
        if not isinstance(s, dict):
            continue
        out["queries"] += int(s.get("queries") or 0)
        out["hits"] += int(s.get("hits") or 0)
        out["answers_with_citations"] += int(s.get("answers_with_citations") or 0)
        out["raw_score_sum"] += float(s.get("raw_score_sum") or 0.0)
        out["errors"] += int(s.get("errors") or 0)
        out["scored"] += int(s.get("scored") or 0)
        samp = s.get("raw_sample")
        if isinstance(samp, list):
            out["raw_sample"].extend(float(x) for x in samp
                                     if isinstance(x, (int, float)))
        for i, n in enumerate(s.get("raw_hist") or []):
            if i < state.RAW_HIST_BUCKETS:
                out["raw_hist"][i] += int(n or 0)
        for i, n in enumerate(s.get("cited_hist") or []):
            if i < state.CITED_HIST_RANKS:
                out["cited_hist"][i] += int(n or 0)
    return out


def pass_rate_exact(sample: list[float], threshold: float) -> float | None:
    """Share of sampled scores at or above ``threshold`` — the runtime predicate itself.

    ``rag.search_rag`` keeps a chunk when ``score >= threshold``. This applies exactly
    that test to a uniform sample of the scores actually seen, so the number shown is
    the number that happens, not one modelled from bucket edges.
    """
    if not sample:
        return None
    return sum(1 for x in sample if x >= threshold) / len(sample)


def suggest_threshold_exact(sample: list[float], target: float) -> float | None:
    """The highest threshold at which ``target`` of sampled queries still retrieve.

    That is the (1-target) quantile of the observed scores, rounded DOWN to 0.01 so the
    answer is achievable rather than one the sample only just misses.
    """
    if not sample:
        return None
    ordered = sorted(sample)
    idx = int((1.0 - target) * len(ordered))
    idx = max(0, min(len(ordered) - 1, idx))
    return math.floor(ordered[idx] * 100) / 100


def pass_rate(raw_hist: list[int], threshold: float) -> float:
    """Share of queries whose best pre-threshold score would clear ``threshold``.

    The boundary bucket is SPLIT, not swallowed. Counting the threshold's own bucket
    as entirely passing inverts the answer whenever the threshold sits inside a bucket
    rather than on its edge — and it always does, because the slider steps in 0.01 and
    the histogram is bucketed at 0.05. A corpus scoring 0.81 with the threshold at 0.84
    retrieves nothing at runtime, and that version reported "100% clear this" in green.

    Within the boundary bucket the scores are assumed uniform. That is an assumption,
    but a bounded one: it can be wrong by at most the mass of a single bucket, where
    the previous behaviour was wrong by that entire mass in one direction.
    """
    total = sum(raw_hist)
    if not total:
        return 0.0
    first = state.raw_bucket(threshold)
    above = sum(raw_hist[first + 1:])
    lo = first * _BUCKET
    frac_passing = max(0.0, min(1.0, (lo + _BUCKET - threshold) / _BUCKET))
    return (above + raw_hist[first] * frac_passing) / total


def suggest_threshold(raw_hist: list[int], target: float = TARGET_PASS) -> float | None:
    """Highest bucket edge at which ``target`` of queries still produce a vector hit.

    Returns the bucket's lower edge, so the answer is always achievable rather than one
    the distribution only just misses.
    """
    total = sum(raw_hist)
    if not total:
        return None
    cumulative = 0
    for i in range(state.RAW_HIST_BUCKETS - 1, -1, -1):
        cumulative += raw_hist[i]
        if cumulative / total >= target:
            return round(i * _BUCKET, 2)
    return 0.0


# One curious answer that cited rank 18 should not hold eighteen chunks of context open
# for every answer after it. The verdict runs off the shallowest rank covering the bulk
# of citations, so it describes the working set rather than the deepest outlier.
CITED_COVERAGE = 0.95


# ── per-corpus consensus ─────────────────────────────────────────────────────
# The threshold is ONE setting for every corpus, and pooling their scores answers the
# wrong question: a corpus that never matches and one that always does average into
# "fine", and the panel prints that in green while half the deployment retrieves
# nothing. Measured on two synthetic corpora N(0.30,0.06) and N(0.66,0.06): the pooled
# quantile lands at 0.27 where corpus A passes 69%, and if B is queried ten times more
# often it lands at 0.58 where A passes 0%.
#
# So each corpus states what IT needs, and the panel reports the constraint rather than
# the average.

# The resolution of the control itself (index.html: the threshold slider is step="0.01").
# Two corpora wanting values inside one step cannot be served differently — there is no
# value between them to set. This is the instrument's precision, not a tuning choice.
# Each note is a complete sentence: the modal prints it as written, because a caller
# cannot know whether a given note describes a threshold that was set or left alone.
_LEFT = "Similarity Threshold is left unchanged —"


def _join(*parts) -> str | None:
    kept = [p for p in parts if p]
    return " ".join(kept) if kept else None

THRESHOLD_STEP = 0.01

# A statistical convention (95%), not a claim about what a good RAG setting is. It sets
# how sure we insist on being before telling the user two corpora genuinely disagree.
CI_Z = 1.96


def threshold_ci(sample: list[float], target: float) -> tuple[float, float, float] | None:
    """The threshold this corpus needs, with a distribution-free interval around it.

    The estimate is an order statistic, so its uncertainty is the uncertainty in WHICH
    order statistic: the count below the quantile is binomial, giving a half-width of
    z*sqrt(n*p*(1-p)) in index units. A tight corpus gets a tight interval; a corpus with
    25 samples gets a wide one and stops raising false alarms on its own.
    """
    n = len(sample or [])
    if n < MIN_QUERIES:
        return None
    ordered = sorted(sample)
    p = 1.0 - target
    k = int(p * n)
    half = CI_Z * math.sqrt(max(n * p * (1.0 - p), 0.0))
    lo_i = max(0, min(n - 1, int(math.floor(k - half))))
    hi_i = max(0, min(n - 1, int(math.ceil(k + half))))
    k = max(0, min(n - 1, k))
    f = lambda x: math.floor(x * 100) / 100
    return f(ordered[k]), f(ordered[lo_i]), f(ordered[hi_i])


def _corpus_state(row: dict) -> str:
    """Why a corpus does or does not get a vote on the threshold."""
    n = int(row.get("queries") or 0)
    errs = int(row.get("errors") or 0)
    if errs and errs >= max(3, 0.2 * max(1, n + errs)):
        return "failing"
    sample = row.get("raw_sample") or []
    hist = row.get("raw_hist") or []
    # "Never produced a vector candidate" is decided BEFORE the shape of the evidence.
    # Testing it only on the sample let a histogram-only corpus whose every observation
    # sits in the bottom bucket be scored as measurable: it voted, wanted 0.00, and the
    # panel offered a one-click "use 0" — which turns the threshold off.
    if len(sample) >= MIN_QUERIES:
        # No threshold fixes an empty index or a dimension mismatch, and letting it vote
        # drags the consensus to 0.00.
        return "no_signal" if max(sample) <= 0.0 else "measured"
    if sum(hist) >= MIN_QUERIES:
        # A row written before sampling existed has only the histogram. Refusing it a
        # verdict would silently drop every corpus carried over from an older version.
        return "no_signal" if sum(hist[1:]) == 0 else "binned"
    return "thin"


def corpus_rows(stats: dict, thr: float, target: float = TARGET_PASS) -> list[dict]:
    """One row per corpus: what it sees now, and what it would need."""
    rows = []
    for name, row in sorted((stats or {}).items()):
        if not isinstance(row, dict):
            continue
        sample = [float(x) for x in (row.get("raw_sample") or [])
                  if isinstance(x, (int, float)) and not isinstance(x, bool)]
        st = _corpus_state(dict(row, raw_sample=sample))
        hist = list(row.get("raw_hist") or [])
        if st == "measured":
            ci = threshold_ci(sample, target)
        elif st == "binned":
            # The bucket width IS the uncertainty: a 0.05 bucket cannot say where inside
            # itself the quantile falls, so the interval is exactly that wide. It is a
            # measured resolution, not a chosen tolerance — and it makes a legacy corpus
            # too coarse to trigger a disagreement it cannot actually evidence.
            w = suggest_threshold(hist)
            ci = (w, max(0.0, round(w - _BUCKET, 2)), round(w + _BUCKET, 2)) if w is not None else None
        else:
            ci = None
        # A derived threshold of zero says nothing here distinguishes a hit from a miss —
        # most observations sit on the measurement floor. It is a diagnosis, not a value,
        # and a row claiming votes=True while wanting 0.00 contradicts the consensus that
        # then refuses it.
        if ci is not None and ci[0] <= 0:
            st, ci = "no_signal", None
        n = int(row.get("queries") or 0)
        rows.append({
            "id": name,
            "queries": n,
            "errors": int(row.get("errors") or 0),
            "sample_size": len(sample),
            # scored, not queries: raw_score_sum is parked with its regime while
            # queries is lifetime, so dividing by queries collapsed the mean to a
            # twenty-eighth of the truth after any embedding-model or HyDE change.
            "mean_raw": (round(float(row.get("raw_score_sum") or 0.0)
                               / int(row["scored"]), 4)
                         if int(row.get("scored") or 0) else 0.0),
            "pass_now": (pass_rate_exact(sample, thr) if sample
                         else (pass_rate(hist, thr) if sum(hist) else None)),
            "wants": ci[0] if ci else None,
            "wants_ci": [ci[1], ci[2]] if ci else None,
            "state": st,
            "votes": st in ("measured", "binned"),
        })
    # sorted by what each needs, so the first and last voters are the two the UI names
    rows.sort(key=lambda r: (r["wants"] is None, r["wants"] if r["wants"] is not None else 0,
                             -r["queries"]))
    return rows


def consensus(rows: list[dict], target: float = TARGET_PASS) -> dict:
    """The one threshold that serves every corpus that can speak — or the conflict.

    min, not the pooled quantile and not a midpoint. The asymmetry is measured, not
    preferred: too low costs precision (extra weak chunks, which a reranker can absorb),
    too high switches vector retrieval OFF for that corpus. min is the highest value at
    which every measurable corpus still meets its target.
    """
    voters = [r for r in rows if r["votes"]]
    out = {
        "target_pass": target, "step": THRESHOLD_STEP,
        "voters": len(voters),
        "thin": sum(1 for r in rows if r["state"] == "thin"),
        "no_signal": sum(1 for r in rows if r["state"] == "no_signal"),
        "failing": sum(1 for r in rows if r["state"] == "failing"),
        "agree": True, "gap": None, "suggested": None, "lowest": None, "highest": None,
    }
    # Second line of defence: corpus_rows already demotes a zero-wanting corpus to
    # no_signal. A derived zero is not a setting, it is a diagnosis, and the panel used
    # to offer it as a one-click "use 0" — which accepts every chunk in the corpus.
    voters = [r for r in voters if (r["wants"] or 0) > 0]
    out["voters"] = len(voters)
    if not voters:
        return out
    lo = min(voters, key=lambda r: r["wants"])
    hi = max(voters, key=lambda r: r["wants"])
    out["suggested"] = lo["wants"]
    out["lowest"] = {"id": lo["id"], "wants": lo["wants"]}
    out["highest"] = {"id": hi["id"], "wants": hi["wants"]}
    if len(voters) > 1:
        # Disjoint intervals, by at least one step of the control. Overlapping intervals
        # mean the difference is sampling noise; a sub-step difference means there is no
        # value between them to set anyway.
        gap = max(r["wants_ci"][0] for r in voters) - min(r["wants_ci"][1] for r in voters)
        out["gap"] = round(gap, 4)
        out["agree"] = gap < THRESHOLD_STEP
    return out


def pass_now_min(rows: list[dict], thr: float) -> float | None:
    """The pass rate of the WORST measurable corpus at the current threshold.

    Both the threshold panel and the hybrid panel answer "is vector search matching
    anything here". Reading that off the average let one healthy corpus hide a dark one.
    """
    # VOTERS only. Including a no_signal corpus took the number from a corpus that by
    # construction cannot speak, while the headline named a voting one: the panel printed
    # "Only 0% of queries clear this on wiki" with wiki at 96.5%, and then the
    # arithmetically impossible "At 0.73, 88% would" beside it — pass rate cannot rise as
    # the threshold rises. A dark corpus is reported by its own branch and counted in the
    # detail; it does not get to supply a percentage about someone else.
    vals = [r["pass_now"] for r in rows if r["votes"] and r["pass_now"] is not None]
    return min(vals) if vals else None



def cited_coverage_rank(cited_hist: list[int], coverage: float = CITED_COVERAGE) -> int:
    """The shallowest rank at or above which `coverage` of all citations fall."""
    total = sum(cited_hist)
    if not total:
        return 0
    run = 0
    for i, n in enumerate(cited_hist):
        run += n
        if run >= total * coverage:
            return i + 1
    return len(cited_hist)


def _unknown(value, why: str) -> dict:
    return {"value": value, "status": "unknown", "headline": "Not enough data yet",
            "detail": why, "suggested": None}


def _static(value, detail: str) -> dict:
    """A setting whose effect this deployment cannot observe — no colour, no number."""
    return {"value": value, "status": "static", "headline": "Not measurable here",
            "detail": detail, "suggested": None}


def _by_id(rows: list[dict], cid: str) -> dict:
    return next((r for r in rows if r["id"] == cid), {})


def _sample_of(stats: dict, cid: str) -> list[float]:
    row = (stats or {}).get(cid) or {}
    return [float(x) for x in (row.get("raw_sample") or [])
            if isinstance(x, (int, float)) and not isinstance(x, bool)]


def build_advice(cfg: dict | None = None, stats: dict | None = None) -> dict:
    """Per-setting verdicts for the RAG controls."""
    cfg = cfg if cfg is not None else state._config
    rag_cfg = cfg.get("rag", {}) or {}
    rr_cfg = cfg.get("reranker", {}) or {}
    raw_stats = stats if stats is not None else state._rag_stats
    t = _totals(raw_stats)
    n = t["queries"]
    # Provenance, not a verdict. Retrieval does not run on a semantic-cache hit, so a
    # replayed question contributes no score and is absent from everything below. At a
    # 90% hit rate it takes 200 questions to reach the 20 the threshold verdict needs,
    # and without this the user cannot tell a quiet deployment from a well-cached one.
    # It carries no colour: there is no measured basis for calling a hit rate too high
    # or too low, and inventing one would be the table of good values this feature exists
    # to avoid.
    replays = sum(int((r or {}).get("cache_replays") or 0)
                  for r in (raw_stats or {}).values() if isinstance(r, dict))
    # One question searches every selected corpus, so summing per-corpus queries counts
    # it once per corpus while a replay is recorded once per question — the ratio was
    # wrong by the corpus count, and this is the one number the disclosure exists to
    # state. The most-queried corpus saw every question, so it IS the question count.
    measured_q = max((int((r or {}).get("queries") or 0)
                      for r in (raw_stats or {}).values() if isinstance(r, dict)),
                     default=0)
    asked = measured_q + replays
    out: dict = {"samples": n, "cited_answers": t["answers_with_citations"],
                 "raw_hist": t["raw_hist"],
                 "coverage": {"measured": measured_q, "replayed": replays,
                              "asked": asked,
                              "share": round(measured_q / asked, 4) if asked else None,
                              "note": (f"Measured over {measured_q:,} of {asked:,} "
                                       f"questions — "
                                       f"the other {replays:,} were answered from cache, "
                                       f"which runs no retrieval." if replays else None)},
                 "settings": {}}

    # ── similarity_threshold ────────────────────────────────────────────────
    thr = float(rag_cfg.get("similarity_threshold",
                            constants.DEFAULT_CONFIG["rag"]["similarity_threshold"]))
    # One number, read once, and it is the WORST corpus's — not the average. Both this
    # panel and the hybrid panel answer "is vector search matching anything here";
    # computing it from a pooled sample let one healthy corpus hide a dark one, and
    # computing it twice by two routes let the two panels contradict each other.
    rows = corpus_rows(raw_stats, thr)
    cons = consensus(rows)
    out["corpora"] = rows
    out["consensus"] = dict(cons, setting="rag.similarity_threshold")
    pass_now = pass_now_min(rows, thr)
    if pass_now is None:
        pass_now = pass_rate_exact(t["raw_sample"], thr)
    if pass_now is None:
        pass_now = pass_rate(t["raw_hist"], thr)

    failing = [r for r in rows if r["state"] == "failing"]
    dark    = [r for r in rows if r["state"] == "no_signal"]
    def _corpus_ids(rs):
        return [r["id"] for r in rs]

    # Retrieval that ERRORED is not evidence about the corpus, and it is a property of
    # ONE corpus: pooling the error count painted the global threshold red because a
    # single dead instance had five failures beside a healthy one with fifteen queries.
    # A failing corpus takes over the panel only when nothing else can be measured.
    # Otherwise the healthy corpora still get their verdict and the failure is named
    # in the detail — losing usable advice because one instance is dead helps nobody.
    if failing and not cons["voters"]:
        names = ", ".join(_corpus_ids(failing)[:2])
        more = f" and {len(failing) - 2} others" if len(failing) > 2 else ""
        errs = sum(r["errors"] for r in failing)
        out["settings"]["similarity_threshold"] = {
            "value": thr, "status": "bad",
            "headline": f"Retrieval is failing on {names}{more}",
            "detail": f"{errs} searches raised an error, so the score distribution for "
                      f"{'those corpora' if len(failing) > 1 else 'that corpus'} cannot "
                      f"be measured. Check Settings → Status and the server log — a "
                      f"changed embedding model needs the corpus re-ingested.",
            "suggested": None, "scope": "per_corpus",
            "corpus_ids": _corpus_ids(failing)}
    elif dark and not cons["voters"]:
        # Every corpus that has been queried enough has never produced a single vector
        # candidate. No threshold fixes that, and deriving one from those zeros is how
        # the panel ended up proposing 0.00.
        out["settings"]["similarity_threshold"] = {
            "value": thr, "status": "bad",
            "headline": (f"{_corpus_ids(dark)[0]} is not matching anything"
                         if len(dark) == 1 else
                         f"{len(dark)} corpora are not matching anything"),
            "detail": "Almost every search came back with nothing to score, so no "
                      "threshold can be read from the results and lowering this one will "
                      "not help. The corpus is empty, or its index was built with a "
                      "different embedding model and needs re-ingesting.",
            "suggested": None, "scope": "per_corpus", "corpus_ids": _corpus_ids(dark)}
    elif not cons["voters"]:
        thin_n = max((r["sample_size"] for r in rows), default=0)
        out["settings"]["similarity_threshold"] = _unknown(
            thr, f"{thin_n} of {MIN_QUERIES} scored searches needed before the score "
                 f"distribution can be read.")
    else:
        sug = cons["suggested"]
        rate = pass_now if pass_now is not None else 0.0
        pct = round(rate * 100)
        worst = min((r for r in rows if r["votes"]), key=lambda r: r["pass_now"] or 0.0)
        if not cons["agree"]:
            # The conflict IS the finding. A percentage here would be a number for a
            # corpus, presented as a number for the deployment.
            lo, hi = cons["lowest"], cons["highest"]
            extra = (f" +{cons['voters'] - 2} others between them"
                     if cons["voters"] > 2 else "")
            status = "bad" if rate < 0.25 else "warn"
            head = f"{lo['id']} and {hi['id']} need different thresholds{extra}"
            detail = (f"At {thr:.2f}, {lo['id']} produces a vector hit on "
                      f"{round((_by_id(rows, lo['id'])['pass_now'] or 0) * 100)}% of its "
                      f"queries and {hi['id']} on "
                      f"{round((_by_id(rows, hi['id'])['pass_now'] or 0) * 100)}%. "
                      f"{lo['id']} needs {lo['wants']:.2f}, {hi['id']} needs "
                      f"{hi['wants']:.2f}. {sug:.2f} is the highest value both can meet. "
                      f"The threshold is one setting for every corpus — if the difference "
                      f"matters, query them in separate turns, or leave reranking on so "
                      f"the extra weak chunks are dropped before the prompt.")
        else:
            if rate < 0.25:
                status, head = "bad", f"Only {pct}% of queries clear this"
            elif rate < 0.60:
                status, head = "warn", f"{pct}% of queries clear this"
            else:
                status, head = "ok", f"{pct}% of queries clear this"
            # Named whenever there is more than one corpus at all, not more than one
            # VOTER: a single voter beside a dead corpus is exactly when the reader most
            # needs to know whose number this is.
            if len(rows) > 1:
                head += f" on {worst['id']}"
            detail = (f"Across {n} queries the best pre-threshold score averaged "
                      f"{_mean_raw(t):.2f}. At {thr:.2f}, {pct}% produce a vector hit; "
                      f"the rest fall back to keyword matching or to no context at all.")
            if sug is not None and abs(sug - thr) >= 0.01:
                # Say which target picked it. The quantile is the user's data; the share
                # it is read at is this module's choice, and "measured from your own
                # retrieval history" without it invites the number to be read as the
                # only one the data supports.
                detail += (f" Aiming to keep {int(TARGET_PASS * 100)}% of searches "
                           f"retrieving, that is {sug:.2f}.")
                # what the WORST corpus would get at the suggested value — the number
                # the user is actually buying
                worst_sample = _sample_of(raw_stats, worst["id"])
                at_sug = (pass_rate_exact(worst_sample, sug) if worst_sample
                          else pass_rate((raw_stats.get(worst["id"]) or {}).get("raw_hist") or [],
                                         sug))
                if at_sug is not None:
                    detail += f" At {sug:.2f}, {round(at_sug * 100)}% would."
        if failing:
            detail += (f" {', '.join(_corpus_ids(failing)[:2])} "
                       f"{'is' if len(failing) == 1 else 'are'} raising errors and "
                       f"{'was' if len(failing) == 1 else 'were'} left out of this.")
        if len(rows) > 1:
            detail += (f" Measured on {cons['voters']} of {len(rows)} corpora"
                       + (f"; {cons['thin']} not measured yet" if cons["thin"] else "")
                       + (f"; {cons['no_signal']} never matched" if cons["no_signal"] else "")
                       + ".")
        # The pass rate at every position of the slider, computed by the same function
        # that produced the verdict so there is no second implementation to drift. It
        # turns the control from a number you set and wait on into one you can read.
        worst_sample = _sample_of(raw_stats, worst["id"])
        curve = ([round(pass_rate_exact(worst_sample, i / 100) or 0.0, 4)
                  for i in range(101)] if worst_sample else None)
        # The WORST VOTER's histogram, not the pool. Drawing the pooled one under a
        # percentage about one corpus put the tallest bar on screen — 40 of 100 zeros
        # from the corpus that percentage excludes — beside a green "100% clear this".
        worst_hist = list((raw_stats.get(worst["id"]) or {}).get("raw_hist")
                          or t["raw_hist"])
        out["settings"]["similarity_threshold"] = {
            "value": thr, "status": status, "headline": head, "detail": detail,
            "suggested": (sug if sug is not None and abs(sug - thr) >= 0.02 else None),
            "scope": "per_corpus" if cons["voters"] > 1 else "pooled",
            "corpus_ids": [r["id"] for r in rows if r["votes"]],
            # what the numbers are ABOUT, so a reader can check them rather than trust them
            "curve": curve,
            "curve_of": worst["id"] if len(rows) > 1 else None,
            "hist": worst_hist}

    # ── top_k ───────────────────────────────────────────────────────────────
    top_k = int(rag_cfg.get("top_k", 5))
    chunk_size = int(rag_cfg.get("chunk_size", 180))
    # What actually reaches the model. With reranking on, top_n chunks are sent and the
    # rest discarded, so costing top_k overstated the context by 4x while the line above
    # it said only five ranks were in use.
    _rr_n = int(rr_cfg.get("top_n") or top_k) if rr_cfg.get("enabled") else top_k
    sent_k = min(top_k, _rr_n)
    approx_tokens = int(sent_k * chunk_size * 1.35)     # ~1.35 tokens per word
    if t["answers_with_citations"] < MIN_CITED_ANSWERS:
        d = _unknown(top_k, f"{t['answers_with_citations']} of {MIN_CITED_ANSWERS} cited "
                            f"answers needed before chunk usage can be judged.")
        d["detail"] += (f" At {sent_k} x {chunk_size} words this sends about "
                        f"{approx_tokens:,} tokens of context per grounded answer.")
        out["settings"]["top_k"] = d
    else:
        deepest = cited_coverage_rank(t["cited_hist"])
        # With reranking on, only top_n chunks ever reach the model, so a deeper rank
        # could not have been cited whatever its quality. Judging against top_k there
        # reads "nothing below rank 5 is used" from chunks that were never sent.
        effective_k = (min(top_k, int(rr_cfg.get("top_n") or top_k))
                       if rr_cfg.get("enabled") else top_k)
        # Ranks recorded under a larger top_k outlive the change. Left unclamped, a
        # deepest of 18 against a current top_k of 5 produced a negative "waste", which
        # fell through to the healthy branch and rendered "ranks up to 18 are in use" in
        # green — a claim the current setting makes impossible.
        stale = deepest > effective_k
        deepest = min(deepest, effective_k)
        waste = effective_k - deepest
        if stale:
            status, head = "ok", f"All {effective_k} chunks are being cited"
        elif not deepest:
            # Citations were counted but no rank landed in the tracked range — a green
            # "ranks up to 0 are in use" would be worse than admitting we cannot tell.
            status, head = "unknown", "Citations recorded, but no rank in range"
        elif waste > TOPK_HEADROOM * 2:
            status, head = "bad", f"Almost nothing below rank {deepest} gets cited"
        elif waste > TOPK_HEADROOM:
            status, head = "warn", f"Citations tail off after rank {deepest}"
        else:
            status, head = "ok", f"Ranks up to {deepest} are in use"
        out["settings"]["top_k"] = {
            "value": top_k, "status": status, "headline": head,
            "detail": (f"Across {t['answers_with_citations']} cited answers, "
                       f"{int(CITED_COVERAGE * 100)}% of citations fall within the top "
                       f"{deepest}. Sending {sent_k} costs about {approx_tokens:,} tokens "
                       f"of context per grounded answer."),
            "suggested": (min(effective_k, deepest + TOPK_HEADROOM)
                          if deepest and waste > TOPK_HEADROOM else None)}

    # ── reranker ────────────────────────────────────────────────────────────
    rr_on = bool(rr_cfg.get("enabled", False))
    if rr_on:
        out["settings"]["reranker"] = {
            "value": True, "status": "ok", "headline": "On",
            "detail": "Candidates are re-scored by a cross-encoder before the best are "
                      "sent, so top_k can stay small without losing recall.",
            "suggested": None}
    elif (t["answers_with_citations"] >= MIN_CITED_ANSWERS
          and cited_coverage_rank(t["cited_hist"]) and top_k > cited_coverage_rank(t["cited_hist"]) + TOPK_HEADROOM):
        # Coloured only where a COUNTER backs it. "top_k > 8" is a number this module
        # chose, and warning on it painted an amber verdict with a one-click apply on a
        # deployment with zero measurements — the static recommended value the feature
        # exists to abolish, and it survived the reset that promises otherwise.
        _deep = cited_coverage_rank(t["cited_hist"])
        out["settings"]["reranker"] = {
            "value": False, "status": "warn", "headline": "Off, and top_k is wider than use",
            "detail": f"Across {t['answers_with_citations']} cited answers, "
                      f"{int(CITED_COVERAGE * 100)}% of citations fall within the top "
                      f"{_deep}, but all {top_k} are sent. Reranking lets you retrieve "
                      f"wide and send few.",
            "suggested": True}
    elif t["answers_with_citations"] < MIN_CITED_ANSWERS:
        out["settings"]["reranker"] = _unknown(
            False, f"{t['answers_with_citations']} of {MIN_CITED_ANSWERS} cited answers "
                   f"needed before chunk usage can say whether reranking would help.")
    else:
        out["settings"]["reranker"] = {
            "value": False, "status": "ok", "headline": "Off",
            "detail": "With a small top_k the ordering from hybrid search is usually "
                      "enough; reranking costs CPU on every grounded turn.",
            "suggested": None}

    # ── hybrid search ───────────────────────────────────────────────────────
    hybrid = bool(rag_cfg.get("hybrid_search", True))
    if not hybrid and n >= MIN_QUERIES and pass_now < 0.25:
        out["settings"]["hybrid_search"] = {
            "value": False, "status": "bad", "headline": "Off, and vector search is not matching",
            "detail": "With hybrid off, a query that misses on cosine similarity returns "
                      "nothing at all — there is no keyword leg to fall back on.",
            "suggested": True}
    else:
        # Amber with a one-click "turn on" needs a counter behind it. Warning purely
        # because the toggle is off painted a verdict on a deployment that had measured
        # nothing, and survived the reset that promises every verdict returns to
        # "not enough data yet". Off is stated; it is not graded until it can be.
        graded = (not hybrid) and n >= MIN_QUERIES
        out["settings"]["hybrid_search"] = {
            "value": hybrid, "status": "ok" if hybrid else ("warn" if graded else "unknown"),
            "headline": "On" if hybrid else "Off",
            "detail": ("Vector and BM25 results are fused, so a query that matches on "
                       "wording still retrieves when the embedding misses."
                       if hybrid else
                       "Only vector similarity is used. Exact terms — error codes, flags, "
                       "identifiers — retrieve less reliably without the keyword leg."
                       + ("" if graded else
                          f" Not graded yet: {n} of {MIN_QUERIES} scored searches.")),
            "suggested": True if graded else None}

    # ── settings this deployment cannot measure ─────────────────────────────
    out["settings"]["chunk_size"] = _static(
        rag_cfg.get("chunk_size"),
        "Changing this requires re-ingesting every document, so the alternative cannot be "
        "observed from here. Smaller chunks retrieve more precisely and carry less "
        "surrounding context; larger ones the reverse.")
    out["settings"]["chunk_overlap"] = _static(
        rag_cfg.get("chunk_overlap"),
        "Also fixed at ingest time. Overlap stops an answer being split across a chunk "
        "boundary, at the cost of storing the shared words twice.")
    out["settings"]["history_max_tokens"] = _static(
        cfg.get("history_max_tokens"),
        "A cost-against-context preference rather than a retrieval quality one: the whole "
        "budget is resent as input on every turn.")
    return out


def _mean_raw(t: dict) -> float:
    """Mean best pre-threshold score — the exact sum, not bucket midpoints.

    ``raw_score_sum`` has been recorded all along. Reconstructing the mean from midpoints
    instead put it out by up to half a bucket: a corpus scoring exactly 0.60 every time
    reported 0.63, which is enough to talk someone into a threshold above every score
    they have.
    """
    # scored, not queries: a search that retrieved nothing contributes no score, so
    # dividing the sum by every query understates the mean by the share of empty ones.
    n = t.get("scored") or sum(t["raw_hist"]) or t.get("queries")
    if not n:
        return 0.0
    return t.get("raw_score_sum", 0.0) / n


# ── Presets ───────────────────────────────────────────────────────────────────
# Starting points, not modes. Applying one writes the values and forgets; the moment a
# slider moves, a remembered "you are in Recall mode" label would be a lie.
#
# Deliberately NOT included: chunk_size and chunk_overlap. Changing those silently
# invalidates every existing embedding — a preset button must not require a re-ingest
# nobody asked for.
# A preset states an INTENT, never a threshold. What counts as a strict threshold is a
# property of the corpus, not of the preset: 0.80 is selective on a corpus scoring
# 0.85-0.95 and switches vector search off on one scoring 0.60-0.75. This codebase has
# already been bitten once — constants.py records that a shipped 0.75 default "cleared
# almost nothing, leaving the lexical (BM25) leg to carry retrieval on its own".
#
# So each preset declares the share of queries it wants to keep retrieving, and the
# actual number is read off the scores this deployment has observed. With no
# observations the threshold is simply left alone rather than guessed at.
#
# Deliberately NOT included: chunk_size and chunk_overlap. Changing those invalidates
# every stored embedding — a preset button must not require a re-ingest nobody asked for.
RAG_PRESETS = [
    {
        "id": "precision",
        "name": "Precision",
        "when": "A clean, well-matched corpus where a wrong answer costs more than a "
                "missing one. Keeps only the strongest matches, so some questions go "
                "unanswered rather than answered from weak context.",
        "pass_target": 0.60,        # strict: 40% of queries keep no vector hit
        "values": {"rag.top_k": 5, "rag.hybrid_search": True,
                   "reranker.enabled": True, "reranker.top_n": 5,
                   "rag.rerank_candidates": 40},
    },
    {
        "id": "recall",
        "name": "Recall",
        "when": "A large or uneven corpus where the answer exists but is hard to find. "
                "Casts a wide net and lets the reranker choose, so it sends no more "
                "context than Precision does.",
        "pass_target": 0.95,        # permissive: almost every query keeps a vector hit
        "values": {"rag.top_k": 8, "rag.hybrid_search": True,
                   "reranker.enabled": True, "reranker.top_n": 8,
                   "rag.rerank_candidates": 80},
    },
    {
        "id": "economy",
        "name": "Economy",
        "when": "A paid API where input tokens are the constraint. Sends less of "
                "everything and skips reranking; expect to miss answers a wider search "
                "would have found.",
        "pass_target": 0.85,
        "values": {"rag.top_k": 4, "rag.hybrid_search": True,
                   "reranker.enabled": False, "history_max_tokens": 1500},
    },
]


def _cap_by_evidence(values: dict, cited_hist: list[int], cited_answers: int) -> str | None:
    """Never send more chunks than the citations show are read.

    The preset's own top_k / top_n are declared starting points, not measurements, and
    they contradicted the verdict on the same screen: the panel said "ranks up to 3 are
    in use, suggested: none" while a preset button raised the kept-chunk count to 8.
    Where there IS evidence it wins; where there is none the preset's figure stands and
    the caller says so.
    """
    if cited_answers < MIN_CITED_ANSWERS:
        return None
    deep = cited_coverage_rank(cited_hist or [])
    if not deep:
        return None
    cap = deep + TOPK_HEADROOM
    capped = [k for k in ("rag.top_k", "reranker.top_n")
              if isinstance(values.get(k), int) and values[k] > cap]
    for k in capped:
        values[k] = cap
    if not capped:
        return None
    return (f"Trimmed to {cap} chunks: across {cited_answers} cited answers "
            f"{int(CITED_COVERAGE * 100)}% of citations fall within the top {deep}.")


def preset_values_for(preset: dict, rows: list[dict],
                      cited_hist: list[int] | None = None,
                      cited_answers: int = 0) -> tuple[dict, str | None, dict]:
    """Resolve a preset against EACH corpus, not against a pooled sample.

    "keeps 60% of queries retrieving" was never meant to mean "60% of one corpus and 0%
    of another" — so pass_target is read per corpus and the preset takes the value every
    measurable corpus can meet.
    """
    values = dict(preset["values"])
    cap_note = _cap_by_evidence(values, cited_hist or [], cited_answers)
    target = preset["pass_target"]
    scored = []
    for r in rows:
        if not r["votes"]:
            continue
        ci = threshold_ci(r["_sample"], target) if r["_sample"] else None
        if ci is None and r["state"] == "binned":
            w = suggest_threshold(r["_hist"], target)
            ci = (w, max(0.0, round(w - _BUCKET, 2)), round(w + _BUCKET, 2)) if w is not None else None
        if ci is not None:
            scored.append(dict(r, wants=ci[0], wants_ci=[ci[1], ci[2]]))
    # Non-voters stay in the list so the disclosure counts (thin / never matched) are
    # the real ones; only the re-scored voters can vote.
    others = [r for r in rows if not r["votes"]]
    cons = consensus(scored + others, target)
    if not scored:
        thin = max((r["sample_size"] for r in rows), default=0)
        return values, _join(cap_note, f"{_LEFT} only {thin} of {MIN_QUERIES} scored "
                                       f"searches so far, not enough to choose one."), cons
    thr = cons["suggested"]
    if thr is None or thr <= 0:
        return values, _join(cap_note, f"{_LEFT} retrieval is not matching, so no "
                                       f"threshold can be derived from it."), cons
    values["rag.similarity_threshold"] = thr
    note = None
    if not cons["agree"]:
        lo, hi = cons["lowest"], cons["highest"]
        # hi wants the STRICT value; setting it switches off lo, which needs the low one.
        # Deliberately NOT the "left unchanged" phrasing: this branch DID set it. The
        # modal used to prepend that sentence to every note, so a conflict note appeared
        # directly beside a diff row showing the threshold changing.
        note = (f"{preset['name']} cannot be as strict as {hi['id']} needs without "
                f"switching off vector search for {lo['id']}. Set to {thr:.2f} — the "
                f"highest value both can meet.")
    return values, _join(cap_note, note), cons


def preset_values(preset: dict, sample: list[float] | None) -> tuple[dict, str | None]:
    """Resolve a preset's intent into concrete values against the observed scores.

    Returns the values and, when the threshold could not be derived, the reason — so the
    UI can say "left unchanged: not enough data" instead of quietly omitting it.
    """
    values = dict(preset["values"])
    # The SAME evidence gate the verdict above enforces. Without it the panel refused to
    # read the distribution at 3 observations while the preset happily derived a
    # threshold from those 3 and offered to apply it — and on a deployment whose scores
    # were all 0.00 that meant every preset proposed 0.90 -> 0.00, which accepts every
    # chunk. Advice and action have to answer to the same standard.
    n = len(sample or [])
    if n < MIN_QUERIES:
        return values, (f"{_LEFT} only {n} of {MIN_QUERIES} scored searches so far, "
                        f"not enough to choose one.")
    thr = suggest_threshold_exact(sample, preset["pass_target"])
    if thr is None:
        return values, f"{_LEFT} not enough retrieval history to choose one yet."
    if thr <= 0:
        # A derived zero is not a setting, it is a diagnosis: nothing in this corpus
        # resembles what is being asked. Applying it would accept every chunk.
        return values, (f"{_LEFT} retrieval is not matching, so no threshold can be "
                        f"derived from it.")
    values["rag.similarity_threshold"] = thr
    return values, None


def preset_diff(preset_id: str, cfg: dict | None = None,
                sample: list[float] | None = None) -> dict | None:
    """What applying a preset would change, so it can be shown before it is applied.

    A button that silently rewrites six settings is alarming; one that says which six is
    a decision the user can make.
    """
    preset = next((p for p in RAG_PRESETS if p["id"] == preset_id), None)
    if preset is None:
        return None
    cfg = cfg if cfg is not None else state._config
    conflict = None
    if sample is None:
        thr_now = float((cfg.get("rag") or {}).get(
            "similarity_threshold", constants.DEFAULT_CONFIG["rag"]["similarity_threshold"]))
        rows = corpus_rows(state._rag_stats, thr_now, preset["pass_target"])
        for r in rows:                       # the sample the row was built from
            r["_sample"] = _sample_of(state._rag_stats, r["id"])
            r["_hist"] = list((state._rag_stats.get(r["id"]) or {}).get("raw_hist") or [])
        tot = _totals(state._rag_stats)
        values, note, conflict = preset_values_for(
            preset, rows, tot["cited_hist"], tot["answers_with_citations"])
        for r in rows:
            r.pop("_sample", None); r.pop("_hist", None)
    else:
        values, note = preset_values(preset, sample)
    changes = []
    for path, want in values.items():
        section, _, key = path.rpartition(".")
        current = (cfg.get(section, {}) or {}).get(key) if section else cfg.get(key)
        if current != want:
            changes.append({"path": path, "label": _LABELS.get(path, path),
                            "from": current, "to": want})
    return {"id": preset["id"], "name": preset["name"], "when": preset["when"],
            "changes": changes,
            # The FULL resolved map, so applying a preset writes every value it names.
            # Applying only the diff left a control the user had staged differently
            # untouched, and the preset then silently was not what it claimed to be.
            "values": values,
            "note": note,
            # so the diff modal can name the conflict instead of quietly picking a side
            "conflict": conflict}
