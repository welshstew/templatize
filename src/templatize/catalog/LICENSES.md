# Bundled JSON Schemas: sources and licences

These schemas are third-party works, redistributed unchanged except for compact
re-serialisation and gzip compression (`scripts/fetch-schemas.py`). Each is pinned to
a commit or release; `manifest.json` records the SHA-256 of the download and of the
stored file.

- **SchemaStore** (github-workflow, github-action, chart, prometheus, prometheus.rules,
  dependabot-2.0, package): Apache License 2.0, <https://github.com/SchemaStore/schemastore>
- **Compose specification**: Apache License 2.0, <https://github.com/compose-spec/compose-spec>
- **cloud-init**: dual-licensed Apache License 2.0 / GPLv3; used under Apache 2.0,
  <https://github.com/canonical/cloud-init>
- **GitLab CI schema**: MIT (GitLab CE source), <https://gitlab.com/gitlab-org/gitlab>
- **Kubernetes schemas**: generated from the Kubernetes OpenAPI specification (Apache
  License 2.0) by <https://github.com/yannh/kubernetes-json-schema>

| File | Schema | Licence | Source |
|---|---|---|---|
| `github-workflow.json.gz` | GitHub Actions workflow | Apache-2.0 (SchemaStore) | <https://raw.githubusercontent.com/SchemaStore/schemastore/e2606e03284e60792672dc9e7e930f3c496c5c3d/src/schemas/json/github-workflow.json> |
| `github-action.json.gz` | GitHub Action metadata (action.yml) | Apache-2.0 (SchemaStore) | <https://raw.githubusercontent.com/SchemaStore/schemastore/e2606e03284e60792672dc9e7e930f3c496c5c3d/src/schemas/json/github-action.json> |
| `gitlab-ci.json.gz` | GitLab CI pipeline | MIT (GitLab) | <https://gitlab.com/gitlab-org/gitlab/-/raw/v18.11.14-ee/app/assets/javascripts/editor/schema/ci.json> |
| `compose-spec.json.gz` | Compose specification | Apache-2.0 (compose-spec) | <https://raw.githubusercontent.com/compose-spec/compose-spec/3a4a6995e4a971f6173dff9130cb3e38e6738651/schema/compose-spec.json> |
| `cloud-init.json.gz` | cloud-init cloud-config | Apache-2.0 / GPL-3.0 (cloud-init, dual) | <https://raw.githubusercontent.com/canonical/cloud-init/26.2/cloudinit/config/schemas/schema-cloud-config-v1.json> |
| `helm-chart.json.gz` | Helm Chart.yaml | Apache-2.0 (SchemaStore) | <https://raw.githubusercontent.com/SchemaStore/schemastore/e2606e03284e60792672dc9e7e930f3c496c5c3d/src/schemas/json/chart.json> |
| `prometheus.json.gz` | Prometheus configuration | Apache-2.0 (SchemaStore) | <https://raw.githubusercontent.com/SchemaStore/schemastore/e2606e03284e60792672dc9e7e930f3c496c5c3d/src/schemas/json/prometheus.json> |
| `prometheus-rules.json.gz` | Prometheus rules | Apache-2.0 (SchemaStore) | <https://raw.githubusercontent.com/SchemaStore/schemastore/e2606e03284e60792672dc9e7e930f3c496c5c3d/src/schemas/json/prometheus.rules.json> |
| `dependabot.json.gz` | Dependabot configuration | Apache-2.0 (SchemaStore) | <https://raw.githubusercontent.com/SchemaStore/schemastore/e2606e03284e60792672dc9e7e930f3c496c5c3d/src/schemas/json/dependabot-2.0.json> |
| `package-json.json.gz` | package.json | Apache-2.0 (SchemaStore) | <https://raw.githubusercontent.com/SchemaStore/schemastore/e2606e03284e60792672dc9e7e930f3c496c5c3d/src/schemas/json/package.json> |
| `k8s-deployment-apps-v1.json.gz` | Kubernetes Deployment (apps/v1) | Apache-2.0 (Kubernetes OpenAPI, via yannh/kubernetes-json-schema) | <https://raw.githubusercontent.com/yannh/kubernetes-json-schema/6ea44f54bb5cdedb2f61fba42c9b342e97959262/v1.34.12-standalone-strict/deployment-apps-v1.json> |
| `k8s-statefulset-apps-v1.json.gz` | Kubernetes StatefulSet (apps/v1) | Apache-2.0 (Kubernetes OpenAPI, via yannh/kubernetes-json-schema) | <https://raw.githubusercontent.com/yannh/kubernetes-json-schema/6ea44f54bb5cdedb2f61fba42c9b342e97959262/v1.34.12-standalone-strict/statefulset-apps-v1.json> |
| `k8s-daemonset-apps-v1.json.gz` | Kubernetes DaemonSet (apps/v1) | Apache-2.0 (Kubernetes OpenAPI, via yannh/kubernetes-json-schema) | <https://raw.githubusercontent.com/yannh/kubernetes-json-schema/6ea44f54bb5cdedb2f61fba42c9b342e97959262/v1.34.12-standalone-strict/daemonset-apps-v1.json> |
| `k8s-service-v1.json.gz` | Kubernetes Service (v1) | Apache-2.0 (Kubernetes OpenAPI, via yannh/kubernetes-json-schema) | <https://raw.githubusercontent.com/yannh/kubernetes-json-schema/6ea44f54bb5cdedb2f61fba42c9b342e97959262/v1.34.12-standalone-strict/service-v1.json> |
| `k8s-configmap-v1.json.gz` | Kubernetes ConfigMap (v1) | Apache-2.0 (Kubernetes OpenAPI, via yannh/kubernetes-json-schema) | <https://raw.githubusercontent.com/yannh/kubernetes-json-schema/6ea44f54bb5cdedb2f61fba42c9b342e97959262/v1.34.12-standalone-strict/configmap-v1.json> |
| `k8s-secret-v1.json.gz` | Kubernetes Secret (v1) | Apache-2.0 (Kubernetes OpenAPI, via yannh/kubernetes-json-schema) | <https://raw.githubusercontent.com/yannh/kubernetes-json-schema/6ea44f54bb5cdedb2f61fba42c9b342e97959262/v1.34.12-standalone-strict/secret-v1.json> |
| `k8s-persistentvolumeclaim-v1.json.gz` | Kubernetes PersistentVolumeClaim (v1) | Apache-2.0 (Kubernetes OpenAPI, via yannh/kubernetes-json-schema) | <https://raw.githubusercontent.com/yannh/kubernetes-json-schema/6ea44f54bb5cdedb2f61fba42c9b342e97959262/v1.34.12-standalone-strict/persistentvolumeclaim-v1.json> |
| `k8s-serviceaccount-v1.json.gz` | Kubernetes ServiceAccount (v1) | Apache-2.0 (Kubernetes OpenAPI, via yannh/kubernetes-json-schema) | <https://raw.githubusercontent.com/yannh/kubernetes-json-schema/6ea44f54bb5cdedb2f61fba42c9b342e97959262/v1.34.12-standalone-strict/serviceaccount-v1.json> |
| `k8s-pod-v1.json.gz` | Kubernetes Pod (v1) | Apache-2.0 (Kubernetes OpenAPI, via yannh/kubernetes-json-schema) | <https://raw.githubusercontent.com/yannh/kubernetes-json-schema/6ea44f54bb5cdedb2f61fba42c9b342e97959262/v1.34.12-standalone-strict/pod-v1.json> |
| `k8s-namespace-v1.json.gz` | Kubernetes Namespace (v1) | Apache-2.0 (Kubernetes OpenAPI, via yannh/kubernetes-json-schema) | <https://raw.githubusercontent.com/yannh/kubernetes-json-schema/6ea44f54bb5cdedb2f61fba42c9b342e97959262/v1.34.12-standalone-strict/namespace-v1.json> |
| `k8s-ingress-networking-v1.json.gz` | Kubernetes Ingress (networking.k8s.io/v1) | Apache-2.0 (Kubernetes OpenAPI, via yannh/kubernetes-json-schema) | <https://raw.githubusercontent.com/yannh/kubernetes-json-schema/6ea44f54bb5cdedb2f61fba42c9b342e97959262/v1.34.12-standalone-strict/ingress-networking-v1.json> |
| `k8s-networkpolicy-networking-v1.json.gz` | Kubernetes NetworkPolicy (networking.k8s.io/v1) | Apache-2.0 (Kubernetes OpenAPI, via yannh/kubernetes-json-schema) | <https://raw.githubusercontent.com/yannh/kubernetes-json-schema/6ea44f54bb5cdedb2f61fba42c9b342e97959262/v1.34.12-standalone-strict/networkpolicy-networking-v1.json> |
| `k8s-job-batch-v1.json.gz` | Kubernetes Job (batch/v1) | Apache-2.0 (Kubernetes OpenAPI, via yannh/kubernetes-json-schema) | <https://raw.githubusercontent.com/yannh/kubernetes-json-schema/6ea44f54bb5cdedb2f61fba42c9b342e97959262/v1.34.12-standalone-strict/job-batch-v1.json> |
| `k8s-cronjob-batch-v1.json.gz` | Kubernetes CronJob (batch/v1) | Apache-2.0 (Kubernetes OpenAPI, via yannh/kubernetes-json-schema) | <https://raw.githubusercontent.com/yannh/kubernetes-json-schema/6ea44f54bb5cdedb2f61fba42c9b342e97959262/v1.34.12-standalone-strict/cronjob-batch-v1.json> |
| `k8s-horizontalpodautoscaler-autoscaling-v2.json.gz` | Kubernetes HorizontalPodAutoscaler (autoscaling/v2) | Apache-2.0 (Kubernetes OpenAPI, via yannh/kubernetes-json-schema) | <https://raw.githubusercontent.com/yannh/kubernetes-json-schema/6ea44f54bb5cdedb2f61fba42c9b342e97959262/v1.34.12-standalone-strict/horizontalpodautoscaler-autoscaling-v2.json> |
| `k8s-poddisruptionbudget-policy-v1.json.gz` | Kubernetes PodDisruptionBudget (policy/v1) | Apache-2.0 (Kubernetes OpenAPI, via yannh/kubernetes-json-schema) | <https://raw.githubusercontent.com/yannh/kubernetes-json-schema/6ea44f54bb5cdedb2f61fba42c9b342e97959262/v1.34.12-standalone-strict/poddisruptionbudget-policy-v1.json> |
| `k8s-role-rbac-v1.json.gz` | Kubernetes Role (rbac.authorization.k8s.io/v1) | Apache-2.0 (Kubernetes OpenAPI, via yannh/kubernetes-json-schema) | <https://raw.githubusercontent.com/yannh/kubernetes-json-schema/6ea44f54bb5cdedb2f61fba42c9b342e97959262/v1.34.12-standalone-strict/role-rbac-v1.json> |
| `k8s-rolebinding-rbac-v1.json.gz` | Kubernetes RoleBinding (rbac.authorization.k8s.io/v1) | Apache-2.0 (Kubernetes OpenAPI, via yannh/kubernetes-json-schema) | <https://raw.githubusercontent.com/yannh/kubernetes-json-schema/6ea44f54bb5cdedb2f61fba42c9b342e97959262/v1.34.12-standalone-strict/rolebinding-rbac-v1.json> |
| `k8s-clusterrole-rbac-v1.json.gz` | Kubernetes ClusterRole (rbac.authorization.k8s.io/v1) | Apache-2.0 (Kubernetes OpenAPI, via yannh/kubernetes-json-schema) | <https://raw.githubusercontent.com/yannh/kubernetes-json-schema/6ea44f54bb5cdedb2f61fba42c9b342e97959262/v1.34.12-standalone-strict/clusterrole-rbac-v1.json> |
| `k8s-clusterrolebinding-rbac-v1.json.gz` | Kubernetes ClusterRoleBinding (rbac.authorization.k8s.io/v1) | Apache-2.0 (Kubernetes OpenAPI, via yannh/kubernetes-json-schema) | <https://raw.githubusercontent.com/yannh/kubernetes-json-schema/6ea44f54bb5cdedb2f61fba42c9b342e97959262/v1.34.12-standalone-strict/clusterrolebinding-rbac-v1.json> |
