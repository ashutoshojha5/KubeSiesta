#!/usr/bin/env bash
# One-time setup for EKS: an IAM role that KubeSiesta assumes (via IRSA) to scale the cluster's
# node group Auto Scaling Groups. Prints the role ARN for eks.roleArn in the Helm values.
# Uses your current AWS credentials (AWS_PROFILE etc.).
# Usage: AWS_REGION=<region> CLUSTER=<eks cluster> deploy/setup-aws-irsa.sh
set -euo pipefail

export AWS_REGION=${AWS_REGION:?set AWS_REGION to the cluster region}
CLUSTER=${CLUSTER:?set CLUSTER to the EKS cluster name}
ROLE=${ROLE:-kube-siesta-$CLUSTER}
NAMESPACE=${NAMESPACE:-kube-siesta}
SERVICE_ACCOUNT=${SERVICE_ACCOUNT:-kube-siesta}

ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
ISSUER=$(aws eks describe-cluster --name "$CLUSTER" --query cluster.identity.oidc.issuer --output text)
OIDC=${ISSUER#https://}
PROVIDER_ARN=arn:aws:iam::$ACCOUNT:oidc-provider/$OIDC

aws iam get-open-id-connect-provider --open-id-connect-provider-arn "$PROVIDER_ARN" >/dev/null

TRUST=$(cat <<JSON
{"Version": "2012-10-17", "Statement": [{
  "Effect": "Allow",
  "Principal": {"Federated": "$PROVIDER_ARN"},
  "Action": "sts:AssumeRoleWithWebIdentity",
  "Condition": {"StringEquals": {
    "$OIDC:aud": "sts.amazonaws.com",
    "$OIDC:sub": "system:serviceaccount:$NAMESPACE:$SERVICE_ACCOUNT"}}}]}
JSON
)

# Describe is not resource-scoped; updates are limited to ASGs owned by this cluster, and to this
# cluster's managed nodegroups (which are scaled through the EKS API)
POLICY=$(cat <<JSON
{"Version": "2012-10-17", "Statement": [
  {"Effect": "Allow", "Action": "autoscaling:DescribeAutoScalingGroups", "Resource": "*"},
  {"Effect": "Allow", "Action": "autoscaling:UpdateAutoScalingGroup", "Resource": "*",
   "Condition": {"StringEquals": {"aws:ResourceTag/kubernetes.io/cluster/$CLUSTER": "owned"}}},
  {"Effect": "Allow", "Action": "eks:UpdateNodegroupConfig",
   "Resource": "arn:aws:eks:$AWS_REGION:$ACCOUNT:nodegroup/$CLUSTER/*/*"}]}
JSON
)

aws iam create-role --role-name "$ROLE" --assume-role-policy-document "$TRUST" >/dev/null
aws iam put-role-policy --role-name "$ROLE" --policy-name scale-node-groups --policy-document "$POLICY"

echo "eks.roleArn: arn:aws:iam::$ACCOUNT:role/$ROLE"
