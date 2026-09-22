from corenova import golden


class _FakeCfn:
    def __init__(self, events):
        self._events = events

    def describe_stack_events(self, StackName):  # noqa: N803
        return {"StackEvents": self._events}


class _FakeAws:
    def __init__(self, events):
        self.cfn = _FakeCfn(events)


def test_stack_reason_surfaces_first_create_failed_over_recent_deletes():
    # describe_stack_events is newest-first: during rollback the head is all
    # DELETE_* noise and the actionable CREATE_FAILED sits deeper in the page.
    events = [
        {"LogicalResourceId": "Role", "ResourceStatus": "DELETE_COMPLETE", "ResourceStatusReason": ""},
        {"LogicalResourceId": "VPC", "ResourceStatus": "DELETE_IN_PROGRESS", "ResourceStatusReason": ""},
        {"LogicalResourceId": "VPC", "ResourceStatus": "CREATE_FAILED",
         "ResourceStatusReason": "The maximum number of VPCs has been reached (quota 5)"},
        {"LogicalResourceId": "VPC", "ResourceStatus": "CREATE_IN_PROGRESS", "ResourceStatusReason": ""},
    ]
    reason = golden._stack_reason(_FakeAws(events), "stack-x")
    assert "CREATE_FAILED" in reason
    assert "maximum number of VPCs" in reason
    # The first CREATE_FAILED leads the report, deletes follow as context.
    assert reason.index("CREATE_FAILED") < reason.index("DELETE_IN_PROGRESS")


def test_stack_reason_without_failure_returns_recent_events():
    events = [
        {"LogicalResourceId": "Instance", "ResourceStatus": "CREATE_IN_PROGRESS",
         "ResourceStatusReason": "Resource creation Initiated"},
    ]
    reason = golden._stack_reason(_FakeAws(events), "stack-y")
    assert reason == "Instance:CREATE_IN_PROGRESS:Resource creation Initiated"
