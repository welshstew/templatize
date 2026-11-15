"""Gaps closed after the detector sweep (corpus batch five): log timestamps, short
versions, images, AWS IDs, Markdown keys, ranges, person lists and service names."""

from __future__ import annotations

import pytest

from templatize.detectors import DetectionContext
from templatize.detectors.people import PeopleDetector
from templatize.detectors.repetition import RepetitionDetector
from templatize.loops import find_keyword_lines
from templatize.suggest import suggest


def values(text: str) -> dict[str, str]:
    return {c.value: c.name for c in suggest(text).candidates}


def people(text: str) -> dict[str, str]:
    return {
        c.name: c.value for c in PeopleDetector().detect(DetectionContext(text=text)).candidates
    }


@pytest.mark.parametrize(
    "text, stamp",
    [
        (
            '10.1.2.3 - - [03/Dec/2026:14:22:09 +0000] "GET / HTTP/1.1" 200 5\n',
            "03/Dec/2026:14:22:09 +0000",
        ),
        ("Dec  3 14:30:01 web-03 sshd[1]: started\n", "Dec  3 14:30:01"),
        ("Nov 14 09:02:11 db-1 kernel: oom\n", "Nov 14 09:02:11"),
    ],
)
def test_log_timestamps_are_whole(text: str, stamp: str) -> None:
    found = values(text)
    assert found[stamp] == "timestamp"
    assert not any(v != stamp and v in stamp for v in found)  # no separate date or time


def test_log_lines_are_not_a_loop() -> None:
    text = "Dec  3 14:30:01 web-03 a\nDec  3 14:30:05 web-03 b\nDec  3 14:31:12 web-03 c\n"
    assert find_keyword_lines(text) == []


@pytest.mark.parametrize(
    "text, expected",
    [
        (
            "Move Elasticsearch 7.17 to OpenSearch 2.18.",
            {"7.17": "elasticsearch_version", "2.18": "open_search_version"},
        ),
        (
            "Python 3.12 and Node 22.11 are required.",
            {"3.12": "python_version", "22.11": "node_version"},
        ),
        (
            "Upgrade PostgreSQL on db-1 from 14.11 to 16.6.",
            {"14.11": "from_version", "16.6": "to_version"},
        ),
        ("Version 2.1 is out.", {"2.1": "version"}),
    ],
)
def test_short_versions(text: str, expected: dict[str, str]) -> None:
    found = values(text)
    for value, name in expected.items():
        assert found.get(value) == name


@pytest.mark.parametrize(
    "text",
    ["See Section 4.2 and Table 3.1.", "We scored 4.5 out of 5.", "Total 45.00", "about 3.5 hours"],
)
def test_numbers_that_are_not_versions(text: str) -> None:
    assert not any(n.endswith("version") for n in values(text).values())


def test_image_in_prose_is_whole() -> None:
    text = 'Container image "registry.acme.example.com/payments/checkout:2.14.0" already present\n'
    found = values(text)
    assert found["registry.acme.example.com/payments/checkout:2.14.0"] == "image"
    assert "registry.acme.example.com" not in found and "2.14.0" not in found


def test_kubernetes_images_still_split() -> None:
    text = (
        "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: api\nspec:\n  template:\n"
        "    spec:\n      containers:\n        - name: api\n"
        "          image: registry.example.com/acme/api:1.4.2\n"
    )
    found = values(text)
    assert "registry.example.com/acme/api" in found and "1.4.2" in found


@pytest.mark.parametrize(
    "value, name",
    [
        ("snap-0d4e8f1a2b3c4d5e6", "snapshot_id"),
        ("sg-0a1b2c3d", "security_group_id"),
        ("i-0123456789abcdef0", "instance_id"),
        ("subnet-1a2b3c4d", "subnet_id"),
    ],
)
def test_aws_resource_ids(value: str, name: str) -> None:
    assert values(f"Restore {value} tonight.\n")[value] == name


def test_spaced_sizes() -> None:
    found = values("Reindex the products index (about 1.2 TB) and keep 512 MiB free.\n")
    assert found["1.2 TB"] == "index_size"
    assert "512 MiB" in found


