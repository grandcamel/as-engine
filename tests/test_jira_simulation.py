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


def create_issue(sim, project="SBX", **fields):
    response = sim.call(
        op("createIssue"),
        {},
        {"fields": {"project": {"key": project}, "summary": "New task", **fields}},
    )
    assert response.status == 201
    return response.body


def test_deleted_highest_created_identity_is_not_reused_across_transports():
    store = JiraSimulationStore()
    sim = JiraSimulation(store)
    first = create_issue(sim)
    assert first == {"id": "3", "key": "SBX-3"}
    assert sim.call(op("deleteIssue"), {"issueIdOrKey": first["key"]}, None).status == 204
    sim.close()
    another = JiraSimulation(store)
    second = create_issue(another)
    assert int(second["id"]) > int(first["id"])
    assert int(second["key"].rsplit("-", 1)[1]) > int(first["key"].rsplit("-", 1)[1])
    for identity in first.values():
        assert another.call(op("getIssue"), {"issueIdOrKey": identity}, None).status == 404


def test_deleted_seed_maxima_survive_even_when_all_issues_are_deleted():
    store = JiraSimulationStore(
        {
            "issues": [
                {"id": "400", "key": "SBX-27", "fields": {}},
                {"id": "900", "key": "SBX-4", "fields": {}},
            ]
        }
    )
    sim = JiraSimulation(store)
    for key in ("SBX-27", "SBX-4"):
        assert sim.call(op("deleteIssue"), {"issueIdOrKey": key}, None).status == 204
    assert store.snapshot()["issues"] == []
    assert create_issue(sim) == {"id": "901", "key": "SBX-28"}


def test_project_keys_are_independent_and_issue_ids_are_global():
    store = JiraSimulationStore(
        {
            "issues": [
                {"id": "80", "key": "SBX-20", "fields": {}},
                {"id": "120", "key": "OTHER-7", "fields": {}},
            ]
        }
    )
    sim = JiraSimulation(store)
    assert sim.call(op("deleteIssue"), {"issueIdOrKey": "OTHER-7"}, None).status == 204
    created = [create_issue(sim, project) for project in ("SBX", "OTHER", "NEW", "SBX")]
    assert [row["key"] for row in created] == ["SBX-21", "OTHER-8", "NEW-1", "SBX-22"]
    assert [row["id"] for row in created] == ["121", "122", "123", "124"]
    ids = [row["id"] for row in store.snapshot()["issues"]]
    assert len(ids) == len(set(ids))


def test_subtasks_share_allocator_and_cascade_delete_keeps_high_water_marks():
    sim = JiraSimulation(JiraSimulationStore({"issues": []}))
    parent = create_issue(sim)
    child = create_issue(
        sim, parent={"key": parent["key"]}, issuetype={"name": "Sub-task", "subtask": True}
    )
    assert parent == {"id": "1", "key": "SBX-1"}
    assert child == {"id": "2", "key": "SBX-2"}
    assert sim.call(
        op("deleteIssue"), {"issueIdOrKey": parent["key"], "deleteSubtasks": "true"}, None
    ).status == 204
    assert sim.store.snapshot()["issues"] == []
    assert create_issue(sim) == {"id": "3", "key": "SBX-3"}
    for row in (parent, child):
        for identity in row.values():
            assert sim.call(op("getIssue"), {"issueIdOrKey": identity}, None).status == 404


def test_directly_inserted_fixture_raises_retained_identity_counters():
    store = JiraSimulationStore()
    sim = JiraSimulation(store)
    store.issues.append({"id": "800", "key": "SBX-50", "fields": {}})
    created = create_issue(sim)
    assert created == {"id": "801", "key": "SBX-51"}
    for key in ("SBX-50", created["key"]):
        assert sim.call(op("deleteIssue"), {"issueIdOrKey": key}, None).status == 204
    assert create_issue(sim) == {"id": "802", "key": "SBX-52"}
