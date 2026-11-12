"""The chat LLM budget is optional: no cap unless --llm-budget-usd is given."""

from adt4_console.chat import UsageLedger
from adt4_console.settings import Settings


def test_default_settings_have_no_llm_cap():
    assert Settings().llm_budget_usd is None


def test_ledger_without_cap_never_runs_out(tmp_path):
    ledger = UsageLedger(str(tmp_path / "usage.json"), None)
    ledger.data["spent_usd"] = 1e6
    assert ledger.remaining() > 0


def test_ledger_with_cap_runs_out(tmp_path):
    ledger = UsageLedger(str(tmp_path / "usage.json"), 10.0)
    ledger.data["spent_usd"] = 10.06
    assert ledger.remaining() <= 0
