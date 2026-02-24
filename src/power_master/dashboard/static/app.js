/* Power Master - Dashboard JavaScript */

// ── Sidebar Toggle ──────────────────────────────────
function toggleSidebar() {
    var sidebar = document.getElementById('sidebar');
    if (!sidebar) return;

    // Mobile: slide-in drawer. Desktop: collapsed rail.
    if (window.matchMedia('(max-width: 768px)').matches) {
        sidebar.classList.toggle('open');
        return;
    }

    sidebar.classList.toggle('collapsed');
    try {
        localStorage.setItem('sidebar_collapsed', sidebar.classList.contains('collapsed') ? '1' : '0');
    } catch (e) {}
}

// Close sidebar on mobile when clicking outside
document.addEventListener('click', function(e) {
    var sidebar = document.getElementById('sidebar');
    var toggle = document.querySelector('.menu-toggle');
    if (sidebar && sidebar.classList.contains('open') &&
        !sidebar.contains(e.target) && (!toggle || !toggle.contains(e.target))) {
        sidebar.classList.remove('open');
    }
});

document.addEventListener('DOMContentLoaded', function() {
    var sidebar = document.getElementById('sidebar');
    if (!sidebar) return;
    try {
        if (!window.matchMedia('(max-width: 768px)').matches && localStorage.getItem('sidebar_collapsed') === '1') {
            sidebar.classList.add('collapsed');
        }
    } catch (e) {}
});

// ── Mode Selector ───────────────────────────────────
document.addEventListener('DOMContentLoaded', function() {
    var modeButtons = document.querySelectorAll('.mode-btn');
    modeButtons.forEach(function(btn) {
        btn.addEventListener('click', function() {
            var mode = parseInt(this.dataset.mode);
            modeButtons.forEach(function(b) { b.classList.remove('active', 'user-active', 'optimiser-active'); });
            this.classList.add('user-active');

            var payload = { mode: mode, timeout_s: 3600 };
            // Include power_w for force charge/discharge if user specified one
            if (mode === 3 || mode === 4) {
                var powerInput = document.getElementById('mode-power');
                if (powerInput && parseInt(powerInput.value) > 0) {
                    payload.power_w = parseInt(powerInput.value);
                }
            }

            fetch('/api/mode', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload)
            }).then(function(resp) { return resp.json(); })
            .then(function(data) {
                if (data.status === 'ok') {
                    // Refetch full mode state for proper highlighting
                    fetch('/api/mode').then(function(r) { return r.json(); })
                    .then(function(modeData) { updateModeDisplay(modeData); })
                    .catch(function() {});
                }
            })
            .catch(function(err) { console.error('Mode change failed:', err); });

            // Show/hide power input
            var powerGroup = document.getElementById('mode-power-group');
            if (powerGroup) {
                powerGroup.style.display = (mode === 3 || mode === 4) ? 'block' : 'none';
            }
        });
    });

    // Fetch initial mode state
    fetch('/api/mode').then(function(r) { return r.json(); })
    .then(function(data) { updateModeDisplay(data); })
    .catch(function() {});
});

function updateModeDisplay(data) {
    var modeButtons = document.querySelectorAll('.mode-btn');
    var userMode = data.user_mode;
    var optimiserMode = data.optimiser_mode;
    var autoActive = data.auto_active;

    // Apply button highlighting: blue=user, green=optimiser
    modeButtons.forEach(function(b) {
        var btnMode = parseInt(b.dataset.mode);
        b.classList.remove('active', 'user-active', 'optimiser-active');

        if (userMode !== null && userMode !== undefined && btnMode === userMode) {
            b.classList.add('user-active');
        } else if (autoActive && btnMode === 0) {
            // Optimiser is in control — highlight AUTO button in blue
            b.classList.add('user-active');
        }

        if (optimiserMode !== null && optimiserMode !== undefined && btnMode === optimiserMode) {
            b.classList.add('optimiser-active');
        }
    });

    // Update override indicator
    var indicator = document.getElementById('mode-source');
    if (indicator) {
        var overrideActive = data.override_active;
        var source = data.source || 'default';
        var remaining = data.override_remaining_s || 0;

        if (overrideActive && remaining > 0) {
            var mins = Math.ceil(remaining / 60);
            indicator.textContent = 'Manual Override: ' + mins + 'm remaining';
            indicator.className = 'mode-source override';
        } else if (source === 'plan') {
            var optimiserName = data.optimiser_mode_name || data.mode_name || 'SELF_USE';
            // Format the mode name nicely
            optimiserName = optimiserName.replace(/_/g, ' ').replace(/\b\w/g, function(c) { return c.toUpperCase(); });
            indicator.textContent = 'Optimiser: ' + optimiserName;
            indicator.className = 'mode-source plan';
        } else {
            indicator.textContent = 'Default: Self-Use';
            indicator.className = 'mode-source default';
        }
    }

    // Update sidebar mode badge
    var badge = document.getElementById('system-mode');
    if (badge) {
        var modeName = data.mode_name || 'SELF_USE';
        modeName = modeName.replace(/_/g, ' ').replace(/\b\w/g, function(c) { return c.toUpperCase(); });
        badge.textContent = 'Mode: ' + modeName;
    }

    // Update status bar — Optimiser and Override
    var statusOpt = document.getElementById('status-optimiser');
    if (statusOpt) {
        if (data.optimiser_mode_name) {
            var optName = data.optimiser_mode_name.replace(/_/g, ' ').replace(/\b\w/g, function(c) { return c.toUpperCase(); });
            statusOpt.textContent = optName;
            statusOpt.style.color = 'var(--accent-green)';
        } else {
            statusOpt.textContent = 'No Plan';
            statusOpt.style.color = '';
        }
    }
    var statusOvr = document.getElementById('status-override');
    if (statusOvr) {
        if (data.override_active) {
            var ovrName = (data.user_mode_name || 'Manual').replace(/_/g, ' ').replace(/\b\w/g, function(c) { return c.toUpperCase(); });
            var ovrRemain = data.override_remaining_s || 0;
            if (ovrRemain > 0) {
                ovrName += ' (' + Math.ceil(ovrRemain / 60) + 'm)';
            }
            statusOvr.textContent = ovrName;
            statusOvr.style.color = 'var(--accent-orange)';
        } else {
            statusOvr.textContent = 'None';
            statusOvr.style.color = '';
        }
    }
}

// ── 24h Rolling Chart ───────────────────────────────
var rollingChart = null;
var MAX_REASONABLE_KW = 50;
var ROLLING_WINDOW_HOURS = (function() {
    var el = document.querySelector('[data-rolling-window-hours]');
    var v = el ? Number(el.dataset.rollingWindowHours) : NaN;
    if (!Number.isFinite(v) || v < 1) return 12;
    return Math.round(v);
})();
var ROLLING_STEP_MINUTES = 30;
var ROLLING_REFRESH_MS = 60000;
var rollingRefreshTimer = null;
var rollingTimeline = null;
var rollingNowIndex = -1;

// Fixed configurable max power for rolling chart axis (default 20kW).
var ROLLING_POWER_MAX_KW = (function() {
    var el = document.querySelector('[data-rolling-power-max-kw]');
    var v = el ? Number(el.dataset.rollingPowerMaxKw) : NaN;
    return Number.isFinite(v) && v > 0 ? v : 20;
})();
var PRICE_SPIKE_THRESHOLD_CENTS = (function() {
    var el = document.querySelector('[data-price-spike-threshold-cents]');
    var v = el ? Number(el.dataset.priceSpikeThresholdCents) : NaN;
    return Number.isFinite(v) && v > 0 ? v : 100;
})();
var BUY_BAND_LOW = (function() {
    var el = document.querySelector('[data-buy-band-low]');
    var raw = el ? el.dataset.buyBandLow : '';
    var v = (raw === '' || raw === undefined || raw === null) ? NaN : Number(raw);
    return Number.isFinite(v) ? v : null;
})();
var BUY_BAND_HIGH = (function() {
    var el = document.querySelector('[data-buy-band-high]');
    var raw = el ? el.dataset.buyBandHigh : '';
    var v = (raw === '' || raw === undefined || raw === null) ? NaN : Number(raw);
    return Number.isFinite(v) ? v : null;
})();
var SELL_BAND_LOW = (function() {
    var el = document.querySelector('[data-sell-band-low]');
    var raw = el ? el.dataset.sellBandLow : '';
    var v = (raw === '' || raw === undefined || raw === null) ? NaN : Number(raw);
    return Number.isFinite(v) ? v : null;
})();
var SELL_BAND_HIGH = (function() {
    var el = document.querySelector('[data-sell-band-high]');
    var raw = el ? el.dataset.sellBandHigh : '';
    var v = (raw === '' || raw === undefined || raw === null) ? NaN : Number(raw);
    return Number.isFinite(v) ? v : null;
})();

