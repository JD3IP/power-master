"""Tests for TOU tariff DSL configuration schema validation."""

from __future__ import annotations

from datetime import date

import pytest
from pydantic import ValidationError

from power_master.config.schema import (
    BandBase,
    BillingCycleConfig,
    CreditConfig,
    FeedInBand,
    FeedInTier,
    FreeWindowConfig,
    TariffPlanConfig,
    TariffProviderConfig,
    TariffVersion,
    VPPConfig,
)


class TestBandBase:
    """Tests for import band definitions."""

    def test_valid_band(self) -> None:
        band = BandBase(descriptor="peak", windows=["18:00-22:00"], rate_c_per_kwh=55.55)
        assert band.descriptor == "peak"
        assert band.windows == ["18:00-22:00"]
        assert band.rate_c_per_kwh == 55.55

    def test_band_with_no_windows_is_default(self) -> None:
        """Empty windows list = default/shoulder band."""
        band = BandBase(descriptor="shoulder", windows=[], rate_c_per_kwh=34.1)
        assert band.windows == []

    def test_window_format_validation_success(self) -> None:
        """Valid HH:MM-HH:MM formats."""
        band = BandBase(
            descriptor="test",
            windows=["00:00-23:59", "10:00-14:00", "22:00-07:00"],
            rate_c_per_kwh=10.0,
        )
        assert len(band.windows) == 3

    def test_midnight_crossing_window_accepted(self) -> None:
        """Midnight-crossing windows (start > end) are allowed."""
        band = BandBase(descriptor="off_peak", windows=["22:00-07:00"], rate_c_per_kwh=28.6)
        assert band.windows == ["22:00-07:00"]

    def test_invalid_window_format(self) -> None:
        """Malformed window format raises."""
        with pytest.raises(ValidationError) as exc_info:
            BandBase(descriptor="bad", windows=["10-14:00"], rate_c_per_kwh=10.0)
        assert "HH:MM-HH:MM" in str(exc_info.value)

    def test_invalid_hour_range(self) -> None:
        """Hour out of 0-23 range raises."""
        with pytest.raises(ValidationError) as exc_info:
            BandBase(descriptor="bad", windows=["25:00-26:00"], rate_c_per_kwh=10.0)
        assert "Invalid" in str(exc_info.value)

    def test_invalid_minute_range(self) -> None:
        """Minute out of 0-59 range raises."""
        with pytest.raises(ValidationError) as exc_info:
            BandBase(descriptor="bad", windows=["10:70-14:80"], rate_c_per_kwh=10.0)
        assert "Invalid" in str(exc_info.value)

    def test_non_string_window_raises(self) -> None:
        """Non-string window entry raises."""
        with pytest.raises(ValidationError) as exc_info:
            BandBase(descriptor="bad", windows=[12345], rate_c_per_kwh=10.0)  # type: ignore
        assert "string" in str(exc_info.value)


class TestFreeWindowConfig:
    """Tests for free/capped import windows."""

    def test_valid_free_window(self) -> None:
        fw = FreeWindowConfig(
            name="four4free",
            windows=["10:00-14:00"],
            rate_c_per_kwh=0.0,
            cap_kwh_per_day=50.0,
            applies_to_channel="general",
            over_cap_falls_back_to="offpeak_balance",
        )
        assert fw.name == "four4free"
        assert fw.cap_kwh_per_day == 50.0
        assert fw.rate_c_per_kwh == 0.0

    def test_free_window_defaults(self) -> None:
        """Channel defaults to 'general'."""
        fw = FreeWindowConfig(
            name="test",
            windows=["10:00-14:00"],
            rate_c_per_kwh=0.0,
            cap_kwh_per_day=50.0,
            over_cap_falls_back_to="other",
        )
        assert fw.applies_to_channel == "general"

    def test_free_window_controlled_load_channel(self) -> None:
        """Can specify controlled_load channel."""
        fw = FreeWindowConfig(
            name="ev_charge",
            windows=["09:00-16:00"],
            rate_c_per_kwh=0.0,
            cap_kwh_per_day=30.0,
            applies_to_channel="controlled_load",
            over_cap_falls_back_to="default",
        )
        assert fw.applies_to_channel == "controlled_load"


