"""Configuration security and identity invariants."""

import base64
import json
from copy import deepcopy

import pytest
import yaml
from fixtures import CLUSTER_ID, EXAMPLES, cluster, cluster_data, profile, profile_data
from pydantic import ValidationError

from lab.config import parse_cluster, parse_index, quantity, safe_document, secret_reference
from lab.errors import LabError


def modify_kube(data, transform):
    kube = yaml.safe_load(data["kubernetes"]["kubeconfig"])
    transform(kube)
    data["kubernetes"]["kubeconfig"] = yaml.safe_dump(kube)
    return data


def test_readable_index_name_is_the_identity():
    text = (EXAMPLES / "index.yaml").read_text()
    original = parse_index(text)
    renamed = parse_index(text.replace("research-example", "new-name"))
    assert original.clusters[0].id == "research-example"
    assert renamed.clusters[0].id == "new-name"
    assert set(original.clusters[0].model_dump()) == {"id", "config_ref"}
    assert original.session.max_age_seconds == 604800
    assert parse_index(json.dumps(original.model_dump(mode="json"))) == original


@pytest.mark.parametrize(
    "text",
    [
        "schema_version: 1\nschema_version: 1",
        "a: &x [1]\nb: *x",
        "a: !!str secret",
        "a: !Run secret",
        "a: " + "[" * 33 + "0" + "]" * 33,
        "a: " + "x" * (1024 * 1024),
        "1: invalid",
        "[1, 2]",
        "a: [SECRET_VALUE",
    ],
)
def test_hostile_documents_are_bounded_and_redacted(text):
    with pytest.raises(LabError) as caught:
        safe_document(text)
    assert "SECRET_VALUE" not in str(caught.value)


def test_unknown_fields_never_echo_input_values_or_unknown_keys():
    data = profile_data()
    data["namespace_rules"]["research"]["SECRET_KEY_VALUE"] = "SECRET_BODY_VALUE"
    with pytest.raises(LabError) as caught:
        profile(data)
    assert "SECRET" not in str(caught.value)
    assert "namespace_rules" in str(caught.value)


def test_duplicate_key_error_reports_location_without_echoing_key():
    with pytest.raises(LabError, match="line 2, column 1") as caught:
        safe_document("SECRET: a\nSECRET: b")
    assert "SECRET" not in str(caught.value)


@pytest.mark.parametrize(
    "field,value",
    [
        ("exec", {"command": "/tmp/DO_NOT_RUN"}),
        ("tokenFile", "/tmp/DO_NOT_READ"),
        ("auth-provider", {"name": "gcp"}),
        ("client-certificate-data", "PRIVATE"),
        ("client-key", "/tmp/DO_NOT_READ"),
    ],
)
def test_imported_authentication_is_inert_and_rejected(field, value):
    data = modify_kube(cluster_data(), lambda k: k["users"][0]["user"].update({field: value}))
    with pytest.raises(LabError, match="static bearer-token"):
        cluster(data)


@pytest.mark.parametrize(
    "field,value",
    [
        ("certificate-authority", "/tmp/DO_NOT_READ"),
        ("proxy-url", "https://proxy.example.invalid"),
        ("insecure-skip-tls-verify", True),
    ],
)
def test_imported_routing_and_file_trust_are_rejected(field, value):
    data = modify_kube(cluster_data(), lambda k: k["clusters"][0]["cluster"].update({field: value}))
    with pytest.raises(LabError):
        cluster(data)


@pytest.mark.parametrize(
    "server",
    [
        "http://api.example.invalid",
        "https://SECRET@api.example.invalid",
        "https://api.example.invalid/other",
        "https://api.example.invalid?token=SECRET",
        "https://api.example.invalid:99999",
        "https://api.example.invalid/#fragment",
    ],
)
def test_endpoint_must_be_an_unambiguous_https_origin(server):
    data = modify_kube(cluster_data(), lambda k: k["clusters"][0]["cluster"].update(server=server))
    with pytest.raises(LabError) as caught:
        cluster(data)
    assert "SECRET" not in str(caught.value)


def test_selected_context_resolves_exactly_and_preserves_tls_identity():
    data = cluster_data()
    ca = base64.b64encode(b"SYNTHETIC_PUBLIC_CA").decode()
    modify_kube(
        data,
        lambda k: k["clusters"][0]["cluster"].update(
            {
                "certificate-authority-data": ca,
                "tls-server-name": "expected.example.invalid",
            }
        ),
    )
    selected = cluster(data)
    assert selected.connection.ca_data == ca
    assert selected.connection.tls_name == "expected.example.invalid"
    assert "FICTIONAL_NOT_A_CREDENTIAL" not in repr(selected)
    assert "FICTIONAL_NOT_A_CREDENTIAL" not in selected.model_dump_json()


def test_duplicate_context_is_rejected():
    data = modify_kube(cluster_data(), lambda k: k["contexts"].append(deepcopy(k["contexts"][0])))
    with pytest.raises(LabError, match="ambiguous"):
        cluster(data)


def test_snapshots_are_immutable():
    selected = cluster()
    with pytest.raises(ValidationError):
        selected.transport.mode = "ssh"
    selected = profile()
    with pytest.raises(TypeError):
        selected.namespace_rules["different"] = selected.namespace_rules["research"]
    with pytest.raises(TypeError):
        selected.namespace_rules["research"].placement.cpu_required_selector["other"] = "value"


