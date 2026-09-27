"""Validate bounded, inert operator configuration.

Configuration errors identify fields without reproducing secret values.
"""

from __future__ import annotations

import base64
import binascii
import re
from decimal import Decimal, InvalidOperation, localcontext
from ipaddress import ip_address
from pathlib import PurePosixPath
from typing import Annotated, Literal, Self, cast
from urllib.parse import urlsplit

import yaml
from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    SecretStr,
    ValidationError,
    field_validator,
    model_validator,
)

from lab.errors import LabError

MAX_DOCUMENT_BYTES = 1024 * 1024
OPERATION_LABEL = "lab.operations/id"


class FrozenDict(dict):
    def _immutable(self, *args, **kwargs):
        raise TypeError("Configuration is immutable")

    __setitem__ = __delitem__ = clear = pop = popitem = setdefault = update = __ior__ = _immutable


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, hide_input_in_errors=True)

    @model_validator(mode="before")
    @classmethod
    def literal_types(cls, data):
        if isinstance(data, dict):
            if "schema_version" in data and type(data["schema_version"]) is not int:
                raise ValueError("Schema version must be an integer")
            if "requests_equal_limits" in data and data["requests_equal_limits"] is not True:
                raise ValueError("Requests must equal limits")
        return data

    @model_validator(mode="after")
    def freeze_maps(self) -> Self:
        for name in type(self).model_fields:
            value = getattr(self, name)
            if isinstance(value, dict):
                object.__setattr__(self, name, FrozenDict(value))
        return self


def nonempty(value: str) -> str:
    if not value or not value.strip() or any(ord(c) < 32 for c in value):
        raise ValueError("Expected a nonempty string without control characters")
    return value


def dns_name(value: str) -> str:
    if len(value) > 253 or not all(
        len(part) <= 63 and re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", part)
        for part in value.split(".")
    ):
        raise ValueError("Expected a DNS name")
    return value


def namespace_name(value: str) -> str:
    dns_name(value)
    if len(value) > 63 or "." in value:
        raise ValueError("Expected a DNS label namespace")
    return value


def label_value(value: str) -> str:
    if len(value) > 63 or (
        value and not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9_.-]*[A-Za-z0-9])?", value)
    ):
        raise ValueError("Expected a Kubernetes label value")
    return value


def label_key(value: str) -> str:
    parts = value.split("/")
    if len(parts) > 2 or not parts[-1]:
        raise ValueError("Expected a Kubernetes label key")
    if len(parts) == 2:
        dns_name(parts[0])
    label_value(parts[-1])
    return value


def resource_key(value: str) -> str:
    label_key(value)
    if "/" not in value or value.split("/")[0] in {"kubernetes.io", "k8s.io"}:
        raise ValueError("Expected an extended resource key")
    return value


def absolute_path(value: str) -> str:
    nonempty(value)
    if not value.startswith("/") or value.startswith("//") or ".." in value.split("/"):
        raise ValueError("Expected an absolute POSIX path without traversal")
    return str(PurePosixPath(value))


def secret_reference(value: str) -> str:
    if not re.fullmatch(r"op://[^/?#\s]+/[^/?#\s]+/(?:[^/?#\s]+/)?[^/?#\s]+", value):
        raise ValueError("Expected an op:// vault/item/field reference")
    return value


def cluster_name(value: str) -> str:
    """Validate the readable identity used by the index, CLI and profile filename."""
    namespace_name(value)
    if value == "template":
        raise ValueError("The name template is reserved for the public example")
    return value


