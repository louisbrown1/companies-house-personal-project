import asyncio
import json
from pathlib import Path

import pytest

from redteam.client import ApplicationClient, Settings
from redteam.run import garak_campaign, pyrit_campaign, summarize


@pytest.mark.parametrize("engine", ["pyrit", "garak"])
def test_framework_reaches_application_adapter(engine):
    pytest.importorskip(engine)
    cases = json.loads((Path(__file__).parents[1] / "cases.json").read_text())[:2]
    client = ApplicationClient(Settings(max_reports=2))
    errors = (
        asyncio.run(pyrit_campaign(client, cases))
        if engine == "pyrit"
        else garak_campaign(client, cases)
    )
    assert errors == []
    report = summarize(client, errors, engine=engine, case_count=2)
    assert report["counts"] == {"smoke_only": 2}
    assert report["exit_code"] == 0
