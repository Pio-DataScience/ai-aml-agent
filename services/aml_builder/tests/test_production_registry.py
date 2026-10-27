"""Tests for deploying a scenario once: the same detection SQL is never registered twice.

No database is needed: the Oracle connection is replaced by an in-memory fake.

Usage:
    PYTHONPATH=services/aml_builder pytest services/aml_builder/tests/test_production_registry.py
"""

import json
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from langchain_core.messages import ToolMessage

from services.aml_builder.web.api.routes import deploy as deploy_route
from services.aml_builder.web.services import production_registry as registry
from services.aml_builder.web.services import tools as aml_tools

SQL = """SELECT t.CUS_NUM, SUM(t.EQU_TRA_AMT) AS TOTAL
FROM BI_DWH.PIO_TRANSACTIONS t
WHERE t.COUNTRY_CODE = 400 AND t.INST_CODE = 1 AND t.EXPL_CODE IN ('1', '660') AND t.DEB_CRE_IND = '2'
GROUP BY t.CUS_NUM HAVING SUM(t.EQU_TRA_AMT) > 10000"""
METADATA = {"COUNTRY_CODE": 400, "INST_CODE": 1, "RISK_DEGREE": "H", "CATEG_CODE": "1"}


class FakeDB:
    """PIO_AML_PRODUCTION_SCENARIOS as a list; records every statement and whether it committed."""

    def __init__(self, rows: Optional[List[Tuple[str, str, int, str]]] = None) -> None:
        self.rows = list(rows or [])  # (SCENARIO_ID, SCENARIO_NAME, IS_ACTIVE, RAW_SQL)
        self.statements: List[str] = []
        self.commits = 0

    @contextmanager
    def connection(self) -> Any:
        pending: List[Tuple[str, str, int, str]] = []
        db = self

        class Cursor:
            def __init__(self) -> None:
                self._result: List[Any] = []

            def execute(self, sql: str, params: Optional[Dict[str, Any]] = None) -> None:
                text = " ".join(sql.split())
                db.statements.append(text)
                if text.startswith("SELECT SCENARIO_ID FROM PIO_AML_PRODUCTION_SCENARIOS WHERE"):
                    self._result = [(r[0],) for r in db.rows if r[0] == params["sid"]]
                elif text.startswith("SELECT SCENARIO_ID, SCENARIO_NAME, IS_ACTIVE, RAW_SQL"):
                    self._result = list(db.rows)
                elif text.startswith("INSERT INTO PIO_AML_PRODUCTION_SCENARIOS"):
                    pending.append((params["sid"], params["sname"], 1, params["sql"]))
                else:
                    self._result = []

            def fetchone(self) -> Any:
                return self._result[0] if self._result else None

            def fetchall(self) -> List[Any]:
                return self._result

        yield SimpleNamespace(cursor=Cursor)
        db.rows.extend(pending)  # committed only on a clean exit, like atomic_connection
        db.commits += 1


@pytest.fixture
def db(monkeypatch: pytest.MonkeyPatch) -> FakeDB:
    """A fake registry wired into production_registry."""
    fake = FakeDB()
    monkeypatch.setattr(registry, "atomic_connection", fake.connection)
    monkeypatch.setattr(registry, "init_production_scenarios_table", lambda: None)
    monkeypatch.setattr(registry, "init_scenario_metadata_table", lambda: None)
    return fake


def _save(scenario_id: str, sql: str = SQL) -> bool:
    return registry.save_production_scenario(
        scenario_id=scenario_id, scenario_name="Cash deposits over 10,000 JOD", scenario_type="CUSTOMER",
        detection_logic="Cash deposits over 10,000 JOD", raw_sql=sql, scenario_metadata=dict(METADATA))


@pytest.mark.parametrize("variant, same", [
    (SQL, True),
    ("  " + SQL.lower().replace("\n", "   ") + " ;", True),        # spacing, case, trailing ';'
    ("-- deployed from chat\n" + SQL.replace(" > ", ">"), True),     # a comment, operator spacing
    (SQL.replace("10000", "20000"), False),                          # another threshold
    (SQL.replace("'660'", "'661'"), False),                          # another code
    (SQL.replace("'1'", "'1 '"), False),                             # text inside a literal counts
    (SQL.replace("('1', '660')", "('660','1')"), True),              # the order inside IN (...)
    (SQL.replace("('1', '660')", "('1')"), False),                   # a code less
])
def test_the_fingerprint_ignores_layout_not_meaning(variant: str, same: bool) -> None:
    """Two texts of one rule match; any change to what the rule selects does not."""
    assert (registry.sql_fingerprint(variant) == registry.sql_fingerprint(SQL)) is same


def test_the_first_deploy_is_written(db: FakeDB) -> None:
    """A new detection SQL is registered once."""
    assert _save("PRD_AAAA0001") is True
    assert [r[0] for r in db.rows] == ["PRD_AAAA0001"]


def test_deploying_the_same_scenario_again_writes_nothing(db: FakeDB) -> None:
    """Live case: a second deploy registered the scenario again under a new ID; now it is refused."""
    _save("PRD_AAAA0001")

    with pytest.raises(registry.DuplicateScenarioError) as raised:
        _save("PRD_BBBB0002", sql="  " + SQL.lower() + ";")

    assert raised.value.existing["scenario_id"] == "PRD_AAAA0001"
    assert "already deployed as PRD_AAAA0001" in raised.value.user_message
    assert [r[0] for r in db.rows] == ["PRD_AAAA0001"]
    assert not any(s.startswith("INSERT") for s in db.statements[-2:])