def quantity(value: str, *, cpu: bool = False, allow_zero: bool = False) -> int:
    """Return exact millicores or bytes, rejecting rounding and overflow."""
    match = re.fullmatch(
        r"([+]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+))([eE][+-]?[0-9]+|[numkKMGTPE]|[KMGTPE]i)?", value
    )
    if not match or len(value) > 80:
        raise ValueError("Expected a positive Kubernetes resource quantity")
    suffix = match[2] or ""
    if suffix.startswith(("e", "E")) and len(suffix) > 1 and suffix != "Ei":
        exponent = int(suffix[1:])
        if abs(exponent) > 18:
            raise ValueError("Quantity exponent exceeds supported bounds")
        factor = Decimal(10) ** exponent
    elif suffix.endswith("i"):
        factor = Decimal(1024) ** ("KMGTPE".index(suffix[0]) + 1)
    else:
        factor = (
            Decimal(10)
            ** {
                "n": -9,
                "u": -6,
                "m": -3,
                "": 0,
                "k": 3,
                "K": 3,
                "M": 6,
                "G": 9,
                "T": 12,
                "P": 15,
                "E": 18,
            }[suffix]
        )
    try:
        with localcontext() as context:
            context.prec = 128
            result = Decimal(match[1]) * factor * (1000 if cpu else 1)
    except InvalidOperation:
        raise ValueError("Invalid resource quantity") from None
    if (
        result < 0
        or (result == 0 and not allow_zero)
        or result != result.to_integral_value()
        or result > 2**63 - 1
    ):
        raise ValueError("Quantity must be positive, exact and within supported bounds")
    return int(result)


def cpu_quantity(value: str) -> str:
    quantity(value, cpu=True)
    return value


def memory_quantity(value: str) -> str:
    quantity(value)
    return value


Text = Annotated[str, AfterValidator(nonempty)]
DNSName = Annotated[str, AfterValidator(dns_name)]
NamespaceName = Annotated[str, AfterValidator(namespace_name)]
LabelKey = Annotated[str, AfterValidator(label_key)]
LabelValue = Annotated[str, AfterValidator(label_value)]
Path = Annotated[str, AfterValidator(absolute_path)]
Reference = Annotated[str, AfterValidator(secret_reference)]
ClusterID = Annotated[str, AfterValidator(cluster_name)]
CPU = Annotated[str, AfterValidator(cpu_quantity)]
Memory = Annotated[str, AfterValidator(memory_quantity)]
ResourceKey = Annotated[str, AfterValidator(resource_key)]


class SessionPolicy(Model):
    max_age_seconds: int = Field(default=604800, gt=0)


class ClusterReference(Model):
    id: ClusterID
    config_ref: Reference


class Index(Model):
    schema_version: Literal[1]
    data_key_ref: Reference
    session: SessionPolicy = Field(default_factory=SessionPolicy)
    clusters: tuple[ClusterReference, ...]

    @model_validator(mode="after")
    def unique_clusters(self) -> Self:
        if len({c.id for c in self.clusters}) != len(self.clusters):
            raise ValueError("Cluster names must be unique")
        return self


class Transport(Model):
    mode: Literal["direct", "ssh"]
    ssh_target: Text | None = None

    @model_validator(mode="after")
    def target(self) -> Self:
        if (self.mode == "ssh") != (self.ssh_target is not None):
            raise ValueError("Only SSH mode requires ssh_target")
        if self.mode == "direct" and "ssh_target" in self.model_fields_set:
            raise ValueError("Direct transport accepts only mode")
        if self.ssh_target and (
            self.ssh_target.startswith("-") or any(c.isspace() for c in self.ssh_target)
        ):
            raise ValueError("Expected one SSH configuration target")
        return self


class Placement(Model):
    cpu_required_selector: dict[LabelKey, LabelValue]
    gpu_required_selector: dict[LabelKey, LabelValue]


class ResourceRatio(Model):
    cpu: CPU
    memory: Memory


class Resources(Model):
    requests_equal_limits: Literal[True]
    per_gpu: ResourceRatio | None = None


class GPUType(Model):
    value: Annotated[str, AfterValidator(nonempty), AfterValidator(label_value)]
    resource_name: ResourceKey
    selector: dict[LabelKey, LabelValue]


class GPUDefaults(Model):
    resource_name: ResourceKey
    type_label: LabelKey


class SupplementalMount(Model):
    name: DNSName
    mount_path: Path
    medium: Literal["disk", "memory"]
    size_limit: Memory
    read_only: bool = False

    @field_validator("name")
    @classmethod
    def volume_name(cls, value: str) -> str:
        if value == "data" or "." in value or len(value) > 63:
            raise ValueError("Expected an unreserved volume name")
        return value