function toKw(value) {
    var n = Number(value);
    if (!Number.isFinite(n)) return null;
    var kw = n / 1000;
    if (Math.abs(kw) > MAX_REASONABLE_KW) return null;
    return kw;
}

function initRollingChart() {
    var canvas = document.getElementById('rolling-chart');
    if (!canvas || typeof Chart === 'undefined') return;

    var ctx = canvas.getContext('2d');
    rollingChart = new Chart(ctx, {
        type: 'line',
        plugins: [rollingNowMarkerPlugin],
        data: {
            labels: [],
            datasets: [
                {
                    label: 'SOC %',
                    data: [],
                    borderColor: '#3fb950',
                    backgroundColor: 'rgba(63, 185, 80, 0.1)',
                    fill: true,
                    yAxisID: 'y-soc',
                    tension: 0.3,
                    pointRadius: 0,
                },
                {
                    label: 'Solar (kW)',
                    data: [],
                    borderColor: '#d29922',
                    borderWidth: 1.5,
                    yAxisID: 'y-power',
                    tension: 0.3,
                    pointRadius: 0,
                },
                {
                    label: 'Load (kW)',
                    data: [],
                    borderColor: '#58a6ff',
                    borderWidth: 1.5,
                    yAxisID: 'y-power',
                    tension: 0.3,
                    pointRadius: 0,
                },
                {
                    label: 'Grid (kW)',
                    data: [],
                    borderColor: '#f85149',
                    borderWidth: 1.5,
                    yAxisID: 'y-power',
                    tension: 0.3,
                    pointRadius: 0,
                },
                {
                    label: 'Price (c/kWh)',
                    data: [],
                    borderColor: 'rgba(255, 255, 255, 0.3)',
                    borderWidth: 1,
                    borderDash: [4, 4],
                    yAxisID: 'y-price',
                    tension: 0.1,
                    pointRadius: 0,
                },
                {
                    label: 'Plan SOC %',
                    data: [],
                    borderColor: '#3fb950',
                    backgroundColor: 'rgba(63, 185, 80, 0.06)',
                    borderWidth: 1.5,
                    borderDash: [6, 3],
                    yAxisID: 'y-soc',
                    tension: 0.3,
                    pointRadius: 0,
                    fill: true,
                },
                {
                    label: 'Plan Self-Use',
                    data: [],
                    borderWidth: 6,
                    yAxisID: 'y-power',
                    tension: 0,
                    pointRadius: 0,
                    spanGaps: true,
                    clip: false,
                    borderColor: modeColor(1),
                    planModeValue: 1,
                },
                {
                    label: 'Plan Zero Export',
                    data: [],
                    borderWidth: 6,
                    yAxisID: 'y-power',
                    tension: 0,
                    pointRadius: 0,
                    spanGaps: true,
                    clip: false,
                    borderColor: modeColor(2),
                    planModeValue: 2,
                },
                {
                    label: 'Plan Force Charge',
                    data: [],
                    borderWidth: 6,
                    yAxisID: 'y-power',
                    tension: 0,
                    pointRadius: 0,
                    spanGaps: true,
                    clip: false,
                    borderColor: modeColor(3),
                    planModeValue: 3,
                },
                {
                    label: 'Plan Force Discharge',
                    data: [],
                    borderWidth: 6,
                    yAxisID: 'y-power',
                    tension: 0,
                    pointRadius: 0,
                    spanGaps: true,
                    clip: false,
                    borderColor: modeColor(4),
                    planModeValue: 4,
                },
                {
                    label: 'Plan Charge No Import',
                    data: [],
                    borderWidth: 6,
                    yAxisID: 'y-power',
                    tension: 0,
                    pointRadius: 0,
                    spanGaps: true,
                    clip: false,
                    borderColor: modeColor(5),
                    planModeValue: 5,
                },
                {
                    label: 'Plan Mode',
                    data: [],
                    yAxisID: 'y-power',
                    borderWidth: 0,
                    pointRadius: 0,
                    hidden: true,
                },
            ]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            interaction: {
                mode: 'index',
                intersect: false,
            },
            plugins: {
                legend: {
                    position: 'top',
                    labels: {
                        filter: function(item, chartData) {
                            var ds = chartData.datasets[item.datasetIndex];
                            return !(ds && ds.planModeValue);
                        },
                        color: '#8b949e',
                        font: { size: 11 },
                        boxWidth: 12,
                        padding: 12,
                    }
                },
                tooltip: {
                    filter: function(context) {
                        if (!context || !context.dataset) return true;
                        var ds = context.dataset;
                        if (!ds.planModeValue) return true;
                        var idx = context.dataIndex;
                        var modes = Array.isArray(ds.modeData) ? ds.modeData : [];
                        var actual = (idx >= 0 && idx < modes.length) ? Number(modes[idx]) : NaN;
                        return Number.isFinite(actual) ? actual === ds.planModeValue : true;
                    },
                    callbacks: {
                        label: function(context) {
                            if (!context || !context.dataset) return '';
                            var ds = context.dataset;
                            if (!ds.planModeValue) return undefined;
                            var idx = context.dataIndex;
                            var out = ['Plan: ' + modeName(ds.planModeValue)];
                            var loads = Array.isArray(ds.scheduledLoadsData) ? ds.scheduledLoadsData : [];
                            var scheduled = (idx >= 0 && idx < loads.length && Array.isArray(loads[idx])) ? loads[idx] : [];
                            if (scheduled.length) out.push('Loads: ' + scheduled.join(', '));
                            return out;
                        }
                    }
                },
            },
            scales: {
                x: {
                    grid: { color: 'rgba(48, 54, 61, 0.5)' },
                    ticks: { color: '#6e7681', font: { size: 10 }, maxTicksLimit: 12 }
                },
                'y-soc': {
                    position: 'left',
                    min: 0, max: 100,
                    grid: { color: 'rgba(48, 54, 61, 0.3)' },
                    ticks: { color: '#3fb950', font: { size: 10 } },
                    title: { display: true, text: 'SOC %', color: '#3fb950', font: { size: 10 } }
                },
                'y-power': {
                    position: 'right',
                    min: -ROLLING_POWER_MAX_KW,
                    max: ROLLING_POWER_MAX_KW,
                    grid: { display: false },
                    ticks: { color: '#8b949e', font: { size: 10 } },
                    title: { display: true, text: 'kW', color: '#8b949e', font: { size: 10 } }
                },
                'y-price': {
                    position: 'right',
                    grid: { display: false },
                    display: false,
                }
            }
        }
    });

    loadRollingChartData();
    if (rollingRefreshTimer) clearInterval(rollingRefreshTimer);
    rollingRefreshTimer = setInterval(loadRollingChartData, ROLLING_REFRESH_MS);
}

function buildRollingTimeline() {
    var stepMs = ROLLING_STEP_MINUTES * 60 * 1000;
    var anchor = new Date();
    anchor.setSeconds(0, 0);
    anchor.setMinutes(Math.floor(anchor.getMinutes() / ROLLING_STEP_MINUTES) * ROLLING_STEP_MINUTES);
    var anchorMs = anchor.getTime();
    var startMs = anchorMs - ROLLING_WINDOW_HOURS * 3600 * 1000;
    var endMs = anchorMs + ROLLING_WINDOW_HOURS * 3600 * 1000;
    var points = [];
    for (var t = startMs; t <= endMs; t += stepMs) {
        points.push(t);
    }
    return {
        startMs: startMs,
        endMs: endMs,
        anchorMs: anchorMs,
        stepMs: stepMs,
        points: points,
        labels: points.map(function(ts) { return formatTimeLabel(new Date(ts).toISOString()); }),
    };
}

