"""The case of a node symbol's letter is part of the symbol, and differs by layer
and by map: objects are "O710", agents "a12", mesh places "T3349" on current
Hydra maps and "t3349" on older ones. Operators type places the way the planner
prints them ("t3349"). On the 2026-11-17 bench test, "SGET: add a box at place
t3349" ran MATCH (p:MeshPlace {nodeSymbol: 't3349'}) on a map that stores
"T3349", found nothing, and the chat told the operator the place does not exist.

run_read respells a symbol only when the map has it in the other case alone."""

from types import SimpleNamespace as NS

import pytest

from adt4_console import cypher_tools
from adt4_console.cypher_tools import match_symbol_case

# a current Hydra map: T places, O objects, R rooms, lowercase agents
HYDRA = {"T3349", "T7375", "O710", "O536", "R10", "a12", "a13"}
# an older map: lowercase t places
OLDER = {"t3349", "t7375", "O710", "a12"}


class FakeSrc:
    """A source whose database holds ``symbols``; records what it was asked."""

    def __init__(self, symbols, fail_lookup=False):
        self.symbols, self.fail_lookup = set(symbols), fail_lookup
        self.lookups, self.ran = [], []

    def db(self):
        return NS(driver=NS(session=lambda **kw: _Session(self)))


class _NoRows(list):
    def keys(self):
        return []


class _Session:
    def __init__(self, src):
        self.src = src

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def run(self, query, c=None):  # the symbol lookup
        if self.src.fail_lookup:
            raise RuntimeError("database gone")
        self.src.lookups.append(list(c))
        return [{"s": s} for s in c if s in self.src.symbols]

    def execute_read(self, work):
        src = self.src

        class Tx:
            def run(self, query):
                src.ran.append(query)
                return _NoRows()

        return getattr(work, "__wrapped__", work)(Tx())


def test_a_lowercase_place_finds_the_T_place_of_a_hydra_map():
    q = "MATCH (p:MeshPlace {nodeSymbol: 't3349'}) RETURN p.center AS center"
    assert match_symbol_case(FakeSrc(HYDRA), q) == (
        "MATCH (p:MeshPlace {nodeSymbol: 'T3349'}) RETURN p.center AS center"
    )


def test_a_lowercase_place_is_kept_on_a_map_that_stores_it_lowercase():
    q = "MATCH (p:MeshPlace {nodeSymbol: 't3349'}) RETURN p"
    assert match_symbol_case(FakeSrc(OLDER), q) == q


def test_an_uppercase_place_finds_the_t_place_of_an_older_map():
    q = "MATCH (p:MeshPlace) WHERE p.nodeSymbol = 'T3349' RETURN p"
    assert match_symbol_case(FakeSrc(OLDER), q) == (
        "MATCH (p:MeshPlace) WHERE p.nodeSymbol = 't3349' RETURN p"
    )


def test_an_agent_symbol_stays_lowercase():
    q = "MATCH (a:Agent {nodeSymbol: 'a12'}) RETURN a"
    assert match_symbol_case(FakeSrc(HYDRA), q) == q


def test_an_agent_typed_uppercase_finds_the_lowercase_agent():
    q = "MATCH (a:Agent {nodeSymbol: 'A12'}) RETURN a"
    assert match_symbol_case(FakeSrc(HYDRA), q) == (
        "MATCH (a:Agent {nodeSymbol: 'a12'}) RETURN a"
    )


def test_each_symbol_of_a_query_is_decided_by_itself():
    q = (
        'MATCH (n) WHERE n.nodeSymbol = "o710" OR n.nodeSymbol IN '
        "['a12', \"r10\", 'T7375', 't3349', 'o999'] OR n.nodeSymbol <> 'a13' RETURN n"
    )
    assert match_symbol_case(FakeSrc(HYDRA), q) == (
        'MATCH (n) WHERE n.nodeSymbol = "O710" OR n.nodeSymbol IN '
        "['a12', \"R10\", 'T7375', 'T3349', 'o999'] OR n.nodeSymbol <> 'a13' RETURN n"
    )


def test_a_symbol_the_map_has_in_both_cases_is_left_as_asked():
    src = FakeSrc({"p850", "P850"})
    for q in (
        "MATCH (p:Place {nodeSymbol: 'p850'}) RETURN p",
        "MATCH (p:MeshPlace {nodeSymbol: 'P850'}) RETURN p",
    ):
        assert match_symbol_case(src, q) == q


def test_a_symbol_the_map_does_not_have_is_left_as_asked():
    q = "MATCH (p:MeshPlace {nodeSymbol: 't99999'}) RETURN p"
    assert match_symbol_case(FakeSrc(HYDRA), q) == q


@pytest.mark.parametrize(
    "q",
    [
        # other properties, even when the value looks like a symbol
        "MATCH (o:Object {class: 'box', name: 't3349'}) RETURN o",
        # not an exact comparison
        "MATCH (o:Object) WHERE o.nodeSymbol STARTS WITH 't' RETURN o",
        "MATCH (p) WHERE toLower(p.nodeSymbol) = 't3349' RETURN p",
        # not a symbol
        "MATCH (o {nodeSymbol: 'euclid_a12'}) RETURN o",
        "MATCH (o {nodeSymbol: 'O(710)'}) RETURN o",
    ],
)
def test_other_strings_are_left_alone(q):
    assert match_symbol_case(FakeSrc(HYDRA), q) == q


def test_a_query_without_symbols_asks_the_database_nothing():
    src = FakeSrc(HYDRA)
    q = "MATCH (o:Object) WHERE o.class = 'box' RETURN count(o)"
    assert match_symbol_case(src, q) == q
    assert src.lookups == []


def test_a_failed_lookup_leaves_the_query_as_written():
    q = "MATCH (p:MeshPlace {nodeSymbol: 't3349'}) RETURN p"
    assert match_symbol_case(FakeSrc(HYDRA, fail_lookup=True), q) == q


def test_run_read_runs_the_respelled_query():
    src = FakeSrc(HYDRA)
    out = cypher_tools.run_read(src, "MATCH (p {nodeSymbol: 't3349'}) RETURN p")
    assert src.ran == ["MATCH (p {nodeSymbol: 'T3349'}) RETURN p"]
    assert out["row_count"] == 0 and "error" not in out


def test_the_schema_shows_one_symbol_per_label_as_stored():
    """So the model writes agents as a<n> and this map's places as T<n>."""

    def fake_read(src, query, max_rows=200, timeout_s=30.0):
        if "UNWIND labels(n)" in query:
            return {"rows": [{"l": "MeshPlace", "c": 201}, {"l": "Agent", "c": 277},
                             {"l": "Observation", "c": 5}]}  # fmt: skip
        if "UNWIND keys(n)" in query:
            return {"rows": [{"k": "nodeSymbol"}, {"k": "center"}]}
        if "RETURN n.nodeSymbol AS s LIMIT 1" in query:
            sym = {"MeshPlace": "T12", "Agent": "a3"}
            return {"rows": [{"s": sym[lab]} for lab in sym if f"`{lab}`" in query]}
        return {"rows": []}

    real, cypher_tools.run_read = cypher_tools.run_read, fake_read
    try:
        text = cypher_tools.describe(None)
    finally:
        cypher_tools.run_read = real
    assert '- MeshPlace: 201  (center, nodeSymbol)  symbols like "T12"' in text
    assert '- Agent: 277  (center, nodeSymbol)  symbols like "a3"' in text
    assert "- Observation: 5  (center, nodeSymbol)\n" in text + "\n"