class Storage(Model):
    kind: Literal["hostPath"]
    allowed_source_roots: tuple[Path, ...] = Field(min_length=1)
    require_same_mount_path: bool
    mount_read_only: bool
    supplemental_mounts: tuple[SupplementalMount, ...]

    @model_validator(mode="after")
    def unique_mounts(self) -> Self:
        if len({m.name for m in self.supplemental_mounts}) != len(self.supplemental_mounts) or len(
            {m.mount_path for m in self.supplemental_mounts}
        ) != len(self.supplemental_mounts):
            raise ValueError("Supplemental mount names and targets must be unique")
        return self


class Startup(Model):
    mode: Literal["image_default", "command_override"]
    command: tuple[Text, ...] | None = None
    args: tuple[str, ...] | None = None

    @model_validator(mode="after")
    def startup_contract(self) -> Self:
        if self.mode == "image_default" and ({"command", "args"} & self.model_fields_set):
            raise ValueError("Image-default startup accepts only mode")
        if self.mode == "command_override" and (not self.command or self.args is None):
            raise ValueError("Command override requires nonempty command and args arrays")
        return self


def image_parts(value: str) -> tuple[str, str]:
    """Parse an image reference into a canonical registry and repository."""
    nonempty(value)
    if (
        len(value) > 512
        or "://" in value
        or value.startswith(("/", "."))
        or any(c.isspace() for c in value)
    ):
        raise ValueError("Expected a container image reference")
    name, at, digest = value.partition("@")
    if at and not re.fullmatch(r"[a-z][a-z0-9_+.-]*:[a-fA-F0-9]{32,}", digest):
        raise ValueError("Invalid image digest")
    if at:
        algorithm, encoded = digest.split(":", 1)
        expected_length = {"sha256": 64, "sha384": 96, "sha512": 128}.get(algorithm)
        if expected_length is not None and len(encoded) != expected_length:
            raise ValueError("Invalid image digest length")
    parts = name.split("/")
    if ":" in parts[-1]:
        parts[-1], tag = parts[-1].rsplit(":", 1)
        if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}", tag):
            raise ValueError("Invalid image tag")
    registry = "docker.io"
    if len(parts) > 1 and ("." in parts[0] or ":" in parts[0] or parts[0] == "localhost"):
        registry = canonical_registry(parts.pop(0))
    if not parts or not all(
        re.fullmatch(r"[a-z0-9]+(?:(?:[._]|__|[-]+)[a-z0-9]+)*", p) for p in parts
    ):
        raise ValueError("Invalid image repository")
    if registry == "docker.io" and len(parts) == 1:
        parts.insert(0, "library")
    return registry, "/".join(parts)


