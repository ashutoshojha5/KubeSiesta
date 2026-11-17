import pytest

from app.config import Settings
from app.eks import EksNodePools


def group(name, desired, min_size=None, max_size=None, instances=None):
    n = len(instances) if instances is not None else desired
    return {
        "AutoScalingGroupName": f"eksctl-demo-nodegroup-{name}-NodeGroup-X",
        "MinSize": desired if min_size is None else min_size,
        "MaxSize": desired if max_size is None else max_size,
        "DesiredCapacity": desired,
        "Tags": [{"Key": "alpha.eksctl.io/nodegroup-name", "Value": name}],
        "Instances": [{"InstanceType": "m5.xlarge", "LifecycleState": "InService"}] * n,
    }


def managed_group(name, desired, min_size=None, max_size=None):
    g = group(name, desired, min_size, max_size)
    g["AutoScalingGroupName"] = f"eks-{name}-X"
    g["Tags"] = [{"Key": "eks:nodegroup-name", "Value": name}]
    return g


class FakeAsg:
    def __init__(self, groups):
        self.groups = {g["AutoScalingGroupName"]: g for g in groups}
        self.updates = []

    def get_paginator(self, _):
        outer = self

        class P:
            def paginate(self, Filters):
                assert Filters == [{"Name": "tag-key", "Values": ["kubernetes.io/cluster/demo"]}]
                return [{"AutoScalingGroups": list(outer.groups.values())}]
        return P()

    def describe_auto_scaling_groups(self, AutoScalingGroupNames):
        return {"AutoScalingGroups": [self.groups[n] for n in AutoScalingGroupNames]}

    def update_auto_scaling_group(self, AutoScalingGroupName, MinSize, MaxSize, DesiredCapacity):
        self.updates.append((AutoScalingGroupName, MinSize, MaxSize, DesiredCapacity))
        self.resize(AutoScalingGroupName, MinSize, MaxSize, DesiredCapacity)

    def resize(self, AutoScalingGroupName, MinSize, MaxSize, DesiredCapacity):
        g = self.groups[AutoScalingGroupName]
        g.update(MinSize=MinSize, MaxSize=MaxSize, DesiredCapacity=DesiredCapacity)
        g["Instances"] = g["Instances"][:DesiredCapacity] + [
            {"InstanceType": "m5.xlarge", "LifecycleState": "InService"}] * max(0, DesiredCapacity - len(g["Instances"]))


class FakeEks:
    """EKS resizes the managed nodegroup's ASG itself, and rejects maxSize 0."""

    def __init__(self, asg):
        self.asg = asg
        self.updates = []

    def update_nodegroup_config(self, clusterName, nodegroupName, scalingConfig):
        assert clusterName == "demo" and scalingConfig["maxSize"] >= 1
        self.updates.append((nodegroupName, scalingConfig["minSize"], scalingConfig["maxSize"],
                             scalingConfig["desiredSize"]))
        self.asg.resize(f"eks-{nodegroupName}-X", scalingConfig["minSize"], scalingConfig["maxSize"],
                        scalingConfig["desiredSize"])


@pytest.fixture
def pools(monkeypatch):
    monkeypatch.setenv("SIESTA_PROVIDER", "eks")
    monkeypatch.setenv("SIESTA_AWS_REGION", "us-east-1")
    monkeypatch.setenv("SIESTA_EKS_CLUSTER_NAME", "demo")
    monkeypatch.setenv("SIESTA_SYSTEM_NODEPOOLS", '["system", "system-1"]')
    monkeypatch.setenv("SIESTA_AUTOSCALED_NODEPOOLS", '["demo-scaled-1"]')
    monkeypatch.setenv("SIESTA_PROTECTED_NODEPOOLS", '["keep"]')
    asg = FakeAsg([
        group("system", 1), group("demo", 3), group("demo-scaled", 0, 0, 1), group("keep", 1),
        managed_group("system-1", 1), managed_group("demo-1", 3), managed_group("demo-scaled-1", 0, 0, 1)])
    clients = {"autoscaling": asg, "eks": FakeEks(asg)}
    monkeypatch.setattr("boto3.client", lambda service, **k: clients[service])
    return EksNodePools(Settings())


def test_list_maps_groups_to_pools(pools):
    result = {p.name: p for p in pools.list()}
    assert [p.name for p in pools.list()][0] == "system"  # system first
    assert result["system"].mode == "System" and result["demo"].mode == "User"
    assert result["demo"].count == 3 and result["demo"].vm_size == "m5.xlarge"
    assert result["demo-scaled"].autoscaling and not result["demo"].autoscaling
    assert result["keep"].protected
    assert result["system-1"].mode == "System" and not result["demo-1"].autoscaling
    assert result["demo-scaled-1"].autoscaling


def test_scale_sets_min_max_desired(pools):
    pools.scale("demo", 0)
    assert pools.asg.updates == [("eksctl-demo-nodegroup-demo-NodeGroup-X", 0, 0, 0)]
    assert pools.get("demo").count == 0
    pools.scale("demo", 3)
    assert pools.get("demo").count == 3 and not pools.get("demo").autoscaling


def test_scale_managed_uses_eks_api(pools):
    pools.scale("demo-1", 0)
    assert pools.eks.updates == [("demo-1", 0, 1, 0)] and pools.asg.updates == []
    # Asleep it is min 0 / max 1, but still a fixed pool the schedule can wake
    assert pools.get("demo-1").count == 0 and not pools.get("demo-1").autoscaling
    pools.scale("demo-1", 3)
    assert pools.eks.updates[-1] == ("demo-1", 3, 3, 3)
    assert pools.get("demo-1").count == 3


def test_scale_managed_noop_skips_api(pools):
    pools.scale("demo-1", 3)
    assert pools.eks.updates == []


@pytest.mark.parametrize("name,count,error", [
    ("system", 0, ValueError), ("demo-scaled", 1, ValueError), ("keep", 0, PermissionError), ("nope", 1, LookupError),
    ("system-1", 0, ValueError), ("demo-scaled-1", 1, ValueError)])
def test_scale_refusals(pools, name, count, error):
    with pytest.raises(error):
        pools.scale(name, count)
    assert pools.asg.updates == [] and pools.eks.updates == []
