"""Jira state transitions through the public transport seam."""
from as_engine.index import Operation
from as_engine.simulation import JiraSimulation, JiraSimulationStore, SimulationStore


def op(name):
    return Operation(name, "GET", "/simulation", [], None, None, [], None, None, {}, [])


def test_issue_transition_search_and_detached_state():
    store = JiraSimulationStore()
    sim = JiraSimulation(store)
    before = store.snapshot()
    response = sim.call(op("searchAndReconsileIssuesUsingJql"), {"jql": "project = SBX AND status = Open", "maxResults": 1}, None)
    assert response.body["nextPageToken"] == "1"
    assert sim.call(op("doTransition"), {"issueIdOrKey": "SBX-1"}, {"transition": {"id": "31"}}).status == 204
    found = sim.call(op("searchAndReconsileIssuesUsingJql"), {"jql": "project = SBX AND status = Open"}, None)
    assert [x["key"] for x in found.body["issues"]] == ["SBX-2"]
    assert before["issues"][0]["fields"]["status"]["name"] == "Open"
    assert sim.call(op("doTransition"), {"issueIdOrKey": "SBX-2"}, {"transition": {"id": "bad"}}).status == 400
    assert store.snapshot()["issues"][1]["fields"]["status"]["name"] == "Open"


def test_create_link_worklog_and_delete_model_observable_mutations():
    store = JiraSimulationStore()
    sim = JiraSimulation(store)
    created = sim.call(op("createIssue"), {}, {"fields": {"project": {"key": "SBX"}, "summary": "Clone", "parent": {"key": "SBX-1"}}})
    assert created.body["key"] == "SBX-3"
    assert sim.call(op("linkIssues"), {}, {"inwardIssue": {"key": "SBX-1"}, "outwardIssue": {"key": "SBX-3"}, "type": {"name": "Cloners"}}).status == 201
    assert sim.call(op("addWorklog"), {"issueIdOrKey": "SBX-3"}, {"timeSpentSeconds": 600}).status == 201
    assert sim.call(op("getIssueWorklog"), {"issueIdOrKey": "SBX-3"}, None).body["worklogs"][0]["timeSpentSeconds"] == 600
    assert sim.call(op("deleteIssue"), {"issueIdOrKey": "SBX-1"}, None).status == 400
    assert sim.call(op("deleteIssue"), {"issueIdOrKey": "SBX-1", "deleteSubtasks": "true"}, None).status == 204
    assert [x["key"] for x in store.snapshot()["issues"]] == ["SBX-2"]


def test_jira_seed_does_not_change_confluence_and_unknown_never_falls_back():
    before = SimulationStore().snapshot()
    sim = JiraSimulation(JiraSimulationStore())
    assert sim.call(op("getFields"), {}, None).body[1]["schema"]["custom"].endswith(":textarea")
    assert sim.call(op("getPageById"), {}, None).status == 501
    assert sim.call(op("searchAndReconsileIssuesUsingJql"), {"jql": "arbitrary()"}, None).status == 400
    assert SimulationStore().snapshot() == before


def test_customer_creation_and_membership_is_ordered():
    store = JiraSimulationStore()
    sim = JiraSimulation(store)
    assert sim.call(op("addCustomers"), {"serviceDeskId": 1}, {"accountIds": ["missing"]}).status == 400
    response = sim.call(op("createCustomer"), {}, {"email": "a@example.test", "displayName": "A"})
    account = response.body["accountId"]
    assert sim.call(op("addCustomers"), {"serviceDeskId": 1}, {"accountIds": [account]}).status == 204
    assert store.snapshot()["desk_customers"] == {"1": [account]}


def test_sprint_full_and_partial_updates_have_distinct_wire_semantics():
    store = JiraSimulationStore({"sprints": [{"id": 1, "name": "One", "goal": "Keep", "startDate": "2026-09-01", "endDate": "2026-09-14", "state": "active"}]})
    sim = JiraSimulation(store)
    partial = sim.call(op("partiallyUpdateSprint"), {"sprintId": 1}, {"state": "closed"})
    assert partial.body["name"] == "One" and partial.body["goal"] == "Keep"
    assert partial.body["startDate"] == "2026-09-01"
    full = sim.call(op("updateSprint"), {"sprintId": 1}, {"state": "active"})
    assert full.body["name"] is None and full.body["goal"] is None
    assert full.body["startDate"] is None and full.body["state"] == "active"