def canonical_registry(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?(?::[0-9]{1,5})?", value):
        raise ValueError("Expected a registry host with optional port")
    host, separator, port = value.lower().partition(":")
    dns_name(host)
    if separator and not 1 <= int(port) <= 65535:
        raise ValueError("Invalid registry port")
    return "docker.io" if value.lower() == "index.docker.io" else value.lower()


def image_reference(value: str) -> str:
    image_parts(value)
    return value


Image = Annotated[str, AfterValidator(image_reference)]


class Runtime(Model):
    default: Startup
    exact_images: dict[Image, Startup]


class RegistryRule(Model):
    registry: Annotated[str, AfterValidator(canonical_registry)]
    repository_prefix: str | None = None
    pull_secret_names: tuple[DNSName, ...] = Field(min_length=1)

    @field_validator("repository_prefix")
    @classmethod
    def repository(cls, value: str | None) -> str | None:
        if value is not None and not all(
            re.fullmatch(r"[a-z0-9]+(?:(?:[._]|__|[-]+)[a-z0-9]+)*", p) for p in value.split("/")
        ):
            raise ValueError("Expected a repository path prefix")
        return value


class NamespaceRule(Model):
    placement: Placement
    resources: Resources
    gpu_types: tuple[GPUType, ...]
    gpu_defaults: GPUDefaults | None = None
    storage: Storage
    runtime: Runtime
    registry_rules: tuple[RegistryRule, ...]
    unmatched_registry: Literal["anonymous", "reject"]

    @model_validator(mode="after")
    def unique_gpu_types(self) -> Self:
        if len({gpu.value for gpu in self.gpu_types}) != len(self.gpu_types):
            raise ValueError("GPU type values must be unique")
        return self


class Kubernetes(Model):
    context: Text
    kubeconfig: SecretStr

    @property
    def namespace(self) -> str | None:
        """Return only the explicitly configured namespace of the selected context."""
        document = safe_document(self.kubeconfig.get_secret_value())
        for item in document["contexts"]:
            if item["name"] == self.context and item["context"].get("namespace"):
                try:
                    return namespace_name(item["context"]["namespace"])
                except (TypeError, ValueError):
                    raise LabError(
                        "Invalid namespace in the selected kubeconfig context.", 4
                    ) from None
        return None


class Connection(Model):
    server: str
    tls_name: str
    ca_data: str | None
    token: SecretStr


class ClusterNote(Model):
    kubernetes: Kubernetes
    transport: Transport
    _connection: Connection = PrivateAttr()

    @model_validator(mode="after")
    def validate_connection(self) -> Self:
        self._connection = kube_connection(self.kubernetes)
        return self

    @property
    def connection(self) -> Connection:
        return self._connection


class Cluster(ClusterNote):
    cluster_id: ClusterID


class SafeLoader(yaml.SafeLoader):
    def construct_mapping(self, node, deep=False):
        mapping = {}
        for key_node, value_node in node.value:
            if key_node.tag != "tag:yaml.org,2002:str":
                raise yaml.MarkedYAMLError(
                    problem="Mapping keys must be strings", problem_mark=key_node.start_mark
                )
            key = self.construct_object(key_node, deep=deep)
            if key in mapping:
                raise yaml.MarkedYAMLError(
                    problem="Duplicate mapping key", problem_mark=key_node.start_mark
                )
            mapping[key] = self.construct_object(value_node, deep=deep)
        return mapping


def safe_document(text: str) -> dict:
    """Parse at most 1 MiB of YAML/JSON with 32 levels and no aliases."""
    try:
        if len(text) > MAX_DOCUMENT_BYTES or len(text.encode("utf-8")) > MAX_DOCUMENT_BYTES:
            raise ValueError("Document exceeds 1 MiB")
        depth = 0
        for event in yaml.parse(text, Loader=SafeLoader):
            if isinstance(event, yaml.AliasEvent) or getattr(event, "anchor", None):
                raise yaml.MarkedYAMLError(
                    problem="YAML aliases and anchors are unsupported",
                    problem_mark=cast(yaml.error.Mark, event.start_mark),
                )
            if getattr(event, "tag", None) is not None:
                raise yaml.MarkedYAMLError(
                    problem="Explicit YAML tags are unsupported",
                    problem_mark=cast(yaml.error.Mark, event.start_mark),
                )
            if isinstance(event, (yaml.MappingStartEvent, yaml.SequenceStartEvent)):
                depth += 1
                if depth > 32:
                    raise yaml.MarkedYAMLError(
                        problem="Document exceeds 32 nesting levels",
                        problem_mark=cast(yaml.error.Mark, event.start_mark),
                    )
            elif isinstance(event, (yaml.MappingEndEvent, yaml.SequenceEndEvent)):
                depth -= 1
        data = yaml.load(text, Loader=SafeLoader)
        if not isinstance(data, dict):
            raise ValueError("Document must be a mapping")
        return data
    except (ValueError, yaml.YAMLError, UnicodeError, RecursionError) as exc:
        location = getattr(exc, "problem_mark", None)
        suffix = f" at line {location.line + 1}, column {location.column + 1}" if location else ""
        raise LabError("Invalid configuration document" + suffix, code=4) from None


def _tuples(value):
    if isinstance(value, list):
        return tuple(_tuples(item) for item in value)
    if isinstance(value, dict):
        return {key: _tuples(item) for key, item in value.items()}
    return value


def validate_model[T: Model](model: type[T], data: dict) -> T:
    """Validate an input mapping while keeping input values out of errors."""
    try:
        return model.model_validate(_tuples(data))
    except ValidationError as exc:
        error = exc.errors(include_url=False, include_context=False, include_input=False)[0]
        # Map keys can contain secret text; report only model-declared field names.
        fields = {name for cls in Model.__subclasses__() for name in cls.model_fields}
        path = ".".join(
            str(part) if isinstance(part, int) or part in fields else "<entry>"
            for part in error["loc"]
        )
        raise LabError(
            f"Invalid configuration at {path or '<root>'}: {error['type']}", code=4
        ) from None


def parse_index(text: str) -> Index:
    """Read a validated immutable root index from YAML or JSON."""
    return validate_model(Index, safe_document(text))


def parse_cluster_note(text: str) -> ClusterNote:
    """Read the connection-only Secure Note without internal metadata or policy."""
    return validate_model(ClusterNote, safe_document(text))


def parse_cluster(text: str, cluster_id: str) -> Cluster:
    """Associate connection settings with the stable identity in the root index."""
    data = safe_document(text)
    try:
        expected = cluster_name(cluster_id)
    except (ValueError, TypeError, AttributeError):
        raise LabError("Invalid cluster name", code=4) from None
    note = validate_model(ClusterNote, data)
    return Cluster(cluster_id=expected, kubernetes=note.kubernetes, transport=note.transport)


def _keys(data, required, optional=()):
    if (
        not isinstance(data, dict)
        or not set(required) <= data.keys()
        or data.keys() - set(required) - set(optional)
    ):
        raise LabError(
            "Unsupported kubeconfig fields; only static bearer-token authentication "
            "and inline CA or system trust are supported",
            code=4,
        )


def _named(items, body_key, required, optional=()):
    if not isinstance(items, list):
        raise LabError("Invalid kubeconfig named collection", code=4)
    result = {}
    for item in items:
        _keys(item, ("name", body_key))
        name = item["name"]
        if not isinstance(name, str) or not name or name in result:
            raise LabError("Missing or ambiguous kubeconfig name", code=4)
        _keys(item[body_key], required, optional)
        result[name] = item[body_key]
    return result


def kube_connection(config: Kubernetes) -> Connection:
    data = safe_document(config.kubeconfig.get_secret_value())
    _keys(
        data,
        ("apiVersion", "kind", "clusters", "users", "contexts"),
        ("current-context", "preferences"),
    )
    if data["apiVersion"] != "v1" or data["kind"] != "Config" or data.get("preferences", {}) != {}:
        raise LabError("Unsupported kubeconfig version or preferences", code=4)
    contexts = _named(data["contexts"], "context", ("cluster", "user"), ("namespace",))
    clusters = _named(
        data["clusters"], "cluster", ("server",), ("certificate-authority-data", "tls-server-name")
    )
    users = _named(data["users"], "user", ("token",))
    context = contexts.get(config.context)
    if not context or not all(isinstance(v, str) for v in context.values()):
        raise LabError("Selected kubeconfig context is missing or invalid", code=4)
    cluster = clusters.get(context["cluster"])
    user = users.get(context["user"])
    if cluster is None or user is None:
        raise LabError("Selected kubeconfig cluster or user is missing", code=4)
    try:
        server = nonempty(cluster["server"])
        endpoint = urlsplit(server)
        if (
            endpoint.scheme != "https"
            or not endpoint.hostname
            or endpoint.username is not None
            or endpoint.password is not None
            or endpoint.path not in {"", "/"}
            or endpoint.query
            or endpoint.fragment
            or endpoint.port == 0
        ):
            raise ValueError("Expected an HTTPS origin")
        tls_name = nonempty(cluster.get("tls-server-name", endpoint.hostname))
        for hostname in (endpoint.hostname, tls_name):
            try:
                ip_address(hostname)
            except ValueError:
                dns_name(hostname.lower())
        token = nonempty(user["token"])
        if any(c.isspace() for c in token):
            raise ValueError("Invalid bearer token")
        ca_data = cluster.get("certificate-authority-data")
        if "certificate-authority-data" in cluster and (
            not isinstance(ca_data, str) or not base64.b64decode(ca_data, validate=True)
        ):
            raise ValueError("Invalid embedded CA data")
    except (ValueError, TypeError, AttributeError, binascii.Error):
        raise LabError(
            "Invalid selected kubeconfig endpoint, trust or bearer token", code=4
        ) from None
    return Connection(server=server, tls_name=tls_name, ca_data=ca_data, token=SecretStr(token))
