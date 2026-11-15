{{/*
Names, labels and references shared by the templates.

Every object is named after the release, <release>-<component>, and lives in
the release's namespace, so that releases of other names can share a
namespace. The release name is limited to 40 characters: the StatefulSet's
name, <release>-postgresql, has to stay within 52.
*/}}

{{- define "pubdex.fullname" -}}
{{- if gt (len .Release.Name) 40 -}}
{{- fail (printf "the release name %q is longer than 40 characters; choose a shorter one" .Release.Name) -}}
{{- end -}}
{{- .Release.Name -}}
{{- end -}}

{{- define "pubdex.apiName" -}}
{{- printf "%s-api" (include "pubdex.fullname" .) -}}
{{- end -}}

{{- define "pubdex.postgresqlName" -}}
{{- printf "%s-postgresql" (include "pubdex.fullname" .) -}}
{{- end -}}

{{- define "pubdex.configName" -}}
{{- printf "%s-config" (include "pubdex.fullname" .) -}}
{{- end -}}

{{- define "pubdex.setupName" -}}
{{- printf "%s-setup" (include "pubdex.fullname" .) -}}
{{- end -}}

{{/*
The ServiceAccount of the ingestion pods, and the prefix of their CronJobs.
*/}}
{{- define "pubdex.ingestName" -}}
{{- printf "%s-ingest" (include "pubdex.fullname" .) -}}
{{- end -}}

{{- define "pubdex.demoName" -}}
{{- printf "%s-demo" (include "pubdex.fullname" .) -}}
{{- end -}}

{{/*
The ConfigMap with the specification of the attached one-off pod, and the
name that pod gets.
*/}}
{{- define "pubdex.runName" -}}
{{- printf "%s-run" (include "pubdex.fullname" .) -}}
{{- end -}}

{{/*
The label that lets a pod of the release's namespace reach the read service
while networkPolicy.enabled is true: <release>-client: "true".
*/}}
{{- define "pubdex.clientLabel" -}}
{{- printf "%s-client" (include "pubdex.fullname" .) -}}
{{- end -}}

{{/*
The components whose pods connect to the database. Evaluation mode's
NetworkPolicy admits exactly these to PostgreSQL.
*/}}
{{- define "pubdex.databaseClients" -}}
{{- list "api" "demo" "ingest" | toJson -}}
{{- end -}}

{{/*
Selector labels of one component; call with (dict "root" $ "component" "api").
They never change between chart versions: Deployments, StatefulSets and
volume claim templates cannot change their selectors.
*/}}
{{- define "pubdex.selectorLabels" -}}
app.kubernetes.io/name: {{ .root.Chart.Name }}
app.kubernetes.io/instance: {{ .root.Release.Name }}
app.kubernetes.io/component: {{ .component }}
{{- end -}}

