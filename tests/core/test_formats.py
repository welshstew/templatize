"""Formats and patterns found by the detector sweep (tests/quality/corpus)."""

from __future__ import annotations

import pytest

from templatize.detectors import DetectionContext
from templatize.detectors.commands import CommandsDetector
from templatize.detectors.keyvalue import KeyValueDetector
from templatize.detectors.regex import RegexDetector
from templatize.suggest import suggest


def kv(text: str) -> dict[str, str]:
    return {
        c.name: c.value for c in KeyValueDetector().detect(DetectionContext(text=text)).candidates
    }


def cmds(text: str) -> dict[str, str]:
    return {
        c.name: c.value for c in CommandsDetector().detect(DetectionContext(text=text)).candidates
    }


def regex_named(text: str) -> dict[str, str]:
    return {c.name: c.value for c in RegexDetector().detect(DetectionContext(text=text)).candidates}


def values(text: str) -> dict[str, str]:
    return {c.value: c.name for c in suggest(text).candidates}


def test_lowercase_keyword_config() -> None:
    text = "bind 10.0.0.7\nport 6380\nrequirepass s3cr3t\nmaxmemory-policy allkeys-lru\n"
    assert kv(text) == {
        "port": "6380",
        "requirepass": "s3cr3t",
        "maxmemory_policy": "allkeys-lru",
        "bind": "10.0.0.7",
    }


def test_prose_is_not_a_keyword_config() -> None:
    assert kv("we moved the servers\nthen we restarted them.\nall good now\n") == {}


def test_apache_acronym_keywords() -> None:
    text = "ServerName a.example.org\nSSLEngine on\nSSLCertificateFile /etc/ssl/a.crt\n"
    assert kv(text)["ssl_certificate_file"] == "/etc/ssl/a.crt"


def test_icalendar_and_vcard() -> None:
    ics = (
        "BEGIN:VCALENDAR\nBEGIN:VEVENT\nDTSTART:20261120T140000Z\nSUMMARY:Review\n"
        "ORGANIZER;CN=Lena Fischer:mailto:lena@example.com\nEND:VEVENT\nEND:VCALENDAR\n"
    )
    assert kv(ics) == {
        "dtstart": "20261120T140000Z",
        "summary": "Review",
        "organizer_name": "Lena Fischer",
        "organizer_email": "lena@example.com",
    }
    vcf = "BEGIN:VCARD\nFN:Grace Liu\nADR;TYPE=WORK:;;350 Fifth Avenue;New York;NY;10118;USA\nEND:VCARD\n"
    assert kv(vcf) == {
        "full_name": "Grace Liu",
        "address_street": "350 Fifth Avenue",
        "address_city": "New York",
        "address_region": "NY",
        "address_postcode": "10118",
        "address_country": "USA",
    }


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            'conn: "Server=sql01;Database=Payroll;User Id=svc;Password=p&3;"\n',
            {"server": "sql01", "database": "Payroll", "user_id": "svc", "password": "p&3"},
        ),
        ('dsn = "host=db port=5432 dbname=app"\n', {"host": "db", "port": "5432", "dbname": "app"}),
    ],
)
def test_key_value_pair_strings(text: str, expected: dict[str, str]) -> None:
    assert kv(text) == expected


def test_dockerfile() -> None:
    text = (
        "ARG BASE=python\nFROM python:3.12-slim AS build\nENV APP_HOME=/srv/app \\\n    PORT=8000\n"
        "ENV LOG_LEVEL info\nEXPOSE 8000 9090\nUSER 10001\nWORKDIR /srv/app\nRUN make\n"
    )
    found = KeyValueDetector().detect(DetectionContext(text=text)).candidates
    assert [(c.name, c.value) for c in found] == [
        ("base", "python"),
        ("base_image", "python:3.12-slim"),
        ("app_home", "/srv/app"),
        ("port", "8000"),
        ("log_level", "info"),
        ("port", "8000"),
        ("port", "9090"),
        ("user", "10001"),
        ("workdir", "/srv/app"),
    ]


