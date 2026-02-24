"""Australian Bureau of Meteorology storm alert provider.

Parses BOM XML precis data feed and specific warning products.
Sources:
  - Precis forecast: https://www.bom.gov.au/fwo/{state_code}.xml
  - Warning products: https://www.bom.gov.au/fwo/{product_id}.xml
    - IDQ21033: Severe Thunderstorm Warning
    - IDQ21035: Severe Weather Warning
    - IDQ21037: Tropical Cyclone Warning
    - IDQ21038: Flood Warning
"""

from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

import httpx

from power_master.config.schema import StormProviderConfig
from power_master.forecast.base import StormAlert, StormForecast, StormProvider

logger = logging.getLogger(__name__)

# BOM FTP is also accessible via HTTP
BOM_BASE_URL = "https://www.bom.gov.au/fwo"

# Keywords in precis text that indicate storm risk
STORM_KEYWORDS = {
    "thunderstorm": 0.7,
    "severe thunderstorm": 0.9,
    "storm": 0.6,
    "severe": 0.8,
    "damaging winds": 0.7,
    "destructive winds": 0.9,
    "hail": 0.6,
    "heavy rain": 0.5,
    "intense rain": 0.7,
    "flash flooding": 0.8,
}

# Severity mapping for warning product IDs
WARNING_PRODUCT_SEVERITY: dict[str, tuple[str, float]] = {
    "IDQ21033": ("severe", 0.9),    # Severe Thunderstorm Warning
    "IDQ21035": ("severe", 0.85),   # Severe Weather Warning
    "IDQ21037": ("severe", 0.95),   # Tropical Cyclone Warning
    "IDQ21038": ("moderate", 0.75), # Flood Warning
}