{{/*
All labels of one component; call as pubdex.selectorLabels. Without a
component, for objects that belong to the whole release.
*/}}
{{- define "pubdex.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .root.Chart.Name .root.Chart.Version | replace "+" "_" }}
app.kubernetes.io/name: {{ .root.Chart.Name }}
app.kubernetes.io/instance: {{ .root.Release.Name }}
{{- with .component }}
app.kubernetes.io/component: {{ . }}
{{- end }}
app.kubernetes.io/version: {{ .root.Chart.AppVersion | quote }}
app.kubernetes.io/part-of: pubdex
app.kubernetes.io/managed-by: {{ .root.Release.Service }}
{{- end -}}

{{/*
An image reference, <registry>/<repository>:<tag>@<digest>, from one of the
image values; the parts left empty are left out. values.schema.json requires
a tag or a digest.
*/}}
{{- define "pubdex.image" -}}
{{- $ref := .repository -}}
{{- if .registry -}}
{{- $ref = printf "%s/%s" .registry .repository -}}
{{- end -}}
{{- if .tag -}}
{{- $ref = printf "%s:%s" $ref .tag -}}
{{- end -}}
{{- if .digest -}}
{{- $ref = printf "%s@%s" $ref .digest -}}
{{- end -}}
{{- $ref -}}
{{- end -}}

{{/*
The name of the operator-created Secret, or a failure that says how to
create it; external mode needs only the keys of the two application roles.
*/}}
{{- define "pubdex.secretName" -}}
{{- $keys := list "postgres_password" "app_readonly_password" "app_writer_password" -}}
{{- if eq .Values.mode "external" -}}
{{- $keys = list "app_readonly_password" "app_writer_password" -}}
{{- end -}}
{{- $files := list -}}
{{- range $keys -}}
{{- $files = append $files (printf "--from-file=secrets/%s" .) -}}
{{- end -}}
{{- $help := printf `secrets.name is required: the name of an existing Secret in namespace %s with the keys %s. Create the files with ./create_secrets.sh, then the Secret:
  kubectl create secret generic pubdex-credentials --namespace %s %s
and name it with --set secrets.name=pubdex-credentials` .Release.Namespace (join ", " $keys) .Release.Namespace (join " " $files) -}}
{{- required $help .Values.secrets.name -}}
{{- end -}}

{{/*
Where the containers find the files of the Secret.
*/}}
{{- define "pubdex.secretDir" -}}
/run/secrets/pubdex
{{- end -}}

{{/*
The database server PubDex connects to: in evaluation mode the release's
own PostgreSQL Service, in external mode externalDatabase.host.
*/}}
{{- define "pubdex.databaseHost" -}}
{{- if eq .Values.mode "external" -}}
{{- required "externalDatabase.host is required in external mode: the host name or address of your PostgreSQL 16 server, as the release's pods reach it, for example --set externalDatabase.host=db.example.org" .Values.externalDatabase.host -}}
{{- else -}}
{{- include "pubdex.postgresqlName" . -}}
{{- end -}}
{{- end -}}

{{- define "pubdex.databasePort" -}}
{{- if eq .Values.mode "external" -}}
{{- .Values.externalDatabase.port | int64 -}}
{{- else -}}
5432
{{- end -}}
{{- end -}}

{{/*
Where the containers that connect to an external server find its CA
certificate, when externalDatabase.caCertificate names one; otherwise empty.
*/}}
{{- define "pubdex.caDir" -}}
/run/pubdex/ca
{{- end -}}

{{- define "pubdex.sslRootCert" -}}
{{- $ca := .Values.externalDatabase.caCertificate -}}
{{- if and (eq .Values.mode "external") (or $ca.secretName $ca.configMapName) -}}
{{- printf "%s/%s" (include "pubdex.caDir" .) $ca.key -}}
{{- end -}}
{{- end -}}

{{/*
The volume and the mount of that CA certificate, for a container that
connects to the external server; empty when none is configured.
*/}}
{{- define "pubdex.caVolume" -}}
{{- $ca := .Values.externalDatabase.caCertificate -}}
{{- if include "pubdex.sslRootCert" . -}}
# The CA certificate of the external server; not a credential.
- name: database-ca
  {{- if $ca.secretName }}
  secret:
    secretName: {{ $ca.secretName | quote }}
    defaultMode: 0440
  {{- else }}
  configMap:
    name: {{ $ca.configMapName | quote }}
    defaultMode: 0444
  {{- end }}
    items:
      - key: {{ $ca.key | quote }}
        path: {{ $ca.key | quote }}
{{- end -}}
{{- end -}}

{{- define "pubdex.caMount" -}}
{{- if include "pubdex.sslRootCert" . -}}
- name: database-ca
  mountPath: {{ include "pubdex.caDir" . }}
  readOnly: true
{{- end -}}
{{- end -}}

{{/*
A libpq connection string for one role, without a password: the server,
the database and, in external mode, sslmode and the CA certificate. Call
with (dict "root" $ "user" "app_readonly").
*/}}
{{- define "pubdex.connectionString" -}}
{{- $root := .root -}}
{{- $parts := list (printf "host=%s" (include "pubdex.databaseHost" $root)) (printf "port=%s" (include "pubdex.databasePort" $root)) (printf "dbname=%s" $root.Values.database.name) (printf "user=%s" .user) -}}
{{- if eq $root.Values.mode "external" -}}
{{- $parts = append $parts (printf "sslmode=%s" $root.Values.externalDatabase.sslmode) -}}
{{- with include "pubdex.sslRootCert" $root -}}
{{- $parts = append $parts (printf "sslrootcert=%s" .) -}}
{{- end -}}
{{- end -}}
{{- join " " $parts -}}
{{- end -}}

{{/*
Values that depend on each other; values.schema.json refuses the
contradictions it can express, and these fail with what to set instead.
Rendered from configmap.yaml, which every mode renders.
*/}}
{{- define "pubdex.checkValues" -}}
{{- include "pubdex.checkPodMetadata" . -}}
{{- if and .Values.demo.enabled (eq .Values.mode "external") -}}
{{- fail "demo.enabled is for evaluation mode only: the demonstration writes synthetic records, and in external mode they would go into your own server's database. Try it out with mode=evaluation, in a release you uninstall afterwards" -}}
{{- end -}}
{{- range $task, $spec := .Values.ingestion.tasks -}}
{{- $name := printf "%s-%s" (include "pubdex.ingestName" $) $task -}}
{{- if gt (len $name) 52 -}}
{{- fail (printf "ingestion.tasks.%s: the CronJob's name %s is longer than 52 characters; choose a shorter task or release name" $task $name) -}}
{{- end -}}
{{- if eq (index $spec.command 2) "people_pubs.sync.add_people" -}}
{{- fail (printf "ingestion.tasks.%s runs people_pubs.sync.add_people: a roster holds personal data and never goes into the values, which Helm keeps in the release's history. Import it on standard input into the attached one-off pod (helm get notes %s prints the command)" $task $.Release.Name) -}}
{{- end -}}
{{- end -}}
{{- if eq .Values.mode "external" -}}
{{- $db := .Values.externalDatabase -}}
{{- $host := include "pubdex.databaseHost" . -}}
{{- if and (has $db.sslmode (list "verify-ca" "verify-full")) (not (or $db.caCertificate.secretName $db.caCertificate.configMapName)) -}}
{{- fail (printf "externalDatabase.sslmode %s checks the server's certificate against a CA certificate, and none is named. Either put the CA certificate that signed the server's certificate into a Secret or ConfigMap in namespace %s, for example\n  kubectl create configmap pubdex-db-ca --namespace %s --from-file=ca.crt=<CA certificate>\nand name it in externalDatabase.caCertificate.configMapName (or .secretName), with its key in externalDatabase.caCertificate.key; or choose a weaker externalDatabase.sslmode on purpose, such as require, which encrypts but lets any host pose as the server (values.yaml lists what each mode gives up)" $db.sslmode .Release.Namespace .Release.Namespace) -}}
{{- end -}}
{{- if $db.setup.enabled -}}
{{- if not $db.setup.adminUser -}}
{{- fail "externalDatabase.setup.adminUser is required while externalDatabase.setup.enabled is true: the role that owns the database and has CREATEROLE, which the setup Job prepares the database as. Or set externalDatabase.setup.enabled=false and prepare the database by hand (KUBERNETES.md, Installing without the setup Job)" -}}
{{- end -}}
{{- if not $db.setup.adminSecret.name -}}
{{- fail (printf "externalDatabase.setup.adminSecret.name is required while externalDatabase.setup.enabled is true: an existing Secret with the password of %s, for example\n  kubectl create secret generic pubdex-db-admin --namespace %s --from-file=password=admin_password\nOr set externalDatabase.setup.enabled=false and prepare the database by hand (KUBERNETES.md, Installing without the setup Job)" $db.setup.adminUser .Release.Namespace) -}}
{{- end -}}
{{- if eq $db.setup.adminSecret.name .Values.secrets.name -}}
{{- fail "externalDatabase.setup.adminSecret.name names the Secret of secrets.name: keep the admin role's password in a Secret of its own, which only the setup Job mounts and which you delete after an install or upgrade" -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{/*
The scheduling and metadata values of a component's pods, as JSON:
nodeSelector, tolerations, affinity, podAnnotations, podLabels and
priorityClassName. api and postgresql take their own key when it is set,
neither left out nor null, and the top-level one otherwise; their own value
replaces the top-level one, never merged with it. Every other component
takes the top-level keys. Call with (dict "root" $ "component" "api").
*/}}
{{- define "pubdex.podValues" -}}
{{- $root := .root -}}
{{- $own := dict -}}
{{- if has .component (list "api" "postgresql") -}}
{{- $own = index $root.Values .component -}}
{{- end -}}
{{- $result := dict -}}
{{- range $key := list "nodeSelector" "tolerations" "affinity" "podAnnotations" "podLabels" "priorityClassName" -}}
{{- $value := index $root.Values $key -}}
{{- $override := index $own $key -}}
{{- if not (kindIs "invalid" $override) -}}
{{- $value = $override -}}
{{- end -}}
{{- $_ := set $result $key $value -}}
{{- end -}}
{{- toJson $result -}}
{{- end -}}

{{/*
The extra labels and annotations of a component's pods, as YAML, empty when
there are none; pubdex.checkPodMetadata has refused the chart's own keys.
*/}}
{{- define "pubdex.podLabels" -}}
{{- with (include "pubdex.podValues" . | fromJson).podLabels -}}
{{- toYaml . -}}
{{- end -}}
{{- end -}}

{{- define "pubdex.podAnnotations" -}}
{{- with (include "pubdex.podValues" . | fromJson).podAnnotations -}}
{{- toYaml . -}}
{{- end -}}
{{- end -}}

{{/*
The scheduling fields of a component's pod specification, as YAML: only
those that are set.
*/}}
{{- define "pubdex.podScheduling" -}}
{{- $values := include "pubdex.podValues" . | fromJson -}}
{{- with $values.nodeSelector }}
nodeSelector:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- with $values.tolerations }}
tolerations:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- with $values.affinity }}
affinity:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- with $values.priorityClassName }}
priorityClassName: {{ . | quote }}
{{- end }}
{{- end -}}

