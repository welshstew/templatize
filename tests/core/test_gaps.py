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