class TestFeedInBand:
    """Tests for export (feed-in) bands with tiered rates."""

    def test_flat_rate_feed_in(self) -> None:
        """Simple flat-rate export band."""
        band = FeedInBand(
            name="default_fit",
            windows=[],
            rate_c_per_kwh=8.0,
        )
        assert band.rate_c_per_kwh == 8.0
        assert band.tiers == []

    def test_tiered_feed_in(self) -> None:
        """Tiered export band with volume caps."""
        band = FeedInBand(
            name="evening_premium",
            windows=["18:00-21:00"],
            tiers=[
                FeedInTier(up_to_kwh_per_day=15.0, rate_c_per_kwh=10.0),
                FeedInTier(up_to_kwh_per_day=None, rate_c_per_kwh=2.0),
            ],
        )
        assert len(band.tiers) == 2
        assert band.tiers[0].up_to_kwh_per_day == 15.0
        assert band.tiers[1].up_to_kwh_per_day is None

    def test_tier_ordering_ascending(self) -> None:
        """Tiers must have ascending caps."""
        with pytest.raises(ValidationError) as exc_info:
            FeedInBand(
                name="bad",
                windows=[],
                tiers=[
                    FeedInTier(up_to_kwh_per_day=20.0, rate_c_per_kwh=10.0),
                    FeedInTier(up_to_kwh_per_day=15.0, rate_c_per_kwh=5.0),
                ],
            )
        assert "strictly ascending" in str(exc_info.value)

    def test_null_tier_must_be_last(self) -> None:
        """Open-ended (null) tier must come after all capped tiers."""
        with pytest.raises(ValidationError) as exc_info:
            FeedInBand(
                name="bad",
                windows=[],
                tiers=[
                    FeedInTier(up_to_kwh_per_day=None, rate_c_per_kwh=10.0),
                    FeedInTier(up_to_kwh_per_day=15.0, rate_c_per_kwh=2.0),
                ],
            )
        assert "last" in str(exc_info.value)

    def test_at_most_one_null_tier(self) -> None:
        """Only one open-ended tier allowed."""
        with pytest.raises(ValidationError) as exc_info:
            FeedInBand(
                name="bad",
                windows=[],
                tiers=[
                    FeedInTier(up_to_kwh_per_day=None, rate_c_per_kwh=10.0),
                    FeedInTier(up_to_kwh_per_day=None, rate_c_per_kwh=2.0),
                ],
            )
        assert "at most one tier" in str(exc_info.value)

    def test_cannot_have_both_tiers_and_flat_rate(self) -> None:
        """Either tiers or rate_c_per_kwh, not both."""
        with pytest.raises(ValidationError) as exc_info:
            FeedInBand(
                name="bad",
                windows=[],
                tiers=[FeedInTier(up_to_kwh_per_day=None, rate_c_per_kwh=10.0)],
                rate_c_per_kwh=5.0,
            )
        assert "cannot specify both" in str(exc_info.value)

    def test_must_have_either_tiers_or_flat_rate(self) -> None:
        """Must specify one of tiers or rate_c_per_kwh."""
        with pytest.raises(ValidationError) as exc_info:
            FeedInBand(name="bad", windows=[])
        assert "must specify either" in str(exc_info.value)


class TestCreditConfig:
    """Tests for conditional daily credits."""

    def test_valid_credit(self) -> None:
        credit = CreditConfig(
            name="zerohero_evening",
            type="low_import_window",
            windows=["18:00-21:00"],
            max_import_kwh_per_hour=0.03,
            reward_dollars_per_day=1.00,
            enforcement="soft",
            credit_priority_weight=0.5,
        )
        assert credit.name == "zerohero_evening"
        assert credit.enforcement == "soft"
        assert credit.credit_priority_weight == 0.5

    def test_credit_enforcement_defaults_to_soft(self) -> None:
        credit = CreditConfig(
            name="test",
            type="low_import_window",
            windows=["18:00-21:00"],
            max_import_kwh_per_hour=0.03,
            reward_dollars_per_day=1.00,
        )
        assert credit.enforcement == "soft"

    def test_credit_priority_weight_defaults_to_0_5(self) -> None:
        credit = CreditConfig(
            name="test",
            type="low_import_window",
            windows=["18:00-21:00"],
            max_import_kwh_per_hour=0.03,
            reward_dollars_per_day=1.00,
        )
        assert credit.credit_priority_weight == 0.5

    def test_credit_priority_weight_range(self) -> None:
        """Priority weight must be in [0, 1]."""
        with pytest.raises(ValidationError):
            CreditConfig(
                name="bad",
                type="low_import_window",
                windows=["18:00-21:00"],
                max_import_kwh_per_hour=0.03,
                reward_dollars_per_day=1.00,
                credit_priority_weight=1.5,
            )
        with pytest.raises(ValidationError):
            CreditConfig(
                name="bad",
                type="low_import_window",
                windows=["18:00-21:00"],
                max_import_kwh_per_hour=0.03,
                reward_dollars_per_day=1.00,
                credit_priority_weight=-0.1,
            )

    def test_enforcement_must_be_soft_or_hard(self) -> None:
        """Enforcement field validation."""
        with pytest.raises(ValidationError) as exc_info:
            CreditConfig(
                name="bad",
                type="low_import_window",
                windows=["18:00-21:00"],
                max_import_kwh_per_hour=0.03,
                reward_dollars_per_day=1.00,
                enforcement="invalid",
            )
        assert "soft" in str(exc_info.value) and "hard" in str(exc_info.value)