function timelineIndex(timeline, iso) {
    if (!iso) return -1;
    var ts = new Date(iso).getTime();
    if (!Number.isFinite(ts)) return -1;
    if (ts < timeline.startMs || ts > timeline.endMs) return -1;
    var idx = Math.round((ts - timeline.startMs) / timeline.stepMs);
    return (idx >= 0 && idx < timeline.points.length) ? idx : -1;
}

function modeColor(mode) {
    if (mode === 3) return '#3fb950';
    if (mode === 4) return '#f85149';
    if (mode === 2) return '#d29922';
    if (mode === 5) return '#58a6ff';
    return 'rgba(139, 148, 158, 0.9)';
}

function modeName(mode) {
    if (mode === 3) return 'Force Charge';
    if (mode === 4) return 'Force Discharge';
    if (mode === 2) return 'Zero Export';
    if (mode === 5) return 'Charge No Import';
    if (mode === 1) return 'Self-Use';
    return 'Unknown';
}

function parseModeValue(value) {
    if (value === null || value === undefined) return null;
    var n = Number(value);
    if (Number.isFinite(n)) return n;

    var s = String(value).toUpperCase();
    if (s.indexOf('DISCHARGE') >= 0) return 4;
    if (s.indexOf('CHARGE NO IMPORT') >= 0 || s.indexOf('NO IMPORT') >= 0) return 5;
    if (s.indexOf('CHARGE') >= 0) return 3;
    if (s.indexOf('ZERO_EXPORT') >= 0 || s.indexOf('ZERO EXPORT') >= 0) return 2;
    if (s.indexOf('SELF_USE') >= 0 || s.indexOf('SELF USE') >= 0 || s.indexOf('AUTO') >= 0) return 1;
    return null;
}

function parseScheduledLoads(slot) {
    if (!slot) return [];
    if (Array.isArray(slot.scheduled_loads)) return slot.scheduled_loads;
    var raw = slot.scheduled_loads_json;
    if (typeof raw === 'string' && raw.length) {
        try {
            var parsed = JSON.parse(raw);
            return Array.isArray(parsed) ? parsed : [];
        } catch (e) {
            return [];
        }
    }
    return [];
}

function buildModeSeries(planMode, modeValue, yValue) {
    var out = new Array(planMode.length).fill(null);
    for (var i = 0; i < planMode.length; i++) {
        var cur = planMode[i];
        var prev = i > 0 ? planMode[i - 1] : null;
        var next = i < planMode.length - 1 ? planMode[i + 1] : null;

        if (cur === modeValue) {
            out[i] = yValue;
            continue;
        }
        // Bridge boundaries so there is no visible gap at mode transitions.
        if (prev === modeValue || next === modeValue) {
            out[i] = yValue;
        }
    }
    return out;
}

var rollingNowMarkerPlugin = {
    id: 'rollingNowMarker',
    afterDatasetsDraw: function(chart) {
        if (!chart || !chart.chartArea || rollingNowIndex < 0) return;
        var xScale = chart.scales.x;
        if (!xScale) return;
        var x = xScale.getPixelForValue(rollingNowIndex);
        var top = chart.chartArea.top;
        var bottom = chart.chartArea.bottom;
        var ctx = chart.ctx;

        ctx.save();
        ctx.strokeStyle = 'rgba(255, 255, 255, 0.65)';
        ctx.lineWidth = 1;
        ctx.setLineDash([5, 4]);
        ctx.beginPath();
        ctx.moveTo(x, top);
        ctx.lineTo(x, bottom);
        ctx.stroke();
        ctx.setLineDash([]);
        ctx.fillStyle = 'rgba(255, 255, 255, 0.8)';
        ctx.font = '10px sans-serif';
        ctx.textAlign = 'center';
        ctx.textBaseline = 'bottom';
        ctx.fillText('Now', x, bottom - 4);
        ctx.restore();
    }
};

function loadRollingChartData() {
    if (!rollingChart) return;
    rollingTimeline = buildRollingTimeline();
    var N = rollingTimeline.points.length;

    var soc = new Array(N).fill(null);
    var solar = new Array(N).fill(null);
    var load = new Array(N).fill(null);
    var grid = new Array(N).fill(null);
    var price = new Array(N).fill(null);
    var planSoc = new Array(N).fill(null);
    var planMode = new Array(N).fill(null);
    var planModeName = new Array(N).fill(null);
    var planScheduledLoads = new Array(N).fill(null);
    var modeStripY = -ROLLING_POWER_MAX_KW;

    Promise.all([
        fetch('/api/telemetry/history?hours=' + ROLLING_WINDOW_HOURS).then(function(r) { return r.json(); }),
        fetch('/api/prices/history?hours=' + ROLLING_WINDOW_HOURS).then(function(r) { return r.json(); }),
        fetch('/api/plan/active').then(function(r) { return r.json(); }),
    ]).then(function(results) {
        var telemetryRows = results[0] || [];
        var priceRows = results[1] || [];
        var planData = results[2] || {};
        var nowIdx = Math.round((rollingTimeline.anchorMs - rollingTimeline.startMs) / rollingTimeline.stepMs);
        rollingNowIndex = nowIdx;

        telemetryRows.forEach(function(row) {
            var ts = row.recorded_at || row.timestamp || row.created_at || '';
            var tsMs = new Date(ts).getTime();
            if (!Number.isFinite(tsMs) || tsMs >= rollingTimeline.anchorMs) return;
            var idx = timelineIndex(rollingTimeline, ts);
            if (idx < 0) return;
            soc[idx] = row.soc !== null && row.soc !== undefined ? Math.round(Number(row.soc) * 100) : null;
            solar[idx] = toKw(row.solar_power_w);
            load[idx] = toKw(row.load_power_w);
            grid[idx] = toKw(row.grid_power_w);
            var histMode = parseModeValue(row.inverter_mode);
            if (histMode !== null) {
                planMode[idx] = histMode;
                planModeName[idx] = String(row.inverter_mode || modeName(histMode));
            }
        });

        priceRows.forEach(function(row) {
            var ts = row.recorded_at || row.timestamp || row.created_at || '';
            var idx = timelineIndex(rollingTimeline, ts);
            if (idx < 0 || idx >= nowIdx) return;
            var p = Number(row.import_price_cents);
            if (Number.isFinite(p)) price[idx] = p;
        });

        if (planData && Array.isArray(planData.slots)) {
            planData.slots.forEach(function(slot) {
                var slotMs = new Date(slot.slot_start).getTime();
                if (!Number.isFinite(slotMs)) return;
                var idx = timelineIndex(rollingTimeline, slot.slot_start);
                if (idx < 0) return;
                // Keep forecast traces on the future side to avoid history/forecast overlap.
                if (idx >= nowIdx) {
                    var sF = toKw(slot.solar_forecast_w);
                    var lF = toKw(slot.load_forecast_w);
                    if (sF !== null) solar[idx] = sF;
                    if (lF !== null) load[idx] = lF;
                    var p = Number(slot.import_rate_cents);
                    if (Number.isFinite(p)) price[idx] = p;
                    if (slot.expected_soc !== null && slot.expected_soc !== undefined) {
                        planSoc[idx] = Math.round(Number(slot.expected_soc) * 100);
                    }
                }
                var mode = parseModeValue(slot.operating_mode);
                if (mode === null) mode = parseModeValue(slot.mode);
                if (mode !== null) {
                    planMode[idx] = mode;
                    planModeName[idx] = modeName(mode);
                    planScheduledLoads[idx] = parseScheduledLoads(slot);
                }
            });
        }

        // If telemetry does not include inverter_mode, backfill historical side from plan slots.
        for (var j = 0; j < nowIdx; j++) {
            if (planMode[j] === null) continue;
            if (!planModeName[j]) planModeName[j] = modeName(Number(planMode[j]));
        }

        for (var i = 0; i < N; i++) {
            if (planMode[i] !== null) {
                planMode[i] = Number(planMode[i]);
                planMode[i] = Number.isFinite(planMode[i]) ? planMode[i] : null;
            }
        }
        var planModeY1 = buildModeSeries(planMode, 1, modeStripY);
        var planModeY2 = buildModeSeries(planMode, 2, modeStripY);
        var planModeY3 = buildModeSeries(planMode, 3, modeStripY);
        var planModeY4 = buildModeSeries(planMode, 4, modeStripY);
        var planModeY5 = buildModeSeries(planMode, 5, modeStripY);
        var planModeY = planMode.map(function(m) { return m === null ? null : modeStripY; });

        rollingChart.data.labels = rollingTimeline.labels;
        rollingChart.data.datasets[0].data = soc;
        rollingChart.data.datasets[1].data = solar;
        rollingChart.data.datasets[2].data = load;
        rollingChart.data.datasets[3].data = grid;
        rollingChart.data.datasets[4].data = price;
        rollingChart.data.datasets[5].data = planSoc;
        rollingChart.data.datasets[6].data = planModeY1;
        rollingChart.data.datasets[7].data = planModeY2;
        rollingChart.data.datasets[8].data = planModeY3;
        rollingChart.data.datasets[9].data = planModeY4;
        rollingChart.data.datasets[10].data = planModeY5;
        // Hidden aggregate used only for debug/compat if needed.
        rollingChart.data.datasets[11].data = planModeY;
        for (var d = 6; d <= 10; d++) {
            rollingChart.data.datasets[d].scheduledLoadsData = planScheduledLoads;
            rollingChart.data.datasets[d].modeData = planMode;
        }
        rollingChart.update('none');
    })
    .catch(function(err) {
        console.error('Failed to load rolling chart data:', err);
        rollingNowIndex = Math.round((rollingTimeline.anchorMs - rollingTimeline.startMs) / rollingTimeline.stepMs);
        rollingChart.data.labels = rollingTimeline.labels;
        rollingChart.data.datasets.forEach(function(ds) {
            ds.data = new Array(N).fill(null);
        });
        rollingChart.update('none');
    });
}

