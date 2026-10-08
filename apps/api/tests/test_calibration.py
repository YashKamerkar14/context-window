from context_window.calibration import Pair, Scored, choose_thresholds
from context_window.schemas.enums import Beat


def _scored(similarity: float, same: bool, n: int) -> Scored:
    pair = Pair(
        id=n,
        first="a",
        second="b",
        same_event=same,
        origin="test",
        beats=[Beat.INDUSTRY, Beat.INDUSTRY],
    )
    return Scored(pair, similarity)


def test_thresholds_come_from_the_gap_with_margins() -> None:
    same = [0.95, 0.93, 0.90, 0.88, 0.86, 0.84, 0.80, 0.78, 0.75, 0.74] * 2
    different = [0.70, 0.62, 0.60, 0.55]
    scored = [_scored(s, True, i) for i, s in enumerate(same)]
    scored += [_scored(s, False, 100 + i) for i, s in enumerate(different)]
    t = choose_thresholds(scored)
    assert t.ask == 0.73  # 0.03 above the most similar different-event pair
    assert t.recall_at_ask == 1.0
    # 95% of 20 is 19 pairs, which reaches down to the first 0.74 pair; merge never
    # goes below ask.
    assert t.merge == 0.74
    assert t.precision_at_merge == 1.0 and t.recall_at_merge == 1.0
    assert t.suppress == 0.77
    assert t.band_pairs == 0


def test_merge_stays_above_ask_when_the_classes_overlap() -> None:
    scored = [_scored(s, True, i) for i, s in enumerate([0.95, 0.90, 0.85, 0.60])]
    scored += [_scored(0.80, False, 9)]
    t = choose_thresholds(scored)
    # 95% of 4 pairs is all 4, down to 0.60, so merge falls back to ask.
    assert t.ask == 0.83 and t.merge == 0.83 and t.suppress == 0.86
    assert t.band_pairs == 0 and t.recall_at_merge == 0.75