class TestTariffVersion:
    """Tests for versioned tariff definitions."""

    def test_valid_version(self) -> None:
        version = TariffVersion(
            valid_from=date(2026, 6, 1),
            valid_until=None,
            import_bands=[
                BandBase(descriptor="peak", windows=["18:00-22:00"], rate_c_per_kwh=55.55),
                BandBase(descriptor="shoulder", windows=[], rate_c_per_kwh=34.1),
            ],
        )
        assert version.valid_from == date(2026, 6, 1)
        assert version.valid_until is None

    def test_version_must_have_at_least_one_band(self) -> None:
        """Version requires at least one import band."""
        with pytest.raises(ValidationError) as exc_info:
            TariffVersion(
                valid_from=date(2026, 6, 1),
                valid_until=None,
                import_bands=[],
            )
        assert "at least one import_band" in str(exc_info.value)

    def test_version_must_have_default_band(self) -> None:
        """Version requires at least one band with no windows (default)."""
        with pytest.raises(ValidationError) as exc_info:
            TariffVersion(
                valid_from=date(2026, 6, 1),
                valid_until=None,
                import_bands=[
                    BandBase(descriptor="peak", windows=["18:00-22:00"], rate_c_per_kwh=55.55),
                    BandBase(descriptor="off_peak", windows=["10:00-14:00"], rate_c_per_kwh=0.0),
                ],
            )
        assert "default/shoulder band" in str(exc_info.value)

    def test_free_window_band_reference_integrity(self) -> None:
        """Free window over_cap_falls_back_to must reference existing band."""
        with pytest.raises(ValidationError) as exc_info:
            TariffVersion(
                valid_from=date(2026, 6, 1),
                valid_until=None,
                import_bands=[
                    BandBase(descriptor="shoulder", windows=[], rate_c_per_kwh=34.1),
                ],
                free_windows=[
                    FreeWindowConfig(
                        name="four4free",
                        windows=["10:00-14:00"],
                        rate_c_per_kwh=0.0,
                        cap_kwh_per_day=50.0,
                        over_cap_falls_back_to="nonexistent_band",
                    )
                ],
            )
        assert "does not match any import_band" in str(exc_info.value)

    def test_valid_version_with_free_windows(self) -> None:
        """Valid version with free window referencing existing band."""
        version = TariffVersion(
            valid_from=date(2026, 6, 1),
            valid_until=None,
            import_bands=[
                BandBase(descriptor="peak", windows=["18:00-22:00"], rate_c_per_kwh=55.55),
                BandBase(descriptor="off_peak_balance", windows=["10:00-14:00"], rate_c_per_kwh=28.6),
                BandBase(descriptor="shoulder", windows=[], rate_c_per_kwh=34.1),
            ],
            free_windows=[
                FreeWindowConfig(
                    name="four4free",
                    windows=["10:00-14:00"],
                    rate_c_per_kwh=0.0,
                    cap_kwh_per_day=50.0,
                    over_cap_falls_back_to="off_peak_balance",
                )
            ],
        )
        assert len(version.free_windows) == 1


