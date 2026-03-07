# Power Master — Notifications System

## What This Is

A configurable notifications system for Power Master that alerts users to important events (price spikes, inverter issues, battery thresholds, system health) across multiple channels (Telegram, Email, Pushover, ntfy.sh, Webhook). Users configure channels and event rules via the dashboard settings page, with per-event severity levels that map to channel-specific priority (e.g. push notification urgency).

## Core Value

Users get timely, actionable alerts about their energy system without having to watch the dashboard — and they control exactly what they hear about and how.

## Requirements

### Validated

- ✓ Dashboard settings page with config persistence — existing
- ✓ Pydantic config schema with hot-reload — existing
- ✓ SSE real-time telemetry pipeline — existing
- ✓ Resilience/health check system — existing
- ✓ Amber price spike detection — existing
- ✓ Battery SOC monitoring — existing
- ✓ Inverter connection status tracking — existing
- ✓ MQTT infrastructure — existing
- ✓ Async event-driven architecture — existing

### Active

- [ ] Multi-channel notification dispatch (Telegram, Email/SMTP, Pushover, ntfy.sh, Webhook)
- [ ] Event-driven notification triggers (price, inverter, battery, health, log errors)
- [ ] Per-event severity levels (info/warning/critical) mapping to channel priority
- [ ] Per-event cooldown to prevent notification spam
- [ ] Log error forwarding with configurable minimum level
- [ ] Dashboard settings UI for channel config and event rules
- [ ] Test notification button per channel

### Out of Scope

- SMS notifications — cost per message, Pushover/ntfy cover mobile
- Notification history/log UI — can check application logs
- Scheduled digest/summary notifications — real-time alerts only for v1
- Two-way interaction (e.g. reply to Telegram to trigger actions) — one-way alerts only

## Context

Power Master is a brownfield Python/FastAPI application running on Raspberry Pi that controls a Fox-ESS KH solar inverter. It already has:
- A control loop (5-min ticks) that evaluates telemetry and dispatches commands
- A telemetry poll loop (15s) that reads inverter state
- Price spike detection via Amber tariff provider
- Health checks for all providers with resilience levels
- A dashboard settings page with Pydantic-backed config persistence
- MQTT for load control and Home Assistant integration

The notification system should plug into existing event sources (telemetry, pricing, health checks, logging) without coupling tightly to them — an event bus or observer pattern fits naturally.

## Constraints

- **Runtime**: Must work on Raspberry Pi (ARM, limited resources) — keep dependencies light
- **Async**: All channel dispatchers must be async (httpx for HTTP-based, aiosmtplib for email)
- **Config**: Must use existing Pydantic config schema pattern and YAML persistence
- **UI**: Settings page uses Jinja2 + HTMX — no React/Vue, keep consistent with existing dashboard

## Key Decisions

| Decision | Rationale | Outcome |
|----------|-----------|---------|
| Event bus pattern for notification triggers | Decouples event sources from notification dispatch; existing code emits events, notification system subscribes | — Pending |
| 5 channels in v1 (Telegram, Email, Pushover, ntfy.sh, Webhook) | Covers major use cases; all HTTP-based except SMTP | — Pending |
| Per-event cooldown (not global rate limit) | More intuitive — "don't tell me about low battery more than once per hour" | — Pending |
| Severity maps to channel priority | ntfy.sh and Pushover support priority levels natively; others degrade gracefully (prefix in subject/message) | — Pending |
| Log error forwarding via configurable level | User picks WARNING/ERROR/CRITICAL minimum; custom logging handler routes to notification bus | — Pending |

---
*Last updated: 2026-03-08 after initialization*
