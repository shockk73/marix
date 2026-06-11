import re
from urllib.parse import urlsplit

import db as db_module
from config import ATLAS_PROXY


_TARGET_RE = re.compile(r"__cr\.[a-z]{2}(?:,[a-z]{2})*(?:;asn\.\d+)?")
_COUNTRY_RE = re.compile(r"__cr\.([a-z]{2}(?:,[a-z]{2})*)")
_ASN_RE = re.compile(r";asn\.(\d+)")

_COUNTRY_ALLOWLIST = {
    "at", "ch", "sk", "ua", "cz", "pl", "lv", "lt", "ee", "de", "md",
    "fi", "se", "nl", "fr", "it", "es", "gb", "be", "ro",
}
_COUNTRY_BLOCKLIST = {"by", "ru"}


def normalize_country(country: str) -> str:
    value = country.strip().lower()
    if value in _COUNTRY_BLOCKLIST:
        raise ValueError("BY/RU proxy targets are disabled")
    if value not in _COUNTRY_ALLOWLIST:
        raise ValueError(f"Unsupported proxy country: {country}")
    return value


def normalize_asn(asn: str | None) -> str | None:
    if asn is None or asn == "":
        return None
    value = str(asn).strip()
    if not value.isdigit():
        raise ValueError("ASN must contain only digits")
    return value


def apply_proxy_target(base_proxy: str, country: str | None, asn: str | None = None) -> str:
    if not base_proxy or not country:
        return base_proxy
    country = normalize_country(country)
    asn = normalize_asn(asn)
    marker = f"__cr.{country}"
    if asn:
        marker += f";asn.{asn}"
    if not _TARGET_RE.search(base_proxy):
        raise ValueError("ATLAS_PROXY must contain DataImpulse target marker like __cr.at")
    return _TARGET_RE.sub(marker, base_proxy, count=1)


def _extract_target(proxy_url: str) -> dict:
    country_match = _COUNTRY_RE.search(proxy_url or "")
    asn_match = _ASN_RE.search(proxy_url or "")
    return {
        "country": country_match.group(1) if country_match else None,
        "asn": asn_match.group(1) if asn_match else None,
    }


def describe_proxy_url(proxy_url: str, source: str) -> dict:
    if not proxy_url:
        return {"configured": False, "source": source, "country": None, "asn": None, "host": None}
    parsed = urlsplit(proxy_url)
    target = _extract_target(proxy_url)
    return {
        "configured": True,
        "source": source,
        "country": target["country"],
        "asn": target["asn"],
        "host": parsed.hostname,
        "port": parsed.port,
    }


async def get_effective_atlas_proxy() -> str:
    try:
        target = await db_module.get_atlas_proxy_target()
    except Exception:
        target = {}
    return apply_proxy_target(
        ATLAS_PROXY,
        target.get("country"),
        target.get("asn"),
    )


async def get_atlas_proxy_status() -> dict:
    try:
        target = await db_module.get_atlas_proxy_target()
    except Exception as exc:
        status = describe_proxy_url(ATLAS_PROXY, "env")
        status["runtime_state_error"] = f"{type(exc).__name__}: {exc}"
        return status
    if target:
        try:
            proxy = apply_proxy_target(
                ATLAS_PROXY,
                target.get("country"),
                target.get("asn"),
            )
            status = describe_proxy_url(proxy, "runtime_override")
            status["runtime_override"] = target
            return status
        except ValueError as exc:
            return {
                "configured": bool(ATLAS_PROXY),
                "source": "runtime_override",
                "error": str(exc),
                "runtime_override": target,
            }
    return describe_proxy_url(ATLAS_PROXY, "env")


async def set_atlas_proxy_target(country: str, asn: str | None = None) -> dict:
    country = normalize_country(country)
    asn = normalize_asn(asn)
    apply_proxy_target(ATLAS_PROXY, country, asn)
    await db_module.set_atlas_proxy_target(country, asn)
    return await get_atlas_proxy_status()