function formatTimeLabel(isoStr) {
    if (!isoStr) return '';
    try {
        var d = new Date(isoStr);
        return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    } catch(e) {
        return isoStr.substring(11, 16);
    }
}

document.addEventListener('DOMContentLoaded', function() {
    initRollingChart();
});

function initOverviewLoadToggles() {
    var toggles = document.querySelectorAll('.load-enabled-toggle');
    if (!toggles.length) return;

    toggles.forEach(function(toggle) {
        toggle.addEventListener('change', function() {
            var name = this.dataset.deviceName;
            var type = this.dataset.deviceType;
            var enabled = !!this.checked;
            var labelSpan = this.parentNode ? this.parentNode.querySelector('span') : null;
            var oldText = labelSpan ? labelSpan.textContent : '';
            if (labelSpan) labelSpan.textContent = enabled ? 'On' : 'Off';

            var base = type === 'mqtt' ? '/api/loads/mqtt/' : '/api/loads/shelly/';
            var self = this;
            fetch(base + encodeURIComponent(name), {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ enabled: enabled }),
            })
            .then(function(r) { return r.json(); })
            .then(function(resp) {
                if (!resp || resp.status !== 'ok') {
                    self.checked = !enabled;
                    if (labelSpan) labelSpan.textContent = oldText || (self.checked ? 'On' : 'Off');
                    alert('Failed to update device enabled state');
                }
            })
            .catch(function() {
                self.checked = !enabled;
                if (labelSpan) labelSpan.textContent = oldText || (self.checked ? 'On' : 'Off');
                alert('Failed to update device enabled state');
            });
        });
    });
}

document.addEventListener('DOMContentLoaded', initOverviewLoadToggles);

// ── SSE Live Updates ────────────────────────────────
var sseHeartbeatTimer = null;
var SSE_TIMEOUT_MS = 30000; // Force reconnect if no data for 30s

function connectSSE() {
    var source = new EventSource('/api/events');

    function resetHeartbeat() {
        if (sseHeartbeatTimer) clearTimeout(sseHeartbeatTimer);
        sseHeartbeatTimer = setTimeout(function() {
            console.warn('SSE heartbeat timeout — reconnecting');
            source.close();
            connectSSE();
        }, SSE_TIMEOUT_MS);
    }

    resetHeartbeat();

    source.onmessage = function(e) {
        resetHeartbeat();
        try {
            var data = JSON.parse(e.data);
            if (data.error) {
                console.error('SSE error:', data.error);
                return;
            }

            // Update telemetry display
            if (data.telemetry) {
                updateTelemetryDisplay(data.telemetry);
            }

            if (data.price_import_cents !== undefined || data.price_export_cents !== undefined) {
                updatePriceDisplay(data.price_import_cents, data.price_export_cents);
            }

            // Update spike alert
            updateSpikeAlert(data.spike_active);

            // Update mode display
            if (data.mode) {
                updateModeDisplay({
                    current_mode: data.mode.current,
                    mode_name: data.mode.name,
                    override_active: data.mode.override_active,
                    override_remaining_s: data.mode.override_remaining_s,
                    source: data.mode.source,
                    optimiser_mode: data.mode.optimiser_mode,
                    optimiser_mode_name: data.mode.optimiser_mode_name,
                    user_mode: data.mode.user_mode,
                    user_mode_name: data.mode.user_mode_name,
                    auto_active: data.mode.auto_active,
                });
            }

            // Update accounting display
            if (data.accounting) {
                updateAccountingDisplay(data.accounting);
            }
        } catch(err) {
            console.error('SSE parse error:', err);
        }
    };

    source.onerror = function() {
        if (sseHeartbeatTimer) clearTimeout(sseHeartbeatTimer);
        source.close();
        setTimeout(connectSSE, 5000);
    };
}

function setValueTone(el, tone) {
    if (!el) return;
    el.classList.remove('value-good', 'value-bad', 'value-info', 'value-load', 'value-warn');
    if (tone) el.classList.add(tone);
}