@pytest.mark.parametrize(
    "value", ["1n", "0", "-1", "0.0001", "1.00000000000000000000001", "1e999", "NaN"]
)
def test_cpu_rejects_inexact_or_unbounded_quantities(value):
    with pytest.raises(ValueError):
        quantity(value, cpu=True)


def test_unit_equivalence_is_exact():
    assert quantity("2", cpu=True) == quantity("2000m", cpu=True) == 2000
    assert quantity("1Gi") == quantity("1024Mi") == quantity("1073741824")
    assert quantity("1e3") == 1000


@pytest.mark.parametrize(
    "value", ["https://example.invalid/a/b", "op://vault/item", "op://vault/item/field?x=1"]
)
def test_secret_references_are_not_fetchable_urls(value):
    with pytest.raises(ValueError):
        secret_reference(value)


@pytest.mark.parametrize("version", [True, 1.0, "1", 2])
def test_schema_version_is_strict(version):
    data = yaml.safe_load((EXAMPLES / "index.yaml").read_text())
    data["schema_version"] = version
    with pytest.raises(LabError):
        parse_index(yaml.safe_dump(data))


def connection_note():
    data = cluster_data()
    return {
        "kubernetes": {key: data["kubernetes"][key] for key in ("context", "kubeconfig")},
        "transport": data["transport"],
    }


def test_connection_note_uses_index_identity_and_infers_system_trust():
    from lab.config import parse_cluster_note

    text = yaml.safe_dump(connection_note())
    note = parse_cluster_note(text)
    assert set(note.model_dump()) == {"kubernetes", "transport"}
    assert note.connection.ca_data is None
    assert "FICTIONAL_NOT_A_CREDENTIAL" not in note.model_dump_json()
    selected = parse_cluster(text, CLUSTER_ID)
    assert selected.cluster_id == CLUSTER_ID
    assert set(selected.model_dump()) == {"cluster_id", "kubernetes", "transport"}


def test_connection_note_uses_embedded_ca_without_trust_override():
    from lab.config import parse_cluster_note

    data = connection_note()
    ca = base64.b64encode(b"synthetic-ca").decode()
    modify_kube(
        data, lambda k: k["clusters"][0]["cluster"].update({"certificate-authority-data": ca})
    )
    assert parse_cluster_note(yaml.safe_dump(data)).connection.ca_data == ca
    modify_kube(
        data, lambda k: k["clusters"][0]["cluster"].update({"certificate-authority-data": ""})
    )
    with pytest.raises(LabError):
        parse_cluster_note(yaml.safe_dump(data))


@pytest.mark.parametrize(
    "field", ["schema_version", "cluster_id", "owner_label", "controller", "namespace_rules"]
)
def test_connection_note_does_not_accept_internal_metadata_or_notebook_policy(field):
    from lab.config import parse_cluster_note

    data = connection_note()
    data[field] = "SECRET_LEGACY_VALUE"
    with pytest.raises(LabError):
        parse_cluster_note(yaml.safe_dump(data))


def test_empty_index_supports_first_registration():
    data = yaml.safe_load((EXAMPLES / "index.yaml").read_text())
    data["clusters"] = []
    assert parse_index(yaml.safe_dump(data)).clusters == ()


def test_connection_identity_comes_only_from_index():
    other_id = "research-other"
    text = yaml.safe_dump(cluster_data())
    assert parse_cluster(text, other_id).cluster_id == other_id
    with pytest.raises(LabError, match="cluster name"):
        parse_cluster(text, "../invalid")


@pytest.mark.parametrize(
    "field", ["schema_version", "cluster_id", "owner_label", "controller", "namespace_rules"]
)
def test_parse_cluster_rejects_embedded_identity_and_policy(field):
    data = cluster_data()
    data[field] = "SECRET_LEGACY_VALUE"
    with pytest.raises(LabError) as caught:
        parse_cluster(yaml.safe_dump(data), CLUSTER_ID)
    assert "SECRET_LEGACY_VALUE" not in str(caught.value)


@pytest.mark.parametrize("name", ["a", "h200", "research-h200", "1-cluster", "a" * 63])
def test_cluster_name_is_shared_by_connection_and_index(name):
    data = yaml.safe_load((EXAMPLES / "index.yaml").read_text())
    data["clusters"][0]["id"] = name
    entry = parse_index(yaml.safe_dump(data)).clusters[0]
    assert entry.id == name
    assert parse_cluster(yaml.safe_dump(cluster_data()), name).cluster_id == name


@pytest.mark.parametrize(
    "name",
    [
        "",
        "Research",
        "a_b",
        "a.b",
        "a@b",
        "../a",
        "/a",
        "a/b",
        "a\\b",
        "-a",
        "a-",
        "a" * 64,
        "template",
    ],
)
def test_invalid_names_cannot_enter_the_index(name):
    data = yaml.safe_load((EXAMPLES / "index.yaml").read_text())
    data["clusters"][0]["id"] = name
    with pytest.raises(LabError):
        parse_index(yaml.safe_dump(data))


def test_duplicate_names_and_separate_aliases_are_rejected():
    data = yaml.safe_load((EXAMPLES / "index.yaml").read_text())
    data["clusters"].append(deepcopy(data["clusters"][0]))
    with pytest.raises(LabError):
        parse_index(yaml.safe_dump(data))
    data["clusters"].pop()
    data["clusters"][0]["alias"] = "display-only"
    with pytest.raises(LabError):
        parse_index(yaml.safe_dump(data))
