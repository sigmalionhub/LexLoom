"""Thirty questions with graded relevance judgements, and the reason for each.

**How these were written, because that is the only thing that makes them worth
anything.** A gold set is trivially easy to write so that it flatters whatever
the system already does: pick questions whose wording shares a rare term with
exactly one chunk and lexical search looks excellent; write the questions after
watching the vector mode succeed and the vector mode looks excellent. Either way
the metric measures the author.

The procedure used here, in order, and none of it reversed:

1. The question text was written first, from the regulation's own structure —
   which articles exist, what each is about — and from the vocabulary a person
   researching GDPR uses, not the vocabulary the corpus uses. Written and then
   frozen.
2. Only then were the relevant chunks located, by walking to the article by its
   number in the parsed text (`Article 33`, `Article 15`), never by searching for
   the question's own words. Locating ground truth by keyword overlap is how a
   gold set ends up defining relevance as "whatever the lexical mode would find".
3. `search_chunks` was not run against any question until every judgement in
   this file was fixed and the manifest written. The numbers in `results.json`
   are the first time these questions met the retriever.

**Relevance is graded, not binary.** `2` — the chunk answers the question; `1` —
the chunk is on point and supports or completes the answer without being it. A
binary scale would make `nDCG@10` identical to `precision@10` with a different
denominator: a fourth metric carrying no fourth piece of information.

**Every judgement carries its reason.** A judgement with no reason cannot be
audited, and this file's whole claim is that a sceptic can check it. `why` is
prose for a human; it is deliberately excluded from `goldset_sha256` so that
improving an explanation does not invalidate a measurement that nothing changed.

**The six unanswerable questions are not filler.** They exist for
`answerability_auc`, which is the only metric they can move: `search_chunks` has
no score threshold and returns `limit` rows for any non-empty `tsquery`, so
their precision and MRR are the constant 0 in every mode at every quality. That
constant is why they are excluded from recall, MRR and nDCG rather than averaged
in as zeros.

Two rules of authorship follow, and both are held by tests rather than by
review:

*They must be genuinely unanswerable from this corpus, not merely hard.* Each
asks about something the GDPR does not contain at all — an institution outside
the Regulation, a deadline it declines to set, the subject matter of a different
instrument. Each carries a `note` saying what makes it unanswerable.

*They must be length- and vocabulary-matched to the answerable ones.* `ts_rank`
grows with the number of query lexemes and varies with document length, so a set
of short, simple unanswerable questions against long, specialist answerable ones
produces a splendid AUC that measures nothing but question length. See
`test_the_unanswerable_questions_are_length_matched_to_the_answerable_ones`.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from app.evals.score import ChunkKey

Kind = Literal["verbatim", "paraphrase", "synonym", "multi_hop", "unanswerable"]

# How many questions of each kind, and what each kind is for. Checked by
# `test_the_gold_set_has_the_coverage_it_claims`, so a question quietly
# retyped from one kind to another is an error rather than a silent change to
# what a regression report means.
EXPECTED_COVERAGE: Mapping[Kind, int] = {
    # Wording taken from the article itself. The floor: if these fail, nothing
    # above them means anything.
    "verbatim": 6,
    # The same question in different words. Lexical search should lose ground
    # here.
    "paraphrase": 7,
    # The concept named by a different term entirely — "the entity deciding the
    # purposes" for "controller". Where the vector mode should gain most.
    "synonym": 6,
    # The answer spans two articles, so `recall@5` is the metric that moves and
    # `MRR` is not.
    "multi_hop": 5,
    # No answer in this corpus at all. Only `answerability_auc` can move.
    "unanswerable": 6,
}

# Grades, spelled rather than left as bare integers at 60 call sites.
ANSWERS = 2
SUPPORTS = 1


@dataclass(frozen=True)
class Judgement:
    """One chunk marked relevant to one question, with the reason it was marked.

    `key` is a `(source_key, chunk_ordinal)` pair rendered as
    `"gdpr-full:184"`. Never a UUID: `document_chunks.id` is generated at insert
    and differs per tenant and per reseed.
    """

    key: ChunkKey
    grade: Literal[1, 2]
    why: str


@dataclass(frozen=True)
class Question:
    id: str
    text: str
    kind: Kind
    # Empty for the unanswerable six. That is a value, not a missing value, and
    # the scorers in `score.py` all branch on it explicitly.
    judgements: tuple[Judgement, ...]
    # Which property of retrieval this question exists to exercise, or — for an
    # unanswerable one — why the corpus cannot answer it. Recorded so a
    # regression reads as "paraphrases broke" rather than "MRR fell 0.04".
    note: str

    @property
    def relevance(self) -> Mapping[ChunkKey, int]:
        """The mapping the scorers take. Derived, never stored twice.

        Two parallel structures — grades here, reasons there — is how a
        judgement ends up with a reason attached to a different chunk than the
        one it grades, with nothing raised anywhere.
        """
        return {judgement.key: judgement.grade for judgement in self.judgements}

    @property
    def is_answerable(self) -> bool:
        return bool(self.judgements)


def referenced_keys(questions: Sequence[Question]) -> list[ChunkKey]:
    """Every chunk any judgement names, sorted and de-duplicated.

    This is exactly the set `manifest.json` pins content hashes for: pinning the
    whole corpus would make the artefact unreadable in a diff, pinning less
    would leave a chunk a metric depends on unwatched.
    """
    return sorted({judgement.key for question in questions for judgement in question.judgements})


def canonical_payload(questions: Sequence[Question]) -> str:
    """The bytes `goldset_sha256` hashes: questions and relevances, nothing else.

    Not the bytes of this file. A renamed variable, a reflowed comment or a
    clarified `why` would otherwise look like a different gold set and force a
    re-measurement of something nothing changed — and the fastest way to teach
    someone to regenerate a provenance hash without reading it is to make it
    change for reasons that do not matter.

    `sort_keys` and a fixed separator, so the hash depends on the content and
    not on the order a dict happened to be built in.
    """
    return json.dumps(
        [
            {
                "id": question.id,
                "text": question.text,
                "kind": question.kind,
                "relevance": dict(sorted(question.relevance.items())),
            }
            for question in sorted(questions, key=lambda q: q.id)
        ],
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def goldset_sha256(questions: Sequence[Question] | None = None) -> str:
    return hashlib.sha256(
        canonical_payload(QUESTIONS if questions is None else questions).encode("utf-8")
    ).hexdigest()


_J = Judgement

# Chunk keys are dense and unmemorable on purpose — they are positions, not
# names. As an orientation for a reader checking a judgement: `gdpr-full`
# ordinals 0-428 are the recitals, 429 onward the enacting articles, and the
# `seed:gdpr-excerpts` ordinals are the thirty extracts listed in
# `backend/seed/gdpr_excerpts.json`, each of which states the article it came
# from in its own first line.
#
# A note on why so many judgements name a `seed:gdpr-excerpts` chunk alongside a
# `gdpr-full` one: the excerpts are extracts of the same regulation, so the two
# often hold the same sentence. Marking only the catalogue copy would score a
# correct answer as a miss whenever it arrived as the (cleaner, shorter) seeded
# duplicate — a defect in the gold set that would read as a defect in retrieval.
QUESTIONS: tuple[Question, ...] = (
    # -------------------------------------------------------------- verbatim
    Question(
        id="v-breach-72h",
        text=(
            "What must a controller do without undue delay and, where feasible, not later "
            "than 72 hours after becoming aware of a personal data breach?"
        ),
        kind="verbatim",
        note="Article 33(1), asked in the article's own words. The floor for lexical search.",
        judgements=(
            _J("gdpr-full:625", ANSWERS, "Article 33(1) itself: notify the supervisory authority."),
            _J(
                "seed:gdpr-excerpts:23",
                ANSWERS,
                "The same Article 33(1) sentence as a seeded excerpt. A correct answer may "
                "legitimately arrive as this duplicate rather than as the catalogue copy.",
            ),
            _J(
                "gdpr-full:626",
                SUPPORTS,
                "Continues 33(1): a notification later than 72 hours must carry reasons for "
                "the delay. Completes the answer without being it.",
            ),
            _J(
                "gdpr-full:624",
                SUPPORTS,
                "Carries the Article 33 heading and the run-up to paragraph 1; useful context, "
                "not the obligation.",
            ),
        ),
    ),
    Question(
        id="v-principles",
        text="What are the principles relating to processing of personal data?",
        kind="verbatim",
        note=(
            "The heading of Article 5, used verbatim. Deliberately broad: the answer is spread "
            "over thirteen chunks, so recall@5 cannot exceed 5/13 here however good retrieval "
            "is. That ceiling is a property of scoring 500-character chunks and is reported "
            "rather than engineered away by trimming judgements."
        ),
        judgements=(
            _J(
                "gdpr-full:464",
                ANSWERS,
                "The Article 5 heading and the opening of 5(1). "
                "5(1)(a): lawfulness, fairness and transparency."
            ),
            _J(
                "gdpr-full:465",
                SUPPORTS,
                "5(1)(b): purpose limitation, one of the six principles the question asks for."
            ),
            _J(
                "gdpr-full:466",
                SUPPORTS,
                "5(1)(c): data minimisation, one of the six principles the question asks for. "
                "5(1)(d): accuracy, one of the six principles the question asks for."
            ),
            _J(
                "gdpr-full:468",
                SUPPORTS,
                "5(1)(e): storage limitation, one of the six principles the question asks for."
            ),
            _J(
                "gdpr-full:470",
                SUPPORTS,
                "5(1)(f): integrity and confidentiality. "
                "5(2): accountability (shares a chunk with Article 6)."
            ),
            _J(
                "seed:gdpr-excerpts:4",
                SUPPORTS,
                "5(1)(b) as a seeded excerpt: the same principle, clean of the parser's spacing."
            ),
            _J(
                "seed:gdpr-excerpts:5",
                SUPPORTS,
                "5(1)(c) as a seeded excerpt: the same principle, clean of the parser's spacing."
            ),
            _J(
                "seed:gdpr-excerpts:6",
                SUPPORTS,
                "5(1)(e) as a seeded excerpt: the same principle, clean of the parser's spacing."
            ),
            _J(
                "seed:gdpr-excerpts:7",
                SUPPORTS,
                "5(1)(f) as a seeded excerpt: the same principle, clean of the parser's spacing."
            ),
            _J("seed:gdpr-excerpts:8", SUPPORTS, "5(2) accountability as a seeded excerpt."),
        ),
    ),
    Question(
        id="v-erasure",
        text="What is the right to erasure, also known as the right to be forgotten?",
        kind="verbatim",
        note="The Article 17 heading, including its parenthetical, used as the question.",
        judgements=(
            _J("gdpr-full:546", ANSWERS, "Article 17(1): the right and the controller's duty."),
            _J("seed:gdpr-excerpts:14", ANSWERS, "Article 17(1) as a seeded excerpt."),
            _J("gdpr-full:545", SUPPORTS, "Carries the Article 17 heading with the parenthetical."),
            _J(
                "gdpr-full:547",
                SUPPORTS,
                "Grounds 17(1)(a) and (b): when erasure may be demanded."
            ),
            _J(
                "gdpr-full:548",
                SUPPORTS,
                "Grounds 17(1)(c) to (e): further cases in which erasure may be demanded."
            ),
            _J("gdpr-full:551", SUPPORTS, "17(3): where the right does not apply."),
        ),
    ),
    Question(
        id="v-fines-upper",
        text=(
            "What is the maximum administrative fine for infringements of the basic principles "
            "for processing and the conditions for consent?"
        ),
        kind="verbatim",
        note="Article 83(5)(a), quoting its own phrasing of what falls in the upper tier.",
        judgements=(
            _J(
                "gdpr-full:935",
                ANSWERS,
                "Article 83(5): 20 000 000 EUR or 4 % of worldwide annual turnover, and point "
                "(a) is exactly the basic principles and conditions for consent the "
                "question names.",
            ),
            _J("seed:gdpr-excerpts:29", ANSWERS, "Article 83(5) as a seeded excerpt."),
            _J("gdpr-full:936", SUPPORTS, "Continues the 83(5) list, points (b) to (d)."),
            _J(
                "gdpr-full:933",
                SUPPORTS,
                "Article 83(4), the 10 000 000 EUR / 2 % tier. The contrast is what makes the "
                "answer meaningful rather than a number in isolation.",
            ),
        ),
    ),
    Question(
        id="v-dpia",
        text="In which cases is a data protection impact assessment required before processing?",
        kind="verbatim",
        note="Article 35(1) and 35(3), in the Regulation's own vocabulary.",
        judgements=(
            _J("gdpr-full:635", ANSWERS, "Article 35(1): the high-risk trigger."),
            _J("seed:gdpr-excerpts:25", ANSWERS, "Article 35(1) as a seeded excerpt."),
            _J("gdpr-full:636", SUPPORTS, "Opens 35(3), the enumerated mandatory cases."),
            _J(
                "gdpr-full:637",
                SUPPORTS,
                "35(3)(a): systematic and extensive automated evaluation."
            ),
            _J(
                "gdpr-full:638",
                SUPPORTS,
                "35(3)(b) and (c): special categories, public monitoring."
            ),
            _J("gdpr-full:634", SUPPORTS, "Carries the Article 35 heading and the section title."),
        ),
    ),
    Question(
        id="v-info-collected",
        text=(
            "What information must be provided to the data subject where personal data are "
            "collected from the data subject?"
        ),
        kind="verbatim",
        note="Article 13's heading, almost word for word.",
        judgements=(
            _J("gdpr-full:517", ANSWERS, "Article 13(1) and its points (a) and (b)."),
            _J("gdpr-full:518", SUPPORTS, "13(1)(c) to (e): purposes, legal basis, recipients."),
            _J("gdpr-full:519", SUPPORTS, "13(1)(f): third-country transfers and safeguards."),
            _J(
                "gdpr-full:520",
                SUPPORTS,
                "13(2)(a): the storage period, part of what must be disclosed at collection."
            ),
            _J("gdpr-full:521", SUPPORTS, "13(2)(b) and (c): the data subject's rights."),
            _J("gdpr-full:516", SUPPORTS, "Carries the Article 13 heading and the section title."),
        ),
    ),
    # ------------------------------------------------------------ paraphrase
    Question(
        id="p-breach-speed",
        text="How quickly do we have to tell the regulator if customer records are lost or stolen?",
        kind="paraphrase",
        note=(
            "Article 33(1) again, this time with none of its words: 'regulator' for supervisory "
            "authority, 'lost or stolen' for personal data breach, 'how quickly' for the deadline. "
            "Paired with v-breach-72h so the two can be compared directly."
        ),
        judgements=(
            _J(
                "gdpr-full:625",
                ANSWERS,
                "Article 33(1): the 72-hour deadline the question asks for."
            ),
            _J("seed:gdpr-excerpts:23", ANSWERS, "Article 33(1) as a seeded excerpt."),
            _J(
                "gdpr-full:626",
                SUPPORTS,
                "Reasons must accompany a notification made after 72 hours."
            ),
        ),
    ),
    Question(
        id="p-delete-me",
        text=(
            "Can a person insist that a company wipe everything it holds about them, and when "
            "can the company refuse?"
        ),
        kind="paraphrase",
        note="Article 17(1) and 17(3), asked without the words 'erasure' or 'data subject'.",
        judgements=(
            _J("gdpr-full:546", ANSWERS, "Article 17(1): the right to obtain erasure."),
            _J("seed:gdpr-excerpts:14", ANSWERS, "Article 17(1) as a seeded excerpt."),
            _J(
                "gdpr-full:551",
                ANSWERS,
                "Article 17(3): the exceptions. This answers the second half of the question, "
                "which is the half a real researcher actually needs.",
            ),
            _J("gdpr-full:547", SUPPORTS, "The grounds on which erasure may be demanded."),
            _J("gdpr-full:548", SUPPORTS, "Further grounds, 17(1)(c) to (e)."),
        ),
    ),
    Question(
        id="p-tell-people",
        text="What do we have to tell people at the moment we collect their details from them?",
        kind="paraphrase",
        note="Article 13, with 'people' for data subject and 'details' for personal data.",
        judgements=(
            _J(
                "gdpr-full:517",
                ANSWERS,
                "Article 13(1): the information to be provided at collection."
            ),
            _J("gdpr-full:518", SUPPORTS, "Purposes, legal basis and recipients."),
            _J("gdpr-full:520", SUPPORTS, "13(2): the further information required."),
            _J("gdpr-full:521", SUPPORTS, "13(2)(b): the rights that must be mentioned."),
        ),
    ),
    Question(
        id="p-penalty-size",
        text="How large can the financial penalties get for a company that breaks these rules?",
        kind="paraphrase",
        note=(
            "Article 83(4) and (5), asked with 'financial penalties' rather than 'administrative "
            "fines' and 'company' rather than 'undertaking'."
        ),
        judgements=(
            _J("gdpr-full:935", ANSWERS, "The upper tier: 20 000 000 EUR or 4 % of turnover."),
            _J("gdpr-full:933", ANSWERS, "The lower tier: 10 000 000 EUR or 2 % of turnover."),
            _J("seed:gdpr-excerpts:29", ANSWERS, "Article 83(5) as a seeded excerpt."),
            _J(
                "gdpr-full:937",
                SUPPORTS,
                "83(6): the same ceiling for ignoring an authority's order."
            ),
            _J(
                "gdpr-full:927",
                SUPPORTS,
                "83(1): fines must be effective, proportionate and dissuasive — the qualifier a "
                "reader needs before quoting a ceiling.",
            ),
        ),
    ),
    Question(
        id="p-risk-check",
        text=(
            "When do we need to assess in advance whether a new project is risky for the people "
            "whose data it uses?"
        ),
        kind="paraphrase",
        note="Article 35, without the phrase 'data protection impact assessment' anywhere in it.",
        judgements=(
            _J(
                "gdpr-full:635",
                ANSWERS,
                "Article 35(1): assessment required prior to the processing."
            ),
            _J("seed:gdpr-excerpts:25", ANSWERS, "Article 35(1) as a seeded excerpt."),
            _J("gdpr-full:637", SUPPORTS, "35(3)(a): one of the cases where it is mandatory."),
            _J("gdpr-full:638", SUPPORTS, "35(3)(b) and (c): the other mandatory cases."),
        ),
    ),
    Question(
        id="p-privacy-officer",
        text=(
            "Do we have to appoint someone inside the company to be responsible for data "
            "protection, and what do they do?"
        ),
        kind="paraphrase",
        note=(
            "Article 37(1) is the answer; Article 39 supports it. Filed as a paraphrase rather "
            "than as multi-hop because the kind labels describe the wording challenge, and the "
            "question's core — must we appoint one — is answered by a single article."
        ),
        judgements=(
            _J(
                "gdpr-full:655",
                ANSWERS,
                "Article 37(1): when a data protection officer must be designated."
            ),
            _J("seed:gdpr-excerpts:26", ANSWERS, "Article 37(1) as a seeded excerpt."),
            _J("gdpr-full:656", SUPPORTS, "37(1)(b): the large-scale monitoring trigger."),
            _J("gdpr-full:657", SUPPORTS, "37(1)(c): the special-categories trigger."),
            _J(
                "gdpr-full:665",
                SUPPORTS,
                "Article 39(1)(a): to inform and advise — 'what they do'."
            ),
            _J("gdpr-full:666", SUPPORTS, "Article 39(1)(b): to monitor compliance."),
        ),
    ),
    Question(
        id="p-send-abroad",
        text=(
            "What do we need in place before sending personal records to a company outside the "
            "European Union?"
        ),
        kind="paraphrase",
        note=(
            "Chapter V, asked as an operations question. 'Outside the European Union' rather "
            "than 'third country', 'personal records' rather than 'personal data'."
        ),
        judgements=(
            _J(
                "gdpr-full:714",
                ANSWERS,
                "Article 44: the general principle governing any transfer."
            ),
            _J("seed:gdpr-excerpts:27", ANSWERS, "Article 44 as a seeded excerpt."),
            _J("gdpr-full:716", SUPPORTS, "Article 45(1): the adequacy-decision route."),
            _J("gdpr-full:729", SUPPORTS, "Article 46(1): the appropriate-safeguards route."),
            _J(
                "gdpr-full:730",
                SUPPORTS,
                "46(2): binding corporate rules and the other instruments."
            ),
            _J("gdpr-full:731", SUPPORTS, "46(2)(c) and (d): standard data protection clauses."),
        ),
    ),
    # --------------------------------------------------------------- synonym
    Question(
        id="s-controller",
        text="Who is the organisation that decides why and how personal information is used?",
        kind="synonym",
        note=(
            "'Controller' never appears. 'Decides why and how' stands in for 'determines the "
            "purposes and means', 'personal information' for 'personal data'. The definition "
            "chunk shares almost no content word with the question."
        ),
        judgements=(
            _J("gdpr-full:444", ANSWERS, "Article 4(7): the definition of controller."),
            _J("seed:gdpr-excerpts:0", ANSWERS, "Article 4(7) as a seeded excerpt."),
            _J(
                "gdpr-full:445",
                SUPPORTS,
                "Article 4(8): the processor, the contrasting role. A reader asking who decides "
                "needs to know who merely acts on instructions.",
            ),
            _J("seed:gdpr-excerpts:1", SUPPORTS, "Article 4(8) as a seeded excerpt."),
        ),
    ),
    Question(
        id="s-processor",
        text=(
            "What rules apply to a supplier that handles personal information only on our "
            "instructions?"
        ),
        kind="synonym",
        note="'Supplier' for processor; the word 'processor' does not appear in the question.",
        judgements=(
            _J(
                "gdpr-full:594",
                ANSWERS,
                "Article 28(1): sufficient guarantees required of a processor."
            ),
            _J(
                "gdpr-full:596",
                ANSWERS,
                "Article 28(3): the mandatory contract and what it must set out."
            ),
            _J("seed:gdpr-excerpts:16", ANSWERS, "Article 28(1) as a seeded excerpt."),
            _J("seed:gdpr-excerpts:18", ANSWERS, "Article 28(3) as a seeded excerpt."),
            _J("gdpr-full:597", SUPPORTS, "28(3)(a): processing only on documented instructions."),
            _J("seed:gdpr-excerpts:19", SUPPORTS, "Article 28(3)(a) as a seeded excerpt."),
            _J(
                "gdpr-full:445",
                SUPPORTS,
                "Article 4(8): what a processor is, for a reader who does not know the term."
            ),
        ),
    ),
    Question(
        id="s-special-data",
        text=(
            "What extra conditions apply to information about someone's health, religion or "
            "political opinions?"
        ),
        kind="synonym",
        note=(
            "'Special categories of personal data' is the corpus's term and appears nowhere in "
            "the question; the three examples are named instead, which is how a researcher "
            "arrives at this article."
        ),
        judgements=(
            _J(
                "gdpr-full:489",
                ANSWERS,
                "Article 9(1): the prohibition, naming exactly these categories."
            ),
            _J("gdpr-full:490", SUPPORTS, "9(2)(a): the explicit-consent exception."),
            _J("gdpr-full:491", SUPPORTS, "9(2)(b): the employment and social security exception."),
            _J(
                "gdpr-full:488",
                SUPPORTS,
                "Carries the Article 9 heading, which names the category the question describes."
            ),
        ),
    ),
    Question(
        id="s-watchdog",
        text="Which national body enforces these rules and what powers does it have?",
        kind="synonym",
        note=(
            "'National body' and 'enforces' for 'supervisory authority' and its "
            "Article 58 powers."
        ),
        judgements=(
            _J(
                "gdpr-full:766",
                ANSWERS,
                "Article 51(1): each Member State provides an independent authority."
            ),
            _J("gdpr-full:800", ANSWERS, "Article 58(1): the investigative powers."),
            _J("gdpr-full:802", SUPPORTS, "Opens 58(2), the corrective powers."),
            _J("gdpr-full:801", SUPPORTS, "Further investigative powers, 58(1)(c) to (e)."),
            _J("gdpr-full:764", SUPPORTS, "Carries the Chapter VI and Article 51 headings."),
        ),
    ),
    Question(
        id="s-take-my-data-elsewhere",
        text=(
            "Can someone get their information back in a format they can hand to a competing "
            "service?"
        ),
        kind="synonym",
        note="'Data portability' never appears; the question describes the effect instead.",
        judgements=(
            _J(
                "gdpr-full:560",
                ANSWERS,
                "Article 20(1): the right to receive and transmit the data."
            ),
            _J("gdpr-full:561", SUPPORTS, "20(2): direct controller-to-controller transmission."),
            _J("gdpr-full:559", SUPPORTS, "Carries the Article 20 heading."),
        ),
    ),
    Question(
        id="s-coded-identifiers",
        text=(
            "What does it mean to replace identifying details with a code that is kept "
            "separately?"
        ),
        kind="synonym",
        note=(
            "A description of pseudonymisation that never uses the word. The one content word "
            "shared with the definition is 'separately'."
        ),
        judgements=(
            _J("gdpr-full:442", ANSWERS, "Article 4(5): the definition of pseudonymisation."),
            _J("gdpr-full:64", SUPPORTS, "Recital 28: what pseudonymisation is for."),
            _J(
                "gdpr-full:620",
                SUPPORTS,
                "Article 32(1)(a): pseudonymisation as a security measure."
            ),
            _J(
                "gdpr-full:584",
                SUPPORTS,
                "Article 25(1): pseudonymisation as a data-protection-by-design measure."
            ),
            _J("seed:gdpr-excerpts:15", SUPPORTS, "Article 25(1) as a seeded excerpt."),
        ),
    ),
    # -------------------------------------------------------------- multi_hop
    Question(
        id="m-breach-two-notices",
        text=(
            "After a breach, when must the regulator be told and when must the affected "
            "individuals be told as well?"
        ),
        kind="multi_hop",
        note=(
            "Article 33 answers the first half and Article 34 the second, and the two thresholds "
            "differ (any risk versus high risk). A mode that returns only Article 33 has answered "
            "half the question — which recall@5 sees and MRR does not."
        ),
        judgements=(
            _J("gdpr-full:625", ANSWERS, "Article 33(1): 72 hours, to the supervisory authority."),
            _J(
                "gdpr-full:630",
                ANSWERS,
                "Article 34(1): without undue delay, to the data subject, on high risk."
            ),
            _J("seed:gdpr-excerpts:23", ANSWERS, "Article 33(1) as a seeded excerpt."),
            _J("seed:gdpr-excerpts:24", ANSWERS, "Article 34(1) as a seeded excerpt."),
            _J(
                "gdpr-full:631",
                SUPPORTS,
                "34(2): what the communication to the individual must say."
            ),
            _J("gdpr-full:633", SUPPORTS, "34(3): when the individual need not be told."),
        ),
    ),
    Question(
        id="m-basis-and-health",
        text=(
            "Which lawful grounds allow processing, and what additional grounds are "
            "needed for health data?"
        ),
        kind="multi_hop",
        note=(
            "Article 6 gives the six bases; Article 9 adds a separate prohibition and its own "
            "exceptions for health data. Answering from Article 6 alone is a plausible and wrong "
            "answer, which is what makes this a hop rather than a long question."
        ),
        judgements=(
            _J("gdpr-full:471", ANSWERS, "Article 6(1) opening and point (a)."),
            _J("gdpr-full:489", ANSWERS, "Article 9(1): the prohibition on health data."),
            _J(
                "gdpr-full:490",
                ANSWERS,
                "Article 9(2)(a): the additional ground of explicit consent."
            ),
            _J(
                "gdpr-full:472",
                SUPPORTS,
                "6(1)(b) to (d): contract, legal obligation, vital interests."
            ),
            _J(
                "gdpr-full:473",
                SUPPORTS,
                "6(1)(f): legitimate interests, the sixth of the lawful grounds."
            ),
            _J("seed:gdpr-excerpts:9", SUPPORTS, "Article 6(1)(a) as a seeded excerpt."),
            _J("seed:gdpr-excerpts:10", SUPPORTS, "Article 6(1)(f) as a seeded excerpt."),
        ),
    ),
    Question(
        id="m-access-and-fix",
        text=(
            "What can a person demand to see about themselves, and how can they get inaccurate "
            "details corrected?"
        ),
        kind="multi_hop",
        note="Article 15 for access, Article 16 for rectification — adjacent but distinct rights.",
        judgements=(
            _J(
                "gdpr-full:539",
                ANSWERS,
                "Article 15(1): the right of access and what comes with it."
            ),
            _J(
                "gdpr-full:545",
                ANSWERS,
                "Article 16: the right to rectification. "
                "Article 16's second sentence: completing incomplete data."
            ),
            _J("seed:gdpr-excerpts:13", ANSWERS, "Article 15(1) as a seeded excerpt."),
            _J(
                "gdpr-full:540",
                SUPPORTS,
                "15(1)(b) and (c): the categories and recipients disclosed."
            ),
            _J("gdpr-full:541", SUPPORTS, "15(1)(d) to (g): retention, rights, complaint, source."),
        ),
    ),
    Question(
        id="m-records-and-exemption",
        text=(
            "Who has to keep records of processing activities, and which organisations "
            "are exempt from that?"
        ),
        kind="multi_hop",
        note=(
            "The hop is between Article 30(1) and Article 30(5), which land eight chunks apart. "
            "One article rather than two, and kept here anyway: the retrieval property being "
            "exercised — surface two separated passages, not just the first — is the same, and "
            "the exemption is the half a reader is actually looking for."
        ),
        judgements=(
            _J("gdpr-full:611", ANSWERS, "Article 30(1): every controller must maintain a record."),
            _J(
                "gdpr-full:618",
                ANSWERS,
                "Article 30(5): the fewer-than-250-persons exemption and its conditions."
            ),
            _J("seed:gdpr-excerpts:21", ANSWERS, "Article 30(1) as a seeded excerpt."),
            _J("gdpr-full:614", SUPPORTS, "Article 30(2): the processor's parallel obligation."),
        ),
    ),
    Question(
        id="m-transfer-routes",
        text=(
            "How can data be sent to a third country without an adequacy decision, and what does "
            "an adequacy decision itself involve?"
        ),
        kind="multi_hop",
        note=(
            "Article 46 answers the first half only by reference to Article 45, which answers the "
            "second. This is the one question whose two halves are worded to pull in opposite "
            "directions lexically."
        ),
        judgements=(
            _J(
                "gdpr-full:729",
                ANSWERS,
                "Article 46(1): transfers in the absence of an adequacy decision."
            ),
            _J(
                "gdpr-full:730",
                ANSWERS,
                "46(2): the instruments that provide appropriate safeguards."
            ),
            _J(
                "gdpr-full:716",
                ANSWERS,
                "Article 45(1): what an adequacy decision is. "
                "45(2): opening of the elements the Commission assesses."
            ),
            _J(
                "gdpr-full:717",
                SUPPORTS,
                "45(2)(a): the rule of law and access by public authorities."
            ),
            _J("gdpr-full:731", SUPPORTS, "46(2)(c) and (d): standard data protection clauses."),
        ),
    ),
    # ---------------------------------------------------------- unanswerable
    #
    # No judgements, by construction. Each `note` records what makes the
    # question unanswerable *from this corpus* and, where there is one, the
    # near-miss the corpus does contain — because a question that is
    # unanswerable only because nothing in the corpus is even adjacent to it is
    # a question about vocabulary, not about answerability.
    #
    # Absence was verified against the corpus text with whitespace collapsed and
    # case folded, so a claim of absence cannot be an artefact of the parser's
    # spacing. The probes and their counts are in the task report.
    Question(
        id="u-min-retention",
        text=(
            "What is the minimum period a controller must retain personal data for after the "
            "processing purpose has ended?"
        ),
        kind="unanswerable",
        note=(
            "The Regulation sets no minimum retention period anywhere. Article 5(1)(e) and "
            "Article 13(2)(a) constrain the *maximum* and require the period to be disclosed; a "
            "floor is left entirely to other law. Near-miss: several chunks discuss storage "
            "periods, so this is hard rather than obscure."
        ),
        judgements=(),
    ),
    Question(
        id="u-daily-damages",
        text=(
            "How much compensation per day must a controller pay for a late response to an "
            "access request?"
        ),
        kind="unanswerable",
        note=(
            "Article 82 gives a right to compensation for damage actually suffered and fixes no "
            "tariff; Article 12(3) sets the one-month deadline but attaches no money to missing "
            "it. 'per day', 'daily' and 'statutory compensation' appear nowhere in the corpus. "
            "Near-miss: both halves of the premise exist separately, which is exactly the shape "
            "of question a retriever should be unable to answer confidently."
        ),
        judgements=(),
    ),
    Question(
        id="u-essential-services",
        text=(
            "What incident reporting duties apply to operators of essential services in the "
            "energy and transport sectors?"
        ),
        kind="unanswerable",
        note=(
            "The subject matter of the NIS Directive, not of this Regulation. 'Operators of "
            "essential services' and 'network and information system' appear nowhere. Article 33 "
            "is about personal data breaches and is not sector-specific, so a mode that returns "
            "it has returned something plausible and wrong."
        ),
        judgements=(),
    ),
    Question(
        id="u-ecb-transfers",
        text=(
            "What role does the European Central Bank play in approving transfers of personal "
            "data abroad?"
        ),
        kind="unanswerable",
        note=(
            "Chapter V gives that role to the Commission (adequacy decisions), to supervisory "
            "authorities and to the Board. The European Central Bank appears nowhere in the "
            "corpus. An institution outside the Regulation, asked about a mechanism inside it."
        ),
        judgements=(),
    ),
    Question(
        id="u-board-appeal-fee",
        text=(
            "What fee must a data subject pay to bring an appeal before the European Data "
            "Protection Board?"
        ),
        kind="unanswerable",
        note=(
            "The Board has no appellate jurisdiction over an individual's complaint; remedies go "
            "to a supervisory authority (Article 77) or to a court (Articles 78 and 79). "
            "Near-miss, and a strong one: the corpus does contain 'a reasonable fee' twice "
            "(Article 12(5) and Article 15(3)), for manifestly unfounded or repeated requests. "
            "So the words are present and the answer is not."
        ),
        judgements=(),
    ),
    Question(
        id="u-log-retention",
        text="How many hours may a processor keep server access logs before it must delete them?",
        kind="unanswerable",
        note=(
            "Article 32 requires security measures appropriate to the risk and prescribes no "
            "figure; 'access log', 'server log' and 'logs' appear nowhere in the corpus. Paired "
            "against v-breach-72h deliberately: both ask for a number of hours, and only one of "
            "them has an answer."
        ),
        judgements=(),
    ),
)