class BOMStormProvider(StormProvider):
    """BOM XML precis feed + warning product storm alert provider."""

    def __init__(self, config: StormProviderConfig) -> None:
        self._config = config
        self._client = httpx.AsyncClient(
            timeout=30.0,
            headers={"User-Agent": "PowerMaster/1.0 (solar-optimization)"},
        )
        self._cached_xml: str | None = None
        self._cached_at: datetime | None = None
        self._warning_cache: dict[str, tuple[str, datetime]] = {}

    async def fetch_alerts(self) -> StormForecast:
        """Fetch and parse BOM XML for storm alerts at the configured location."""
        alerts: list[StormAlert] = []
        target_aac = self._config.location_aac

        if not target_aac:
            logger.warning("No BOM location configured (location_aac is empty)")
            return StormForecast(
                fetched_at=datetime.now(timezone.utc), provider="bom"
            )

        # 1. Parse precis forecast for storm keywords
        alerts.extend(await self._parse_precis_alerts(target_aac))

        # 2. Fetch and parse specific warning products
        alerts.extend(await self._parse_warning_products(target_aac))

        logger.info(
            "BOM storm alerts: %d total for %s (max prob: %.0f%%)",
            len(alerts),
            target_aac,
            max((a.probability for a in alerts), default=0) * 100,
        )

        return StormForecast(
            alerts=alerts,
            fetched_at=datetime.now(timezone.utc),
            provider="bom",
        )

    async def _parse_precis_alerts(self, target_aac: str) -> list[StormAlert]:
        """Parse precis forecast XML for storm-related keywords."""
        alerts = []
        try:
            xml_data = await self._fetch_precis_xml()
            root = ET.fromstring(xml_data)
        except Exception as e:
            logger.warning("Failed to fetch precis XML: %s", e)
            return alerts

        for area in root.iter("area"):
            aac = area.get("aac", "")
            if aac != target_aac:
                continue

            description = area.get("description", "")

            for fc in area.iter("forecast-period"):
                precis_elem = fc.find(".//text[@type='precis']")
                if precis_elem is None or precis_elem.text is None:
                    continue

                precis = precis_elem.text.lower()
                probability = self._assess_storm_probability(precis)

                if probability > 0.0:
                    start_str = fc.get("start-time-utc", "")
                    end_str = fc.get("end-time-utc", "")
                    try:
                        valid_from = datetime.fromisoformat(
                            start_str.replace("Z", "+00:00")
                        )
                        valid_to = datetime.fromisoformat(
                            end_str.replace("Z", "+00:00")
                        )
                    except (ValueError, AttributeError):
                        valid_from = datetime.now(timezone.utc)
                        valid_to = datetime.now(timezone.utc)

                    severity = "severe" if probability >= 0.8 else (
                        "moderate" if probability >= 0.5 else "low"
                    )

                    alerts.append(
                        StormAlert(
                            location=description,
                            probability=probability,
                            description=precis_elem.text,
                            valid_from=valid_from,
                            valid_to=valid_to,
                            severity=severity,
                        )
                    )
        return alerts

    async def _parse_warning_products(self, target_aac: str) -> list[StormAlert]:
        """Fetch and parse BOM warning product XMLs for active alerts."""
        alerts = []
        product_ids = getattr(self._config, "warning_product_ids", [])

        for product_id in product_ids:
            try:
                xml_data = await self._fetch_warning_xml(product_id)
                if not xml_data:
                    continue

                root = ET.fromstring(xml_data)
                alerts.extend(
                    self._extract_warning_alerts(root, product_id, target_aac)
                )
            except ET.ParseError as e:
                logger.debug("Failed to parse warning XML %s: %s", product_id, e)
            except Exception as e:
                logger.debug("Warning product %s not available: %s", product_id, e)

        return alerts

    def _extract_warning_alerts(
        self, root: ET.Element, product_id: str, target_aac: str
    ) -> list[StormAlert]:
        """Extract alerts from a BOM warning product XML.

        BOM warning XMLs contain area elements and text content describing
        the warning. We check if our location's AAC is referenced.
        """
        alerts = []
        now = datetime.now(timezone.utc)
        severity_info = WARNING_PRODUCT_SEVERITY.get(
            product_id, ("moderate", 0.75)
        )

        # Check if any area in the warning matches or contains our location
        location_match = False
        area_desc = ""
        for area in root.iter("area"):
            aac = area.get("aac", "")
            desc = area.get("description", "")
            # Match exact AAC or parent region (e.g. QLD_PW001 matches QLD_)
            if aac == target_aac:
                location_match = True
                area_desc = desc
                break
            # Check if our AAC's parent region is in the warning areas
            aac_prefix = target_aac.split("_")[0] if "_" in target_aac else ""
            if aac_prefix and aac.startswith(aac_prefix) and area.get("type") == "metropolitan" or area.get("type") == "public_district":
                location_match = True
                area_desc = desc

        if not location_match:
            return alerts

        # Extract warning text content
        warning_texts = []
        for text_elem in root.iter("text"):
            text_type = text_elem.get("type", "")
            if text_type in ("warning_text", "body", "headline", "summary", "precis"):
                if text_elem.text:
                    warning_texts.append(text_elem.text.strip())

        if not warning_texts:
            # Try paragraphs
            for p in root.iter("p"):
                if p.text:
                    warning_texts.append(p.text.strip())

        combined_text = " ".join(warning_texts[:3])
        if not combined_text:
            combined_text = f"Active weather warning ({product_id})"

        # Assess probability from warning text, use at least the product severity
        keyword_prob = self._assess_storm_probability(combined_text.lower())
        probability = max(keyword_prob, severity_info[1])

        alerts.append(
            StormAlert(
                location=area_desc or f"Warning {product_id}",
                probability=probability,
                description=f"BOM {product_id}: {combined_text[:200]}",
                valid_from=now,
                valid_to=now + timedelta(hours=24),
                severity=severity_info[0],
            )
        )

        return alerts

    async def get_available_locations(self) -> list[dict[str, str]]:
        """Parse BOM XML and return all available area locations."""
        xml_data = await self._fetch_precis_xml()
        root = ET.fromstring(xml_data)

        locations = []
        seen = set()
        for area in root.iter("area"):
            aac = area.get("aac", "")
            desc = area.get("description", "")
            area_type = area.get("type", "")
            # Only include location-level areas (not state/region)
            if aac and desc and aac not in seen and area_type == "location":
                locations.append({"aac": aac, "description": desc})
                seen.add(aac)

        return sorted(locations, key=lambda x: x["description"])

    async def is_healthy(self) -> bool:
        try:
            resp = await self._client.head(
                f"{BOM_BASE_URL}/{self._config.state_code}.xml"
            )
            return resp.status_code == 200
        except Exception:
            return False

    async def close(self) -> None:
        await self._client.aclose()

    async def _fetch_precis_xml(self) -> str:
        """Fetch precis forecast XML, with simple caching (1 hour)."""
        now = datetime.now(timezone.utc)
        if (
            self._cached_xml
            and self._cached_at
            and (now - self._cached_at).total_seconds() < 3600
        ):
            return self._cached_xml

        url = f"{BOM_BASE_URL}/{self._config.state_code}.xml"
        resp = await self._client.get(url)
        resp.raise_for_status()
        self._cached_xml = resp.text
        self._cached_at = now
        return self._cached_xml

    async def _fetch_warning_xml(self, product_id: str) -> str | None:
        """Fetch a warning product XML. Returns None if not found (404).

        Warning products only exist on BOM's FTP when the warning is active.
        A 404 means no current warning for that product — this is normal.
        """
        now = datetime.now(timezone.utc)

        # Check cache (1 hour TTL)
        cached = self._warning_cache.get(product_id)
        if cached and (now - cached[1]).total_seconds() < 3600:
            return cached[0] or None

        url = f"{BOM_BASE_URL}/{product_id}.xml"
        try:
            resp = await self._client.get(url)
            if resp.status_code == 404:
                # No active warning — cache the absence
                self._warning_cache[product_id] = ("", now)
                return None
            resp.raise_for_status()
            self._warning_cache[product_id] = (resp.text, now)
            return resp.text
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                self._warning_cache[product_id] = ("", now)
                return None
            raise

    @staticmethod
    def _assess_storm_probability(precis_text: str) -> float:
        """Assess storm probability from precis text keywords.

        Also considers precipitation probability if mentioned.
        """
        text = precis_text.lower()
        max_prob = 0.0

        for keyword, prob in STORM_KEYWORDS.items():
            if keyword in text:
                max_prob = max(max_prob, prob)

        # Check for probability of precipitation percentage
        # e.g. "80% chance of rain"
        pct_match = re.search(r"(\d+)%\s*chance", text)
        if pct_match and max_prob > 0:
            pct = int(pct_match.group(1)) / 100.0
            max_prob = max_prob * pct  # Scale storm probability by rain probability

        return max_prob