class TestTariffPlanConfig:
    """Tests for complete TOU tariff plans."""

    def test_valid_plan(self) -> None:
        plan = TariffPlanConfig(
            versions=[
                TariffVersion(
                    valid_from=date(2026, 6, 1),
                    valid_until=None,
                    import_bands=[
                        BandBase(descriptor="peak", windows=["18:00-22:00"], rate_c_per_kwh=55.55),
                        BandBase(descriptor="shoulder", windows=[], rate_c_per_kwh=34.1),
                    ],
                )
            ],
            billing_cycle=BillingCycleConfig(length_days=28, anchor_date=date(2026, 6, 1)),
            supply_charge_c_per_day=148.5,
        )
        assert len(plan.versions) == 1

    def test_plan_must_have_at_least_one_version(self) -> None:
        """Plan requires at least one version."""
        with pytest.raises(ValidationError) as exc_info:
            TariffPlanConfig(
                versions=[],
                billing_cycle=BillingCycleConfig(length_days=28, anchor_date=date(2026, 6, 1)),
                supply_charge_c_per_day=148.5,
            )
        assert "at least one version" in str(exc_info.value)

    def test_plan_with_vpp_seam(self) -> None:
        """Plan can include VPP seam (stub)."""
        plan = TariffPlanConfig(
            versions=[
                TariffVersion(
                    valid_from=date(2026, 6, 1),
                    valid_until=None,
                    import_bands=[
                        BandBase(descriptor="shoulder", windows=[], rate_c_per_kwh=34.1),
                    ],
                )
            ],
            billing_cycle=BillingCycleConfig(length_days=28, anchor_date=date(2026, 6, 1)),
            vpp=VPPConfig(enabled=False),
            supply_charge_c_per_day=148.5,
        )
        assert plan.vpp.enabled is False