function updateTelemetryDisplay(data) {
    var FLOW_DOT_THRESHOLD_W = 100; // +/-0.1kW
    var finalLoadW = null;

    var socEl = document.getElementById('soc-value');
    if (socEl && data.soc !== undefined) {
        var socPctText = Math.round(data.soc * 100);
        socEl.textContent = socPctText + '%';
        if (socPctText < 20) setValueTone(socEl, 'value-bad');
        else if (socPctText < 50) setValueTone(socEl, 'value-warn');
        else setValueTone(socEl, 'value-good');
    }

    var solarEl = document.getElementById('solar-value');
    if (solarEl && data.solar_power_w !== undefined) {
        solarEl.textContent = (data.solar_power_w / 1000).toFixed(1) + ' kW';
        setValueTone(solarEl, 'value-good');
    }

    var gridEl = document.getElementById('grid-value');
    if (gridEl && data.grid_power_w !== undefined) {
        var gw = data.grid_power_w;
        if (gw > 0) {
            gridEl.innerHTML = '<span class="importing">&#8595; ' + (gw / 1000).toFixed(1) + ' kW</span>';
            setValueTone(gridEl, 'value-bad');
        } else if (gw < 0) {
            gridEl.innerHTML = '<span class="exporting">&#8593; ' + (-gw / 1000).toFixed(1) + ' kW</span>';
            setValueTone(gridEl, 'value-good');
        } else {
            gridEl.textContent = '0.0 kW';
            setValueTone(gridEl, 'value-info');
        }
    }

    var loadEl = document.getElementById('load-value');
    if (loadEl) {
        var loadW = Number(data.load_power_w);
        if ((!Number.isFinite(loadW) || loadW <= 0) &&
            Number.isFinite(Number(data.solar_power_w)) &&
            Number.isFinite(Number(data.grid_power_w)) &&
            Number.isFinite(Number(data.battery_power_w))) {
            var derived = Number(data.solar_power_w) + Number(data.grid_power_w) - Number(data.battery_power_w);
            if (Number.isFinite(derived) && derived > 0 && Math.abs(derived) <= (MAX_REASONABLE_KW * 1000)) {
                loadW = derived;
            }
        }
        if (Number.isFinite(loadW) && loadW > 0) {
            loadEl.textContent = (loadW / 1000).toFixed(1) + ' kW';
            finalLoadW = loadW;
        } else {
            loadEl.textContent = '0.0 kW';
            finalLoadW = 0;
        }
        setValueTone(loadEl, 'value-load');
    }

    // Update SOC bar
    var socFill = document.getElementById('battery-level-fill') || document.querySelector('.soc-fill');
    if (socFill && data.soc !== undefined) {
        var socPct = Math.max(0, Math.min(100, Number(data.soc) * 100));
        socFill.style.width = socPct + '%';

        var battery3d = document.getElementById('battery-3d');
        if (battery3d) {
            battery3d.classList.remove('battery-low', 'battery-medium', 'battery-high');
            if (socPct < 20) {
                battery3d.classList.add('battery-low');
            } else if (socPct < 50) {
                battery3d.classList.add('battery-medium');
            } else {
                battery3d.classList.add('battery-high');
            }
        }
    }

    // Update battery flow status
    var bfEl = document.getElementById('battery-flow-value');
    if (bfEl && data.battery_power_w !== undefined) {
        var bp = data.battery_power_w;
        if (bp > 0) {
            bfEl.innerHTML = '<span class="charging">Charging ' + (bp / 1000).toFixed(1) + ' kW</span>';
            setValueTone(bfEl, 'value-good');
        } else if (bp < 0) {
            bfEl.innerHTML = '<span class="discharging">Discharging ' + (-bp / 1000).toFixed(1) + ' kW</span>';
            setValueTone(bfEl, 'value-bad');
        } else {
            bfEl.textContent = 'Idle';
            setValueTone(bfEl, 'value-info');
        }
    }

    // Update work mode (inverter panel) and status bar
    var workModeEl = document.getElementById('work-mode-value');
    if (workModeEl && data.inverter_mode) {
        workModeEl.textContent = data.inverter_mode;
        setValueTone(workModeEl, 'value-info');
    }
    var statusInverter = document.getElementById('status-inverter');
    if (statusInverter && data.inverter_mode) {
        statusInverter.textContent = data.inverter_mode;
        setValueTone(statusInverter, 'value-info');
    }

    // Update inverter flow graphic
    var flowPv = document.getElementById('flow-pv-value');
    var flowPvArrow = document.getElementById('flow-arrow-pv');
    if (flowPv && data.solar_power_w !== undefined) {
        var fpv = Number(data.solar_power_w || 0);
        if (Math.abs(fpv) <= FLOW_DOT_THRESHOLD_W) {
            flowPv.textContent = '';
            setValueTone(flowPv, 'value-info');
            if (flowPvArrow) flowPvArrow.innerHTML = '&#8226;';
        } else {
            flowPv.textContent = (fpv / 1000).toFixed(1) + 'kW';
            setValueTone(flowPv, fpv > 0 ? 'value-good' : 'value-info');
            if (flowPvArrow) flowPvArrow.innerHTML = '&#8600;';
        }
    }

    var flowGrid = document.getElementById('flow-grid-value');
    var flowGridArrow = document.getElementById('flow-arrow-grid');
    if (flowGrid && data.grid_power_w !== undefined) {
        var fg = Number(data.grid_power_w || 0);
        if (Math.abs(fg) <= FLOW_DOT_THRESHOLD_W) {
            flowGrid.textContent = '';
            setValueTone(flowGrid, 'value-info');
            if (flowGridArrow) flowGridArrow.innerHTML = '&#8226;';
        } else if (fg > 0) {
            flowGrid.textContent = (fg / 1000).toFixed(1) + 'kW';
            setValueTone(flowGrid, 'value-bad');
            if (flowGridArrow) flowGridArrow.innerHTML = '&#8601;';
        } else if (fg < 0) {
            flowGrid.textContent = (-fg / 1000).toFixed(1) + 'kW';
            setValueTone(flowGrid, 'value-good');
            if (flowGridArrow) flowGridArrow.innerHTML = '&#8599;';
        }
    }

    var flowBatt = document.getElementById('flow-batt-value');
    var flowBattArrow = document.getElementById('flow-arrow-batt');
    if (flowBatt && data.battery_power_w !== undefined) {
        var fb = Number(data.battery_power_w || 0);
        if (Math.abs(fb) <= FLOW_DOT_THRESHOLD_W) {
            flowBatt.textContent = '';
            setValueTone(flowBatt, 'value-info');
            if (flowBattArrow) flowBattArrow.innerHTML = '&#8226;';
        } else if (fb > 0) {
            flowBatt.textContent = (fb / 1000).toFixed(1) + 'kW';
            setValueTone(flowBatt, 'value-good');
            if (flowBattArrow) flowBattArrow.innerHTML = '&#8601;';
        } else if (fb < 0) {
            flowBatt.textContent = (-fb / 1000).toFixed(1) + 'kW';
            setValueTone(flowBatt, 'value-bad');
            if (flowBattArrow) flowBattArrow.innerHTML = '&#8599;';
        }
    }

    var flowLoad = document.getElementById('flow-load-value');
    var flowLoadArrow = document.getElementById('flow-arrow-load');
    if (flowLoad) {
        if (finalLoadW === null && Number.isFinite(Number(data.load_power_w))) {
            finalLoadW = Number(data.load_power_w);
        }
        if (Number.isFinite(finalLoadW) && Math.abs(finalLoadW) <= FLOW_DOT_THRESHOLD_W) {
            flowLoad.textContent = '';
            if (flowLoadArrow) flowLoadArrow.innerHTML = '&#8226;';
        } else if (Number.isFinite(finalLoadW) && finalLoadW > 0) {
            flowLoad.textContent = (finalLoadW / 1000).toFixed(1) + 'kW';
            if (flowLoadArrow) flowLoadArrow.innerHTML = '&#8600;';
        } else if (Number.isFinite(finalLoadW) && finalLoadW < 0) {
            flowLoad.textContent = (-finalLoadW / 1000).toFixed(1) + 'kW';
            if (flowLoadArrow) flowLoadArrow.innerHTML = '&#8598;';
        } else {
            flowLoad.textContent = '';
            if (flowLoadArrow) flowLoadArrow.innerHTML = '&#8226;';
        }
        setValueTone(flowLoad, 'value-load');
    }

    var flowBattSocFill = document.getElementById('flow-batt-soc-fill');
    if (flowBattSocFill && data.soc !== undefined) {
        var socMini = Math.max(0, Math.min(100, Number(data.soc) * 100));
        flowBattSocFill.style.width = socMini + '%';
        if (socMini < 20) {
            flowBattSocFill.style.background = 'linear-gradient(90deg, #b0332c 0%, #f85149 50%, #ff8c84 100%)';
        } else if (socMini < 50) {
            flowBattSocFill.style.background = 'linear-gradient(90deg, #b07917 0%, #d29922 50%, #e7bb6f 100%)';
        } else {
            flowBattSocFill.style.background = 'linear-gradient(90deg, #1f9d3a 0%, #3fb950 50%, #7cd88a 100%)';
        }
    }
}

function updatePriceDisplay(importPriceCents, exportPriceCents) {
    function buyToneClass(cents) {
        if (!Number.isFinite(cents)) return 'value-info';
        if (BUY_BAND_LOW !== null && BUY_BAND_HIGH !== null) {
            if (cents <= BUY_BAND_LOW) return 'value-good';
            if (cents >= BUY_BAND_HIGH) return 'value-bad';
            return 'value-warn';
        }
        var good = PRICE_SPIKE_THRESHOLD_CENTS * 0.2;
        var average = PRICE_SPIKE_THRESHOLD_CENTS * 0.6;
        if (cents <= good) return 'value-good';
        if (cents <= average) return 'value-warn';
        return 'value-bad';
    }

    function sellToneClass(cents) {
        if (!Number.isFinite(cents)) return 'value-info';
        if (SELL_BAND_LOW !== null && SELL_BAND_HIGH !== null) {
            if (cents <= SELL_BAND_LOW) return 'value-bad';
            if (cents >= SELL_BAND_HIGH) return 'value-good';
            return 'value-warn';
        }
        return 'value-good';
    }

    // Update inline price display (new panel layout)
    var buyInline = document.getElementById('price-buy-inline');
    if (buyInline) {
        var ip = Number(importPriceCents);
        buyInline.innerHTML = Number.isFinite(ip) ? 'Buy ' + ip.toFixed(1) + 'c' : 'Buy --c';
        setValueTone(buyInline, buyToneClass(ip));
    }
    var sellInline = document.getElementById('price-sell-inline');
    if (sellInline) {
        var ep = Number(exportPriceCents);
        sellInline.innerHTML = Number.isFinite(ep) ? 'Sell ' + ep.toFixed(1) + 'c' : 'Sell --c';
        setValueTone(sellInline, sellToneClass(ep));
    }
}