def test_markdown_bold_keys() -> None:
    text = "**Location:** Manchester, UK (hybrid)\n- **Team**: Platform Engineering\n"
    found = values(text)
    assert found["Manchester, UK"] == "location"
    assert found["Platform Engineering"] == "team"


@pytest.mark.parametrize(
    "text, expected",
    [
        ("**Salary:** £75,000 \u2013 £90,000\n", {"75,000": "salary_min", "90,000": "salary_max"}),
        ("Budget: $4,000 to $6,500 per month", {"4,000": "budget_min", "6,500": "budget_max"}),
        ("between $40 and $60 an hour", {"40": "amount_min", "60": "amount_max"}),
    ],
)
def test_amount_ranges(text: str, expected: dict[str, str]) -> None:
    found = values(text)
    for value, name in expected.items():
        assert found[value] == name


def test_two_amounts_are_not_a_range() -> None:
    found = values("paid £580.00 and £116.00 today")
    assert not any(n.endswith(("_min", "_max")) for n in found.values())


def test_attendee_list_with_first_names_used_alone() -> None:
    text = (
        "Attendees: Priya Nair, Tom Okoro, Lena Fischer\n\n"
        "- Tom presented the plan.\n- Lena raised concerns.\n"
    )
    found = people(text)
    assert found["attendee_1_name"] == "Priya Nair"
    assert found["attendee_2_first_name"] == "Tom"
    assert found["attendee_2_last_name"] == "Okoro"
    c = next(
        c
        for c in PeopleDetector().detect(DetectionContext(text=text)).candidates
        if c.name == "attendee_3_first_name"
    )
    assert len(c.spans) == 2


def test_list_that_is_not_people() -> None:
    found = people("Notes\n\nAttendees: the whole team\n")
    assert not any(n.startswith("attendee") for n in found)


@pytest.mark.parametrize(
    "text, name, value",
    [
        ("You'll report to Grace Liu, Head of Platform.", "manager_name", "Grace Liu"),
        ("Questions to Hannah Kim or #billing-eng.", "contact_name", "Hannah Kim"),
        ("The review was led by Amir Haddad.", "lead_name", "Amir Haddad"),
        ("Approved by Sofia Martins on Monday.", "approver_name", "Sofia Martins"),
    ],
)
def test_people_after_phrases(text: str, name: str, value: str) -> None:
    assert people(text)[name] == value


@pytest.mark.parametrize(
    "text",
    ["Please contact Customer Support for help.", "Questions to Platform Engineering please."],
)
def test_phrases_naming_teams_are_not_people(text: str) -> None:
    found = people(text)
    assert not any(n.endswith("_name") for n in found)


def test_service_names_in_prose() -> None:
    text = "The Checkout Service retried, but the Ledger Service pool was too small.\n"
    found = people(text)
    assert found["checkout_service"] == "Checkout Service"
    assert found["ledger_service"] == "Ledger Service"


def test_signature_title_and_company_without_legal_suffix() -> None:
    text = "Kind regards,\nOliver Grant\nAccount Manager, Northgate IT Services\n"
    found = people(text)
    assert found["sender_title"] == "Account Manager"
    assert found["sender_company"] == "Northgate IT Services"


def test_employer_in_as_a_title_at_company() -> None:
    found = people("As a Senior Engineer at Brightwave, you will run the platform.\n")
    assert found["company"] == "Brightwave"
    assert found["job_title"] == "Senior Engineer"


def test_well_known_constants_do_not_repeat_into_variables() -> None:
    text = (
        '1.1.1.1 "GET /" "Mozilla/5.0 (X11; Linux x86_64)"\n'
        '1.1.1.2 "GET /a" "Mozilla/5.0 (X11; Linux x86_64)"\n'
        "Warning BackOff\nWarning Failed\n"
    )
    found = {c.value for c in RepetitionDetector().detect(DetectionContext(text=text)).candidates}
    assert not found & {"Mozilla/5.0", "Linux", "x86_64", "Warning"}


# --- corpus batch six -------------------------------------------------------------


def test_variable_references_are_not_repeated_values() -> None:
    text = "- docker build -t $REG:$CI_COMMIT_SHA .\n- docker push $REG:$CI_COMMIT_SHA\n${HOME} ${HOME}\n"
    found = {c.value for c in RepetitionDetector().detect(DetectionContext(text=text)).candidates}
    assert not found & {"CI_COMMIT_SHA", "HOME", "REG"}