class TestTariffProviderConfig:
    """Tests for tariff provider configuration (amber vs tou)."""

    def test_amber_provider_legacy(self) -> None:
        """Amber provider (legacy) still works unchanged."""
        config = TariffProviderConfig(
            type="amber",
            api_key="test_key",
            site_id="test_site",
            update_interval_seconds=300,
            validity_seconds=300,
            max_requests_per_5min=50,
        )
        assert config.type == "amber"
        assert config.api_key == "test_key"

    def test_amber_provider_defaults(self) -> None:
        """Amber is the default type."""
        config = TariffProviderConfig()
        assert config.type == "amber"

    def test_grid_charge_policy_default_amber(self) -> None:
        """Grid charge policy defaults to allow_arbitrage for Amber (legacy behaviour)."""
        config = TariffProviderConfig(type="amber")
        assert config.grid_charge_policy == "allow_arbitrage"

    def test_grid_charge_policy_default_tou(self) -> None:
        """Grid charge policy defaults to free_window_and_solar_only for TOU (safe default)."""
        config = TariffProviderConfig(
            type="tou",
            timezone="Australia/Brisbane",
            plan=TariffPlanConfig(
                versions=[
                    TariffVersion(
                        valid_from=date(2026, 6, 1),
                        valid_until=None,
                        import_bands=[
                            BandBase(descriptor="shoulder", windows=[], rate_c_per_kwh=34.1),
                        ],
                    )
                ],
                billing_cycle=BillingCycleConfig(length_days=28, anchor_date=date(2026, 6, 1)),
                supply_charge_c_per_day=148.5,
            ),
        )
        assert config.grid_charge_policy == "free_window_and_solar_only"

    def test_grid_charge_policy_explicit_allow_arbitrage_amber(self) -> None:
        """Explicit allow_arbitrage on Amber is respected."""
        config = TariffProviderConfig(
            type="amber",
            grid_charge_policy="allow_arbitrage",
        )
        assert config.grid_charge_policy == "allow_arbitrage"

    def test_grid_charge_policy_explicit_free_window_tou(self) -> None:
        """Explicit free_window_and_solar_only on TOU is respected."""
        config = TariffProviderConfig(
            type="tou",
            timezone="Australia/Brisbane",
            plan=TariffPlanConfig(
                versions=[
                    TariffVersion(
                        valid_from=date(2026, 6, 1),
                        valid_until=None,
                        import_bands=[
                            BandBase(descriptor="shoulder", windows=[], rate_c_per_kwh=34.1),
                        ],
                    )
                ],
                billing_cycle=BillingCycleConfig(length_days=28, anchor_date=date(2026, 6, 1)),
                supply_charge_c_per_day=148.5,
            ),
            grid_charge_policy="free_window_and_solar_only",
        )
        assert config.grid_charge_policy == "free_window_and_solar_only"

    def test_grid_charge_policy_explicit_allow_arbitrage_tou(self) -> None:
        """Explicit allow_arbitrage on TOU is respected (override safe default)."""
        config = TariffProviderConfig(
            type="tou",
            timezone="Australia/Brisbane",
            plan=TariffPlanConfig(
                versions=[
                    TariffVersion(
                        valid_from=date(2026, 6, 1),
                        valid_until=None,
                        import_bands=[
                            BandBase(descriptor="shoulder", windows=[], rate_c_per_kwh=34.1),
                        ],
                    )
                ],
                billing_cycle=BillingCycleConfig(length_days=28, anchor_date=date(2026, 6, 1)),
                supply_charge_c_per_day=148.5,
            ),
            grid_charge_policy="allow_arbitrage",
        )
        assert config.grid_charge_policy == "allow_arbitrage"

    def test_grid_charge_policy_explicit_free_window_amber(self) -> None:
        """Explicit free_window_and_solar_only on Amber is respected."""
        config = TariffProviderConfig(
            type="amber",
            grid_charge_policy="free_window_and_solar_only",
        )
        assert config.grid_charge_policy == "free_window_and_solar_only"

    def test_invalid_grid_charge_policy(self) -> None:
        """Invalid grid charge policy raises."""
        with pytest.raises(ValidationError) as exc_info:
            TariffProviderConfig(
                type="amber",
                grid_charge_policy="invalid_policy",
            )
        assert "free_window_and_solar_only" in str(exc_info.value)

    def test_tou_provider_requires_timezone(self) -> None:
        """TOU provider requires timezone."""
        with pytest.raises(ValidationError) as exc_info:
            TariffProviderConfig(
                type="tou",
                plan=TariffPlanConfig(
                    versions=[
                        TariffVersion(
                            valid_from=date(2026, 6, 1),
                            valid_until=None,
                            import_bands=[
                                BandBase(descriptor="shoulder", windows=[], rate_c_per_kwh=34.1),
                            ],
                        )
                    ],
                    billing_cycle=BillingCycleConfig(length_days=28, anchor_date=date(2026, 6, 1)),
                    supply_charge_c_per_day=148.5,
                ),
            )
        assert "REQUIRED" in str(exc_info.value)

    def test_tou_provider_requires_plan(self) -> None:
        """TOU provider requires plan."""
        with pytest.raises(ValidationError) as exc_info:
            TariffProviderConfig(
                type="tou",
                timezone="Australia/Brisbane",
            )
        assert "plan is REQUIRED" in str(exc_info.value)

    def test_invalid_timezone(self) -> None:
        """Invalid IANA timezone raises."""
        with pytest.raises(ValidationError) as exc_info:
            TariffProviderConfig(
                type="tou",
                timezone="Invalid/Timezone",
                plan=TariffPlanConfig(
                    versions=[
                        TariffVersion(
                            valid_from=date(2026, 6, 1),
                            valid_until=None,
                            import_bands=[
                                BandBase(descriptor="shoulder", windows=[], rate_c_per_kwh=34.1),
                            ],
                        )
                    ],
                    billing_cycle=BillingCycleConfig(length_days=28, anchor_date=date(2026, 6, 1)),
                    supply_charge_c_per_day=148.5,
                ),
            )
        assert "Invalid IANA timezone" in str(exc_info.value)

    def test_valid_tou_provider_brisbane(self) -> None:
        """Valid TOU provider with Brisbane timezone."""
        config = TariffProviderConfig(
            type="tou",
            timezone="Australia/Brisbane",
            plan=TariffPlanConfig(
                versions=[
                    TariffVersion(
                        valid_from=date(2026, 6, 1),
                        valid_until=None,
                        import_bands=[
                            BandBase(descriptor="peak", windows=["18:00-22:00"], rate_c_per_kwh=55.55),
                            BandBase(descriptor="shoulder", windows=[], rate_c_per_kwh=34.1),
                        ],
                        free_windows=[
                            FreeWindowConfig(
                                name="four4free",
                                windows=["10:00-14:00"],
                                rate_c_per_kwh=0.0,
                                cap_kwh_per_day=50.0,
                                over_cap_falls_back_to="shoulder",
                            )
                        ],
                    )
                ],
                billing_cycle=BillingCycleConfig(length_days=28, anchor_date=date(2026, 6, 1)),
                supply_charge_c_per_day=148.5,
            ),
            grid_charge_policy="free_window_and_solar_only",
        )
        assert config.type == "tou"
        assert config.timezone == "Australia/Brisbane"
        assert config.plan is not None


