from __future__ import annotations

import pytest

from templatize.detectors import DetectionContext
from templatize.detectors.diff import DiffDetector
from templatize.detectors.keyvalue import KeyValueDetector
from templatize.detectors.regex import RegexDetector
from templatize.detectors.repetition import RepetitionDetector
from templatize.ir import Candidate, VarType
from templatize.suggest import suggest


def regex(text: str) -> list[Candidate]:
    return RegexDetector().detect(DetectionContext(text=text)).candidates


def values(cands: list[Candidate], vtype: VarType | None = None) -> list[str]:
    return [c.value for c in cands if vtype is None or c.type is vtype]


# --- regex --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "vtype", "expected"),
    [
        ("host 192.168.0.1 up", VarType.IPV4, "192.168.0.1"),
        ("net 10.0.0.0/8;", VarType.CIDR, "10.0.0.0/8"),
        ("addr fe80::1ff:fe23:4567:890a end", VarType.IPV6, "fe80::1ff:fe23:4567:890a"),
        ("v6 2001:db8::1", VarType.IPV6, "2001:db8::1"),
        ("mail bob.smith+x@example.co.uk.", VarType.EMAIL, "bob.smith+x@example.co.uk"),
        ("see https://example.com/a?b=c.", VarType.URL, "https://example.com/a?b=c"),
        ("(https://x.io/p)", VarType.URL, "https://x.io/p"),
        ("db postgres://u:p@h:5432/d", VarType.URL, "postgres://u:p@h:5432/d"),
        (
            "id 123e4567-e89b-12d3-a456-426614174000",
            VarType.UUID,
            "123e4567-e89b-12d3-a456-426614174000",
        ),
        ("mac 00:1A:2b:3C:4d:5E", VarType.MAC, "00:1A:2b:3C:4d:5E"),
        ("mac 00-1A-2B-3C-4D-5E", VarType.MAC, "00-1A-2B-3C-4D-5E"),
        ("on 2024-02-29.", VarType.DATE, "2024-02-29"),
        ("at 2024-01-05T10:20:30Z", VarType.DATETIME, "2024-01-05T10:20:30Z"),
        ("due 31/12/2024", VarType.DATE, "31/12/2024"),
        ("on 3 March 2025", VarType.DATE, "3 March 2025"),
        ("on March 3, 2025", VarType.DATE, "March 3, 2025"),
        ("release v1.2.3-rc.1+b5", VarType.SEMVER, "v1.2.3-rc.1+b5"),
        ("listen 8080;", VarType.PORT, "8080"),
        ("api.example.com:8443/x", VarType.PORT, "8443"),
        ("server db01.prod.example.com;", VarType.HOSTNAME, "db01.prod.example.com"),
        ("path /etc/nginx/nginx.conf ok", VarType.PATH, "/etc/nginx/nginx.conf"),
        (r"dir C:\Program Files\App", VarType.PATH, r"C:\Program"),
    ],
)
def test_regex_positive(text: str, vtype: VarType, expected: str) -> None:
    assert expected in values(regex(text), vtype)


@pytest.mark.parametrize(
    ("text", "vtype"),
    [
        ("999.1.1.1", VarType.IPV4),
        ("1.2.3.4.5", VarType.IPV4),
        ("time 12:30:45", VarType.IPV6),
        ("date 2024-02-30", VarType.DATE),
        ("port 99999", VarType.PORT),
        ("call os.path.join(x)", VarType.HOSTNAME),
        ("file nginx.conf", VarType.HOSTNAME),
        ("version 1.2.3.4", VarType.SEMVER),
        ("single /tmp", VarType.PATH),
    ],
)
def test_regex_negative(text: str, vtype: VarType) -> None:
    assert values(regex(text), vtype) == []


# --- key/value ----------------------------------------------------------------


def kv(text: str) -> dict[str, str]:
    cands = KeyValueDetector().detect(DetectionContext(text=text)).candidates
    return {c.name: c.value for c in cands}


def test_kv_formats() -> None:
    text = (
        "name: web\n"
        "image: 'nginx:1.25'  \n"
        "DB_HOST=db.local\n"
        'export API_KEY="abc 123"\n'
        "timeout = 30 # seconds\n"
        '{"serviceName": "billing", "replicaCount": 3, "debug": true}\n'
        "    server_name example.com;\n"
        "Subject: Quarterly report\n"
    )
    got = kv(text)
    assert got["name"] == "web"
    assert got["image"] == "nginx:1.25"
    assert got["db_host"] == "db.local"
    assert got["api_key"] == "abc 123"
    assert got["timeout"] == "30"
    assert got["service_name"] == "billing"
    assert got["replica_count"] == "3"
    assert got["server_name"] == "example.com"
    assert got["subject"] == "Quarterly report"


def test_kv_yaml_parent_qualifies_generic_keys() -> None:
    text = "database:\n  host: db1\n  port: 5432\nredis:\n  host: cache\n"
    got = kv(text)
    assert got["database_host"] == "db1"
    assert got["redis_host"] == "cache"
    assert got["database_port"] == "5432"


