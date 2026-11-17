"""EKS node pool listing and scaling.

A node pool is an EC2 Auto Scaling Group tagged `kubernetes.io/cluster/<cluster>` (eksctl nodegroups,
managed and self-managed alike). Its name is the nodegroup name from the eksctl / EKS tags.

Self-managed groups are scaled on the ASG by setting min = max = desired, so they stay recognisable as
fixed; those whose min and max differ are left to the cluster autoscaler and reported as `autoscaling`.

EKS managed nodegroups (ASGs tagged `eks:nodegroup-name`) are scaled through the EKS API, since EKS owns
their ASG. EKS needs max >= 1, so a managed pool at 0 is min 0 / max 1 and min != max can't mark the
autoscaler's pools; those are listed in `autoscaled_nodepools` instead.
"""

import time

import boto3

from app.config import Settings
from app.nodepools import NodePool

MANAGED_TAG = "eks:nodegroup-name"
NODEGROUP_TAGS = (MANAGED_TAG, "alpha.eksctl.io/nodegroup-name")
WAIT_TIMEOUT_SECONDS = 600
WAIT_POLL_SECONDS = 10


def _tags(asg: dict) -> dict[str, str]:
    return {t["Key"]: t["Value"] for t in asg.get("Tags", [])}


def _name(asg: dict) -> str:
    tags = _tags(asg)
    return next((tags[k] for k in NODEGROUP_TAGS if k in tags), asg["AutoScalingGroupName"])


def _managed(asg: dict) -> bool:
    return MANAGED_TAG in _tags(asg)


def _instance_types(asg: dict) -> str:
    policy = asg.get("MixedInstancesPolicy") or {}
    overrides = policy.get("LaunchTemplate", {}).get("Overrides", [])
    types = [o["InstanceType"] for o in overrides if o.get("InstanceType")]
    if not types:
        types = [i["InstanceType"] for i in asg.get("Instances", []) if i.get("InstanceType")]
    return ", ".join(sorted(set(types)))


def _in_service(asg: dict) -> int:
    return sum(1 for i in asg.get("Instances", []) if i["LifecycleState"] == "InService")


class EksNodePools:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.asg = boto3.client("autoscaling", region_name=settings.aws_region)
        self.eks = boto3.client("eks", region_name=settings.aws_region)

    def _groups(self) -> list[dict]:
        tag = f"kubernetes.io/cluster/{self.settings.eks_cluster_name}"
        groups = []
        for page in self.asg.get_paginator("describe_auto_scaling_groups").paginate(
                Filters=[{"Name": "tag-key", "Values": [tag]}]):
            groups += page["AutoScalingGroups"]
        return groups

    def _find(self, name: str) -> dict:
        group = next((g for g in self._groups() if _name(g) == name), None)
        if group is None:
            raise LookupError(f"node group {name} not found in cluster {self.settings.eks_cluster_name}")
        return group

    def _to_model(self, group: dict) -> NodePool:
        name = _name(group)
        desired = group["DesiredCapacity"]
        settled = _in_service(group) == desired and len(group.get("Instances", [])) == desired
        return NodePool(
            name=name,
            mode="System" if name in self.settings.system_nodepools else "User",
            vm_size=_instance_types(group),
            count=desired,
            autoscaling=name in self.settings.autoscaled_nodepools
            or (not _managed(group) and group["MinSize"] != group["MaxSize"]),
            provisioning_state="Succeeded" if settled else "Scaling",
            protected=name in self.settings.protected_nodepools,
        )

    def list(self) -> list[NodePool]:
        return sorted((self._to_model(g) for g in self._groups()), key=lambda p: (p.mode != "System", p.name))

    def get(self, name: str) -> NodePool:
        return self._to_model(self._find(name))

    def scale(self, name: str, count: int, wait: bool = True) -> None:
        group = self._find(name)
        pool = self._to_model(group)
        if pool.protected:
            raise PermissionError(f"node pool {name} is protected")
        if count < pool.min_count:
            raise ValueError(f"{pool.mode} node pool {name} needs at least {pool.min_count} node(s)")
        if pool.autoscaling:
            raise ValueError(f"node pool {name} uses the cluster autoscaler; change min/max instead")
        if _managed(group):
            if group["MinSize"] != count or group["DesiredCapacity"] != count:
                self.eks.update_nodegroup_config(
                    clusterName=self.settings.eks_cluster_name, nodegroupName=name,
                    scalingConfig={"minSize": count, "maxSize": max(count, 1), "desiredSize": count})
        else:
            self.asg.update_auto_scaling_group(
                AutoScalingGroupName=group["AutoScalingGroupName"],
                MinSize=count, MaxSize=count, DesiredCapacity=count)
        if wait:
            self._wait(group["AutoScalingGroupName"], count)

    def _wait(self, asg_name: str, count: int) -> None:
        deadline = time.monotonic() + WAIT_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            group = self.asg.describe_auto_scaling_groups(AutoScalingGroupNames=[asg_name])["AutoScalingGroups"][0]
            if len(group["Instances"]) == count and _in_service(group) == count:
                return
            time.sleep(WAIT_POLL_SECONDS)
        raise TimeoutError(f"node group {asg_name} did not reach {count} node(s) in {WAIT_TIMEOUT_SECONDS}s")
