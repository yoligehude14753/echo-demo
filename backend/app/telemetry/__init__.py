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
    TelemetryDeleteRequest,
    TelemetryEvent,
    TelemetryIdentityInput,
    TelemetryObservation,
    TelemetryOperation,
    TelemetryPlatform,
    TelemetryProvider,
    TelemetryQuery,
    TelemetryRuntimeConfig,
)
from app.telemetry.ports import TelemetryPort
from app.telemetry.pseudonym import HmacPseudonymizer

__all__ = [
    "DeletionReason",
    "DeletionReceipt",
    "FailureReason",
    "FailureReasonCount",
    "HmacPseudonymizer",
    "InMemoryAggregateTelemetry",
    "NoopTelemetryAdapter",
    "ProviderRegistry",
    "PseudonymousIdentity",
    "TelemetryAggregate",
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
    "build_test_telemetry_adapter",
]


ProviderRegistry = frozenset(TelemetryProvider)