def test_inventory_host_lines() -> None:
    text = "[db]\ndb-01 ansible_host=10.0.2.31 ansible_port=2222\n"
    assert kv(text) == {"ansible_host": "10.0.2.31", "ansible_port": "2222"}


def test_value_with_detail_in_parentheses() -> None:
    assert kv("Owner: Daniel Kim (daniel.kim@example.com)\n") == {"owner": "Daniel Kim"}
    assert kv("Plan: Business (25 seats)\n") == {"seats": "25", "plan": "Business"}


@pytest.mark.parametrize(
    ("text", "name", "value"),
    [
        (
            "helm upgrade --install api ./chart --namespace billing --wait\n",
            "namespace_name",
            "billing",
        ),
        ("aws s3 cp x s3://b --region=eu-west-1\n", "region", "eu-west-1"),
        ("psql -h pg.internal -U monitor -d orders -c 'select 1'\n", "db_user", "monitor"),
        ("psql -h pg.internal -U monitor\n", "db_host", "pg.internal"),
        ("redis-cli -h cache.internal -p 6380 ping\n", "redis_port", "6380"),
        ("ssh ops@bastion.example.com uptime\n", "ssh_user", "ops"),
        ("rsync -az dist/ deploy@docs.example.com:/var/www\n", "ssh_host", "docs.example.com"),
        ("point the `orders-api` deployment at it\n", "deployment", "orders-api"),
        ("in namespace `orders-prod`.\n", "namespace_name", "orders-prod"),
        ("set `DATABASE_HOST=db.internal` first\n", "database_host", "db.internal"),
    ],
)
def test_commands(text: str, name: str, value: str) -> None:
    assert cmds(text).get(name) == value


def test_switches_take_no_value() -> None:
    assert "install" not in cmds("helm upgrade --install api ./chart\n")


@pytest.mark.parametrize(
    ("text", "name", "value"),
    [
        ("your order #A-778213 has shipped", "order_reference", "A-778213"),
        ("PagerDuty service P7X2KQ1, or", "service_id", "P7X2KQ1"),
        ("Our retainer is $12,500, due", "retainer_amount", "12,500"),
        ("Subtotal                   87.95\n", "subtotal", "87.95"),
        ("checkout-api (eu-west-1)", "region", "eu-west-1"),
        ("your verification code is 482913.", "verification_code", "482913"),
        (
            "//npm.example.com/:_authToken=npm_7Qx2vLk9PzR4sT1uW8yB3nM6cD5fG0hJ\n",
            "token",
            "npm_7Qx2vLk9PzR4sT1uW8yB3nM6cD5fG0hJ",
        ),
    ],
)
def test_new_patterns(text: str, name: str, value: str) -> None:
    assert regex_named(text).get(name) == value


@pytest.mark.parametrize(
    "text", ["SHA256 and HTTP2 over UTF8", "Python 3.12 or 1.14.20", "price 45.00 each"]
)
def test_not_ids_or_amounts(text: str) -> None:
    found = RegexDetector().detect(DetectionContext(text=text)).candidates
    assert not [c for c in found if c.detectors[0] in ("regex.opaque_id", "regex.amount")]


def test_url_wins_over_its_parts_and_shared_hosts_split() -> None:
    assert (
        values("ProxyPass /api http://127.0.0.1:9000/api\n")["http://127.0.0.1:9000/api"] == "url"
    )
    found = values("listeners=PLAINTEXT://k3.example.com:9092,SSL://k3.example.com:9093\n")
    assert found["k3.example.com"] == "hostname"


def test_template_expressions_are_not_values() -> None:
    found = values("run: docker build -t app:${{ github.sha }} .\nimage: app:${{ github.sha }}\n")
    assert "github.sha" not in found