def test_an_inactive_copy_also_blocks_and_says_so(db: FakeDB) -> None:
    """A switched-off scenario with the same SQL is not registered a second time either."""
    db.rows.append(("PRD_OLD00001", "Old rule", 0, SQL))

    with pytest.raises(registry.DuplicateScenarioError) as raised:
        _save("PRD_NEW00001")

    assert "currently inactive" in raised.value.user_message and len(db.rows) == 1


def test_a_different_rule_is_still_deployed(db: FakeDB) -> None:
    """Only the same detection SQL is blocked."""
    _save("PRD_AAAA0001")
    assert _save("PRD_CCCC0003", sql=SQL.replace("10000", "20000")) is True
    assert [r[0] for r in db.rows] == ["PRD_AAAA0001", "PRD_CCCC0003"]


def test_updating_a_registered_id_is_not_a_duplicate(db: FakeDB) -> None:
    """Saving under an ID that already exists updates it in place, as before."""
    db.rows.append(("PRD_AAAA0001", "Cash deposits", 1, SQL))
    assert _save("PRD_AAAA0001") is True
    assert not any(s.startswith("SELECT SCENARIO_ID, SCENARIO_NAME") for s in db.statements)


def test_the_deploy_button_gets_409_with_the_existing_id(db: FakeDB, monkeypatch: pytest.MonkeyPatch) -> None:
    """The side panel receives a flat body it can show: not deployed, and under which ID it already is."""
    db.rows.append(("PRD_AAAA0001", "Cash deposits", 1, SQL))

    class Graph:
        async def aget_state(self, config: Dict[str, Any]) -> Any:
            return SimpleNamespace(values={"messages": [ToolMessage(content=json.dumps({"raw_sql": SQL}), tool_call_id="t1")]})

    async def get_graph() -> Graph:
        return Graph()

    monkeypatch.setattr(deploy_route, "get_tool_driven_graph", get_graph)
    monkeypatch.setattr(deploy_route, "validate_scenario_metadata", lambda metadata: (True, []))
    app = FastAPI()
    app.include_router(deploy_route.router)

    response = TestClient(app).post("/scenario/deploy", json={"session_id": "s1", "user_id": "u", "metadata_json": METADATA})

    body = response.json()
    assert response.status_code == 409
    assert body["success"] is False and body["already_deployed"] is True and body["scenario_id"] == "PRD_AAAA0001"
    assert "already deployed as PRD_AAAA0001" in body["message"]
    assert len(db.rows) == 1


def test_the_chat_tool_answers_already_deployed(db: FakeDB, monkeypatch: pytest.MonkeyPatch) -> None:
    """Saving from the chat returns ALREADY_DEPLOYED with the message to show, and writes nothing."""
    db.rows.append(("PRD_AAAA0001", "Cash deposits", 1, SQL))
    intent = {"scenario_name": "Cash deposits over 10,000 JOD", "scenario_type": "CUSTOMER",
              "detection_logic": "Cash deposits over 10,000 JOD", "time_window": {"value": 7, "unit": "DAYS"}}
    monkeypatch.setattr(aml_tools, "validate_scenario_metadata", lambda metadata: (True, []))

    result = json.loads(aml_tools.persist_and_validate_scenario_in_dwh.invoke(
        {"intent_json": json.dumps(intent), "raw_sql": SQL, "metadata_json": json.dumps(METADATA)}))

    assert result["status"] == "ALREADY_DEPLOYED" and result["scenario_id"] == "PRD_AAAA0001"
    assert result["write_success"] is False and "already deployed" in result["user_message"]
    assert len(db.rows) == 1


def test_the_live_second_chat_is_now_the_same_scenario(db: FakeDB) -> None:
    """Live case: two chats wrote the same rule with the codes in another order and other line breaks."""
    first = """SELECT t.CUS_NUM, t.TRA_DATE, t.EQU_TRA_AMT, t.EXPL_CODE
FROM BI_DWH.PIO_TRANSACTIONS t
WHERE t.COUNTRY_CODE = 400
  AND t.INST_CODE = 1
  AND t.EXPL_CODE IN ('2', '466', '2114', '2164', '467', '2108', '3')
  AND t.DEB_CRE_IND = '1'
  AND t.EQU_TRA_AMT > 50000
  AND t.TRA_DATE >= DATE '2019-01-01' AND t.TRA_DATE < DATE '2019-04-01'"""
    second = """SELECT t.CUS_NUM, t.TRA_DATE, t.EQU_TRA_AMT, t.EXPL_CODE
FROM BI_DWH.PIO_TRANSACTIONS t
WHERE t.COUNTRY_CODE = 400 AND t.INST_CODE = 1
  AND t.EXPL_CODE IN ('2', '466', '467', '2164', '2114', '2108', '3')
  AND t.DEB_CRE_IND = '1'
  AND t.EQU_TRA_AMT > 50000
  AND t.TRA_DATE >= DATE '2019-01-01' AND t.TRA_DATE < DATE '2019-04-01'"""
    _save("PRD_BD92929B", sql=first)

    with pytest.raises(registry.DuplicateScenarioError) as raised:
        _save("PRD_9C487F0D", sql=second)

    assert raised.value.existing["scenario_id"] == "PRD_BD92929B" and len(db.rows) == 1