class TestFourFourFreeIntegration:
    """Integration test: FOURFORFREE plan (Site A / EV)."""

    def test_fourforfree_plan_structure(self) -> None:
        """Valid FOURFORFREE plan as per §4 and §14."""
        plan = TariffPlanConfig(
            versions=[
                TariffVersion(
                    valid_from=date(2026, 6, 1),
                    valid_until=None,
                    import_bands=[
                        BandBase(
                            descriptor="peak",
                            windows=["16:00-22:59"],
                            rate_c_per_kwh=55.55,
                        ),
                        BandBase(
                            descriptor="offpeak_balance",
                            windows=["10:00-13:59"],
                            rate_c_per_kwh=28.6,
                        ),
                        BandBase(
                            descriptor="shoulder",
                            windows=["14:00-15:59", "23:00-23:59", "00:00-09:59"],
                            rate_c_per_kwh=34.1,
                        ),
                        BandBase(
                            descriptor="default",
                            windows=[],
                            rate_c_per_kwh=34.1,
                        ),
                    ],
                    free_windows=[
                        FreeWindowConfig(
                            name="four4free",
                            windows=["10:00-13:59"],
                            rate_c_per_kwh=0.0,
                            cap_kwh_per_day=50.0,
                            applies_to_channel="general",
                            over_cap_falls_back_to="offpeak_balance",
                        )
                    ],
                    feed_in_bands=[
                        FeedInBand(
                            name="evening_fit",
                            windows=["16:00-22:59"],
                            rate_c_per_kwh=8.0,
                        ),
                        FeedInBand(
                            name="daytime_zero",
                            windows=["00:00-15:59", "23:00-23:59"],
                            rate_c_per_kwh=0.0,
                        ),
                    ],
                )
            ],
            billing_cycle=BillingCycleConfig(length_days=28, anchor_date=date(2026, 6, 1)),
            supply_charge_c_per_day=148.5,
        )
        assert len(plan.versions) == 1
        version = plan.versions[0]
        assert len(version.import_bands) == 4
        assert len(version.free_windows) == 1
        assert len(version.feed_in_bands) == 2


class TestZEROHEROIntegration:
    """Integration test: ZEROHERO plan (Site B / export)."""

    def test_zerohero_plan_with_credits(self) -> None:
        """Valid ZEROHERO plan with credits (§4 and §14)."""
        plan = TariffPlanConfig(
            versions=[
                TariffVersion(
                    valid_from=date(2026, 6, 12),
                    valid_until=None,
                    import_bands=[
                        BandBase(
                            descriptor="peak",
                            windows=["16:00-22:59"],
                            rate_c_per_kwh=50.6,
                        ),
                        BandBase(
                            descriptor="offpeak_balance",
                            windows=["11:00-13:59"],
                            rate_c_per_kwh=27.5,
                        ),
                        BandBase(
                            descriptor="shoulder",
                            windows=["14:00-15:59", "23:00-23:59", "00:00-10:59"],
                            rate_c_per_kwh=39.6,
                        ),
                        BandBase(
                            descriptor="default",
                            windows=[],
                            rate_c_per_kwh=39.6,
                        ),
                    ],
                    free_windows=[
                        FreeWindowConfig(
                            name="zerocharge",
                            windows=["11:00-13:59"],
                            rate_c_per_kwh=0.0,
                            cap_kwh_per_day=50.0,
                            applies_to_channel="general",
                            over_cap_falls_back_to="offpeak_balance",
                        )
                    ],
                    feed_in_bands=[
                        FeedInBand(
                            name="evening_premium",
                            windows=["18:00-20:59"],
                            tiers=[
                                FeedInTier(up_to_kwh_per_day=15.0, rate_c_per_kwh=10.0),
                                FeedInTier(up_to_kwh_per_day=None, rate_c_per_kwh=2.0),
                            ],
                        ),
                        FeedInBand(
                            name="daytime_zero",
                            windows=["11:00-16:00"],
                            rate_c_per_kwh=0.0,
                        ),
                        FeedInBand(
                            name="default_fit",
                            windows=[],
                            rate_c_per_kwh=2.0,
                        ),
                    ],
                    credits=[
                        CreditConfig(
                            name="zerohero_evening",
                            type="low_import_window",
                            windows=["18:00-20:59"],
                            max_import_kwh_per_hour=0.03,
                            reward_dollars_per_day=1.0,
                            enforcement="soft",
                            credit_priority_weight=0.5,
                        )
                    ],
                )
            ],
            billing_cycle=BillingCycleConfig(length_days=28, anchor_date=date(2026, 6, 12)),
            supply_charge_c_per_day=198.0,
            vpp=VPPConfig(enabled=False),
        )
        assert len(plan.versions) == 1
        version = plan.versions[0]
        assert len(version.import_bands) == 4
        assert len(version.feed_in_bands) == 3
        assert len(version.credits) == 1
        assert version.credits[0].enforcement == "soft"


