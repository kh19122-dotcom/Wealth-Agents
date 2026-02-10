from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .policy import read_yaml


DEFAULT_POLICY_PATH = "data/policy/policy.yml"


@dataclass(frozen=True)
class PolicyInstrument:
    bucket: str
    instrument_id: str
    isin: str
    name: str
    weight_within_bucket: float
    provider: str
    ticker: str


def load_policy_and_instruments(
    policy_path: str = DEFAULT_POLICY_PATH,
) -> tuple[dict[str, Any], dict[str, Any], list[PolicyInstrument]]:
    policy_doc = read_yaml(policy_path)
    policy = _read_policy_section(policy_doc, policy_path)
    instruments = _read_policy_instruments(policy, policy_path)
    return policy_doc, policy, instruments


def _read_policy_section(policy_doc: dict[str, Any], policy_path: str) -> dict[str, Any]:
    policy = policy_doc.get("policy")
    if not isinstance(policy, dict):
        raise ValueError(f"Missing root 'policy' object in {policy_path}.")
    return policy


def _read_policy_instruments(policy: dict[str, Any], policy_path: str) -> list[PolicyInstrument]:
    raw_mapping = policy.get("instruments")
    if not isinstance(raw_mapping, dict) or not raw_mapping:
        raise ValueError("policy.instruments must be a non-empty mapping.")

    parsed: list[PolicyInstrument] = []
    missing_data: list[dict[str, str]] = []
    for bucket, bucket_instruments in raw_mapping.items():
        bucket_name = str(bucket).strip()
        if not bucket_name:
            raise ValueError("policy.instruments contains an empty bucket name.")
        if not isinstance(bucket_instruments, list) or not bucket_instruments:
            raise ValueError(f"policy.instruments['{bucket_name}'] must be a non-empty list.")

        seen_ids: set[str] = set()
        for idx, item in enumerate(bucket_instruments):
            if not isinstance(item, dict):
                raise ValueError(
                    f"Instrument entry in bucket '{bucket_name}' at index {idx} must be a mapping."
                )

            instrument_id = str(item.get("id") or "").strip()
            isin = str(item.get("isin") or "").strip()
            name = str(item.get("name") or "").strip()
            if not instrument_id:
                raise ValueError(f"Instrument in bucket '{bucket_name}' at index {idx} is missing id.")
            if instrument_id in seen_ids:
                raise ValueError(f"Duplicate instrument id '{instrument_id}' in bucket '{bucket_name}'.")
            if not isin:
                raise ValueError(f"Instrument '{instrument_id}' in bucket '{bucket_name}' is missing isin.")
            if not name:
                raise ValueError(f"Instrument '{instrument_id}' in bucket '{bucket_name}' is missing name.")
            seen_ids.add(instrument_id)

            try:
                weight = float(item.get("weight_within_bucket"))
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"Instrument '{instrument_id}' in bucket '{bucket_name}' has invalid weight_within_bucket."
                ) from exc
            if weight <= 0:
                raise ValueError(
                    f"Instrument '{instrument_id}' in bucket '{bucket_name}' must have positive weight_within_bucket."
                )

            data = item.get("data")
            provider = ""
            ticker = ""
            if isinstance(data, dict):
                provider = str(data.get("provider") or "").strip()
                ticker = str(data.get("ticker") or "").strip()

            missing_fields: list[str] = []
            if not provider:
                missing_fields.append("data.provider")
            if not ticker:
                missing_fields.append("data.ticker")
            if missing_fields:
                missing_data.append(
                    {
                        "bucket": bucket_name,
                        "instrument_id": instrument_id,
                        "fields": ", ".join(missing_fields),
                    }
                )
                continue

            parsed.append(
                PolicyInstrument(
                    bucket=bucket_name,
                    instrument_id=instrument_id,
                    isin=isin,
                    name=name,
                    weight_within_bucket=weight,
                    provider=provider.lower(),
                    ticker=ticker,
                )
            )

    if missing_data:
        lines = [
            "Missing market data fields for policy instruments:",
        ]
        for row in missing_data:
            lines.append(
                f"- bucket='{row['bucket']}', instrument_id='{row['instrument_id']}': missing {row['fields']}"
            )
        lines.append(
            f"Action: add `data.provider` and `data.ticker` for each listed instrument in {policy_path}."
        )
        raise ValueError("\n".join(lines))

    return parsed