def test_contract_defined_terms_and_parties() -> None:
    text = (
        'This Agreement (the "Agreement") is made between Halden Robotics Ltd, a company '
        "registered under number\n09876543, whose office is at 22 Mill Lane, Cambridge CB2 1RX\n"
        '("the Client"), and Quayside Software Ltd, whose office is at\n'
        '5 Dock Street, Leeds LS10 1JF ("the Supplier").\n'
        "The Client pays the Supplier monthly under the Agreement.\n"
        "Signed for Halden Robotics Ltd: Amelia Ward, Chief Operating Officer\n"
    )
    found = values(text)
    assert found["Halden Robotics Ltd"] == "client_company"
    assert found["Quayside Software Ltd"] == "supplier_company"
    assert found["22 Mill Lane, Cambridge CB2 1RX"] == "client_address"
    assert found["5 Dock Street, Leeds LS10 1JF"] == "supplier_address"
    assert found["09876543"] == "company_number"
    assert found["Amelia Ward"] == "signatory_name"
    assert found["Chief Operating Officer"] == "signatory_title"
    assert not {"Agreement", "Client", "Supplier"} & set(found)


def test_address_followed_by_a_parenthesis() -> None:
    found = values('at\n5 Dock Street, Leeds LS10 1JF ("the Supplier").\n')
    assert "5 Dock Street, Leeds LS10 1JF" in found


def test_author_list_beats_the_whole_value() -> None:
    text = "**Authors:** Ines Carvalho, Raj Malhotra\n\n- Raj rolled back.\n"
    found = values(text)
    assert found["Ines Carvalho"] == "author_1_name"  # first name not used alone
    assert found["Raj"] == "author_2_first_name"
    assert found["Malhotra"] == "author_2_last_name"
    assert "Ines Carvalho, Raj Malhotra" not in found


def test_reference_in_parentheses_is_not_named_after_a_unit() -> None:
    assert values("- Raj: lower the cache to 512 MiB (PAY-1183)\n")["PAY-1183"] == "reference"


def test_code_span_with_a_plural_noun() -> None:
    found = values("The `checkout-api` pods ran out of memory; alert `CheckoutErrorRate` fired.\n")
    assert found["checkout-api"] == "pod"
    assert found["CheckoutErrorRate"] == "alert"


def test_bank_notice_values() -> None:
    text = (
        "We received a payment of £1,250.00 into your account ending 4471.\n"
        "The payment was from KESTREL HOMES LTD with the reference RENT DEC 26.\n"
        "Your available balance is now £3,812.44.\n"
    )
    found = values(text)
    assert found["4471"] == "account_last_digits"
    assert found["KESTREL HOMES LTD"] == "payer_name"
    assert found["RENT DEC 26"] == "payment_reference"
    assert found["3,812.44"] == "balance"


def test_company_name_broken_over_lines_is_not_cut() -> None:
    text = "the position of Engineer at Orbit\nAnalytics GmbH, reporting to Lena Brandt.\n"
    assert "Analytics GmbH" not in values(text)


def test_greeting_name_gives_the_last_name_of_the_full_mention() -> None:
    found = people("Hello Daniel,\n\nPassenger: Mr Daniel Okafor\n")
    assert found["recipient_first_name"] == "Daniel"
    assert found["recipient_last_name"] == "Okafor"


def test_title_after_a_phrase_person() -> None:
    found = people("You'll be reporting to Lena Brandt, Head of Data Platform.\n")
    assert found["manager_name"] == "Lena Brandt"
    assert found["manager_title"] == "Head of Data Platform"


# --- corpus batch seven -----------------------------------------------------------


def test_docker_run_values() -> None:
    text = (
        "#!/usr/bin/env bash\ndocker run -d --name grafana \\\n"
        "  -e GF_SECURITY_ADMIN_PASSWORD=Gr4f4n4!2026 \\\n  grafana/grafana:11.3.1\n"
    )
    found = values(text)
    assert found["grafana/grafana:11.3.1"] == "image"
    assert found["Gr4f4n4!2026"] == "gf_security_admin_password"
    assert "/usr/bin/env" not in found and "11.3.1" not in found