@pytest.mark.parametrize(
    ("text", "name", "value"),
    [
        ("Thanks for shopping with Larkspur Home.\n", "company", "Larkspur Home"),
        ("Cheers,\nThe Northstar team\n", "company", "Northstar"),
        ("Welcome to Northstar! Your account is ready.\n", "product", "Northstar"),
        (
            "Your account for the Acme Robotics workspace is ready.\n",
            "workspace_name",
            "Acme Robotics",
        ),
        ("Larkspur Bank: your verification code is 482913.\n", "sender", "Larkspur Bank"),
    ],
)
def test_brand_phrases(text: str, name: str, value: str) -> None:
    assert values(text).get(value) == name


def test_sentence_after_a_label_is_not_a_value() -> None:
    assert "your order" not in values("Good news: your order #A-778213 has shipped.\n")


def test_date_and_time_together() -> None:
    assert values("Date: 12/11/2026 16:42\n")["12/11/2026 16:42"] in ("date", "timestamp")
    # ...but not when the date is also written on its own: one variable each.
    found = values("Subject: Review, 9 July 14:00\n\nJoin on 9 July at 14:00.\n")
    assert found.get("9 July") and found.get("14:00")


def test_secret_type_is_structure() -> None:
    text = "apiVersion: v1\nkind: Secret\nmetadata:\n  name: db\ntype: Opaque\nstringData:\n  PASSWORD: x7#kq\n"
    assert "Opaque" not in values(text)


def test_csv_rows_are_a_loop() -> None:
    from templatize.loops import find_csv

    text = 'id,name,note\n1,Ana,"likes, commas"\n2,Ben,"says ""hi"""\n'
    (found,) = find_csv(text)
    assert found.field_names == ["id", "name", "note"]
    assert [text[s.start : s.end] for s in found.fields[2]] == ["likes, commas", 'says ""hi""']
    assert find_csv("a;b\n1;2\n3;4\n")[0].field_names == ["a", "b"]
    assert find_csv("1,2\n3,4\n5,6\n") == []  # no header
    assert find_csv("a,b\n1,\n2,3\n") == []  # an empty cell


def test_xml_elements_and_attributes() -> None:
    pom = "<project>\n  <artifactId>invoice-service</artifactId>\n  <java.version>21</java.version>\n</project>\n"
    assert kv(pom) == {"artifact_id": "invoice-service", "java_version": "21"}
    cfg = '<?xml version="1.0"?>\n<appSettings>\n  <add key="MaxUploadMb" value="50" />\n</appSettings>\n'
    assert kv(cfg) == {"max_upload_mb": "50"}


def test_toml_inline_tables_and_name_email() -> None:
    text = 'tokio = { version = "1.41", features = ["full"] }\nauthors = ["Mira Kowalski <mira@example.com>"]\n'
    assert kv(text) == {"tokio_version": "1.41", "authors_name": "Mira Kowalski"}


def test_terraform_variable_blocks() -> None:
    text = 'variable "node_count" {\n  type    = number\n  default = 5\n}\n'
    assert kv(text) == {"node_count": "5"}


def test_fluent_bit_keywords_and_dotted_keys() -> None:
    assert (
        kv("[SERVICE]\n    Flush        5\n    Log_Level    info\n    HTTP_Port    2020\n")[
            "log_level"
        ]
        == "info"
    )
    assert "net.core.somaxconn" not in values("net.core.somaxconn = 4096\nvm.swappiness = 10\n")


def test_sql_access_control() -> None:
    text = (
        "CREATE USER app_ro WITH PASSWORD 'x!9';\nGRANT CONNECT ON DATABASE shop TO app_ro;\n"
        "GRANT SELECT ON ALL TABLES IN SCHEMA sales TO app_ro;\n"
    )
    found = {c.name: (c.value, len(c.spans)) for c in suggest(text).candidates}
    assert found["role"] == ("app_ro", 3) and found["password"] == ("x!9", 1)
    assert found["database"] == ("shop", 1) and found["schema"] == ("sales", 1)


def test_people_introduced_with_titles_and_roles() -> None:
    text = "Thanks for applying for the Senior Site Reliability Engineer role.\nYou will meet Hannah Lewis (Engineering Manager).\n"
    found = values(text)
    assert found["Senior Site Reliability Engineer"] == "job_title"
    assert found["Hannah Lewis"] == "person_name"
