"""Document type detection on every fixture, corpus document and sample.

Each expected type was reviewed by hand; a change here is a change in what the
classifier says, so check it is an improvement before updating the table.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from templatize.doctypes import TYPES, detect_type, profile_for

ROOT = Path(__file__).resolve().parents[1]

EXPECTED = {
    "fixtures/business_letter.txt": "letter",
    "fixtures/config.json": "json",
    "fixtures/crlf.ini": "ini",
    "fixtures/docker-compose.yml": "docker-compose",
    "fixtures/endraw.txt": "text",
    "fixtures/jinja_literals.txt": "text",
    "fixtures/k8s-app-stack.yaml": "kubernetes",
    "fixtures/k8s-deployment.yaml": "kubernetes",
    "fixtures/mixed_newlines.txt": "text",
    "fixtures/nginx.conf": "nginx",
    "fixtures/no_trailing_newline.env": "dotenv",
    "fixtures/unicode.txt": "text",
    "corpus/CHANGELOG.md": "markdown",
    "corpus/Caddyfile": "text",
    "corpus/Cargo.toml": "toml",
    "corpus/Chart.yaml": "helm-chart",
    "corpus/Dockerfile": "dockerfile",
    "corpus/Jenkinsfile": "jenkinsfile",
    "corpus/Makefile": "makefile",
    "corpus/access.log": "access-log",
    "corpus/alert_rule.yaml": "prometheus-rules",
    "corpus/alerts.yml": "prometheus-rules",
    "corpus/ansible_vars.yml": "yaml",
    "corpus/api_response.json": "json",
    "corpus/app.log": "app-log",
    "corpus/application.properties": "properties",
    "corpus/appointment.txt": "letter",
    "corpus/appsettings.json": "json",
    "corpus/argocd-app.yaml": "kubernetes",
    "corpus/aws_cli.json": "json",
    "corpus/aws_config": "ini",
    "corpus/bank_notice.txt": "letter",
    "corpus/booking_confirmation.txt": "email",
    "corpus/change_request.txt": "text",
    "corpus/contact.vcf": "calendar",
    "corpus/contract_excerpt.txt": "text",
    "corpus/cronjob.yaml": "kubernetes",
    "corpus/crontab": "crontab",
    "corpus/deploy.yml": "github-actions",
    "corpus/dns_change.txt": "text",
    "corpus/docker_compose_override.yml": "docker-compose",
    "corpus/docker_run.sh": "shell",
    "corpus/fluent-bit.conf": "text",
    "corpus/gitconfig": "ini",
    "corpus/github_issue.md": "markdown",
    "corpus/gitlab-ci.yml": "gitlab-ci",
    "corpus/grants.sql": "sql",
    "corpus/haproxy.cfg": "haproxy",
    "corpus/haproxy_backend.cfg": "haproxy",
    "corpus/helm_values.yaml": "yaml",
    "corpus/hosts": "text",
    "corpus/incident.txt": "text",
    "corpus/ingress.yaml": "kubernetes",
    "corpus/interview.txt": "letter",
    "corpus/inventory.ini": "ansible-inventory",
    "corpus/invite.ics": "calendar",
    "corpus/invoice_reminder.txt": "letter",
    "corpus/job_posting.md": "markdown",
    "corpus/k8s-service.yaml": "kubernetes",
    "corpus/k8s_events.txt": "text",
    "corpus/kafka.properties": "properties",
    "corpus/known_hosts_note.md": "text",
    "corpus/kubeconfig": "kubernetes",
    "corpus/kustomization.yaml": "kubernetes",
    "corpus/main.tf": "terraform",
    "corpus/meeting_notes.md": "text",
    "corpus/mongod.conf": "yaml",
    "corpus/mqtt_device.conf": "ini",
    "corpus/my.cnf": "ini",
    "corpus/nginx_access.log": "access-log",
    "corpus/nginx_error.log": "app-log",
    "corpus/npmrc": "ini",
    "corpus/offer_letter_us.txt": "letter",
    "corpus/onboarding.md": "text",
    "corpus/oncall_handover.txt": "text",
    "corpus/outage_notice.txt": "letter",
    "corpus/package.json": "json",
    "corpus/packer.pkr.hcl": "terraform",
    "corpus/password_reset.txt": "letter",
    "corpus/playbook.yml": "ansible-playbook",
    "corpus/pom.xml": "xml",
    "corpus/postgresql.conf": "ini",
    "corpus/postmortem.md": "markdown",
    "corpus/prometheus.yml": "prometheus",
    "corpus/quote.txt": "letter",
    "corpus/receipt.txt": "text",
    "corpus/redis.conf": "keyword-config",
    "corpus/release_email.txt": "letter",
    "corpus/release_tag.txt": "text",
    "corpus/runbook.md": "text",
    "corpus/school_letter.txt": "letter",
    "corpus/secret.yaml": "kubernetes",
    "corpus/server.properties": "properties",
    "corpus/servers.csv": "csv",
    "corpus/shipping.txt": "letter",
    "corpus/slack_message.txt": "text",
    "corpus/sms.txt": "text",
    "corpus/ssh_config": "ssh-config",
    "corpus/status_update.txt": "text",
    "corpus/supervisord.conf": "ini",
    "corpus/sysctl.conf": "properties",
    "corpus/syslog": "syslog",
    "corpus/systemd_timer.timer": "systemd",
    "corpus/table.md": "markdown",
    "corpus/terraform.tfvars": "terraform-tfvars",
    "corpus/tf_plan.txt": "terraform-plan",
    "corpus/ticket.txt": "yaml",
    "corpus/traefik.toml": "toml",
    "corpus/us_letter.txt": "letter",
    "corpus/users.csv": "csv",
    "corpus/utility_bill.txt": "text",
    "corpus/values.yaml": "yaml",
    "corpus/variables.tf": "terraform",
    "corpus/vault-policy.hcl": "terraform",
    "corpus/vhost.conf": "apache",
    "corpus/web.config": "xml",
    "corpus/welcome_email.txt": "letter",
    "corpus/wg0.conf": "ini",
    "corpus/zone.db": "text",
    "corpus/.gitlab-ci.yml": "gitlab-ci",
    "corpus/.npmrc": "ini",
    "sample:nginx": "nginx",
    "sample:k8s": "kubernetes",
    "sample:k8s-stack": "kubernetes",
    "sample:compose": "docker-compose",
    "sample:dotenv": "dotenv",
    "sample:systemd": "systemd",
    "sample:terraform": "terraform",
    "sample:gha": "github-actions",
    "sample:ssh": "ssh-config",
    "sample:ini": "ini",
    "sample:json": "json",
    "sample:letter": "letter",
    "sample:invoice": "text",
    "sample:welcome": "email",
    "sample:offer": "letter",
    "sample:meeting": "letter",
    "sample:release": "markdown",
    "sample:sql": "sql",
    "sample:html-email": "html",
    "sample:python": "python",
    "sample:csv": "csv",
    "sample:cloud-init": "cloud-init",
    "sample:haproxy": "haproxy",
    "sample:nginx-envsubst": "nginx",
}


def _text(key: str) -> str:
    if key.startswith("sample:"):
        samples = json.loads((ROOT / "quality/samples.json").read_text("utf-8"))
        return next(s["text"] for s in samples if s["id"] == key.removeprefix("sample:"))
    folder, name = key.split("/", 1)
    base = ROOT / "fixtures" if folder == "fixtures" else ROOT / "quality/corpus"
    return (base / name).read_bytes().decode("utf-8")


@pytest.mark.parametrize("key", list(EXPECTED))
def test_detected_type(key: str) -> None:
    assert detect_type(_text(key)).id == EXPECTED[key]


def test_every_document_is_covered() -> None:
    corpus = {f"corpus/{p.name}" for p in (ROOT / "quality/corpus").iterdir()}
    samples = {f"sample:{s['id']}" for s in json.loads((ROOT / "quality/samples.json").read_text())}
    assert corpus | samples <= set(EXPECTED)


def test_confirmed_type_wins_and_lists_alternatives() -> None:
    text = _text("corpus/redis.conf")
    d = detect_type(text, confirmed="text")
    assert (d.id, d.confirmed, d.confidence) == ("text", True, 1.0)
    assert d.alternatives[0].id == "keyword-config"


def test_unknown_type_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown document type"):
        detect_type("x", confirmed="liquid")


def test_ids_are_unique() -> None:
    assert len({t.id for t in TYPES}) == len(TYPES)


def test_profiles_apply_only_when_confirmed() -> None:
    detected = detect_type(_text("corpus/ssh_config"))
    assert profile_for(detected).keyword_lines is None
    confirmed = detect_type(_text("corpus/ssh_config"), confirmed="ssh-config")
    assert profile_for(confirmed).keyword_lines is True
    assert profile_for(confirmed).kubernetes is False
    assert profile_for(detect_type("x", confirmed="kubernetes")).kubernetes is True
    assert profile_for(detect_type("x", confirmed="letter")).keyword_lines is False


def test_detection_is_fast_on_huge_input() -> None:
    import time

    text = "Abcdefgh " * 120_000
    start = time.perf_counter()
    detect_type(text)
    assert time.perf_counter() - start < 2.0


def _found(text: str, document_type: str | None = None) -> dict[str, str]:
    from templatize.suggest import suggest

    return {c.value: c.name for c in suggest(text, document_type=document_type).candidates}


def test_confirming_ssh_config_reads_a_short_block() -> None:
    text = "Host bastion\n  HostName 10.0.0.5\n"  # two lines: too few to auto-detect
    assert "bastion" not in _found(text)
    assert _found(text, "ssh-config")["bastion"] == "host"


def test_confirming_plain_text_stops_config_readings() -> None:
    text = "Host gateway\nHostName 10.0.0.1\nUser deploy\n"
    assert _found(text)["deploy"] == "user"  # auto-detected as Keyword value lines
    assert "deploy" not in _found(text, "text")


def test_confirming_another_type_turns_kubernetes_rules_off() -> None:
    text = "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: app\ndata:\n  mode: blue\n"
    assert "ConfigMap" not in _found(text)
    assert "ConfigMap" in _found(text, "yaml")


def test_suggest_reports_the_type() -> None:
    from templatize.suggest import suggest

    res = suggest(_text("corpus/haproxy.cfg"))
    assert res.document_type is not None
    assert (res.document_type.id, res.document_type.confirmed) == ("haproxy", False)
    res = suggest(_text("corpus/haproxy.cfg"), document_type="text")
    assert res.document_type is not None and res.document_type.confirmed