def test_terraform_plan_attributes() -> None:
    text = (
        "Terraform will perform the following actions:\n\n"
        '  + resource "aws_instance" "web" {\n'
        '      + instance_type          = "t3.large"\n    }\n\n'
        "Plan: 1 to add, 0 to change, 0 to destroy.\n"
    )
    found = values(text)
    assert found["t3.large"] == "instance_type"
    assert not any("to add" in v for v in found)


def test_slashed_dates_and_timestamps() -> None:
    found = values("2026/12/04 10:15:02 [error] connect() failed\nBackup on 2026/12/05.\n")
    assert found["2026/12/04 10:15:02"] == "timestamp"
    assert "2026/12/05" in found


def test_version_after_a_lowercase_product() -> None:
    assert values("Safari 18.1 on macOS 15.1\n")["15.1"] == "mac_os_version"


def test_logfmt_value_with_underscore() -> None:
    assert values("ERROR request_id=req_8Kq2Zt timeout=30s\n")["req_8Kq2Zt"] == "request_id"


# --- corpus batch eight -----------------------------------------------------------


def test_prose_is_not_a_keyword_config() -> None:
    text = (
        "Dear Parents and Carers,\n\nYear 6 will visit the museum. Pay through\n"
        "ParentPay by 22 January 2027.\n\nChildren will need a packed lunch and a coat.\n\n"
        "Mrs J. Thompson\nHeadteacher, Oakfield Primary School\n"
    )
    found = values(text)
    assert "Parents and Carers," not in found
    assert not any(v.startswith("will need") for v in found)
    assert found["J. Thompson"] == "sender_name"
    assert found["Headteacher"] == "sender_title"
    assert found["Oakfield Primary School"] == "sender_company"


def test_ssh_block_in_markdown_still_read() -> None:
    text = (
        "Add this to `~/.ssh/config`:\n\n    Host buildbox\n      HostName 10.20.0.15\n"
        "      User ci\n      Port 2201\n\nThen run `ssh buildbox` and check the fingerprint is\n"
    )
    found = values(text)
    assert {"buildbox", "10.20.0.15", "ci", "2201"} <= set(found)
    assert not any(v.startswith("run `ssh") for v in found)


def test_organisation_sign_off() -> None:
    text = "Dear Ms Hughes,\n\nYour balance is £3.00.\n\nNorthbank Customer Services\n"
    assert values(text)["Northbank Customer Services"] == "sender_company"


def test_doctor_name() -> None:
    assert (
        people("your appointment with Dr. Hannah Moore at 09:20")["doctor_name"] == "Hannah Moore"
    )


def test_checksums_and_algorithm_names() -> None:
    digest = "4e1b9a2c7d0f3e6b8a5c2d9f1e0b7a4c6d3f8e2b1a9c0d7e5f4b3a2c1d0e9f8a"
    found = values(f"Checksum (SHA-256): {digest}\nSigned with RSA-2048 over TLS-1.3.\n")
    assert found[digest] == "checksum"
    assert not {"SHA-256", "RSA-2048"} & set(found)


# --- after batch eight ------------------------------------------------------------


def test_port_mappings() -> None:
    found = values('services:\n  api:\n    ports:\n      - "18080:8080"\n')
    assert found["18080"] == "host_port" and found["8080"] == "container_port"
    assert values("docker run -p 9000:80 nginx:1.27\n")["9000"] == "host_port"


def test_timeline_items_are_not_port_mappings() -> None:
    found = values("- 13:02 - Alert fired\n- 13:47 - Error rate back to normal\n")
    assert not any(n.endswith("_port") for n in found.values())


def test_vault_paths() -> None:
    assert (
        values("Pull it from Vault at secret/billing/stripe after 21:15.\n")[
            "secret/billing/stripe"
        ]
        == "vault_path"
    )


def test_nginx_header_values_are_named_after_the_header() -> None:
    text = "server {\n    proxy_set_header X-Env production;\n    proxy_set_header Host $host;\n}\n"
    found = values(text)
    assert found["production"] == "x_env"
    assert "$host" not in found


def test_a_diff_keeps_the_pattern_name() -> None:
    text = "server {\n    listen 443 ssl;\n}\n"
    found = {c.value: c.name for c in suggest(text, [text.replace("443", "8443")]).candidates}
    assert found["443"] == "port"
