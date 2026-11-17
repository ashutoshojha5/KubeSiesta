#!/usr/bin/env bash
# One-time setup: managed identity that KubeSiesta uses (via Workload Identity) to scale AKS node pools.
# Prints the client ID for aks.workloadIdentityClientId in the Helm values.
# Usage: RG=<resource group> CLUSTER=<aks cluster> deploy/setup-azure-identity.sh
set -euo pipefail

RG=${RG:?set RG to the cluster resource group}
CLUSTER=${CLUSTER:?set CLUSTER to the AKS cluster name}
IDENTITY=${IDENTITY:-kube-siesta-$CLUSTER}
NAMESPACE=${NAMESPACE:-kube-siesta}
SERVICE_ACCOUNT=${SERVICE_ACCOUNT:-kube-siesta}

LOCATION=$(az aks show -g "$RG" -n "$CLUSTER" --query location -o tsv)
ISSUER=$(az aks show -g "$RG" -n "$CLUSTER" --query oidcIssuerProfile.issuerUrl -o tsv)
CLUSTER_ID=$(az aks show -g "$RG" -n "$CLUSTER" --query id -o tsv)

az identity create -g "$RG" -n "$IDENTITY" -l "$LOCATION" -o none
CLIENT_ID=$(az identity show -g "$RG" -n "$IDENTITY" --query clientId -o tsv)
PRINCIPAL_ID=$(az identity show -g "$RG" -n "$IDENTITY" --query principalId -o tsv)

az identity federated-credential create -g "$RG" --identity-name "$IDENTITY" -n "$CLUSTER-$SERVICE_ACCOUNT" \
  --issuer "$ISSUER" --subject "system:serviceaccount:$NAMESPACE:$SERVICE_ACCOUNT" \
  --audiences api://AzureADTokenExchange -o none

# Read and write agent pools on this cluster only
az role assignment create --assignee-object-id "$PRINCIPAL_ID" --assignee-principal-type ServicePrincipal \
  --role "Azure Kubernetes Service Contributor Role" --scope "$CLUSTER_ID" -o none

echo "aks.workloadIdentityClientId: $CLIENT_ID"