class TestTariffVersionBoundarySemantics:
    """Tests for per-version boundary validation (valid_until >= valid_from)."""

    def test_version_valid_until_before_valid_from(self) -> None:
        """Version with valid_until < valid_from raises."""
        with pytest.raises(ValidationError) as exc_info:
            TariffVersion(
                valid_from=date(2026, 6, 15),
                valid_until=date(2026, 6, 10),  # Before valid_from
                import_bands=[
                    BandBase(descriptor="default", windows=[], rate_c_per_kwh=30.0),
                ],
            )
        assert "valid_until" in str(exc_info.value) and "cannot be before" in str(exc_info.value)

    def test_version_valid_until_equal_valid_from(self) -> None:
        """Version with valid_until == valid_from is allowed (single-day version)."""
        version = TariffVersion(
            valid_from=date(2026, 6, 15),
            valid_until=date(2026, 6, 15),
            import_bands=[
                BandBase(descriptor="default", windows=[], rate_c_per_kwh=30.0),
            ],
        )
        assert version.valid_from == version.valid_until

    def test_version_valid_until_after_valid_from(self) -> None:
        """Version with valid_until > valid_from is allowed (normal multi-day version)."""
        version = TariffVersion(
            valid_from=date(2026, 6, 1),
            valid_until=date(2026, 6, 30),
            import_bands=[
                BandBase(descriptor="default", windows=[], rate_c_per_kwh=30.0),
            ],
        )
        assert version.valid_from < version.valid_until

    def test_version_valid_until_none_is_allowed(self) -> None:
        """Version with valid_until=None (open-ended) is allowed."""
        version = TariffVersion(
            valid_from=date(2026, 6, 1),
            valid_until=None,
            import_bands=[
                BandBase(descriptor="default", windows=[], rate_c_per_kwh=30.0),
            ],
        )
        assert version.valid_until is None


