{{/*
Create the name of the service account to use
*/}}
{{- define "e2term.serviceAccountName" -}}
{{ default (include "e2term.fullname" .) e2term-service-account }}
{{- end -}}