function updateSpikeAlert(active) {
    var banner = document.getElementById('spike-alert');
    if (banner) {
        banner.style.display = active ? 'block' : 'none';
    }
}

function updateAccountingDisplay(accounting) {
    var wacbEl = document.getElementById('wacb-value');
    if (wacbEl && accounting.wacb_cents !== undefined) {
        wacbEl.textContent = accounting.wacb_cents.toFixed(1) + 'c/kWh avg';
        setValueTone(wacbEl, 'value-info');
    }

    var wacbTotal = document.getElementById('wacb-total');
    if (wacbTotal && accounting.stored_value_cents !== undefined) {
        wacbTotal.textContent = '$' + (accounting.stored_value_cents / 100).toFixed(2);
        setValueTone(wacbTotal, 'value-info');
    }

    // Today's cost
    var todayNet = document.getElementById('today-net');
    if (todayNet && accounting.today_net_cost_cents !== undefined) {
        var todayCents = accounting.today_net_cost_cents;
        todayNet.textContent = '$' + (todayCents / 100).toFixed(2);
        setValueTone(todayNet, todayCents > 0 ? 'value-bad' : (todayCents < 0 ? 'value-good' : 'value-info'));
    }

    // This week's cost
    var weekNet = document.getElementById('week-net');
    if (weekNet && accounting.week_net_cost_cents !== undefined) {
        var weekCents = accounting.week_net_cost_cents;
        weekNet.textContent = '$' + (weekCents / 100).toFixed(2);
        setValueTone(weekNet, weekCents > 0 ? 'value-bad' : (weekCents < 0 ? 'value-good' : 'value-info'));
    }

    if (accounting.cycle) {
        // Billing cycle cost
        var cycleNet = document.getElementById('cycle-net');
        if (cycleNet) {
            var cycleCents = accounting.cycle.net_cost_cents;
            cycleNet.textContent = '$' + (cycleCents / 100).toFixed(2);
            setValueTone(cycleNet, cycleCents > 0 ? 'value-bad' : (cycleCents < 0 ? 'value-good' : 'value-info'));
        }

        // Expected bill (linear projection)
        var expectedBill = document.getElementById('expected-bill');
        if (expectedBill && accounting.cycle.days_elapsed > 0) {
            var dailyAvg = accounting.cycle.net_cost_cents / accounting.cycle.days_elapsed;
            var totalDays = accounting.cycle.days_elapsed + accounting.cycle.days_remaining;
            var expectedCents = dailyAvg * totalDays;
            expectedBill.textContent = '$' + (expectedCents / 100).toFixed(2);
            setValueTone(expectedBill, expectedCents > 0 ? 'value-bad' : (expectedCents < 0 ? 'value-good' : 'value-info'));
        }
    }
}

function loadInitialAccountingSummary() {
    fetch('/api/accounting/summary')
    .then(function(r) { return r.json(); })
    .then(function(data) {
        if (data && Object.keys(data).length) {
            updateAccountingDisplay(data);
        }
    })
    .catch(function() {});
}

// Connect SSE when page loads
document.addEventListener('DOMContentLoaded', connectSSE);
document.addEventListener('DOMContentLoaded', loadInitialAccountingSummary);


// ── Graphs Page ─────────────────────────────────────
var graphCharts = {};

document.addEventListener('DOMContentLoaded', function() {
    // Tab switching (graphs + other tabbed pages)
    var tabs = document.querySelectorAll('.tab');
    tabs.forEach(function(tab) {
        tab.addEventListener('click', function() {
            var target = this.dataset.tab;
            tabs.forEach(function(t) { t.classList.remove('active'); });
            this.classList.add('active');
            document.querySelectorAll('.tab-content').forEach(function(s) {
                s.classList.remove('active');
            });
            var section = document.getElementById('tab-' + target);
            if (section) section.classList.add('active');
            // Delay chart load until after the tab is visible so Chart.js
            // can measure the canvas dimensions correctly
            requestAnimationFrame(function() {
                loadGraphTab(target);
            });
        });
    });

    // Time range buttons
    document.querySelectorAll('.time-range-btn').forEach(function(btn) {
        btn.addEventListener('click', function() {
            var tab = this.closest('.tab-content').id.replace('tab-', '');
            this.parentNode.querySelectorAll('.time-range-btn').forEach(function(b) {
                b.classList.remove('active');
            });
            this.classList.add('active');
            loadGraphTab(tab, this.dataset.hours || this.dataset.days);
        });
    });

    // Auto-load default active tab on graphs page
    if (document.getElementById('energy-chart')) {
        loadGraphTab('energy');
    }
});

function loadGraphTab(tab, range) {
    switch(tab) {
        case 'energy': loadEnergyChart(range || 24); break;
        case 'financial': loadFinancialChart(range || 30); break;
        case 'battery': loadBatteryChart(range || 24); break;
        case 'prices': loadPricesChart(range || 24); break;
        case 'solar': loadSolarChart(range || 48); break;
        case 'load': loadLoadChart(range || 24); break;
    }
}

function createOrUpdateChart(canvasId, config) {
    if (graphCharts[canvasId]) {
        graphCharts[canvasId].destroy();
    }
    var canvas = document.getElementById(canvasId);
    if (!canvas || typeof Chart === 'undefined') return null;
    graphCharts[canvasId] = new Chart(canvas.getContext('2d'), config);
    return graphCharts[canvasId];
}

function loadEnergyChart(hours) {
    fetch('/api/telemetry/history?hours=' + hours)
    .then(function(r) { return r.json(); })
    .then(function(rows) {
        if (!rows.length) return;
        var labels = rows.map(function(r) { return formatTimeLabel(r.recorded_at || r.timestamp || r.created_at); });
        createOrUpdateChart('energy-chart', {
            type: 'line',
            data: {
                labels: labels,
                datasets: [
                    { label: 'Solar (kW)', data: rows.map(function(r) { return (r.solar_power_w || 0) / 1000; }),
                      borderColor: '#d29922', backgroundColor: 'rgba(210, 153, 34, 0.15)', fill: true, tension: 0.3, pointRadius: 0 },
                    { label: 'Load (kW)', data: rows.map(function(r) { return (r.load_power_w || 0) / 1000; }),
                      borderColor: '#58a6ff', backgroundColor: 'rgba(88, 166, 255, 0.15)', fill: true, tension: 0.3, pointRadius: 0 },
                    { label: 'Grid (kW)', data: rows.map(function(r) { return (r.grid_power_w || 0) / 1000; }),
                      borderColor: '#f85149', borderWidth: 1.5, tension: 0.3, pointRadius: 0 },
                    { label: 'Battery (kW)', data: rows.map(function(r) { return (r.battery_power_w || 0) / 1000; }),
                      borderColor: '#3fb950', borderWidth: 1.5, tension: 0.3, pointRadius: 0 },
                ]
            },
            options: chartOptions('kW')
        });
    }).catch(function() {});
}

function loadFinancialChart(days) {
    fetch('/api/accounting/daily?days=' + days)
    .then(function(r) { return r.json(); })
    .then(function(rows) {
        if (!rows.length) return;
        var labels = rows.map(function(r) { return r.day; });
        createOrUpdateChart('financial-chart', {
            type: 'bar',
            data: {
                labels: labels,
                datasets: [
                    { label: 'Import Cost ($)', data: rows.map(function(r) { return (r.import_cents || 0) / 100; }),
                      backgroundColor: '#f85149' },
                    { label: 'Export Revenue ($)', data: rows.map(function(r) { return (r.export_cents || 0) / 100; }),
                      backgroundColor: '#3fb950' },
                    { label: 'Self-Consumption ($)', data: rows.map(function(r) { return (r.self_consumption_cents || 0) / 100; }),
                      backgroundColor: '#58a6ff' },
                    { label: 'Arbitrage ($)', data: rows.map(function(r) { return (r.arbitrage_cents || 0) / 100; }),
                      backgroundColor: '#d29922' },
                ]
            },
            options: chartOptions('$', true)
        });
    }).catch(function() {});
}