class TestTariffPlanVersionChain:
    """Tests for multi-version chain validation (overlaps, gaps, open-ended rules)."""

    def test_single_open_ended_version_is_valid(self) -> None:
        """A single version with valid_until=None is valid (common case)."""
        plan = TariffPlanConfig(
            versions=[
                TariffVersion(
                    valid_from=date(2026, 6, 1),
                    valid_until=None,
                    import_bands=[
                        BandBase(descriptor="default", windows=[], rate_c_per_kwh=30.0),
                    ],
                )
            ],
            billing_cycle=BillingCycleConfig(length_days=28, anchor_date=date(2026, 6, 1)),
            supply_charge_c_per_day=148.5,
        )
        assert len(plan.versions) == 1

    def test_two_version_chain_no_gap_no_overlap(self) -> None:
        """Two contiguous versions (v1 until 2026-06-30, v2 from 2026-07-01) is valid."""
        plan = TariffPlanConfig(
            versions=[
                TariffVersion(
                    valid_from=date(2026, 6, 1),
                    valid_until=date(2026, 6, 30),
                    import_bands=[
                        BandBase(
                            descriptor="v1",
                            windows=["10:00-14:00"],
                            rate_c_per_kwh=25.0,
                        ),
                        BandBase(
                            descriptor="default",
                            windows=[],
                            rate_c_per_kwh=50.0,
                        ),
                    ],
                ),
                TariffVersion(
                    valid_from=date(2026, 7, 1),
                    valid_until=None,
                    import_bands=[
                        BandBase(
                            descriptor="v2",
                            windows=["10:00-14:00"],
                            rate_c_per_kwh=30.0,
                        ),
                        BandBase(
                            descriptor="default",
                            windows=[],
                            rate_c_per_kwh=45.0,
                        ),
                    ],
                ),
            ],
            billing_cycle=BillingCycleConfig(length_days=28, anchor_date=date(2026, 6, 1)),
            supply_charge_c_per_day=148.5,
        )
        assert len(plan.versions) == 2

    def test_version_overlap_raises(self) -> None:
        """Two overlapping versions (both active on same date) raises."""
        with pytest.raises(ValidationError) as exc_info:
            TariffPlanConfig(
                versions=[
                    TariffVersion(
                        valid_from=date(2026, 6, 1),
                        valid_until=date(2026, 6, 15),  # Ends on June 15
                        import_bands=[
                            BandBase(
                                descriptor="v1",
                                windows=["10:00-14:00"],
                                rate_c_per_kwh=25.0,
                            ),
                            BandBase(descriptor="default", windows=[], rate_c_per_kwh=50.0),
                        ],
                    ),
                    TariffVersion(
                        valid_from=date(2026, 6, 10),  # Starts before v1 ends
                        valid_until=None,
                        import_bands=[
                            BandBase(
                                descriptor="v2",
                                windows=["10:00-14:00"],
                                rate_c_per_kwh=30.0,
                            ),
                            BandBase(descriptor="default", windows=[], rate_c_per_kwh=45.0),
                        ],
                    ),
                ],
                billing_cycle=BillingCycleConfig(length_days=28, anchor_date=date(2026, 6, 1)),
                supply_charge_c_per_day=148.5,
            )
        assert "overlap" in str(exc_info.value).lower()

    def test_version_gap_raises(self) -> None:
        """A gap between versions (v1 until 2026-06-29, v2 from 2026-07-01) raises."""
        with pytest.raises(ValidationError) as exc_info:
            TariffPlanConfig(
                versions=[
                    TariffVersion(
                        valid_from=date(2026, 6, 1),
                        valid_until=date(2026, 6, 29),  # Ends on June 29
                        import_bands=[
                            BandBase(
                                descriptor="v1",
                                windows=["10:00-14:00"],
                                rate_c_per_kwh=25.0,
                            ),
                            BandBase(descriptor="default", windows=[], rate_c_per_kwh=50.0),
                        ],
                    ),
                    TariffVersion(
                        valid_from=date(2026, 7, 1),  # Starts on July 1 (gap on June 30)
                        valid_until=None,
                        import_bands=[
                            BandBase(
                                descriptor="v2",
                                windows=["10:00-14:00"],
                                rate_c_per_kwh=30.0,
                            ),
                            BandBase(descriptor="default", windows=[], rate_c_per_kwh=45.0),
                        ],
                    ),
                ],
                billing_cycle=BillingCycleConfig(length_days=28, anchor_date=date(2026, 6, 1)),
                supply_charge_c_per_day=148.5,
            )
        assert "gap" in str(exc_info.value).lower()

    def test_open_ended_version_not_last_raises(self) -> None:
        """An open-ended version (valid_until=None) that isn't the last raises."""
        with pytest.raises(ValidationError) as exc_info:
            TariffPlanConfig(
                versions=[
                    TariffVersion(
                        valid_from=date(2026, 6, 1),
                        valid_until=None,  # Open-ended
                        import_bands=[
                            BandBase(descriptor="default", windows=[], rate_c_per_kwh=50.0),
                        ],
                    ),
                    TariffVersion(
                        valid_from=date(2026, 7, 1),  # Another version after open-ended
                        valid_until=None,
                        import_bands=[
                            BandBase(descriptor="default", windows=[], rate_c_per_kwh=45.0),
                        ],
                    ),
                ],
                billing_cycle=BillingCycleConfig(length_days=28, anchor_date=date(2026, 6, 1)),
                supply_charge_c_per_day=148.5,
            )
        assert "open-ended" in str(exc_info.value).lower() and "last" in str(exc_info.value).lower()

    def test_three_version_chain_sequential_valid(self) -> None:
        """Three sequential versions with no gaps or overlaps is valid."""
        plan = TariffPlanConfig(
            versions=[
                TariffVersion(
                    valid_from=date(2026, 6, 1),
                    valid_until=date(2026, 6, 30),
                    import_bands=[
                        BandBase(descriptor="default", windows=[], rate_c_per_kwh=50.0),
                    ],
                ),
                TariffVersion(
                    valid_from=date(2026, 7, 1),
                    valid_until=date(2026, 7, 31),
                    import_bands=[
                        BandBase(descriptor="default", windows=[], rate_c_per_kwh=45.0),
                    ],
                ),
                TariffVersion(
                    valid_from=date(2026, 8, 1),
                    valid_until=None,  # Open-ended
                    import_bands=[
                        BandBase(descriptor="default", windows=[], rate_c_per_kwh=40.0),
                    ],
                ),
            ],
            billing_cycle=BillingCycleConfig(length_days=28, anchor_date=date(2026, 6, 1)),
            supply_charge_c_per_day=148.5,
        )
        assert len(plan.versions) == 3