{{/*
The labels and annotations the chart sets on pods itself, which podLabels and
podAnnotations cannot set: the standard labels with the selector labels, the
client label of helm test's pod (networkPolicy), the checksum of the settings
and Helm's hook annotations. Fails with the value and the key otherwise.
*/}}
{{- define "pubdex.checkPodMetadata" -}}
{{- $ownLabels := keys (include "pubdex.labels" (dict "root" . "component" "x") | fromYaml) -}}
{{- $ownLabels = append $ownLabels (include "pubdex.clientLabel" .) -}}
{{- $ownAnnotations := list "checksum/config" "helm.sh/hook" "helm.sh/hook-weight" "helm.sh/hook-delete-policy" -}}
{{- range $prefix := list "" "api." "postgresql." -}}
{{- $section := $.Values -}}
{{- if $prefix -}}
{{- $section = index $.Values (trimSuffix "." $prefix) -}}
{{- end -}}
{{- range $key := keys ((index $section "podLabels") | default dict) | sortAlpha -}}
{{- if has $key $ownLabels -}}
{{- fail (printf "%spodLabels sets %s, a label the chart sets on the release's pods itself: the selectors, the NetworkPolicies and Helm rely on it. Choose another key" $prefix $key) -}}
{{- end -}}
{{- end -}}
{{- range $key := keys ((index $section "podAnnotations") | default dict) | sortAlpha -}}
{{- if has $key $ownAnnotations -}}
{{- fail (printf "%spodAnnotations sets %s, an annotation the chart sets on the release's pods itself. Choose another key" $prefix $key) -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{/*
The security settings every container of the chart runs with, as Pod
Security "restricted" requires: no privilege escalation, no capabilities and
a read-only root file system. The user, group and seccomp profile come from
the pod.
*/}}
{{- define "pubdex.containerSecurityContext" -}}
allowPrivilegeEscalation: false
readOnlyRootFilesystem: true
capabilities:
  drop:
    - ALL
{{- end -}}

{{/*
The parts of a pod that runs ingestion commands as app_writer, as
compose.yaml's ingest service does: the image's user, uid and gid 10001,
libpq's connection settings from the release's ConfigMap, app_writer's
password file and no other credential, a private /tmp, and in external mode
the CA certificate when one is set. The CronJobs, the demonstration and the
attached one-off pod all use them.
*/}}
{{- define "pubdex.writerPodSecurityContext" -}}
runAsNonRoot: true
runAsUser: 10001
runAsGroup: 10001
# The group of the password file, which is readable by it only.
fsGroup: 10001
seccompProfile:
  type: RuntimeDefault
{{- end -}}

{{- define "pubdex.writerEnv" -}}
{{- $keys := list "PGHOST" "PGPORT" "PGDATABASE" "PGUSER" "PEOPLE_PUBS_CROSSREF_MAILTO" "PEOPLE_PUBS_ORG_PATTERNS" -}}
{{- if eq .Values.mode "external" -}}
{{- $keys = append $keys "PGSSLMODE" -}}
{{- if include "pubdex.sslRootCert" . -}}
{{- $keys = append $keys "PGSSLROOTCERT" -}}
{{- end -}}
{{- end -}}
{{- $env := list -}}
{{- range $keys -}}
{{- $env = append $env (dict "name" . "valueFrom" (dict "configMapKeyRef" (dict "name" (include "pubdex.configName" $) "key" .))) -}}
{{- end -}}
{{- $env = append $env (dict "name" "PEOPLE_DB_PASSWORD_FILE" "value" (printf "%s/app_writer_password" (include "pubdex.secretDir" .))) -}}
{{- toYaml $env -}}
{{- end -}}

{{- define "pubdex.writerVolumeMounts" -}}
- name: tmp
  mountPath: /tmp
- name: secrets
  mountPath: {{ include "pubdex.secretDir" . }}
  readOnly: true
{{- with include "pubdex.caMount" . }}
{{ . }}
{{- end }}
{{- end -}}

{{- define "pubdex.writerVolumes" -}}
- name: tmp
  emptyDir: {}
# app_writer's password only, readable by the group 10001 only.
- name: secrets
  secret:
    secretName: {{ include "pubdex.secretName" . | quote }}
    defaultMode: 0440
    items:
      - key: app_writer_password
        path: app_writer_password
{{- with include "pubdex.caVolume" . }}
{{ . }}
{{- end }}
{{- end -}}

{{/*
The PostgreSQL health check of compose.yaml: the server answers on TCP --
during the first start's initialisation it listens on its socket only --
and both application roles have SCRAM passwords, which the initialisation
sets last. A first start that failed part-way never passes it.
*/}}
{{- define "pubdex.postgresqlReadyCommand" -}}
- sh
- -c
- >-
  pg_isready -q -h 127.0.0.1 -p 5432 -U postgres -d "$POSTGRES_DB" &&
  psql -XqtA -U postgres -d "$POSTGRES_DB" -c "SELECT count(*) FROM pg_catalog.pg_authid
  WHERE rolname IN ('app_readonly', 'app_writer') AND rolcanlogin
  AND rolpassword LIKE 'SCRAM-SHA-256\$%'" | grep -qx 2
{{- end -}}
