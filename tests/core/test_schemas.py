"""Schema rules: field paths of known document types and what they mean."""

from __future__ import annotations

import pytest

from templatize.doctypes import detect_type
from templatize.schemas import APPLY_CONFIDENCE, _match, _segments, fields, rules_for
from templatize.suggest import suggest


def values(text: str, document_type: str | None = None) -> dict[str, str]:
    return {c.value: c.name for c in suggest(text, document_type=document_type).candidates}


@pytest.mark.parametrize(
    ("pattern", "path", "match"),
    [
        ("services.*.restart", ("services", "api", "restart"), True),
        ("services.*.restart", ("services", "restart"), False),
        (
            "**.imagePullPolicy",
            ("spec", "template", "spec", "containers", "[]", "imagePullPolicy"),
            True,
        ),
        ("**.accessModes[]", ("spec", "accessModes", "[]"), True),
        ("packages[]", ("packages", "[]"), True),
        ("permissions.*", ("permissions", "contents"), True),
        ("permissions.*", ("jobs", "permissions", "contents"), False),
    ],
)
def test_path_patterns(pattern: str, path: tuple[str, ...], match: bool) -> None:
    assert _match(_segments(pattern), path) is match


def test_fields_have_paths_spans_and_list_sizes() -> None:
    text = 'services:\n  api:\n    restart: "always"\n    ports: ["80:80"]\n'
    by_path = {f.path: f for f in fields(text)}
    restart = by_path[("services", "api", "restart")]
    assert text[restart.span.start : restart.span.end] == "always"  # without the quotes
    assert by_path[("services", "api", "ports", "[]")].list_size == 1


def test_fields_survive_bad_yaml() -> None:
    assert fields("key: [unclosed\n") == []
    assert fields("[" * 3000) == []


def test_rules_apply_when_confirmed_or_confident() -> None:
    text = "#cloud-config\nhostname: web-01\n"
    assert rules_for(detect_type(text))  # cloud-init is detected at 0.98
    assert rules_for(detect_type(text, confirmed="cloud-init"))
    weak = detect_type("x: 1\n", confirmed=None)
    assert weak.confidence < APPLY_CONFIDENCE and not rules_for(weak)


def test_kubernetes_enums_are_structure() -> None:
    text = (
        "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: web\nspec:\n  template:\n"
        "    spec:\n      containers:\n        - name: web\n          image: nginx:1.27\n"
        "          imagePullPolicy: IfNotPresent\n"
    )
    assert "IfNotPresent" not in values(text)


def test_service_type_stays_a_value() -> None:
    text = "apiVersion: v1\nkind: Service\nmetadata:\n  name: web\nspec:\n  type: LoadBalancer\n"
    assert "LoadBalancer" in values(text)


def test_compose_restart_and_env_items() -> None:
    text = (
        "services:\n  worker:\n    image: app:1\n    restart: on-failure\n    environment:\n"
        "      - QUEUE_NAME=emails\n      - BROKER_URL=redis://queue:6379/0\n"
    )
    found = values(text)
    assert "on-failure" not in found
    assert found["emails"] == "queue_name"


def test_github_actions_permissions_and_names() -> None:
    text = (
        "name: CI\non: push\npermissions:\n  contents: write\njobs:\n  build:\n"
        "    runs-on: ubuntu-24.04\n    timeout-minutes: 30\n    steps:\n"
        "      - uses: actions/setup-go@v5\n        with:\n          go-version: '1.23'\n"
    )
    found = values(text)
    assert "write" not in found
    assert found["ubuntu-24.04"] == "runs_on"
    assert found["30"] == "timeout_minutes"
    assert found["1.23"] == "go_version"


def test_cloud_init_suggests_a_one_item_package_list() -> None:
    text = "#cloud-config\nhostname: db-02\npackage_update: true\npackages:\n  - postgresql-16\n"
    found = values(text)
    assert found["postgresql-16"] == "package"
    assert found["db-02"] == "hostname"
    assert "true" not in found


def test_a_list_of_several_items_is_left_to_the_loop() -> None:
    text = "#cloud-config\npackages:\n  - nginx\n  - certbot\n"
    res = suggest(text)
    assert "nginx" not in {c.value for c in res.candidates}
    assert [lp.name for lp in res.loops] == ["packages"]


def test_confirming_another_type_turns_the_rules_off() -> None:
    text = "services:\n  api:\n    image: app:1\n    restart: always\n"
    assert "always" not in values(text)
    assert "always" in values(text, "yaml")


def test_a_value_made_of_template_expressions_is_not_a_variable() -> None:
    text = (
        "jobs:\n  build:\n    runs-on: ubuntu-24.04\n    steps:\n"
        "      - uses: docker/build-push-action@v6\n        with:\n"
        "          tags: ${{ env.REGISTRY }}/${{ env.IMAGE_NAME }}:${{ github.ref_name }}\n"
    )
    assert not any("${{" in v for v in values(text))
