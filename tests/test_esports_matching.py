"""Joining a Polymarket LoL match to the same fixture on Riot's feed.

The two venues name teams differently — Riot says "Xi'an Team WE" where
Polymarket says "Team WE" — so the join is fuzzy by necessity. A wrong join is
worse than a missed one: it silently pairs one match's order book with another
match's game state, and nothing downstream can detect it.
"""
import importlib.util
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "record_esports",
    Path(__file__).resolve().parent.parent / "scripts" / "record_esports.py")
rec = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(rec)


@pytest.mark.parametrize("poly,riot,expected,why", [
    (["Team WE", "Anyone's Legend"], ["Xi'an Team WE", "Anyone's Legend"], True,
     "the real observed pair — Riot carries a city prefix Polymarket omits"),
    (["Dark Passage", "Team Phoenix"], ["Dark Passage", "Team Phoenix"], True,
     "exact match"),
    (["Dark Passage", "Team Phoenix"], ["Team Phoenix", "Dark Passage"], True,
     "home/away order differs between the venues"),
    (["Frites Esports Club", "Dynasty"], ["Frites", "Dynasty"], True,
     "noise words (esports, club) carry no identifying information"),
    (["T1", "Dplus KIA"], ["T1", "Dplus KIA"], True,
     "single-token names still match when they are equal"),
    (["Invictus Gaming", "Shenzhen NINJA"],
     ["Invictus Gaming", "Shenzhen NINJAS IN PYJAMAS"], True,
     "one name is a prefix expansion of the other"),
    (["T1", "Dplus KIA"], ["T1", "KIWOOM DRX"], False,
     "same first team, different opponent — not the same fixture"),
    (["Myth Esports", "The Bandits"], ["Myth Esports", "Senshi Esports Club"], False,
     "only one side matches"),
    (["G2 NORD", "Unicorns Of Love Sexy Edition"],
     ["G2 Esports", "Unicorns Of Love"], False,
     "G2 Esports collapses to the single token {g2} and would otherwise "
     "swallow G2 NORD, a different team in a different league"),
    ([], ["A", "B"], False, "no Polymarket teams"),
    (["A"], ["A", "B"], False, "only one Polymarket team"),
])
def test_pair_match(poly, riot, expected, why):
    assert rec._pair_match(poly, riot) is expected, why


def test_subset_matching_needs_two_real_tokens():
    """The guard that stops the G2 case. A one-token name may only match by
    equality, never by being a subset of something longer."""
    assert rec._team_match("G2 Esports", "G2 NORD") is False
    assert rec._team_match("G2 Esports", "G2 Esports") is True
    assert rec._team_match("Team WE", "Xi'an Team WE") is True


def test_noise_words_are_stripped_but_identity_is_not():
    assert rec._norm("Frites Esports Club") == {"frites"}
    assert rec._norm("Gen.G Global Academy") == {"gen", "g", "global", "academy"}


def test_empty_names_never_match():
    assert rec._team_match("", "anything") is False
    assert rec._team_match("Esports Gaming Club", "Dynasty") is False