function loadBatteryChart(hours) {
    fetch('/api/telemetry/history?hours=' + hours)
    .then(function(r) { return r.json(); })
    .then(function(rows) {
        if (!rows.length) return;
        var labels = rows.map(function(r) { return formatTimeLabel(r.recorded_at || r.timestamp || r.created_at); });

        // Use shared configured max power for fixed Y-axis scale
        var maxPower = ROLLING_POWER_MAX_KW;

        // Build charge/discharge mode indicator: +1=charging, -1=discharging, 0=idle
        var modeData = rows.map(function(r) {
            var bp = r.battery_power_w || 0;
            if (bp > 50) return 1;       // charging
            if (bp < -50) return -1;      // discharging
            return 0;                     // idle
        });

        createOrUpdateChart('battery-chart', {
            type: 'line',
            data: {
                labels: labels,
                datasets: [
                    { label: 'SOC %', data: rows.map(function(r) { return r.soc ? Math.round(r.soc * 100) : null; }),
                      borderColor: '#3fb950', backgroundColor: 'rgba(63, 185, 80, 0.1)', fill: true, tension: 0.3, pointRadius: 0, yAxisID: 'y' },
                    { label: 'Battery Power (kW)', data: rows.map(function(r) { return (r.battery_power_w * 1.5 || 0) / 1000; }),
                      borderColor: '#d29922', borderWidth: 1.5, tension: 0.3, pointRadius: 0, yAxisID: 'y1' },
                    { label: 'Mode', data: modeData,
                      borderColor: 'rgba(88, 166, 255, 0.5)', backgroundColor: 'rgba(88, 166, 255, 0.1)',
                      borderWidth: 0, fill: true, tension: 0, pointRadius: 0, yAxisID: 'y2',
                      segment: {
                          backgroundColor: function(ctx) {
                              var v = ctx.p0.parsed.y;
                              if (v > 0) return 'rgba(63, 185, 80, 0.12)';
                              if (v < 0) return 'rgba(248, 81, 73, 0.12)';
                              return 'transparent';
                          }
                      }
                    },
                ]
            },
            options: {
                responsive: true, maintainAspectRatio: false,
                interaction: { mode: 'index', intersect: false },
                plugins: { legend: { labels: { color: '#8b949e' } } },
                scales: {
                    x: { grid: { color: 'rgba(48, 54, 61, 0.5)' }, ticks: { color: '#6e7681', maxTicksLimit: 12 } },
                    y: { position: 'left', min: 0, max: 100, grid: { color: 'rgba(48, 54, 61, 0.3)' },
                         ticks: { color: '#3fb950' }, title: { display: true, text: 'SOC %', color: '#3fb950' } },
                    y1: { position: 'right', min: -maxPower, max: maxPower, grid: { display: false },
                          ticks: { color: '#d29922' }, title: { display: true, text: 'kW', color: '#d29922' } },
                    y2: { display: false, min: -1.5, max: 1.5 },
                }
            }
        });
    }).catch(function() {});
}

function loadPricesChart(hours) {
    // Load both price and grid energy data in parallel
    Promise.all([
        fetch('/api/prices/history?hours=' + hours).then(function(r) { return r.json(); }),
        fetch('/api/telemetry/history?hours=' + hours).then(function(r) { return r.json(); }),
    ]).then(function(results) {
        var priceRows = results[0];
        var telemetryRows = results[1];
        if (!priceRows.length) return;

        var labels = priceRows.map(function(r) { return formatTimeLabel(r.recorded_at); });

        // Build grid energy lookup keyed by time label
        var gridByTime = {};
        telemetryRows.forEach(function(r) {
            var label = formatTimeLabel(r.recorded_at || r.timestamp || r.created_at);
            gridByTime[label] = (r.grid_power_w || 0) / 1000;
        });

        var gridData = labels.map(function(label) {
            return gridByTime[label] !== undefined ? gridByTime[label] : null;
        });

        createOrUpdateChart('prices-chart', {
            type: 'line',
            data: {
                labels: labels,
                datasets: [
                    { label: 'Import (c/kWh)', data: priceRows.map(function(r) { return r.import_price_cents || 0; }),
                      borderColor: '#f85149', borderWidth: 2, tension: 0.1, pointRadius: 0, fill: false, yAxisID: 'y' },
                    { label: 'Export (c/kWh)', data: priceRows.map(function(r) { return r.export_price_cents || 0; }),
                      borderColor: '#3fb950', borderWidth: 2, tension: 0.1, pointRadius: 0, fill: false, yAxisID: 'y' },
                    { label: 'Grid (kW)', data: gridData,
                      borderColor: 'rgba(88, 166, 255, 0.5)', borderWidth: 1, borderDash: [4, 4],
                      tension: 0.3, pointRadius: 0, fill: false, yAxisID: 'y1' },
                ]
            },
            options: {
                responsive: true, maintainAspectRatio: false,
                interaction: { mode: 'index', intersect: false },
                plugins: { legend: { labels: { color: '#8b949e', font: { size: 11 }, boxWidth: 12 } } },
                scales: {
                    x: { grid: { color: 'rgba(48, 54, 61, 0.5)' }, ticks: { color: '#6e7681', font: { size: 10 }, maxTicksLimit: 12 } },
                    y: { position: 'left', grid: { color: 'rgba(48, 54, 61, 0.3)' },
                         ticks: { color: '#8b949e', font: { size: 10 } },
                         title: { display: true, text: 'c/kWh', color: '#8b949e', font: { size: 10 } } },
                    y1: { position: 'right', grid: { display: false },
                          min: -ROLLING_POWER_MAX_KW, max: ROLLING_POWER_MAX_KW,
                          ticks: { color: '#58a6ff', font: { size: 10 } },
                          title: { display: true, text: 'kW', color: '#58a6ff', font: { size: 10 } } },
                }
            }
        });
    }).catch(function() {});
}

function loadSolarChart(hours) {
    fetch('/api/telemetry/history?hours=' + hours)
    .then(function(r) { return r.json(); })
    .then(function(rows) {
        if (!rows.length) return;
        var labels = rows.map(function(r) { return formatTimeLabel(r.recorded_at || r.timestamp || r.created_at); });
        createOrUpdateChart('solar-chart', {
            type: 'line',
            data: {
                labels: labels,
                datasets: [
                    { label: 'Solar (kW)', data: rows.map(function(r) { return (r.solar_power_w || 0) / 1000; }),
                      borderColor: '#d29922', backgroundColor: 'rgba(210, 153, 34, 0.2)', fill: true, tension: 0.3, pointRadius: 0 },
                ]
            },
            options: chartOptions('kW')
        });
    }).catch(function() {});
}

function loadLoadChart(hours) {
    fetch('/api/telemetry/history?hours=' + hours)
    .then(function(r) { return r.json(); })
    .then(function(rows) {
        if (!rows.length) return;
        var labels = rows.map(function(r) { return formatTimeLabel(r.recorded_at || r.timestamp || r.created_at); });
        createOrUpdateChart('load-chart', {
            type: 'line',
            data: {
                labels: labels,
                datasets: [
                    { label: 'Load (kW)', data: rows.map(function(r) { return (r.load_power_w || 0) / 1000; }),
                      borderColor: '#58a6ff', backgroundColor: 'rgba(88, 166, 255, 0.15)', fill: true, tension: 0.3, pointRadius: 0 },
                ]
            },
            options: chartOptions('kW')
        });
    }).catch(function() {});
}

