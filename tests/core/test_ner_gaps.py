"""GLiNER fills gaps: rules applied to its entities without the model or service."""

from __future__ import annotations

from templatize.detectors import DetectionContext
from templatize.detectors.ner import NerConfig, NerDetector, entities_to_candidates, fill_gaps_only
from templatize.doctypes import detect_type, is_prose
from templatize.ir import Candidate, Source, Span


def ent(text: str, value: str, label: str = "organization", nth: int = 0) -> dict[str, object]:
    start = -1
    for _ in range(nth + 1):
        start = text.index(value, start + 1)
    return {"start": start, "end": start + len(value), "label": label, "score": 0.8}


def heuristic(text: str, value: str) -> Candidate:
    start = text.index(value)
    return Candidate(
        name="x",
        name_origin="type",
        value=value,
        spans=[Span(start=start, end=start + len(value))],
        sources=[Source.REGEX],
        detectors=["regex.test"],
        confidence=0.6,
    )


def test_entities_inside_a_token_are_dropped() -> None:
    text = "Serve shop.example.com and /var/log/shop.log for Acme Ltd. today: Acme: ok"
    ents = [
        ent(text, "shop"),  # shop.example.com
        ent(text, "example.com"),  # after "shop."
        ent(text, "shop", nth=1),  # /var/log/shop.log
        ent(text, "Acme Ltd"),  # followed by "." at the end of a sentence: kept
        ent(text, "Acme", nth=1),  # followed by ": ": kept
    ]
    assert [c.value for c in entities_to_candidates(text, ents)] == ["Acme Ltd", "Acme"]


def test_ner_never_displaces_or_joins_another_detector() -> None:
    text = "Dear Ms Hughes, your balance is £1,250.00. Regards, Bristol office\n"
    hughes, amount = heuristic(text, "Hughes"), heuristic(text, "1,250.00")
    bristol = heuristic(text, "Bristol")
    ner = entities_to_candidates(
        text,
        [
            ent(text, "Ms Hughes", "person"),  # wider than Hughes
            ent(text, "£1,250.00", "money"),  # wider than 1,250.00
            ent(text, "Bristol", "location"),  # equal: would tip a merge its way
            ent(text, "Regards", "person"),  # a gap: kept
        ],
    )
    out = fill_gaps_only([hughes, amount, bristol, *ner])
    assert [c.value for c in out] == ["Hughes", "1,250.00", "Bristol", "Regards"]
    assert fill_gaps_only(ner) == ner  # nothing else found: all of it fills gaps


def test_ner_runs_on_prose_only() -> None:
    assert is_prose("letter") and is_prose("text") and is_prose("markdown")
    assert not is_prose("ini") and not is_prose("kubernetes") and not is_prose("nope")
    # An unreachable service would give a notice; a config never calls it at all.
    det = NerDetector(NerConfig(url="http://127.0.0.1:9", timeout=1))
    conf = "[pgbouncer]\nlisten_port = 6432\nauth_type = scram-sha-256\n"
    res = det.detect(DetectionContext(text=conf, doc_type=detect_type(conf)))
    assert res.candidates == [] and res.notices == []
    letter = "Dear Alice,\n\nThank you.\n\nYours sincerely,\nBob\n"
    res = det.detect(DetectionContext(text=letter, doc_type=detect_type(letter)))
    assert res.notices and res.notices[0].kind == "detector_unavailable"