def test_kv_k8s_name_value_pairs() -> None:
    text = 'env:\n  - name: NODE_IP\n    value: "1.2.3.4"\n'
    got = kv(text)
    assert got == {"node_ip": "1.2.3.4"}


def test_kv_skips_block_scalars_and_nulls() -> None:
    assert kv("script: |\n  echo hi\nanchor: &a\nnothing: null\nempty:\n") == {}


# --- repetition ---------------------------------------------------------------


def test_repetition_groups_occurrences() -> None:
    text = "Acme ships to Acme-West. Contact Acme today. the the the"
    cands = RepetitionDetector().detect(DetectionContext(text=text)).candidates
    by_value = {c.value: c for c in cands}
    assert "Acme" in by_value and len(by_value["Acme"].spans) == 2  # Acme-West is its own token
    assert "the" not in by_value


def test_repetition_ignores_keys() -> None:
    text = "image: a\nimage: b\nsvc01: x\nsvc01: y\n"
    cands = RepetitionDetector().detect(DetectionContext(text=text)).candidates
    assert values(cands) == []


# --- diff ---------------------------------------------------------------------


def diff(text: str, *variants: str) -> tuple[list[Candidate], list[str]]:
    res = DiffDetector().detect(DetectionContext(text=text, variants=variants))
    return res.candidates, [n.kind for n in res.notices]


def test_diff_simple_substitution() -> None:
    cands, notices = diff("host: alpha\nport: 80\n", "host: beta\nport: 80\n")
    assert [(c.value, c.variant_values, c.name) for c in cands] == [("alpha", ["beta"], "host")]
    assert notices == []


def test_diff_widens_to_whole_token() -> None:
    cands, _ = diff("ip 10.0.0.1 end", "ip 10.0.0.2 end")
    assert [(c.value, c.variant_values) for c in cands] == [("10.0.0.1", ["10.0.0.2"])]


def test_diff_insertion_widens_to_enclosing_word() -> None:
    cands, notices = diff("Dear Bob,\n", "Dear Dr. Bob,\n")
    assert [(c.value, c.variant_values) for c in cands] == [("Bob", ["Dr. Bob"])]
    assert notices == []


def test_diff_insertion_at_end_of_word() -> None:
    cands, _ = diff("I like dog.\n", "I like dog food.\n")
    assert [(c.value, c.variant_values) for c in cands] == [("dog", ["dog food"])]


def test_diff_deletion_widens() -> None:
    cands, _ = diff("Dear Dr. Bob,\n", "Dear Bob,\n")
    assert len(cands) == 1
    assert cands[0].value.endswith("Bob") and cands[0].variant_values == ["Bob"]


def test_diff_inserted_line_is_structural() -> None:
    cands, notices = diff("a: 1\nc: 3\n", "a: 1\nb: 2\nc: 3\n")
    assert cands == []
    assert notices == ["structural_difference"]


def test_diff_deleted_line_is_structural() -> None:
    cands, notices = diff("a: 1\nb: 2\nc: 3\n", "a: 1\nc: 3\n")
    assert cands == []
    assert notices == ["structural_difference"]


def test_diff_multiline_replacement_is_allowed() -> None:
    cands, notices = diff("x\nold one\nold two\ny\n", "x\nnew one\nnew two\ny\n")
    assert notices == []
    assert {c.value for c in cands} == {"old"}  # same value/variant pair grouped
    assert len(cands[0].spans) == 2


def test_diff_union_across_variants() -> None:
    text = "name=alpha zone=eu\n"
    cands, _ = diff(text, "name=beta zone=eu\n", "name=alpha zone=us\n")
    got = {c.value: c.variant_values for c in cands}
    assert got == {"alpha": ["beta", "alpha"], "eu": ["eu", "us"]}


def test_diff_snaps_to_dates_and_keeps_numbers_whole() -> None:
    text = "Sent 12 March 2025 for GBP 18,450.00.\n"
    cands, _ = diff(text, "Sent 2 April 2025 for GBP 5,765.50.\n")
    got = {c.value: (c.variant_values, c.name) for c in cands}
    assert got == {
        "12 March 2025": (["2 April 2025"], "date"),
        "18,450.00": (["5,765.50"], "amount"),
    }


def test_diff_identical_variant_yields_nothing() -> None:
    assert diff("same\n", "same\n") == ([], [])


def test_diff_crlf_documents() -> None:
    cands, _ = diff("a=1\r\nb=x\r\n", "a=1\r\nb=y\r\n")
    assert [(c.value, c.variant_values) for c in cands] == [("x", ["y"])]


def test_diff_candidates_have_top_confidence_after_merge() -> None:
    res = suggest("server alpha.example.com\n", ["server beta.example.com\n"])
    (c,) = [c for c in res.candidates if c.value == "alpha.example.com"]
    assert c.confidence >= 0.95
    assert "diff" in c.sources