function chartOptions(unit, stacked) {
    var isKw = unit === 'kW';
    return {
        responsive: true,
        maintainAspectRatio: false,
        interaction: { mode: 'index', intersect: false },
        plugins: {
            legend: { labels: { color: '#8b949e', font: { size: 11 }, boxWidth: 12 } },
        },
        scales: {
            x: {
                grid: { color: 'rgba(48, 54, 61, 0.5)' },
                ticks: { color: '#6e7681', font: { size: 10 }, maxTicksLimit: 12 },
                stacked: !!stacked,
            },
            y: {
                grid: { color: 'rgba(48, 54, 61, 0.3)' },
                ticks: { color: '#8b949e', font: { size: 10 } },
                title: { display: true, text: unit, color: '#8b949e', font: { size: 10 } },
                min: isKw ? -ROLLING_POWER_MAX_KW : undefined,
                max: isKw ? ROLLING_POWER_MAX_KW : undefined,
                stacked: !!stacked,
            }
        }
    };
}


// ── Settings Page ───────────────────────────────────
document.addEventListener('DOMContentLoaded', function() {
    // Settings tab switching
    var settingsTabs = document.querySelectorAll('.settings-tab');
    settingsTabs.forEach(function(tab) {
        tab.addEventListener('click', function() {
            var target = this.dataset.tab;
            settingsTabs.forEach(function(t) { t.classList.remove('active'); });
            this.classList.add('active');
            document.querySelectorAll('.settings-panel').forEach(function(p) {
                p.classList.remove('active');
            });
            var panel = document.getElementById('panel-' + target);
            if (panel) panel.classList.add('active');
        });
    });
});


// ── Provider Status ─────────────────────────────────
function loadProviderStatus() {
    var containers = document.querySelectorAll('.provider-status');
    if (!containers.length) return;

    fetch('/api/providers/status')
    .then(function(r) { return r.json(); })
    .then(function(data) {
        var providers = data.providers || {};
        Object.keys(providers).forEach(function(key) {
            var el = document.getElementById('provider-status-' + key);
            if (!el) return;
            var p = providers[key];
            var dot = el.querySelector('.provider-status-dot');
            var text = el.querySelector('.provider-status-text');

            // Determine status
            if (p.healthy === null || p.configured === false) {
                dot.className = 'provider-status-dot dot-grey';
                text.textContent = 'Not configured';
                return;
            }

            var parts = [];
            if (p.healthy) {
                dot.className = 'provider-status-dot dot-green';
                parts.push('Healthy');
            } else {
                dot.className = 'provider-status-dot dot-red';
                parts.push('Unhealthy');
            }

            // Data age
            if (p.data_age_seconds !== null && p.data_age_seconds !== undefined) {
                var age = p.data_age_seconds;
                if (age < 60) {
                    parts.push('updated ' + Math.round(age) + 's ago');
                } else if (age < 3600) {
                    parts.push('updated ' + Math.round(age / 60) + 'm ago');
                } else {
                    parts.push('updated ' + Math.round(age / 3600) + 'h ago');
                }
            } else {
                parts.push('no data yet');
            }

            // Failure info
            if (p.consecutive_failures > 0) {
                parts.push(p.consecutive_failures + ' failures');
            }
            if (p.last_error) {
                parts.push(p.last_error);
            }

            text.textContent = parts.join(' \u2022 ');
        });
    })
    .catch(function() {});
}

document.addEventListener('DOMContentLoaded', function() {
    if (document.querySelector('.provider-status')) {
        loadProviderStatus();
        setInterval(loadProviderStatus, 10000);
    }
});


// ── Load Management ─────────────────────────────────
function addShellyDevice() {
    var form = document.getElementById('shelly-add-form');
    if (!form) return;
    var data = {
        name: form.querySelector('[name="name"]').value,
        host: form.querySelector('[name="host"]').value,
        power_w: parseInt(form.querySelector('[name="power_w"]').value) || 0,
        priority_class: parseInt(form.querySelector('[name="priority_class"]').value) || 5,
        relay_id: parseInt(form.querySelector('[name="relay_id"]').value) || 0,
    };
    fetch('/api/loads/shelly', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(data),
    }).then(function(r) { return r.json(); })
    .then(function(resp) {
        if (resp.status === 'ok') { location.reload(); }
        else { alert('Error: ' + resp.message); }
    }).catch(function(e) { alert('Failed: ' + e); });
}

function deleteShellyDevice(name) {
    if (!confirm('Delete Shelly device "' + name + '"?')) return;
    fetch('/api/loads/shelly/' + encodeURIComponent(name), { method: 'DELETE' })
    .then(function(r) { return r.json(); })
    .then(function(resp) {
        if (resp.status === 'ok') { location.reload(); }
        else { alert('Error: ' + resp.message); }
    }).catch(function(e) { alert('Failed: ' + e); });
}

function addMqttEndpoint() {
    var form = document.getElementById('mqtt-add-form');
    if (!form) return;
    var data = {
        name: form.querySelector('[name="name"]').value,
        command_topic: form.querySelector('[name="command_topic"]').value,
        state_topic: form.querySelector('[name="state_topic"]').value,
        power_w: parseInt(form.querySelector('[name="power_w"]').value) || 0,
        priority_class: parseInt(form.querySelector('[name="priority_class"]').value) || 5,
    };
    fetch('/api/loads/mqtt', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(data),
    }).then(function(r) { return r.json(); })
    .then(function(resp) {
        if (resp.status === 'ok') { location.reload(); }
        else { alert('Error: ' + resp.message); }
    }).catch(function(e) { alert('Failed: ' + e); });
}

function deleteMqttEndpoint(name) {
    if (!confirm('Delete MQTT endpoint "' + name + '"?')) return;
    fetch('/api/loads/mqtt/' + encodeURIComponent(name), { method: 'DELETE' })
    .then(function(r) { return r.json(); })
    .then(function(resp) {
        if (resp.status === 'ok') { location.reload(); }
        else { alert('Error: ' + resp.message); }
    }).catch(function(e) { alert('Failed: ' + e); });
}


// ── Load Device Editing ─────────────────────────────

function editShellyDevice(name, idx) {
    document.getElementById('shelly-edit-' + idx).style.display = 'table-row';
}

function cancelShellyEdit(idx) {
    document.getElementById('shelly-edit-' + idx).style.display = 'none';
}

function saveShellyEdit(idx) {
    var editRow = document.getElementById('shelly-edit-' + idx);
    var grid = editRow.querySelector('.edit-grid');
    var name = grid.dataset.deviceName;
    var data = _collectEditFields(grid);
    fetch('/api/loads/shelly/' + encodeURIComponent(name), {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(data),
    }).then(function(r) { return r.json(); })
    .then(function(resp) {
        if (resp.status === 'ok') { location.reload(); }
        else { alert('Error: ' + resp.message); }
    }).catch(function(e) { alert('Failed: ' + e); });
}

function editMqttEndpoint(name, idx) {
    document.getElementById('mqtt-edit-' + idx).style.display = 'table-row';
}

function cancelMqttEdit(idx) {
    document.getElementById('mqtt-edit-' + idx).style.display = 'none';
}

function saveMqttEdit(idx) {
    var editRow = document.getElementById('mqtt-edit-' + idx);
    var grid = editRow.querySelector('.edit-grid');
    var name = grid.dataset.endpointName;
    var data = _collectEditFields(grid);
    fetch('/api/loads/mqtt/' + encodeURIComponent(name), {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(data),
    }).then(function(r) { return r.json(); })
    .then(function(resp) {
        if (resp.status === 'ok') { location.reload(); }
        else { alert('Error: ' + resp.message); }
    }).catch(function(e) { alert('Failed: ' + e); });
}

function _collectEditFields(grid) {
    var data = {};
    var intFields = ['power_w', 'priority_class', 'relay_id',
                     'min_runtime_minutes', 'ideal_runtime_minutes', 'max_runtime_minutes'];
    var boolFields = ['enabled', 'prefer_solar', 'allow_split_shifts'];

    grid.querySelectorAll('input').forEach(function(input) {
        var key = input.name;
        if (!key) return;
        if (boolFields.indexOf(key) >= 0) {
            data[key] = input.checked;
        } else if (intFields.indexOf(key) >= 0) {
            data[key] = parseInt(input.value) || 0;
        } else if (key === 'days_of_week') {
            data[key] = input.value.split(',').map(function(s) { return parseInt(s.trim()); }).filter(function(n) { return !isNaN(n); });
        } else {
            data[key] = input.value;
        }
    });
    return data;
}
