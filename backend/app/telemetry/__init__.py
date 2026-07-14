"""Independent privacy-preserving telemetry contracts and test adapters."""

from app.telemetry.adapters import (
    InMemoryAggregateTelemetry,
    NoopTelemetryAdapter,
    build_test_telemetry_adapter,
)
from app.telemetry.contracts import (
    DeletionReason,
    DeletionReceipt,
    FailureReason,
    FailureReasonCount,
    PseudonymousIdentity,
    TelemetryAggregate,
    TelemetryContractValidationError,
    TelemetryDeleteRequest,
    TelemetryEvent,
    TelemetryIdentityInput,
    TelemetryObservation,
    TelemetryOperation,
    TelemetryPlatform,
    TelemetryProvider,
    TelemetryQuery,
    TelemetryRuntimeConfig,
    TelemetryValidationCode,
    TelemetryValidationIssue,
    TelemetryValidationLocation,
    parse_telemetry_delete_request,
    parse_telemetry_identity_input,
    parse_telemetry_observation,
    parse_telemetry_query,
    safe_validation_issues,
)
from app.telemetry.ports import TelemetryPort
from app.telemetry.pseudonym import HmacPseudonymizer
from app.telemetry.sqlite import TELEMETRY_SCHEMA_VERSION, SQLiteTelemetryAdapter

__all__ = [
    "TELEMETRY_SCHEMA_VERSION",
    "DeletionReason",
    "DeletionReceipt",
    "FailureReason",
    "FailureReasonCount",
    "HmacPseudonymizer",
    "InMemoryAggregateTelemetry",
    "NoopTelemetryAdapter",
    "ProviderRegistry",
    "PseudonymousIdentity",
    "SQLiteTelemetryAdapter",
    "TelemetryAggregate",
    "TelemetryContractValidationError",
    "TelemetryDeleteRequest",
    "TelemetryEvent",
    "TelemetryIdentityInput",
    "TelemetryObservation",
    "TelemetryOperation",
    "TelemetryPlatform",
    "TelemetryPort",
    "TelemetryProvider",
    "TelemetryQuery",
    "TelemetryRuntimeConfig",
    "TelemetryValidationCode",
    "TelemetryValidationIssue",
    "TelemetryValidationLocation",
    "build_test_telemetry_adapter",
    "parse_telemetry_delete_request",
    "parse_telemetry_identity_input",
    "parse_telemetry_observation",
    "parse_telemetry_query",
    "safe_validation_issues",
]


ProviderRegistry = frozenset(TelemetryProvider)
