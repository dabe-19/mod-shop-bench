// Shop Bench v2.6 — Pro Mechanics Code-Reading Computer, Windows BT-OBD Bridge & AI Copilot
// Torque-Style Live Gauges · 45+ SAE J1979 PID Picker · OBDb/Torque CSV/opendbc Importers
// Direct CAN-Bus Sniffer · Web-Triggered Windows BT-COM Bridge · Diagnostic & LLM Report Export
// Strictly XSS-safe DOM & SVG construction (zero innerHTML / outerHTML / native dialogs).
(function () {
  'use strict';

  const SVG_NS = 'http://www.w3.org/2000/svg';

  const SAMPLE_TORQUE_CSV = [
    'Name,ShortName,ModeAndPID,Equation,Min Value,Max Value,Units,Header',
    'Transmission Fluid Temp,TFT,221E1C,((A*256)+B)*0.0625-40,-40,150,C,7E1',
    'Engine Oil Pressure,EOP,221470,A*4,0,600,kPa,7E0',
    'Misfire Counter Cyl 2,MIS2,221202,(A*256)+B,0,500,cnt,7E0',
  ].join('\n');

  const SAMPLE_OBDB_JSON = JSON.stringify({
    commands: [
      {
        hdr: '7E1',
        cmd: { '22': '1E1C' },
        signals: [{ id: 'TFT', name: 'Transmission Fluid Temp', fmt: { bix: 0, len: 16, mul: 1, div: 16, add: -40, unit: '°C', min: -40, max: 150 } }],
      },
      {
        hdr: '7E0',
        cmd: { '22': '115C' },
        signals: [{ id: 'OIL_P', name: 'Oil Pressure', fmt: { bix: 0, len: 8, mul: 4, div: 1, add: 0, unit: 'kPa', min: 0, max: 600 } }],
      },
    ],
  }, null, 2);

  const SAMPLE_DBC = [
    'BO_ 513 ENGINE_DATA: 8 ECM',
    ' SG_ EngineSpeed : 0|16@1+ (0.25,0) [0|8000] "rpm" Vector__XXX',
    ' SG_ CoolantTemp : 16|8@1+ (1,-40) [-40|215] "C" Vector__XXX',
    ' SG_ ThrottlePos : 24|8@1+ (0.392157,0) [0|100] "%" Vector__XXX',
  ].join('\n');

  const GM_GLOBAL_A_DBC = [
    'BO_ 201 PPEI_Engine_General_Status_1: 8 ECM',
    ' SG_ EngineSpeed : 15|16@0+ (0.25,0) [0|16383.75] "rpm" Vector__XXX',
    ' SG_ AccelActPos : 39|8@0+ (0.392157,0) [0|100] "%" Vector__XXX',
    'BO_ 241 PPEI_Brake_Apply_Status: 6 EBCM',
    ' SG_ BrakePedalPos : 15|8@0+ (0.392157,0) [0|100] "%" Vector__XXX',
    'BO_ 193 Driven_Wheel_Grnd_Velocity_HS: 8 EBCM',
    ' SG_ WhlGrndVlctyLftDrvn : 5|14@0+ (0.03125,0) [0|511.96875] "km/h" Vector__XXX',
    ' SG_ WhlGrndVlctyRtDrvn : 37|14@0+ (0.03125,0) [0|511.96875] "km/h" Vector__XXX',
  ].join('\n');

  function svgEl(tag, attrs, text) {
    const n = document.createElementNS(SVG_NS, tag);
    if (attrs) {
      Object.entries(attrs).forEach(([k, v]) => n.setAttribute(k, String(v)));
    }
    if (text != null) n.textContent = String(text);
    return n;
  }

  function readFileAsBase64(file) {
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => {
        const str = String(reader.result || '');
        const idx = str.indexOf(',');
        resolve(idx >= 0 ? str.slice(idx + 1) : str);
      };
      reader.onerror = () => reject(new Error('File read failed'));
      reader.readAsDataURL(file);
    });
  }

  function readFileAsText(file) {
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(String(reader.result || ''));
      reader.onerror = () => reject(new Error('File read failed'));
      reader.readAsText(file);
    });
  }

  function round1(x) {
    return Math.round(x * 10) / 10;
  }

  function triggerBlobDownload(filename, mimeType, content) {
    const blob = new Blob([String(content || '')], { type: mimeType || 'text/plain;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = filename || 'shop_bench_report.txt';
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  async function copyToClipboard(text, onDone) {
    try {
      if (navigator.clipboard && navigator.clipboard.writeText) {
        await navigator.clipboard.writeText(String(text || ''));
        if (onDone) onDone(true);
        return;
      }
    } catch (_) {
      // fallback below
    }
    const ta = document.createElement('textarea');
    ta.value = String(text || '');
    ta.style.position = 'fixed';
    ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.select();
    let ok = false;
    try { ok = document.execCommand('copy'); } catch (_) { ok = false; }
    ta.remove();
    if (onDone) onDone(ok);
  }

  function driveSafetyHint(dtc) {
    const sev = String(dtc.severity || 'medium').toLowerCase();
    if (sev === 'critical') return 'STOP / INSPECT SOON — risk of stalling, overheating, or loss of control.';
    if (sev === 'high') return 'CAUTION — drive gently to shop; prolonged driving may damage catalyst or leave you stranded.';
    return 'MONITOR — generally safe for short trips, schedule diagnosis soon.';
  }

  // Torque-style semi-circular radial SVG instrument gauge (zero innerHTML)
  function buildRadialGauge(el, opts) {
    const val = opts.value != null && !Number.isNaN(Number(opts.value)) ? Number(opts.value) : null;
    const min = opts.min != null ? opts.min : 0;
    const max = opts.max != null ? opts.max : 100;
    const norm = val == null ? 0 : Math.max(0, Math.min(1, (val - min) / Math.max(0.0001, max - min)));
    const stateCls = opts.state || '';
    const arcColor = stateCls === 'crit' ? '#ef4444' : (stateCls === 'warn' ? '#f59e0b' : (opts.color || '#38bdf8'));

    const cx = 66, cy = 66, r = 50;
    const startAngle = Math.PI;
    const endAngle = 0;
    const sweepAngle = startAngle - norm * Math.PI;

    function pt(ang) {
      return [cx + r * Math.cos(ang), cy - r * Math.sin(ang)];
    }
    const [sx, sy] = pt(startAngle);
    const [ex, ey] = pt(endAngle);
    const [vx, vy] = pt(sweepAngle);
    const trackD = `M ${sx.toFixed(1)} ${sy.toFixed(1)} A ${r} ${r} 0 0 1 ${ex.toFixed(1)} ${ey.toFixed(1)}`;
    const valD = `M ${sx.toFixed(1)} ${sy.toFixed(1)} A ${r} ${r} 0 0 1 ${vx.toFixed(1)} ${vy.toFixed(1)}`;

    const svg = svgEl('svg', { class: 'sb-gauge-svg', viewBox: '0 0 132 82' });
    svg.appendChild(svgEl('path', { d: trackD, fill: 'none', stroke: 'rgba(148,163,184,0.18)', 'stroke-width': 9, 'stroke-linecap': 'round' }));
    if (norm > 0.005) {
      svg.appendChild(svgEl('path', { d: valD, fill: 'none', stroke: arcColor, 'stroke-width': 9, 'stroke-linecap': 'round' }));
    }
    svg.appendChild(svgEl('circle', { cx: vx.toFixed(1), cy: vy.toFixed(1), r: 3.5, fill: '#f8fafc' }));
    svg.appendChild(svgEl('text', {
      x: cx, y: 54, 'text-anchor': 'middle', fill: '#f8fafc', 'font-size': 16, 'font-weight': 700, 'font-family': 'monospace',
    }, val == null ? '—' : opts.format ? opts.format(val) : String(val)));
    svg.appendChild(svgEl('text', {
      x: cx, y: 70, 'text-anchor': 'middle', fill: '#94a3b8', 'font-size': 10, 'font-family': 'monospace',
    }, opts.unit || ''));

    return el('div', { className: 'sb-gauge-card' + (stateCls ? ` ${stateCls}` : '') }, [
      el('div', { className: 'sb-gauge-label', text: opts.label }),
      svg,
      el('div', { className: 'sb-gauge-sub', text: opts.sub || `${min}–${max} ${opts.unit || ''}`.trim() }),
    ]);
  }

  // Multi-channel SVG time-series trend oscilloscope (zero innerHTML)
  function buildTimeSeriesChart(el, points, traces) {
    const w = 680, h = 170, padL = 44, padR = 14, padT = 12, padB = 22;
    const svg = svgEl('svg', { class: 'sb-chart-svg', viewBox: `0 0 ${w} ${h}`, preserveAspectRatio: 'none' });

    if (!points || points.length < 2) {
      svg.appendChild(svgEl('text', { x: w / 2, y: h / 2, 'text-anchor': 'middle', fill: '#64748b', 'font-size': 12, 'font-family': 'monospace' }, 'Click "Start Live Stream" or "Quick 30s Road-Test Sim" to plot real-time sensor waveforms'));
      return svg;
    }

    for (let g = 0; g <= 4; g++) {
      const gy = padT + ((h - padT - padB) * g) / 4;
      svg.appendChild(svgEl('line', { x1: padL, y1: gy, x2: w - padR, y2: gy, stroke: 'rgba(148,163,184,0.12)', 'stroke-width': 1 }));
    }

    const tMin = points[0].t;
    const tMax = Math.max(tMin + 1, points[points.length - 1].t);

    traces.forEach((tr, tidx) => {
      const vals = points.map((p) => p[tr.key]).filter((v) => v != null && !Number.isNaN(Number(v))).map(Number);
      if (vals.length < 2) return;
      let yMin = tr.min != null ? tr.min : Math.min(...vals);
      let yMax = tr.max != null ? tr.max : Math.max(...vals);
      if (Math.abs(yMax - yMin) < 0.01) { yMin -= 1; yMax += 1; }
      const padSpan = (yMax - yMin) * 0.08;
      yMin -= padSpan;
      yMax += padSpan;

      const coords = [];
      points.forEach((p) => {
        const v = p[tr.key];
        if (v == null || Number.isNaN(Number(v))) return;
        const x = padL + ((p.t - tMin) / (tMax - tMin)) * (w - padL - padR);
        const y = h - padB - ((Number(v) - yMin) / (yMax - yMin)) * (h - padT - padB);
        coords.push(`${x.toFixed(1)},${y.toFixed(1)}`);
      });
      if (coords.length >= 2) {
        svg.appendChild(svgEl('polyline', {
          points: coords.join(' '), fill: 'none', stroke: tr.color, 'stroke-width': 2.2, 'stroke-linejoin': 'round',
        }));
      }
      if (tidx === 0) {
        svg.appendChild(svgEl('text', { x: 4, y: padT + 9, fill: tr.color, 'font-size': 9, 'font-family': 'monospace' }, yMax.toFixed(1)));
        svg.appendChild(svgEl('text', { x: 4, y: h - padB, fill: tr.color, 'font-size': 9, 'font-family': 'monospace' }, yMin.toFixed(1)));
      }
    });

    function fmtSpan(sec) {
      const s = Math.max(0, Math.round(sec));
      if (s < 60) return `${s}s`;
      const m = Math.floor(s / 60);
      const r = s % 60;
      return `${m}m ${r}s`;
    }
    const spanSec = tMax - tMin;
    svg.appendChild(svgEl('text', { x: padL, y: h - 5, fill: '#64748b', 'font-size': 9, 'font-family': 'monospace' }, fmtSpan(tMin)));
    if (spanSec >= 4) {
      svg.appendChild(svgEl('text', { x: (padL + (w - padR)) / 2, y: h - 5, 'text-anchor': 'middle', fill: '#64748b', 'font-size': 9, 'font-family': 'monospace' }, fmtSpan(tMin + spanSec / 2)));
    }
    svg.appendChild(svgEl('text', { x: w - padR, y: h - 5, 'text-anchor': 'end', fill: '#64748b', 'font-size': 9, 'font-family': 'monospace' }, fmtSpan(tMax)));
    return svg;
  }

  function mount(host, ctx) {
    const { el } = ctx;
    const agentName = (typeof ctx.getAgentName === 'function' ? ctx.getAgentName() : ctx.agentName) || 'Agent';

    const state = {
      assets: [],
      active: null,
      activeId: null,
      tab: 'scantool',
      transport: 'sim',
      simProfile: 'vacuum_leak_drive',
      portOrAddr: 'tcp://host.docker.internal:35000',
      chartGroup: 'fuel_rpm',
      streaming: false,
      streamTimer: null,
      streamGen: 0,
      streamStartTs: null,
      lastTickSec: 0,
      collectorActive: false,
      lastDrainedId: 0,
      fastFrameMode: false,
      decodedVehicleName: '',
      tickInFlight: false,
      streamErrCount: 0,
      tickIdx: 0,
      triageSymptom: '',
      triageDtcs: '',
      triageUserEdited: false,
      activeRunId: null,
      livePoints: [],
      latestTick: null,
      latestScan: null,
      analysis: null,
      canAnalysis: null,
      adapters: null,
      bleProbe: null,
      pidCatalog: [],
      customPids: [],
      dbcProfiles: [],
      clearBufferOnStart: false,
      canFilter: '',
      canDumpText: '',
      dbcName: '',
      dbcText: '',
      dbcInitialized: false,
      selectedGauges: ['rpm', 'battery_v', 'coolant_c', 'total_ft_b1', 'load_pct', 'o2_b1s1_v'],
      pickerOpen: false,
      uploadedPhotoPath: '',
      uploadedCsvPath: '',
      bridgeCom: 'COM5',
      bridgePort: '35000',
      bridgeBind: '127.0.0.1',
      bridgeCustomBind: '',
      bridgeTargetHost: '',
      bridgeStatus: null,
      bridgeDetectedPorts: [],
      bridgeBusy: false,
    };

    const wrap = el('div', { className: 'sb-wrap' });
    const statusEl = el('div', { className: 'sb-status' });
    const railList = el('div', { style: 'display:flex;flex-direction:column;gap:6px;' });
    const mainHost = el('div', { style: 'display:flex;flex-direction:column;gap:10px;' });
    const addAssetBox = el('div', { style: 'display:none;flex-direction:column;gap:6px;padding-top:8px;border-top:1px solid rgba(148,163,184,0.16);' });

    function setStatus(msg) {
      statusEl.textContent = msg || '';
      statusEl.style.display = msg ? 'block' : 'none';
    }

    const liveSlots = {
      gaugesHost: null,
      dtcHost: null,
      chartTitleEl: null,
      chartSvgHost: null,
      chartPresets: {
        fuel_rpm: [
          { key: 'stft_b1', label: 'STFT B1 (%)', color: '#38bdf8' },
          { key: 'ltft_b1', label: 'LTFT B1 (%)', color: '#a78bfa' },
          { key: 'rpm', label: 'RPM (scaled)', color: '#10b981' },
        ],
        voltage: [
          { key: 'battery_v', label: 'Battery / ECU Voltage (V)', color: '#f59e0b', min: 10.5, max: 15.0 },
          { key: 'load_pct', label: 'Engine Load (%)', color: '#38bdf8', min: 0, max: 100 },
        ],
        o2_cat: [
          { key: 'o2_b1s1_v', label: 'Upstream O2 B1S1 (V)', color: '#38bdf8', min: 0, max: 1.0 },
          { key: 'o2_b1s2_v', label: 'Downstream O2 B1S2 (V)', color: '#ef4444', min: 0, max: 1.0 },
        ],
        thermal: [
          { key: 'coolant_c', label: 'Coolant (°C)', color: '#ef4444' },
          { key: 'timing_deg', label: 'Timing Adv (°)', color: '#10b981' },
          { key: 'knock_deg', label: 'Knock Retard (°)', color: '#f59e0b' },
        ],
      },
    };

    function formatLiveChartTitle() {
      const n = state.livePoints.length;
      if (n < 2) return `Live Time-Series Oscilloscope (${n}/600 buffered)`;
      const span = Math.max(0, state.livePoints[n - 1].t - state.livePoints[0].t);
      const avg = span / Math.max(1, n - 1);
      return `Live Time-Series Oscilloscope (${n}/600 buffered · ${Math.round(span)}s span · ~${avg.toFixed(1)}s/sample)`;
    }

    function buildDtcCardEl() {
      const activeDtcObjs = (state.latestScan && state.latestScan.dtcs)
        || (state.latestTick && state.latestTick.dtcs)
        || [];
      if (!activeDtcObjs.length) return null;
      return el('div', { className: 'sb-card' }, [
        el('div', { className: 'sb-head' }, [
          el('div', { className: 'sb-title', text: `Active & Pending Trouble Codes (${activeDtcObjs.length})` }),
          el('button', {
            type: 'button', className: 'sb-btn sb-btn-amber',
            text: `Diagnose Codes with ${agentName} Copilot →`,
            on: { click: () => { state.tab = 'triage'; renderMain(); } },
          }),
        ]),
        ...activeDtcObjs.map((d) => el('div', { className: 'sb-dtc-card' + (d.severity === 'medium' || d.severity === 'low' ? ' med' : '') }, [
          el('div', { className: 'sb-head' }, [
            el('span', { style: 'font-size:13px;font-weight:700;color:#f8fafc;', text: `${d.code} — ${d.title || 'Diagnostic Trouble Code'}` }),
            el('span', { className: `sb-badge ${d.severity === 'critical' ? 'crit' : 'warn'}`, text: `${d.state || 'active'} · ${d.system || 'ECU'}` }),
          ]),
          el('div', { style: 'font-size:11px;color:#fde68a;', text: `Driveability: ${driveSafetyHint(d)}` }),
          el('div', { className: 'sb-meta', style: 'color:#cbd5e1;', text: `Common causes: ${(d.causes || []).join(' · ') || 'Inspect wiring & live PIDs'}` }),
        ])),
      ]);
    }

    // In-place live telemetry updater: updates ONLY gauges, DTC card & chart SVG without rebuilding mainHost (zero focus loss!)
    function updateLiveTelemetryView() {
      if (state.tab !== 'scantool') return;
      const tick = state.latestTick || {};
      const an = state.analysis;
      if (liveSlots.gaugesHost && liveSlots.gaugesHost.isConnected) {
        liveSlots.gaugesHost.replaceChildren(...state.selectedGauges.map((k) => buildGaugeForKey(k, tick, an)));
      }
      if (liveSlots.dtcHost && liveSlots.dtcHost.isConnected) {
        const card = buildDtcCardEl();
        if (card) liveSlots.dtcHost.replaceChildren(card);
        else liveSlots.dtcHost.replaceChildren();
      }
      if (liveSlots.chartTitleEl && liveSlots.chartTitleEl.isConnected) {
        liveSlots.chartTitleEl.textContent = formatLiveChartTitle();
      }
      if (liveSlots.chartSvgHost && liveSlots.chartSvgHost.isConnected) {
        const curTraces = liveSlots.chartPresets[state.chartGroup] || liveSlots.chartPresets.fuel_rpm;
        liveSlots.chartSvgHost.replaceChildren(buildTimeSeriesChart(el, state.livePoints, curTraces));
      }
    }

    function stopStream() {
      state.streamGen += 1;
      if (state.streamTimer) {
        clearTimeout(state.streamTimer);
        state.streamTimer = null;
      }
      state.streaming = false;
      state.collectorActive = false;
      state.tickInFlight = false;
      state.streamErrCount = 0;
    }

    function scheduleNextTick(gen) {
      if (!state.streaming || state.streamGen !== gen) return;
      const delayMs = state.collectorActive ? 600 : (state.transport === 'sim' ? 1000 : 150);
      state.streamTimer = setTimeout(() => {
        state.streamTimer = null;
        if (state.collectorActive) doDrainTick(gen);
        else doOneTick(gen);
      }, delayMs);
    }

    async function doDrainTick(gen) {
      if (!state.streaming || state.streamGen !== gen || state.tickInFlight) return;
      state.tickInFlight = true;
      try {
        const res = await ctx.action('telemetry_drain', {
          run_id: state.activeRunId,
          asset_id: state.activeId,
          after_id: state.lastDrainedId,
        });
        if (!state.streaming || state.streamGen !== gen) return;
        if (!res || res.status !== 'ok') {
          state.streamErrCount += 1;
          if (state.streamErrCount >= 5) {
            setStatus(`Drain paused after errors: ${(res && res.message) || 'collector error'}`);
            stopStream();
            if (state.tab === 'scantool') renderMain();
            return;
          }
          scheduleNextTick(gen);
          return;
        }
        state.streamErrCount = 0;
        if (res.catalog && res.catalog.length) state.pidCatalog = res.catalog;
        if (res.last_id && res.last_id > state.lastDrainedId) state.lastDrainedId = res.last_id;
        state.fastFrameMode = Boolean(res.fast_frame_mode);
        if (res.vehicle_decoded && res.vehicle_decoded.display_name) {
          state.decodedVehicleName = res.vehicle_decoded.display_name;
        }
        const rows = Array.isArray(res.samples) ? res.samples : [];
        rows.forEach((s) => {
          state.latestTick = mergeTick(state.latestTick, s);
          const sampleTs = (s.ts != null && !Number.isNaN(Number(s.ts))) ? Number(s.ts) : (Date.now() / 1000);
          if (state.streamStartTs == null) state.streamStartTs = sampleTs;
          const tRel = Math.max(0, round1(sampleTs - state.streamStartTs));
          const prevPt = state.livePoints.length ? state.livePoints[state.livePoints.length - 1] : null;
          if (prevPt) state.lastTickSec = Math.max(0.05, round1(tRel - prevPt.t));
          state.livePoints.push({ ...state.latestTick, t: tRel });
          if (state.livePoints.length > 600) state.livePoints.shift();
        });
        const bufCnt = res.buffered_samples || state.livePoints.length;
        const chCnt = res.total_channels_captured || Object.keys(state.latestTick || {}).length;
        const rateVal = res.avg_interval_sec || state.lastTickSec;
        const rateTxt = rateVal > 0 ? ` · ${Number(rateVal).toFixed(2)}s/sample` : '';
        const modeTxt = state.fastFrameMode ? ' [fast-frame]' : '';
        const vehTxt = state.decodedVehicleName ? ` · ${state.decodedVehicleName}` : '';
        if (res.collector_state === 'paused') {
          setStatus(`Run #${state.activeRunId} collector yielded to Copilot scan · Buffered ${bufCnt}/600${vehTxt}`);
        } else if (!res.collector_alive && res.collector_state === 'error') {
          setStatus(`Collector error: ${res.collector_error || 'lost adapter link'}`);
          stopStream();
          if (state.tab === 'scantool') renderMain();
          return;
        } else {
          setStatus(`Buffer & Drain Run #${state.activeRunId} (${state.transport}${modeTxt}) · Buffered ${bufCnt}/600${rateTxt} · ${chCnt} ECU channels${vehTxt}`);
        }
        if (rows.length) updateLiveTelemetryView();
        scheduleNextTick(gen);
      } finally {
        state.tickInFlight = false;
      }
    }

    async function doOneTick(gen) {
      if (!state.streaming || state.streamGen !== gen || state.tickInFlight) return;
      state.tickInFlight = true;
      try {
        const res = await ctx.action('telemetry_tick', {
          run_id: state.activeRunId,
          asset_id: state.activeId,
          transport: state.transport,
          port_or_addr: state.portOrAddr,
          sim_profile: state.simProfile,
          tick_index: state.tickIdx++,
          selected_pids: state.selectedGauges,
        });
        if (!state.streaming || state.streamGen !== gen) return;
        if (!res || res.status !== 'ok') {
          state.streamErrCount += 1;
          const errMsg = (res && res.message) || 'Telemetry poll timeout';
          if (state.streamErrCount >= 3) {
            setStatus(`Stream paused after 3 errors: ${errMsg}`);
            stopStream();
            if (state.tab === 'scantool') renderMain();
            return;
          }
          setStatus(`Transient poll retry (${state.streamErrCount}/3): ${errMsg}`);
          scheduleNextTick(gen);
          return;
        }
        state.streamErrCount = 0;
        if (res.catalog && res.catalog.length) {
          state.pidCatalog = res.catalog;
        }
        state.latestTick = mergeTick(state.latestTick, res);
        const sampleTs = (res.ts != null && !Number.isNaN(Number(res.ts))) ? Number(res.ts) : (Date.now() / 1000);
        if (state.streamStartTs == null) state.streamStartTs = sampleTs;
        const tRel = Math.max(0, round1(sampleTs - state.streamStartTs));
        const prevPt = state.livePoints.length ? state.livePoints[state.livePoints.length - 1] : null;
        state.lastTickSec = prevPt ? Math.max(0.1, round1(tRel - prevPt.t)) : 0;
        state.livePoints.push({ ...state.latestTick, t: tRel });
        if (state.livePoints.length > 600) state.livePoints.shift();
        const bufCnt = res.buffered_samples || state.livePoints.length;
        const chCnt = res.total_channels_captured || Object.keys(state.latestTick).length;
        const rateTxt = state.lastTickSec > 0 ? ` · ${state.lastTickSec.toFixed(1)}s/sample` : '';
        setStatus(`Recording Run #${state.activeRunId} (${state.transport}) · Buffered ${bufCnt}/600 samples${rateTxt} · ${chCnt} rich ECU channels captured for ${agentName} Copilot.`);
        // Update gauges/chart IN-PLACE (never rebuilds mainHost, never steals focus from Copilot chat or inputs!)
        updateLiveTelemetryView();
        scheduleNextTick(gen);
      } finally {
        state.tickInFlight = false;
      }
    }

    function mergeTick(prev, next) {
      const merged = Object.assign({}, prev || {});
      delete merged.t;
      Object.entries(next || {}).forEach(([k, v]) => {
        if (k === 't') return;
        if (k === 'dtcs') {
          if (next.dtcs_checked || (Array.isArray(v) && v.length > 0)) {
            merged.dtcs = v;
          }
          return;
        }
        if (v !== null && v !== undefined && v !== '') {
          merged[k] = v;
        }
      });
      return merged;
    }

    async function loadPidCatalog() {
      const cat = await ctx.action('pid_catalog', state.activeId ? { asset_id: state.activeId } : {});
      if (cat && cat.status === 'ok') {
        state.pidCatalog = cat.standard_pids || [];
        state.customPids = cat.custom_pids || [];
        if (cat.gauge_prefs && cat.gauge_prefs.length) state.selectedGauges = cat.gauge_prefs;
      }
    }

    function effectiveBridgeBind() {
      if (state.bridgeBind === 'custom') return (state.bridgeCustomBind || '0.0.0.0').trim();
      return state.bridgeBind || '127.0.0.1';
    }

    function effectiveBridgeHost(bEnv) {
      const explicit = (state.bridgeTargetHost || '').trim();
      if (explicit) return explicit;
      const b = effectiveBridgeBind();
      if (b !== '0.0.0.0' && b !== '127.0.0.1') return b;
      return (bEnv && bEnv.recommended_host) || '127.0.0.1';
    }

    async function loadBridgeStatus(probeTcp) {
      const res = await ctx.action('bt_bridge_status', {
        com_port: state.bridgeCom,
        tcp_port: Number(state.bridgePort || 35000),
        bind_address: effectiveBridgeBind(),
        target_host: state.bridgeTargetHost || undefined,
        probe_tcp: Boolean(probeTcp),
      });
      if (res && res.status === 'ok') {
        state.bridgeStatus = res;
        if (res.com_port) state.bridgeCom = res.com_port;
        if (res.tcp_port) state.bridgePort = String(res.tcp_port);
        if (res.bind_address) {
          if (res.bind_address === '127.0.0.1' || res.bind_address === '0.0.0.0') {
            state.bridgeBind = res.bind_address;
          } else {
            state.bridgeBind = 'custom';
            state.bridgeCustomBind = res.bind_address;
          }
        }
        if (res.target_host && !state.bridgeTargetHost) state.bridgeTargetHost = res.target_host;
        if (res.tcp_url && !state.portOrAddr) state.portOrAddr = res.tcp_url;
      }
    }

    async function exportDiagnosticReport(fmt) {
      setStatus(`Generating ${fmt.toUpperCase()} diagnostic & AI Copilot report...`);
      const res = await ctx.action('export_report', {
        asset_id: state.activeId,
        run_id: state.activeRunId,
        format: fmt,
        save_file: true,
      });
      if (!res || res.status !== 'ok') {
        setStatus((res && res.message) || 'Report export failed');
        return;
      }
      triggerBlobDownload(res.filename, res.mime_type, res.content);
      setStatus(`Exported ${res.filename} (${res.size_bytes} bytes) — also saved to ${res.file_path}`);
    }

    function buildExportButtons() {
      return el('div', { className: 'sb-row' }, [
        el('button', {
          type: 'button', className: 'sb-btn sb-btn-primary', text: '⬇ Export Report (.md)',
          on: { click: () => exportDiagnosticReport('markdown') },
        }),
        el('button', {
          type: 'button', className: 'sb-btn', text: '⬇ Print HTML (.html)',
          on: { click: () => exportDiagnosticReport('html') },
        }),
        el('button', {
          type: 'button', className: 'sb-btn', text: '⬇ JSON (.json)',
          on: { click: () => exportDiagnosticReport('json') },
        }),
      ]);
    }

    async function reload(selectId) {
      const res = await ctx.action('get_state', selectId ? { asset_id: selectId } : (state.activeId ? { asset_id: state.activeId } : {}));
      if (!res || res.status !== 'ok') {
        setStatus((res && res.message) || 'Failed to load Shop Bench state');
        return;
      }
      state.assets = res.assets || [];
      state.active = res.active || null;
      state.activeId = state.active ? state.active.asset.id : null;
      if (state.active) {
        state.customPids = state.active.custom_pids || [];
        state.dbcProfiles = state.active.dbc_profiles || [];
        if (!state.dbcInitialized) {
          if (state.dbcProfiles.length && state.dbcProfiles[0].dbc_text) {
            state.dbcName = state.dbcProfiles[0].name || '';
            state.dbcText = state.dbcProfiles[0].dbc_text;
          } else {
            state.dbcText = SAMPLE_DBC;
          }
          state.dbcInitialized = true;
        }
        if (state.active.gauge_prefs && state.active.gauge_prefs.length) state.selectedGauges = state.active.gauge_prefs;
        if (state.active.telemetry_runs && state.active.telemetry_runs.length && !state.analysis && !state.streaming) {
          const latestRunId = state.active.telemetry_runs[0].id;
          const an = await ctx.action('telemetry_analyze', { run_id: latestRunId, asset_id: state.activeId });
          if (an && an.status === 'ok' && !state.streaming) {
            state.analysis = an;
            state.activeRunId = latestRunId;
            state.livePoints = an.plot_series || [];
            if (state.livePoints.length) {
              const lastPt = Object.assign({}, state.livePoints[state.livePoints.length - 1]);
              delete lastPt.t;
              state.latestTick = lastPt;
            }
          }
        } else if ((!state.active.telemetry_runs || !state.active.telemetry_runs.length) && !state.streaming) {
          state.analysis = null;
          state.activeRunId = null;
          state.livePoints = [];
          state.latestTick = null;
        }
      }
      if (!state.pidCatalog.length) await loadPidCatalog();
      if (!state.bridgeStatus) await loadBridgeStatus(false);
      renderRail();
      renderMain();
    }

    // Add Asset Form
    const aName = el('input', { className: 'sb-input', placeholder: 'Name (e.g. 2018 Silverado 5.3L)' });
    const aCat = el('select', { className: 'sb-select' }, [
      el('option', { value: 'vehicle', text: 'Vehicle' }),
      el('option', { value: 'small_engine', text: 'Small Engine' }),
      el('option', { value: 'hvac', text: 'HVAC / Home System' }),
      el('option', { value: 'generator', text: 'Generator' }),
      el('option', { value: 'tool', text: 'Shop Tool' }),
      el('option', { value: 'appliance', text: 'Appliance' }),
      el('option', { value: 'marine', text: 'Marine' }),
    ]);
    const aYear = el('input', { className: 'sb-input', placeholder: 'Year' });
    const aMake = el('input', { className: 'sb-input', placeholder: 'Make' });
    const aModel = el('input', { className: 'sb-input', placeholder: 'Model' });
    const aVin = el('input', { className: 'sb-input', placeholder: 'VIN / Serial' });
    const aMeter = el('input', { className: 'sb-input', type: 'number', placeholder: 'Miles / Hours' });
    const aUnit = el('select', { className: 'sb-select' }, [
      el('option', { value: 'mi', text: 'mi' }),
      el('option', { value: 'km', text: 'km' }),
      el('option', { value: 'hrs', text: 'hrs' }),
    ]);

    addAssetBox.append(
      aName, aCat,
      el('div', { className: 'sb-grid-2' }, [aYear, aMake]),
      el('div', { className: 'sb-grid-2' }, [aModel, aVin]),
      el('div', { className: 'sb-grid-2' }, [aMeter, aUnit]),
      el('button', {
        type: 'button',
        className: 'sb-btn sb-btn-primary',
        text: 'Save Vehicle / Asset',
        on: {
          click: async () => {
            if (!aName.value.trim()) { setStatus('Asset name is required.'); return; }
            const res = await ctx.action('save_asset', {
              name: aName.value.trim(), category: aCat.value, year: aYear.value.trim(),
              make: aMake.value.trim(), model: aModel.value.trim(), vin_or_serial: aVin.value.trim(),
              meter_value: Number(aMeter.value || 0), meter_unit: aUnit.value,
            });
            if (res && res.status === 'ok') {
              aName.value = aYear.value = aMake.value = aModel.value = aVin.value = aMeter.value = '';
              addAssetBox.style.display = 'none';
              setStatus('Vehicle/asset saved.');
              await reload(res.asset.id);
            }
          },
        },
      })
    );

    function renderRail() {
      railList.replaceChildren();
      if (!state.assets.length) {
        railList.append(
          el('div', { className: 'sb-meta', text: 'No vehicles or equipment yet.' }),
          el('button', {
            type: 'button',
            className: 'sb-btn sb-btn-primary',
            text: 'Load Sample Fleet',
            on: {
              click: async () => {
                await ctx.action('seed_demo', {});
                setStatus('Sample fleet loaded into offline SQLite ledger.');
                await reload();
              },
            },
          })
        );
        return;
      }
      state.assets.forEach((a) => {
        railList.appendChild(el('button', {
          type: 'button',
          className: 'sb-asset-item' + (state.activeId === a.id ? ' active' : ''),
          on: {
            click: () => {
              stopStream();
              state.analysis = null;
              state.livePoints = [];
              state.latestTick = null;
              state.canAnalysis = null;
              state.dbcInitialized = false;
              reload(a.id);
            },
          },
        }, [
          el('div', { style: 'font-weight:700;color:#f8fafc;', text: a.name }),
          el('div', { className: 'sb-meta', text: `${a.year || ''} ${a.make || ''} ${a.model || ''} · ${a.category}`.trim() }),
          el('div', { className: 'sb-meta', text: `${a.meter_value || 0} ${a.meter_unit || 'mi'}` }),
        ]));
      });
    }

    // ── PRO SCAN-TOOL TOP STATUS BAR (MIL LAMP · READINESS · QUICK ACTIONS) ──
    function buildScanToolHeader() {
      const a = state.active ? state.active.asset : null;
      const dtcs = (state.latestScan && state.latestScan.dtcs)
        || (state.latestTick && state.latestTick.dtcs)
        || ((state.analysis && state.analysis.active_dtcs) ? state.analysis.active_dtcs.map((c) => ({ code: c })) : []);
      const milOn = dtcs.length > 0;
      const readiness = (state.latestScan && state.latestScan.readiness)
        || (state.latestTick && state.latestTick.readiness)
        || { misfire: 'READY', fuel: 'READY', catalyst: 'READY', evap: 'READY', o2_sensor: 'READY' };
      const battV = (state.latestTick && state.latestTick.battery_v) || (state.latestScan && state.latestScan.battery_v) || null;

      const bar = el('div', { className: 'sb-scantool-bar' }, [
        el('div', { style: 'display:flex;flex-direction:column;gap:3px;' }, [
          el('div', { className: 'sb-row' }, [
            el('span', { className: 'sb-mil-lamp' + (milOn ? ' mil-on' : ''), text: milOn ? `⚠ CHECK ENGINE (${dtcs.length} CODES)` : '✓ MIL OFF · NO ACTIVE CODES' }),
            el('span', { className: `sb-badge ${battV && battV < 12.2 ? 'crit' : 'ok'}`, text: battV ? `BATT ${Number(battV).toFixed(2)}V` : 'BATT —' }),
            el('span', { className: 'sb-badge', text: state.transport === 'tcp' ? 'BT-BRIDGE / TCP' : (state.transport === 'serial' ? 'USB/BT SERIAL' : (state.transport === 'ble' ? 'BLE GATT' : 'SIMULATOR')) }),
          ]),
          el('div', { className: 'sb-meta', text: a ? `${a.year || ''} ${a.make || ''} ${a.model || ''} · ${a.name} · VIN: ${a.vin_or_serial || '—'}` : 'Select or add your vehicle on the left rail' }),
        ]),
        el('div', { className: 'sb-readiness-strip', title: 'OBD-II Emissions Readiness Monitors' }, [
          el('span', { className: 'sb-meta', text: 'MONITORS:' }),
          ...[['MIS', readiness.misfire], ['FUEL', readiness.fuel], ['CAT', readiness.catalyst], ['EVAP', readiness.evap], ['O2', readiness.o2_sensor]].map(([lbl, st]) => el('span', {
            className: 'sb-mon-chip' + (st && st !== 'READY' ? ' inc' : ''),
            text: `${lbl}:${st === 'READY' ? 'OK' : 'INC'}`,
          })),
        ]),
      ]);

      const quickBar = el('div', { className: 'sb-quick-actions' }, [
        el('button', {
          type: 'button', className: 'sb-action-tile',
          on: {
            click: async () => {
              state.tab = 'scantool';
              setStatus(`Running Full Health Scan (${state.transport})...`);
              const res = await ctx.action('obd_scan', {
                transport: state.transport, port_or_addr: state.portOrAddr,
                sim_profile: state.simProfile === 'healthy_drive_cycle' ? 'healthy' : (state.simProfile === 'alternator_ripple_brownout' ? 'can_bus_fault' : 'lean_misfire'),
                asset_id: state.activeId, save_session: true,
              });
              if (res && res.status === 'ok') {
                state.latestScan = res;
                const flatPids = {
                  battery_v: res.battery_v || null,
                  vin: res.vin || '',
                  protocol: res.protocol || '',
                  dtcs: res.dtcs || [],
                  dtcs_checked: true,
                };
                Object.entries(res.pids || {}).forEach(([pk, pv]) => {
                  if (pv && pv.value != null) flatPids[pk] = pv.value;
                });
                state.latestTick = mergeTick(state.latestTick, flatPids);
                const pidCnt = Object.keys(res.pids || {}).length;
                setStatus(`Health Scan complete: ${(res.dtcs || []).length} DTCs · ${pidCnt} PIDs · Battery ${res.battery_v || '—'}V${res.vin ? ` · VIN ${res.vin}` : ''}.`);
              } else {
                setStatus((res && res.message) || 'Scan failed');
              }
              renderMain();
            },
          },
        }, [
          el('div', { style: 'font-weight:700;color:#38bdf8;', text: '1. Full Health & Code Scan' }),
          el('div', { className: 'sb-meta', text: 'Read Check Engine codes, plain-English meanings & emissions readiness' }),
        ]),
        el('button', {
          type: 'button', className: 'sb-action-tile',
          on: {
            click: async () => {
              state.tab = 'scantool';
              stopStream();
              setStatus(`Recording 30-sample road test (${state.simProfile}) + CAN stream & running signal analytics...`);
              const anRes = await ctx.action('telemetry_record_burst', {
                asset_id: state.activeId, sim_profile: state.simProfile, count: 30, include_can: true,
              });
              if (anRes && anRes.status === 'ok') {
                state.analysis = anRes;
                state.activeRunId = anRes.run ? anRes.run.id : null;
                state.livePoints = anRes.plot_series || [];
                if (state.livePoints.length) state.latestTick = state.livePoints[state.livePoints.length - 1];
                const canObs = await ctx.action('can_observe', { asset_id: state.activeId });
                if (canObs && canObs.status === 'ok') state.canAnalysis = canObs;
                setStatus(`Recorded Run #${state.activeRunId}: ${(anRes.signatures || []).length} automated fault signature(s) found.`);
              }
              renderMain();
            },
          },
        }, [
          el('div', { style: 'font-weight:700;color:#6ee7b7;', text: '2. Quick 30s Road-Test & Signal Lab' }),
          el('div', { className: 'sb-meta', text: 'Capture time-series trends, voltage ripple, O2 waveforms & correlations' }),
        ]),
        el('button', {
          type: 'button', className: 'sb-action-tile',
          on: { click: () => { state.tab = 'can'; renderMain(); } },
        }, [
          el('div', { style: 'font-weight:700;color:#a78bfa;', text: '3. CAN-Bus & PID Databases' }),
          el('div', { className: 'sb-meta', text: 'Sniff CAN via ELM327/SocketCAN, import Torque CSV, OBDb JSON & opendbc' }),
        ]),
        el('button', {
          type: 'button', className: 'sb-action-tile copilot',
          on: { click: () => { state.tab = 'triage'; renderMain(); } },
        }, [
          el('div', { style: 'font-weight:700;color:#fde68a;', text: `4. Ask ${agentName} Mechanic Copilot` }),
          el('div', { className: 'sb-meta', text: 'Plain-English root-cause diagnosis from live data, codes, voltage & CAN' }),
        ]),
      ]);

      return [bar, quickBar];
    }

    // Helper: Build gauge spec for any selected standard or custom PID key
    function buildGaugeForKey(key, tick, an) {
      if (key === 'total_ft_b1') {
        const totalFt = (tick.stft_b1 != null && tick.ltft_b1 != null) ? round1(Number(tick.stft_b1) + Number(tick.ltft_b1)) : null;
        const ftState = totalFt == null ? '' : (Math.abs(totalFt) >= 15 ? 'crit' : (Math.abs(totalFt) >= 10 ? 'warn' : ''));
        return buildRadialGauge(el, {
          label: 'Total Fuel Trim B1', value: totalFt, min: -25, max: 25, unit: '%',
          format: (v) => (v >= 0 ? `+${v.toFixed(1)}` : v.toFixed(1)),
          state: ftState, color: '#a78bfa',
          sub: `STFT ${tick.stft_b1 ?? '—'}% / LTFT ${tick.ltft_b1 ?? '—'}%`,
        });
      }
      if (key === 'battery_v') {
        const vVal = tick.battery_v != null ? Number(tick.battery_v) : null;
        const vState = vVal == null ? '' : (vVal < 12.2 || vVal > 14.9 ? 'crit' : (vVal < 13.0 ? 'warn' : ''));
        return buildRadialGauge(el, {
          label: 'Battery / ECU', value: vVal, min: 10, max: 15.5, unit: 'V',
          format: (v) => v.toFixed(2), state: vState, color: '#10b981',
          sub: an && an.voltage_health && an.voltage_health.ripple_vpp != null ? `Ripple: ${an.voltage_health.ripple_vpp} Vpp` : 'Normal 13.6–14.5V',
        });
      }
      if (key === 'coolant_c') {
        const cVal = tick.coolant_c != null ? Number(tick.coolant_c) : null;
        const cState = cVal == null ? '' : (cVal >= 106 ? 'crit' : (cVal >= 99 ? 'warn' : ''));
        return buildRadialGauge(el, {
          label: 'Coolant Temp', value: cVal, min: 40, max: 125, unit: '°C',
          format: (v) => v.toFixed(0), state: cState, color: '#38bdf8',
          sub: cVal != null ? `${(cVal * 9 / 5 + 32).toFixed(0)} °F` : 'Warm 85–98°C',
        });
      }
      if (key === 'o2_b1s1_v') {
        return buildRadialGauge(el, {
          label: 'O2 B1S1 Sensor', value: tick.o2_b1s1_v, min: 0, max: 1.0, unit: 'V',
          format: (v) => v.toFixed(2), color: '#f59e0b',
          sub: `B1S2: ${tick.o2_b1s2_v != null ? Number(tick.o2_b1s2_v).toFixed(2) + 'V' : '—'}`,
        });
      }
      const meta = state.pidCatalog.find((p) => p.key === key);
      if (meta) {
        return buildRadialGauge(el, {
          label: meta.label, value: tick[key], min: meta.min, max: meta.max, unit: meta.unit,
          format: (v) => (Math.abs(v) >= 100 ? String(Math.round(v)) : v.toFixed(1)),
          color: '#38bdf8', sub: `PID 01${meta.pid} · ${meta.category}`,
        });
      }
      const cpid = state.customPids.find((p) => `custom:${p.short_name}` === key);
      if (cpid) {
        return buildRadialGauge(el, {
          label: cpid.name, value: tick[key], min: cpid.min_val, max: cpid.max_val, unit: cpid.unit,
          format: (v) => v.toFixed(1), color: '#a78bfa', sub: `HDR ${cpid.header} · ${cpid.cmd}`,
        });
      }
      return buildRadialGauge(el, { label: key, value: tick[key], min: 0, max: 100, unit: '' });
    }

    // ── TAB 1: PRO CODE READER, LIVE TORQUE GAUGES & SIGNAL CORRELATION LAB ──
    function renderScanToolTab(container) {
      const tick = state.latestTick || {};
      const an = state.analysis;

      const transportSel = el('select', { className: 'sb-select', style: 'max-width:260px;' }, [
        el('option', { value: 'sim', text: 'Dynamic Drive Simulator (Offline)' }),
        el('option', { value: 'tcp', text: 'Windows BT-Bridge / Wi-Fi TCP (Carly & WSL2/Rancher)' }),
        el('option', { value: 'serial', text: 'Linux USB / Classic BT Serial (/dev/ttyUSB0, /dev/rfcomm0)' }),
        el('option', { value: 'ble', text: 'Native Linux BLE GATT (bleak)' }),
      ]);
      transportSel.value = state.transport;
      transportSel.addEventListener('change', () => { state.transport = transportSel.value; renderMain(); });

      const simSel = el('select', { className: 'sb-select', style: 'max-width:280px;' }, [
        el('option', { value: 'vacuum_leak_drive', text: 'Sim: Vacuum Leak + Misfire (P0171/P0302)' }),
        el('option', { value: 'alternator_ripple_brownout', text: 'Sim: Alternator Ripple + CAN Brownout (P0562/U0100)' }),
        el('option', { value: 'catalyst_degraded_o2', text: 'Sim: Catalyst Degraded O2 Mirror (P0420)' }),
        el('option', { value: 'healthy_drive_cycle', text: 'Sim: Healthy Warm Drive Cycle' }),
      ]);
      simSel.value = state.simProfile;
      simSel.addEventListener('change', () => { state.simProfile = simSel.value; });

      const portIn = el('input', {
        className: 'sb-input',
        style: 'max-width:260px;',
        value: state.portOrAddr,
        placeholder: 'tcp://host.docker.internal:35000 or /dev/rfcomm0',
      });
      portIn.addEventListener('input', () => { state.portOrAddr = portIn.value.trim(); });

      const startStopBtn = el('button', {
        type: 'button',
        className: state.streaming ? 'sb-btn sb-btn-danger' : 'sb-btn sb-btn-green',
        text: state.streaming ? '⏹ Stop & Analyze Run' : '▶ Start Live Stream & Log',
        on: {
          click: async () => {
            if (state.streaming) {
              const wasCol = state.collectorActive;
              stopStream();
              if (wasCol) {
                await ctx.action('telemetry_stop_run', {});
              }
              if (state.activeRunId) {
                const anRes = await ctx.action('telemetry_analyze', { run_id: state.activeRunId, asset_id: state.activeId });
                if (anRes && anRes.status === 'ok') state.analysis = anRes;
              }
              setStatus('Live stream stopped & signal analysis updated.');
              await reload(state.activeId);
              return;
            }
            const started = await ctx.action('telemetry_start_run', {
              asset_id: state.activeId, transport: state.transport,
              port_or_addr: state.portOrAddr, sim_profile: state.simProfile,
              selected_pids: state.selectedGauges, use_collector: true,
              clear_buffer_first: state.clearBufferOnStart,
            });
            if (!started || started.status !== 'ok') { setStatus((started && started.message) || 'Could not start run'); return; }
            stopStream();
            state.activeRunId = started.run.id;
            state.analysis = null;
            state.latestTick = null;
            state.livePoints = [];
            state.streamStartTs = null;
            state.lastTickSec = 0;
            state.lastDrainedId = 0;
            state.fastFrameMode = false;
            state.collectorActive = Boolean(started.collector_active);
            state.tickIdx = 0;
            state.streaming = true;
            const gen = state.streamGen;
            setStatus(state.collectorActive
              ? `Starting Buffer & Drain collector for Run #${state.activeRunId} (${state.transport})${state.clearBufferOnStart ? ' [fresh buffer]' : ''} — auto-probing fast-frame speed & VIN...`
              : `Recording Run #${state.activeRunId} (${state.transport})${state.clearBufferOnStart ? ' [fresh buffer]' : ''} — serialized live gauges & 600-sample SQLite buffer active.`);
            renderMain();
            if (state.collectorActive) doDrainTick(gen);
            else doOneTick(gen);
          },
        },
      });

      const clearOnStartChk = el('input', { type: 'checkbox' });
      clearOnStartChk.checked = Boolean(state.clearBufferOnStart);
      clearOnStartChk.addEventListener('change', () => {
        state.clearBufferOnStart = Boolean(clearOnStartChk.checked);
      });

      const connCard = el('div', { className: 'sb-card' }, [
        el('div', { className: 'sb-head' }, [
          el('div', { className: 'sb-title' }, [
            el('span', { className: 'sb-live-dot' + (state.streaming ? ' recording' : '') }),
            'Adapter Connection & Live Recording',
          ]),
          el('div', { className: 'sb-row' }, [
            startStopBtn,
            el('button', {
              type: 'button', className: 'sb-btn', text: 'Discover ECU PIDs (0100–0160)',
              on: {
                click: async () => {
                  setStatus(`Querying ECU supported-PID bitmasks (${state.transport})...`);
                  const pr = await ctx.action('pid_probe_supported', { transport: state.transport, port_or_addr: state.portOrAddr, asset_id: state.activeId });
                  if (pr && pr.status === 'ok') {
                    state.pidCatalog = pr.catalog || [];
                    state.pickerOpen = true;
                    setStatus(`ECU reports ${pr.supported_count} supported SAE J1979 PIDs. Pick instruments below!`);
                    renderMain();
                  } else {
                    setStatus((pr && pr.message) || 'PID discovery failed');
                  }
                },
              },
            }),
            el('button', {
              type: 'button', className: 'sb-btn' + (state.pickerOpen ? ' active' : ''),
              text: `Customize Gauges (${state.selectedGauges.length})`,
              on: { click: () => { state.pickerOpen = !state.pickerOpen; renderMain(); } },
            }),
            el('button', {
              type: 'button', className: 'sb-btn', text: 'Scan Adapters',
              on: {
                click: async () => {
                  state.adapters = await ctx.action('obd_discover', { scan_ble: true });
                  setStatus('Adapter & WSL2/Rancher bridge discovery complete.');
                  renderMain();
                },
              },
            }),
            el('button', {
              type: 'button', className: 'sb-btn', text: 'Probe BLE GATT',
              on: {
                click: async () => {
                  setStatus('Probing BLE GATT services/characteristics...');
                  state.bleProbe = await ctx.action('ble_gatt_probe', { address: state.transport === 'ble' ? state.portOrAddr : '' });
                  setStatus(state.bleProbe.verdict || state.bleProbe.message || 'BLE GATT probe finished.');
                  renderMain();
                },
              },
            }),
          ]),
        ]),
        el('div', { className: 'sb-row' }, [transportSel, state.transport === 'sim' ? simSel : portIn]),
        el('div', { className: 'sb-row', style: 'justify-content:space-between;padding-top:6px;border-top:1px solid rgba(148,163,184,0.12);' }, [
          el('label', { className: 'sb-row', style: 'gap:6px;cursor:pointer;font-size:11px;color:#cbd5e1;' }, [
            clearOnStartChk,
            el('span', { text: 'Fresh buffer on start (clear prior telemetry runs & old codes before recording)' }),
          ]),
          el('div', { className: 'sb-row' }, [
            el('button', {
              type: 'button',
              className: 'sb-btn sb-btn-amber',
              text: '🧹 Clear Sim Data',
              title: 'Remove simulator telemetry runs, simulated OBD sessions, and simulated CAN captures so they never mix with real vehicles',
              on: {
                click: async () => {
                  const res = await ctx.action('clear_telemetry_data', { scope: 'sim' });
                  if (res && res.status === 'ok') {
                    state.analysis = null;
                    setStatus(`Cleared simulated data (${res.deleted_runs || 0} sim runs, ${res.deleted_samples || 0} samples, ${res.deleted_sessions || 0} OBD sessions, ${res.deleted_can_captures || 0} CAN captures).`);
                    await reload(state.activeId);
                  } else {
                    setStatus((res && res.message) || 'Failed to clear simulator data');
                  }
                },
              },
            }),
            el('button', {
              type: 'button',
              className: 'sb-btn sb-btn-danger',
              text: '🧹 Clear Vehicle Buffer',
              title: 'Wipe buffered telemetry runs, OBD sessions, and CAN captures for the active vehicle before switching cars or starting a clean test',
              on: {
                click: async () => {
                  const wasCol = state.collectorActive;
                  stopStream();
                  if (wasCol) await ctx.action('telemetry_stop_run', {});
                  const res = await ctx.action('clear_telemetry_data', { scope: 'buffer', asset_id: state.activeId });
                  if (res && res.status === 'ok') {
                    state.activeRunId = null;
                    state.analysis = null;
                    state.livePoints = [];
                    state.latestTick = null;
                    state.latestScan = null;
                    state.canAnalysis = null;
                    setStatus(`Cleared vehicle buffer (${res.deleted_runs || 0} runs, ${res.deleted_samples || 0} samples, ${res.deleted_sessions || 0} scans, ${res.deleted_can_captures || 0} CAN captures). Ready for a clean session!`);
                    await reload(state.activeId);
                  } else {
                    setStatus((res && res.message) || 'Failed to clear vehicle buffer');
                  }
                },
              },
            }),
          ]),
        ]),
      ]);

      // Windows Bluetooth COM -> TCP Bridge Control Strip (Carly Classic BT / WSL2 George / Rancher Hiram)
      const bs = state.bridgeStatus || {};
      const bEnv = bs.environment || {};
      const bCmds = bs.commands || {};
      const bRunning = Boolean(bs.running);
      const bStateTxt = bRunning ? `RUNNING${bs.pid ? ` (PID ${bs.pid})` : ''}` : (bEnv.in_container ? 'HOST BRIDGE MODE' : 'STOPPED');

      const relBridgeScript = '.george/modules/shop-bench/scripts/win_obd_bridge.ps1';
      const liveWslStartCmd = () => {
        const com = state.bridgeCom || 'COM5';
        const port = state.bridgePort || '35000';
        const bind = effectiveBridgeBind();
        return `powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$(wslpath -w ${relBridgeScript})" -ComPort ${com} -TcpPort ${port} -BindAddress ${bind} -BaudRate 38400`;
      };
      const liveWinStartCmd = () => {
        const com = state.bridgeCom || 'COM5';
        const port = state.bridgePort || '35000';
        const bind = effectiveBridgeBind();
        return `$s=(wsl.exe bash -lc 'wslpath -w ~/projects/blue-lodge/${relBridgeScript}' 2>$null).Trim(); if(-not $s){$s='.\\win_obd_bridge.ps1'}; powershell -NoProfile -ExecutionPolicy Bypass -File $s -ComPort ${com} -TcpPort ${port} -BindAddress ${bind} -BaudRate 38400`;
      };
      const liveStopCmd = () => `powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$(wslpath -w ${relBridgeScript})" -Stop -TcpPort ${state.bridgePort || '35000'}`;
      let startCmdCodeEl = null;
      const syncLiveCmdCode = () => {
        if (startCmdCodeEl) startCmdCodeEl.textContent = liveWinStartCmd();
      };

      const comIn = el('input', {
        className: 'sb-input sb-bridge-com',
        value: state.bridgeCom,
        placeholder: 'COM6',
        attrs: { 'aria-label': 'Windows Bluetooth COM Port' },
      });
      comIn.addEventListener('input', () => {
        state.bridgeCom = comIn.value.trim().toUpperCase();
        syncLiveCmdCode();
      });

      const tcpPortIn = el('input', {
        className: 'sb-input sb-bridge-port',
        type: 'number',
        value: state.bridgePort,
        placeholder: '35000',
        attrs: { min: '1024', max: '65535', 'aria-label': 'Bridge TCP Port' },
      });
      tcpPortIn.addEventListener('input', () => {
        state.bridgePort = tcpPortIn.value.trim();
        syncLiveCmdCode();
      });

      const bindSel = el('select', { className: 'sb-select sb-bridge-bind', attrs: { 'aria-label': 'Bridge Bind IP' } }, [
        el('option', { value: '127.0.0.1', text: 'Bind 127.0.0.1 (WSL2 Local)' }),
        el('option', { value: '0.0.0.0', text: 'Bind 0.0.0.0 (Rancher / K8s / NAT)' }),
        el('option', { value: 'custom', text: 'Custom Bind IP…' }),
      ]);
      bindSel.value = state.bridgeBind || (bEnv.in_container ? '0.0.0.0' : '127.0.0.1');

      const customBindIn = el('input', {
        className: 'sb-input',
        value: state.bridgeCustomBind,
        placeholder: 'e.g. 192.168.1.50',
        style: `width:142px;display:${bindSel.value === 'custom' ? 'inline-block' : 'none'};`,
        attrs: { 'aria-label': 'Custom Windows Bind IPv4' },
      });
      customBindIn.addEventListener('input', () => {
        state.bridgeCustomBind = customBindIn.value.trim();
        if (state.bridgeCustomBind) state.bridgeTargetHost = state.bridgeCustomBind;
        syncLiveCmdCode();
      });

      const targetHostIn = el('input', {
        className: 'sb-input',
        value: state.bridgeTargetHost || (bEnv.recommended_host || ''),
        placeholder: 'Host IP (e.g. 192.168.1.50)',
        style: 'width:168px;',
        attrs: { 'aria-label': 'Target Bridge Host IP or Hostname' },
      });
      targetHostIn.addEventListener('input', () => {
        state.bridgeTargetHost = targetHostIn.value.trim();
        if (state.bridgeTargetHost) {
          state.portOrAddr = `tcp://${state.bridgeTargetHost}:${state.bridgePort || '35000'}`;
        }
      });

      bindSel.addEventListener('change', () => {
        state.bridgeBind = bindSel.value;
        customBindIn.style.display = state.bridgeBind === 'custom' ? 'inline-block' : 'none';
        if (state.bridgeBind === 'custom' && !state.bridgeCustomBind && state.bridgeTargetHost && /^\d+\.\d+\.\d+\.\d+$/.test(state.bridgeTargetHost)) {
          state.bridgeCustomBind = state.bridgeTargetHost;
          customBindIn.value = state.bridgeCustomBind;
        }
        syncLiveCmdCode();
      });

      const bridgePanel = el('div', { className: 'sb-bridge-box' }, [
        el('div', { className: 'sb-head' }, [
          el('div', { className: 'sb-row' }, [
            el('span', { className: 'sb-title', text: 'Windows Bluetooth → TCP Bridge (Carly / ELM327 SPP)' }),
            el('span', { className: `sb-badge ${bRunning ? 'ok' : (bEnv.in_container ? 'warn' : '')}`, text: bStateTxt }),
          ]),
          el('span', { className: 'sb-meta', text: bEnv.can_spawn_windows ? 'WSL2 PowerShell Interop Ready' : (bEnv.in_container ? 'K8s / Rancher Container → Windows Host Bridge' : 'TCP Bridge Ready') }),
        ]),
        el('div', { className: 'sb-row' }, [
          el('span', { className: 'sb-meta', text: 'COM Port:' }),
          comIn,
          el('span', { className: 'sb-meta', text: 'TCP Port:' }),
          tcpPortIn,
          bindSel,
          customBindIn,
          ...(bEnv.in_container ? [el('span', { className: 'sb-meta', text: 'Host IP:' }), targetHostIn] : []),
          el('button', {
            type: 'button',
            className: 'sb-btn sb-btn-green',
            text: state.bridgeBusy ? 'Starting…' : '▶ Start Bridge',
            on: {
              click: async () => {
                if (state.bridgeBusy) return;
                state.bridgeBusy = true;
                const effBind = effectiveBridgeBind();
                const effHost = effectiveBridgeHost(bEnv);
                setStatus(`Starting Windows BT-OBD Bridge (${state.bridgeCom} → ${effBind}:${state.bridgePort})...`);
                renderMain();
                try {
                  const res = await ctx.action('bt_bridge_start', {
                    com_port: state.bridgeCom || 'COM5',
                    tcp_port: Number(state.bridgePort || 35000),
                    bind_address: effBind,
                    target_host: effHost,
                  });
                  state.bridgeStatus = res;
                  if (res && res.tcp_url) {
                    state.transport = 'tcp';
                    state.portOrAddr = res.tcp_url;
                  }
                  setStatus((res && res.message) || 'Bridge start finished.');
                } finally {
                  state.bridgeBusy = false;
                  renderMain();
                }
              },
            },
          }),
          el('button', {
            type: 'button',
            className: 'sb-btn sb-btn-danger',
            text: '⏹ Stop Bridge',
            on: {
              click: async () => {
                setStatus('Stopping Windows BT-OBD Bridge & releasing COM port...');
                const res = await ctx.action('bt_bridge_stop', { tcp_port: Number(state.bridgePort || 35000) });
                state.bridgeStatus = res;
                setStatus((res && res.message) || 'Bridge stopped.');
                renderMain();
              },
            },
          }),
          el('button', {
            type: 'button',
            className: 'sb-btn',
            text: 'Detect COM',
            on: {
              click: async () => {
                setStatus('Querying Windows Bluetooth COM ports...');
                const res = await ctx.action('bt_bridge_list_com', {});
                state.bridgeDetectedPorts = (res && res.ports) || [];
                if (state.bridgeDetectedPorts.length === 1) {
                  state.bridgeCom = state.bridgeDetectedPorts[0];
                }
                setStatus((res && res.message) || 'COM port scan finished.');
                renderMain();
              },
            },
          }),
          el('button', {
            type: 'button',
            className: 'sb-btn sb-btn-primary',
            text: 'Test Link',
            on: {
              click: async () => {
                const targetHost = effectiveBridgeHost(bEnv);
                const testUrl = `tcp://${targetHost}:${state.bridgePort || '35000'}`;
                state.transport = 'tcp';
                state.portOrAddr = testUrl;
                setStatus(`Testing ELM327 bridge link at ${testUrl}...`);
                const res = await ctx.action('bt_bridge_test', { tcp_url: testUrl, probe_elm: true });
                if (res && res.reachable) {
                  await loadBridgeStatus(true);
                }
                setStatus((res && res.message) || 'Link test complete.');
                renderMain();
              },
            },
          }),
        ]),
      ]);

      if (state.bridgeDetectedPorts.length) {
        bridgePanel.appendChild(el('div', { className: 'sb-row' }, [
          el('span', { className: 'sb-meta', text: 'Detected Windows COM Ports (click to fill):' }),
          ...state.bridgeDetectedPorts.map((cp) => el('button', {
            type: 'button',
            className: 'sb-btn' + (state.bridgeCom === cp ? ' active' : ''),
            text: cp,
            on: { click: () => { state.bridgeCom = cp; renderMain(); } },
          })),
        ]));
      }

      if (bEnv.in_container || (!bEnv.can_spawn_windows && bCmds.start_ps1_rancher)) {
        startCmdCodeEl = el('code', { className: 'sb-code-pill', text: liveWinStartCmd() });
        bridgePanel.appendChild(el('div', { className: 'sb-bridge-cmd' }, [
          el('div', { className: 'sb-meta', style: 'color:#fde68a;', text: 'K8s / Rancher Host Mode: Paste ONE full command into Windows PowerShell (works from any folder) or WSL Ubuntu, then click Test Link:' }),
          el('div', { className: 'sb-row' }, [
            startCmdCodeEl,
            el('button', {
              type: 'button', className: 'sb-btn sb-btn-primary', text: 'Copy Windows PowerShell',
              on: { click: () => copyToClipboard(liveWinStartCmd(), (ok) => setStatus(ok ? 'Copied full Windows PowerShell command (paste into any PowerShell window).' : 'Copy failed; select text manually.')) },
            }),
            el('button', {
              type: 'button', className: 'sb-btn', text: 'Copy WSL Command',
              on: { click: () => copyToClipboard(liveWslStartCmd(), (ok) => setStatus(ok ? 'Copied full WSL terminal command (run from blue-lodge folder).' : 'Copy failed; select text manually.')) },
            }),
            el('button', {
              type: 'button', className: 'sb-btn', text: 'Copy Stop',
              on: { click: () => copyToClipboard(liveStopCmd(), (ok) => setStatus(ok ? 'Copied Stop command to clipboard.' : 'Copy failed; select text manually.')) },
            }),
          ]),
        ]));
      }
      connCard.appendChild(bridgePanel);

      if (state.adapters) {
        const drv = state.adapters.drivers || {};
        connCard.appendChild(el('div', { className: 'sb-pid-tile' }, [
          el('div', { className: 'sb-meta', text: `Drivers: POSIX Serial=${drv.native_posix_serial ? 'OK' : 'no'} · AF_CAN=${drv.native_linux_af_can ? 'OK' : 'no'} · bleak=${drv.bleak ? 'OK' : 'optional'} · TCP Bridge=BUILT-IN` }),
          ...(state.adapters.carly_and_setup_guide || []).map((g) => el('div', { className: 'sb-meta', style: 'color:#93c5fd;', text: `• ${g}` })),
        ]));
      }
      if (state.bleProbe) {
        connCard.appendChild(el('div', { className: 'sb-pid-tile' }, [
          el('div', { style: 'font-weight:700;color:#fde68a;', text: 'BLE GATT Fingerprint Result' }),
          el('div', { className: 'sb-meta', text: state.bleProbe.verdict || state.bleProbe.message || '' }),
        ]));
      }

      // Selectable Instrument Picker (45+ Standard SAE J1979 PIDs + Imported Custom Extended PIDs)
      if (state.pickerOpen) {
        const allOptions = [
          { key: 'total_ft_b1', label: 'Total Fuel Trim B1 (STFT+LTFT)', cat: 'Fuel & Emissions', unit: '%', supported: true },
          ...state.pidCatalog.map((p) => ({ key: p.key, label: `${p.label} (01${p.pid})`, cat: p.category, unit: p.unit, supported: p.supported })),
          ...state.customPids.map((c) => ({ key: `custom:${c.short_name}`, label: `[Custom] ${c.name} (${c.cmd})`, cat: 'Extended PIDs', unit: c.unit, supported: true })),
        ];
        const grid = el('div', { className: 'sb-pid-picker-grid' });
        allOptions.forEach((opt) => {
          const isChecked = state.selectedGauges.includes(opt.key);
          const cb = el('input', { type: 'checkbox' });
          cb.checked = isChecked;
          const row = el('label', { className: 'sb-pid-check' + (isChecked ? ' checked' : '') }, [
            cb,
            el('span', { text: `${opt.label} [${opt.unit || '—'}]` }),
          ]);
          cb.addEventListener('change', async () => {
            if (cb.checked) {
              if (!state.selectedGauges.includes(opt.key)) state.selectedGauges.push(opt.key);
            } else {
              state.selectedGauges = state.selectedGauges.filter((k) => k !== opt.key);
            }
            await ctx.action('save_gauge_prefs', { asset_id: state.activeId, keys: state.selectedGauges });
            renderMain();
          });
          grid.appendChild(row);
        });
        connCard.appendChild(el('div', { className: 'sb-pid-tile' }, [
          el('div', { className: 'sb-head' }, [
            el('div', { className: 'sb-title', text: `Torque Instrument Picker (${allOptions.length} available gauges — up to 12 pinned)` }),
            el('button', { type: 'button', className: 'sb-btn', text: 'Done', on: { click: () => { state.pickerOpen = false; renderMain(); } } }),
          ]),
          grid,
        ]));
      }

      // Dynamic Torque Gauge Cluster (updated in-place by updateLiveTelemetryView — zero focus loss!)
      const gauges = el('div', { className: 'sb-gauge-cluster' }, state.selectedGauges.map((k) => buildGaugeForKey(k, tick, an)));
      liveSlots.gaugesHost = gauges;

      // Plain-English Active Trouble Codes Card Slot (updated in-place)
      const dtcHost = el('div', {});
      const initialDtcCard = buildDtcCardEl();
      if (initialDtcCard) dtcHost.appendChild(initialDtcCard);
      liveSlots.dtcHost = dtcHost;

      // Multi-Channel Time-Series Oscilloscope (updated in-place with true elapsed timestamps)
      const curTraces = liveSlots.chartPresets[state.chartGroup] || liveSlots.chartPresets.fuel_rpm;
      const chartTitleEl = el('div', { className: 'sb-title', text: formatLiveChartTitle() });
      const chartSvgHost = el('div', {}, [buildTimeSeriesChart(el, state.livePoints, curTraces)]);
      liveSlots.chartTitleEl = chartTitleEl;
      liveSlots.chartSvgHost = chartSvgHost;

      const chartCard = el('div', { className: 'sb-chart-wrap' }, [
        el('div', { className: 'sb-head' }, [
          chartTitleEl,
          el('div', { className: 'sb-row' }, [
            ['fuel_rpm', 'Fuel Trims & RPM'],
            ['voltage', 'Voltage & Ripple'],
            ['o2_cat', 'O2 B1S1 vs B1S2'],
            ['thermal', 'Coolant & Timing'],
          ].map(([gid, glbl]) => el('button', {
            type: 'button',
            className: 'sb-btn' + (state.chartGroup === gid ? ' active' : ''),
            text: glbl,
            on: { click: () => { state.chartGroup = gid; renderMain(); } },
          }))),
        ]),
        chartSvgHost,
        el('div', { className: 'sb-legend' }, curTraces.map((tr) => el('span', {}, [
          el('span', { className: 'sb-legend-dot', style: `background:${tr.color};` }),
          tr.label,
        ]))),
      ]);

      container.append(connCard, gauges, dtcHost, chartCard);

      // Statistical Signal & Correlation Lab Card
      if (an && an.status === 'ok') {
        const vh = an.voltage_health || {};
        const o2h = an.o2_catalyst_health || {};
        container.appendChild(el('div', { className: 'sb-card' }, [
          el('div', { className: 'sb-head' }, [
            el('div', { className: 'sb-title', text: `Signal & Correlation Diagnostics (Run #${state.activeRunId || '—'} · ${an.sample_count} samples / ${an.duration_sec}s)` }),
            el('div', { className: 'sb-row' }, [
              buildExportButtons(),
              el('button', {
                type: 'button', className: 'sb-btn sb-btn-amber',
                text: `Send to ${agentName} Mechanic Copilot →`,
                on: { click: () => { state.tab = 'triage'; renderMain(); } },
              }),
            ]),
          ]),
          el('div', { className: 'sb-grid-3' }, [
            el('div', { className: 'sb-pid-tile' }, [
              el('div', { className: 'sb-meta', text: 'Active / Past DTCs' }),
              el('div', { className: 'sb-pid-val', text: (an.active_dtcs || []).length ? an.active_dtcs.join(', ') : 'NONE' }),
              el('div', { className: 'sb-meta', text: `History: ${(an.historical_dtcs || []).join(', ') || 'none'}` }),
            ]),
            el('div', { className: 'sb-pid-tile' }, [
              el('div', { className: 'sb-meta', text: 'Voltage & Ripple Health' }),
              el('div', { className: 'sb-pid-val', text: vh.status || '—' }),
              el('div', { className: 'sb-meta', text: vh.min_v != null ? `${vh.min_v}V–${vh.max_v}V · ${vh.ripple_vpp}Vpp` : '—' }),
            ]),
            el('div', { className: 'sb-pid-tile' }, [
              el('div', { className: 'sb-meta', text: 'O2 / Catalyst Health' }),
              el('div', { className: 'sb-pid-val', text: o2h.status || '—' }),
              el('div', { className: 'sb-meta', text: o2h.o2_correlation_r != null ? `r=${o2h.o2_correlation_r} · Ratio ${o2h.downstream_to_upstream_ratio}` : '—' }),
            ]),
          ]),
          ...(an.signatures || []).map((sg) => el('div', { className: 'sb-pid-tile' }, [
            el('div', { className: 'sb-head' }, [
              el('span', { style: 'font-weight:700;color:#f8fafc;', text: sg.code }),
              el('span', { className: `sb-badge ${sg.severity === 'critical' ? 'crit' : 'warn'}`, text: `${sg.confidence} CONFIDENCE` }),
            ]),
            el('div', { className: 'sb-meta', style: 'color:#e2e8f0;', text: sg.evidence }),
            el('div', { className: 'sb-meta', text: `Shop checks: ${(sg.recommended_checks || []).join(' · ')}` }),
          ])),
          el('table', { className: 'sb-table' }, [
            el('thead', {}, [el('tr', {}, [el('th', { text: 'Signal Pair' }), el('th', { text: 'Pearson r' }), el('th', { text: 'Meaning' })])]),
            el('tbody', {}, (an.correlations || []).slice(0, 6).map((c) => el('tr', {}, [
              el('td', { style: 'font-weight:700;', text: c.pair }),
              el('td', {}, [el('span', { className: `sb-badge ${Math.abs(c.r) >= 0.65 ? 'warn' : 'ok'}`, text: c.r >= 0 ? `+${c.r}` : String(c.r) })]),
              el('td', { className: 'sb-meta', text: c.interpretation }),
            ]))),
          ]),
        ]));
      }
    }

    // ── TAB 2: DIRECT CAN-BUS OBSERVER + TORQUE CSV / OBDb JSON / opendbc IMPORTERS ──
    function renderCanObserverTab(container) {
      const canFilterIn = el('input', {
        className: 'sb-input',
        style: 'max-width:140px;text-transform:uppercase;',
        value: state.canFilter,
        placeholder: 'e.g. 0C9 (blank=ALL)',
      });
      canFilterIn.addEventListener('input', () => { state.canFilter = canFilterIn.value.trim().toUpperCase(); });

      const canDumpIn = el('textarea', {
        className: 'sb-textarea',
        value: state.canDumpText,
        placeholder: 'Auto-filled when you click "Sniff CAN via ELM327", or paste Linux candump lines here...',
      });
      canDumpIn.addEventListener('input', () => { state.canDumpText = canDumpIn.value; });

      const dbcNameIn = el('input', {
        className: 'sb-input',
        style: 'max-width:220px;',
        value: state.dbcName,
        placeholder: 'Profile Name (e.g. GM Global A)',
      });
      dbcNameIn.addEventListener('input', () => { state.dbcName = dbcNameIn.value; });

      const dbcIn = el('textarea', {
        className: 'sb-textarea',
        value: state.dbcText || SAMPLE_DBC,
        placeholder: 'Paste comma.ai opendbc .dbc definitions (BO_ / SG_) or click a preset above...',
      });
      dbcIn.addEventListener('input', () => { state.dbcText = dbcIn.value; });

      const dbcFileIn = el('input', { type: 'file', attrs: { accept: '.dbc,.txt' }, style: 'font-size:11px;max-width:210px;' });
      dbcFileIn.addEventListener('change', async () => {
        const f = dbcFileIn.files && dbcFileIn.files[0];
        if (!f) return;
        const txt = await readFileAsText(f);
        dbcIn.value = txt;
        state.dbcText = txt;
        dbcNameIn.value = f.name;
        state.dbcName = f.name;
        setStatus(`Loaded ${f.name} into the DBC editor — click "Save DBC Profile" to store it in Shop Bench SQLite.`);
      });

      const extPidTextIn = el('textarea', { className: 'sb-textarea', placeholder: 'Paste Torque Pro Extended PID CSV or OBDb JSON signal set...' });
      const extPidFileIn = el('input', { type: 'file', attrs: { accept: '.csv,.json' }, style: 'font-size:11px;' });
      extPidFileIn.addEventListener('change', async () => {
        const f = extPidFileIn.files && extPidFileIn.files[0];
        if (!f) return;
        extPidTextIn.value = await readFileAsText(f);
      });

      const clearPhraseIn = el('input', { className: 'sb-input', style: 'max-width:260px;', placeholder: 'Type CLEAR_DTCS to confirm Mode 04 clear' });
      const clearRow = el('div', { className: 'sb-row', style: 'display:none;' }, [
        clearPhraseIn,
        el('button', {
          type: 'button', className: 'sb-btn sb-btn-danger', text: 'Confirm Mode 04 Clear',
          on: {
            click: async () => {
              const res = await ctx.action('obd_clear_dtcs', {
                transport: state.transport, port_or_addr: state.portOrAddr,
                confirm_phrase: clearPhraseIn.value.trim(), asset_id: state.activeId,
              });
              setStatus(res.message || JSON.stringify(res));
              clearPhraseIn.value = '';
              clearRow.style.display = 'none';
            },
          },
        }),
      ]);

      // CAN Sniffer + DBC Decoder Card (Clear Step 1 & Step 2 separation)
      container.appendChild(el('div', { className: 'sb-card' }, [
        el('div', { className: 'sb-head' }, [
          el('div', { className: 'sb-title', text: 'Direct CAN-Bus Sniffer & DBC Signal Decoder (ELM327 ATH1/ATMA · SocketCAN)' }),
          el('div', { className: 'sb-row' }, [
            el('button', {
              type: 'button', className: 'sb-btn sb-btn-primary', text: 'Sniff CAN via ELM327 (ATH1/ATMA)',
              on: {
                click: async () => {
                  const filt = canFilterIn.value.trim().toUpperCase();
                  state.canFilter = filt;
                  state.dbcText = dbcIn.value;
                  setStatus(`Sniffing CAN frames (${state.transport}, filter=${filt || 'ALL'}) — ignition must be ON (KOEO or Engine Running)...`);
                  const res = await ctx.action('elm_can_sniff', {
                    transport: state.transport === 'ble' ? 'sim' : state.transport,
                    port_or_addr: state.portOrAddr,
                    filter_can_id: filt,
                    duration_sec: 2.5,
                    dbc_text: dbcIn.value,
                    asset_id: state.activeId,
                  });
                  state.canAnalysis = res;
                  if (res.raw_sample) {
                    canDumpIn.value = res.raw_sample;
                    state.canDumpText = res.raw_sample;
                  }
                  const decCnt = (res.dbc_decoded || []).length;
                  setStatus(res.gateway_hint || `Sniffed ${res.frame_count || 0} CAN frames${decCnt ? ` · decoded ${decCnt} DBC signal frames` : ''}${res.buffer_full_hit ? ' (BUFFER FULL — add a 3-char CAN ID filter like 0C9 or 0F1!)' : ''}.`);
                  renderMain();
                },
              },
            }),
            el('button', {
              type: 'button', className: 'sb-btn sb-btn-green', text: 'Decode Raw Frames with DBC',
              on: {
                click: async () => {
                  state.canDumpText = canDumpIn.value;
                  state.dbcText = dbcIn.value;
                  const res = await ctx.action('can_decode', { candump_text: canDumpIn.value, dbc_text: dbcIn.value, asset_id: state.activeId });
                  state.canAnalysis = res;
                  const decCnt = (res.dbc_decoded || []).length;
                  setStatus(`Decoded ${res.frame_count || 0} CAN frames${decCnt ? ` (${decCnt} matched DBC signals)` : ' (0 DBC matches — check BO_ decimal IDs match captured hex IDs)'}.`);
                  renderMain();
                },
              },
            }),
            el('button', {
              type: 'button', className: 'sb-btn sb-btn-danger', text: 'Mode 04 Clear DTCs',
              on: { click: () => { clearRow.style.display = clearRow.style.display === 'none' ? 'flex' : 'none'; } },
            }),
          ]),
        ]),
        clearRow,
        el('div', { className: 'sb-bridge-box' }, [
          el('div', { className: 'sb-head' }, [
            el('div', { style: 'font-weight:700;color:#7dd3fc;font-size:11px;', text: 'STEP 1 · Live CAN-Bus Capture & Raw Frames (candump)' }),
            el('div', { className: 'sb-row' }, [
              el('span', { className: 'sb-meta', text: 'Optional ATCRA Filter:' }),
              canFilterIn,
              el('button', {
                type: 'button', className: 'sb-btn', text: 'Clear Filter',
                on: { click: () => { state.canFilter = ''; canFilterIn.value = ''; setStatus('Cleared CAN ID filter — next sniff will capture ALL active bus traffic.'); } },
              }),
            ]),
          ]),
          el('div', { className: 'sb-meta', text: 'Leave filter blank to sniff all traffic, or type a 3-char hex ID (e.g. 0C9 for GM RPM/Throttle, 0F1 for Brake) to avoid Bluetooth BUFFER FULL. Vehicle ignition must be ON (KOEO or Running) so HS-CAN is awake.' }),
          canDumpIn,
        ]),
        el('div', { className: 'sb-bridge-box' }, [
          el('div', { className: 'sb-head' }, [
            el('div', { style: 'font-weight:700;color:#c4b5fd;font-size:11px;', text: 'STEP 2 · DBC Signal Decoder Profile (Saved in Shop Bench SQLite — no Windows file needed)' }),
            el('div', { className: 'sb-row' }, [
              el('button', {
                type: 'button', className: 'sb-btn', text: 'Load GM Global A Preset',
                on: {
                  click: () => {
                    state.dbcName = 'GM Global A (Chevy / GMC / Buick)';
                    state.dbcText = GM_GLOBAL_A_DBC;
                    dbcNameIn.value = state.dbcName;
                    dbcIn.value = state.dbcText;
                    setStatus('Loaded GM Global A DBC preset (0x0C9 Engine/Accel, 0x0F1 Brake, 0x0C1 Wheel Speeds). Click "Save DBC Profile" to store it in SQLite, then "Decode Raw Frames with DBC" or "Sniff CAN".');
                  },
                },
              }),
              el('button', {
                type: 'button', className: 'sb-btn', text: 'Load Generic Sample DBC',
                on: {
                  click: () => {
                    state.dbcName = 'Generic Sample DBC';
                    state.dbcText = SAMPLE_DBC;
                    dbcNameIn.value = state.dbcName;
                    dbcIn.value = state.dbcText;
                    setStatus('Loaded generic sample DBC into editor.');
                  },
                },
              }),
            ]),
          ]),
          el('div', { className: 'sb-meta', text: 'Paste DBC rules below (or pick a preset) and click "Save DBC Profile" — it saves directly into Shop Bench SQLite. Only use "Import .dbc file" if you already downloaded a .dbc file to your PC.' }),
          el('div', { className: 'sb-row' }, [
            el('span', { className: 'sb-meta', text: 'Profile Name:' }),
            dbcNameIn,
            el('button', {
              type: 'button', className: 'sb-btn sb-btn-primary', text: 'Save DBC Profile',
              on: {
                click: async () => {
                  const nameVal = dbcNameIn.value.trim() || 'Vehicle DBC';
                  state.dbcName = nameVal;
                  state.dbcText = dbcIn.value;
                  const res = await ctx.action('save_dbc_profile', { name: nameVal, dbc_text: dbcIn.value, asset_id: state.activeId });
                  setStatus(res.status === 'ok' ? `Saved DBC profile "${nameVal}" to Shop Bench SQLite (${res.message_count} CAN messages).` : (res.message || 'Invalid DBC'));
                  await reload(state.activeId);
                },
              },
            }),
            el('span', { className: 'sb-meta', text: 'Optional import .dbc from disk:' }),
            dbcFileIn,
          ]),
          dbcIn,
          state.dbcProfiles.length ? el('div', { className: 'sb-row', style: 'padding-top:4px;' }, [
            el('span', { className: 'sb-meta', text: 'Saved SQLite DBC Profiles:' }),
            ...state.dbcProfiles.map((p) => el('span', { className: 'sb-row', style: 'gap:4px;background:rgba(15,23,42,0.9);border:1px solid rgba(148,163,184,0.28);border-radius:6px;padding:3px 6px;' }, [
              el('span', { className: 'sb-meta', style: 'color:#e2e8f0;', text: `${p.name} (${p.message_count} msgs)` }),
              el('button', {
                type: 'button', className: 'sb-btn', style: 'padding:2px 6px;font-size:10px;', text: 'Load',
                on: {
                  click: () => {
                    state.dbcName = p.name || '';
                    state.dbcText = p.dbc_text || '';
                    dbcNameIn.value = state.dbcName;
                    dbcIn.value = state.dbcText;
                    setStatus(`Loaded saved DBC profile "${p.name}" from SQLite into editor.`);
                  },
                },
              }),
              el('button', {
                type: 'button', className: 'sb-btn sb-btn-danger', style: 'padding:2px 6px;font-size:10px;', text: '×',
                title: 'Delete saved DBC profile from SQLite',
                on: {
                  click: async () => {
                    await ctx.action('delete_dbc_profile', { id: p.id });
                    setStatus(`Deleted DBC profile "${p.name}".`);
                    await reload(state.activeId);
                  },
                },
              }),
            ])),
          ]) : el('div', { className: 'sb-meta', text: 'No DBC profiles saved in SQLite yet. Click "Load GM Global A Preset" → "Save DBC Profile" to start decoding Buick/GM HS-CAN frames.' }),
        ]),
      ]));

      // Bus Health + Decoded DBC Signals + Byte-Activity Heatmap Output
      const ca = state.canAnalysis;
      if (ca && ca.gateway_hint && (!ca.bus_analysis || !ca.frame_count)) {
        container.appendChild(el('div', { className: 'sb-card' }, [
          el('div', { className: 'sb-title', style: 'color:#fde68a;', text: 'CAN Sniff Notice (0 Frames Captured)' }),
          el('div', { className: 'sb-meta', style: 'color:#e2e8f0;', text: ca.gateway_hint }),
        ]));
      }
      if (ca && ca.bus_analysis) {
        const ba = ca.bus_analysis;
        const dbcRowsMap = {};
        (ca.dbc_decoded || []).forEach((d) => {
          const k = `${d.arb_id}|${d.message}`;
          if (!dbcRowsMap[k]) dbcRowsMap[k] = { arb_id: d.arb_id, message: d.message, count: 0, latest: d.signals || {} };
          dbcRowsMap[k].count += 1;
          dbcRowsMap[k].latest = d.signals || {};
        });
        const dbcSummaryRows = Object.values(dbcRowsMap);

        container.appendChild(el('div', { className: 'sb-card' }, [
          el('div', { className: 'sb-head' }, [
            el('div', { className: 'sb-title', text: `CAN Bus Health: ${ba.total_frames} frames · ${ba.unique_id_count} IDs · ${ba.bus_hz} fps` }),
            el('span', { className: 'sb-meta', text: 'Blue = DYNAMIC sensor byte · Amber = counter/state · Gray = static' }),
          ]),
          ...(ba.dropout_alerts || []).map((da) => el('div', { className: 'sb-dtc-card' }, [
            el('div', { style: 'font-weight:700;color:#fca5a5;', text: `⚠ ECU DROPOUT GAP ON ${da.arb_id}` }),
            el('div', { className: 'sb-meta', text: da.note }),
          ])),
          dbcSummaryRows.length ? el('div', { className: 'sb-bridge-box' }, [
            el('div', { style: 'font-weight:700;color:#6ee7b7;font-size:11px;', text: `Decoded DBC Signals (${(ca.dbc_decoded || []).length} matched frames)` }),
            el('table', { className: 'sb-table' }, [
              el('thead', {}, [el('tr', {}, [el('th', { text: 'CAN ID' }), el('th', { text: 'DBC Message' }), el('th', { text: 'Matched Frames' }), el('th', { text: 'Latest Decoded Values' })])]),
              el('tbody', {}, dbcSummaryRows.map((row) => el('tr', {}, [
                el('td', { style: 'font-weight:700;color:#38bdf8;', text: row.arb_id }),
                el('td', { style: 'font-weight:700;color:#e2e8f0;', text: row.message }),
                el('td', { text: String(row.count) }),
                el('td', {}, [el('div', { className: 'sb-row', style: 'gap:6px;' }, Object.entries(row.latest || {}).map(([sName, sObj]) => el('span', {
                  className: 'sb-badge ok',
                  text: `${sName}: ${sObj && sObj.value != null ? sObj.value : '—'}${sObj && sObj.unit ? ` ${sObj.unit}` : ''}`,
                })))]),
              ]))),
            ]),
          ]) : el('div', { className: 'sb-meta', text: 'Tip: To translate raw hex bytes into RPM / Brake % / Wheel Speed, load a matching DBC profile in Step 2 above (e.g. "Load GM Global A Preset") and click "Decode Raw Frames with DBC".' }),
          el('table', { className: 'sb-table' }, [
            el('thead', {}, [el('tr', {}, [el('th', { text: 'CAN ID' }), el('th', { text: 'Frames' }), el('th', { text: 'Rate' }), el('th', { text: 'Jitter / Max Gap' }), el('th', { text: 'Byte Heatmap' })])]),
            el('tbody', {}, (ba.id_profiles || []).map((p) => el('tr', {}, [
              el('td', { style: 'font-weight:700;color:#38bdf8;', text: p.arb_id }),
              el('td', { text: String(p.count) }),
              el('td', { text: p.hz ? `${p.hz} Hz` : 'aperiodic' }),
              el('td', {}, [el('span', { className: `sb-badge ${p.dropout_detected ? 'crit' : 'ok'}`, text: p.max_gap_ms ? `±${p.jitter_ms}ms / max ${p.max_gap_ms}ms` : '—' })]),
              el('td', {}, [el('div', { className: 'sb-byte-row' }, (p.byte_heatmap || []).map((b) => el('span', {
                className: 'sb-byte-cell' + (b.kind === 'DYNAMIC' ? ' dyn' : (b.kind === 'LOW_STATE' ? ' low' : '')),
                title: `B${b.byte}: ${b.kind} (${b.min_hex}..${b.max_hex})`,
                text: `B${b.byte}`,
              })))]),
            ]))),
          ]),
        ]));
      }

      // Open-Source Extended PID Importer Card (Torque Pro CSV & OBDb JSON)
      container.appendChild(el('div', { className: 'sb-card' }, [
        el('div', { className: 'sb-head' }, [
          el('div', { className: 'sb-title', text: `Open-Source Extended PIDs — Torque Pro .csv & OBDb .json (${state.customPids.length} loaded)` }),
          el('div', { className: 'sb-row' }, [
            el('button', { type: 'button', className: 'sb-btn', text: 'Load Sample Torque CSV', on: { click: () => { extPidTextIn.value = SAMPLE_TORQUE_CSV; } } }),
            el('button', { type: 'button', className: 'sb-btn', text: 'Load Sample OBDb JSON', on: { click: () => { extPidTextIn.value = SAMPLE_OBDB_JSON; } } }),
            el('button', {
              type: 'button', className: 'sb-btn sb-btn-primary', text: 'Import Torque CSV',
              on: {
                click: async () => {
                  const res = await ctx.action('import_torque_csv', { csv_text: extPidTextIn.value, asset_id: state.activeId });
                  setStatus(res.status === 'ok' ? `Imported ${res.imported_count} Torque Pro extended PIDs (now selectable in Customize Gauges!).` : (res.message || 'CSV import error'));
                  await reload(state.activeId);
                },
              },
            }),
            el('button', {
              type: 'button', className: 'sb-btn sb-btn-primary', text: 'Import OBDb JSON',
              on: {
                click: async () => {
                  const res = await ctx.action('import_obdb_json', { json_text: extPidTextIn.value, asset_id: state.activeId });
                  setStatus(res.status === 'ok' ? `Imported ${res.imported_count} OBDb vehicle signals.` : (res.message || 'OBDb JSON error'));
                  await reload(state.activeId);
                },
              },
            }),
          ]),
        ]),
        el('div', { className: 'sb-row' }, [el('span', { className: 'sb-meta', text: 'Upload .csv/.json file:' }), extPidFileIn]),
        extPidTextIn,
        state.customPids.length ? el('table', { className: 'sb-table' }, [
          el('thead', {}, [el('tr', {}, [el('th', { text: 'Name' }), el('th', { text: 'Header / Cmd' }), el('th', { text: 'Equation' }), el('th', { text: 'Unit' }), el('th', { text: '' })])]),
          el('tbody', {}, state.customPids.slice(0, 15).map((cp) => el('tr', {}, [
            el('td', { text: cp.name }),
            el('td', { text: `${cp.header} · ${cp.cmd}` }),
            el('td', { className: 'sb-meta', text: cp.equation }),
            el('td', { text: cp.unit || '—' }),
            el('td', {}, [el('button', { type: 'button', className: 'sb-btn', text: '×', on: { click: async () => { await ctx.action('delete_custom_pid', { id: cp.id }); await reload(state.activeId); } } })]),
          ]))),
        ]) : el('div', { className: 'sb-meta', text: 'No extended PIDs imported yet. Pull vehicle JSON from github.com/OBDb or Torque Pro community CSVs.' }),
      ]));
    }

    // ── TAB 3: AI MECHANIC COPILOT & TRIAGE LOGS ──
    function renderTriageLogsTab(container) {
      const b = state.active;
      const an = state.analysis;
      const defaultDtcs = (an && an.active_dtcs && an.active_dtcs.length) ? an.active_dtcs.join(',') : ((state.latestScan && state.latestScan.dtcs) ? state.latestScan.dtcs.map((d) => d.code).join(',') : '');
      const defaultSym = (an && an.signatures && an.signatures.length)
        ? `Recorded Run #${state.activeRunId}: ${an.signatures.map((s) => s.code).join(', ')}. Explain what is wrong in plain English, tell me if it is safe to drive, and give a step-by-step DIY test checklist.`
        : '';

      const initSym = state.triageUserEdited ? state.triageSymptom : (state.triageSymptom || defaultSym);
      const initDtcs = state.triageDtcs || defaultDtcs;

      const symptomIn = el('textarea', { className: 'sb-textarea', value: initSym, placeholder: 'Describe what the car is doing or ask for a full vehicle health check (e.g. "run a full health check across all buffered ECU PIDs and live telemetry")...' });
      symptomIn.addEventListener('input', () => {
        state.triageSymptom = symptomIn.value;
        state.triageUserEdited = true;
      });
      const dtcsIn = el('input', { className: 'sb-input', value: initDtcs, placeholder: 'Active DTCs (e.g. P0171,P0302,U0100 — leave blank to auto-read)' });
      dtcsIn.addEventListener('input', () => {
        state.triageDtcs = dtcsIn.value;
      });
      const diagImgIn = el('input', { type: 'file', attrs: { accept: 'image/png,image/jpeg,image/webp' }, style: 'font-size:11px;' });
      const csvFileIn = el('input', { type: 'file', attrs: { accept: '.csv,.txt,.json' }, style: 'font-size:11px;' });
      const triageStream = el('div', { className: 'sb-stream', style: 'display:none;' });

      diagImgIn.addEventListener('change', async () => {
        const f = diagImgIn.files && diagImgIn.files[0];
        if (!f) return;
        const up = await ctx.action('upload_file', { filename: f.name, content_b64: await readFileAsBase64(f) });
        if (up && up.status === 'ok') { state.uploadedPhotoPath = up.file_path; setStatus(`Uploaded inspection photo: ${f.name}`); }
      });

      csvFileIn.addEventListener('change', async () => {
        const f = csvFileIn.files && csvFileIn.files[0];
        if (!f) return;
        const up = await ctx.action('upload_file', { filename: f.name, content_b64: await readFileAsBase64(f) });
        if (up && up.status === 'ok') {
          state.uploadedCsvPath = up.file_path;
          const parsed = await ctx.action('obd_parse_freeze_frame', { file_path: up.file_path, filename: f.name, asset_id: state.activeId });
          setStatus(`Parsed Torque/Carly CSV (${parsed.row_count || 0} rows, ${(parsed.anomalies || []).length} anomalies).`);
        }
      });

      const logs = (b && b.logs) || [];
      container.append(
        el('div', { className: 'sb-card' }, [
          el('div', { className: 'sb-title', text: `${agentName} AI Mechanic Copilot (Plain-English & Master-Tech Triage)` }),
          el('div', { className: 'sb-meta', text: `Auto-Attached Context: Vehicle Year/Make/Model/VIN · Past Service & DTCs · Telemetry Run #${state.activeRunId || 'latest'} · CAN-Bus Observer` }),
          symptomIn, dtcsIn,
          el('div', { className: 'sb-grid-2' }, [
            el('div', {}, [el('div', { className: 'sb-meta', text: 'Optional Photo (spark plug / belt / leak / PCB):' }), diagImgIn]),
            el('div', {}, [el('div', { className: 'sb-meta', text: 'Optional Torque / Carly Freeze-Frame CSV:' }), csvFileIn]),
          ]),
          el('button', {
            type: 'button', className: 'sb-btn sb-btn-amber',
            text: `Ask ${agentName} to Diagnose & Build Checklist (ALP v1)`,
            on: {
              click: async () => {
                if (!symptomIn.value.trim()) { setStatus('Describe the symptom or click Quick 30s Road-Test first.'); return; }
                triageStream.style.display = 'block';
                triageStream.replaceChildren();
                setStatus(`Launching ${agentName} 200-turn FIFO+ReAct Mechanic Copilot...`);
                try {
                  await ctx.launchTask('diagnostic_triage', {
                    asset_id: state.activeId || undefined,
                    run_id: state.activeRunId || undefined,
                    transport: state.transport,
                    port_or_addr: state.portOrAddr,
                    symptom: symptomIn.value.trim(),
                    dtc_codes: dtcsIn.value.trim(),
                    image_path: state.uploadedPhotoPath || '',
                    freeze_frame_path: state.uploadedCsvPath || '',
                  }, {
                    streamContainer: triageStream,
                    onComplete: () => { setStatus('Copilot diagnosis complete & saved to vehicle log.'); reload(state.activeId); },
                    onError: (err) => setStatus(`Triage error: ${err}`),
                  });
                } catch (e) {
                  setStatus(`Launch failed: ${e.message}`);
                }
              },
            },
          }),
          triageStream,
        ]),
        el('div', { className: 'sb-card' }, [
          el('div', { className: 'sb-head' }, [
            el('div', { className: 'sb-title', text: 'Saved Diagnostic & AI Copilot Reports' }),
            buildExportButtons(),
          ]),
          logs.length ? el('div', { style: 'display:flex;flex-direction:column;gap:8px;' }, logs.map((lg) => el('div', { className: 'sb-pid-tile' }, [
            el('div', { className: 'sb-head' }, [
              el('span', { style: 'font-weight:700;color:#f8fafc;', text: lg.title }),
              el('span', { className: 'sb-badge ok', text: lg.log_type }),
            ]),
            el('div', { className: 'sb-meta', style: 'white-space:pre-wrap;', text: lg.summary }),
            (lg.checklist && lg.checklist.length) ? el('ul', { style: 'margin:4px 0 0 16px;' }, lg.checklist.map((s) => el('li', { text: String(s) }))) : null,
          ]))) : el('div', { className: 'sb-meta', text: 'No triage or service logs yet.' }),
        ])
      );
    }

    // ── TAB 4: SERVICE INTERVALS & NAMEPLATE SCOUT ──
    function renderOverviewTab(container) {
      const b = state.active;
      if (!b) { container.appendChild(el('div', { className: 'sb-card' }, [el('div', { className: 'sb-meta', text: 'Select or add an asset on the left.' })])); return; }
      const a = b.asset;
      const photoFileIn = el('input', { type: 'file', attrs: { accept: 'image/png,image/jpeg,image/webp' }, style: 'font-size:11px;' });
      const hintIn = el('input', { className: 'sb-input', placeholder: 'Equipment hint (e.g. Kawasaki FR691V or Carrier furnace)' });
      const scoutStream = el('div', { className: 'sb-stream', style: 'display:none;' });
      photoFileIn.addEventListener('change', async () => {
        const f = photoFileIn.files && photoFileIn.files[0];
        if (!f) return;
        const up = await ctx.action('upload_file', { filename: f.name, content_b64: await readFileAsBase64(f), asset_id: a.id, set_asset_photo: true });
        if (up && up.status === 'ok') { state.uploadedPhotoPath = up.file_path; setStatus(`Uploaded nameplate photo: ${f.name}`); }
      });
      const tTask = el('input', { className: 'sb-input', placeholder: 'Service Task' });
      const tInt = el('input', { className: 'sb-input', type: 'number', placeholder: 'Interval' });
      const tFluid = el('input', { className: 'sb-input', placeholder: 'Fluid / Part / Capacity' });

      container.append(
        el('div', { className: 'sb-card' }, [
          el('div', { className: 'sb-title', text: 'Nameplate & Manual Scout (Photo OCR → Intervals, Torque & Parts)' }),
          el('div', { className: 'sb-row' }, [el('span', { className: 'sb-meta', text: 'Data Plate Photo:' }), photoFileIn]),
          hintIn,
          el('button', {
            type: 'button', className: 'sb-btn sb-btn-amber', text: `Run ${agentName} Nameplate Scout (ALP v1)`,
            on: {
              click: async () => {
                scoutStream.style.display = 'block';
                scoutStream.replaceChildren();
                await ctx.launchTask('nameplate_manual_scout', {
                  asset_id: a.id, image_path: state.uploadedPhotoPath || a.photo_path || '',
                  equipment_hint: hintIn.value.trim() || `${a.year} ${a.make} ${a.model}`.trim(),
                }, { streamContainer: scoutStream, onComplete: () => reload(a.id) });
              },
            },
          }),
          scoutStream,
        ]),
        el('div', { className: 'sb-card' }, [
          el('div', { className: 'sb-title', text: 'Service Intervals' }),
          el('table', { className: 'sb-table' }, [
            el('thead', {}, [el('tr', {}, [el('th', { text: 'Task' }), el('th', { text: 'Interval' }), el('th', { text: 'Remaining' }), el('th', { text: 'Spec' }), el('th', { text: 'State' })])]),
            el('tbody', {}, (b.intervals || []).map((iv) => el('tr', {}, [
              el('td', { text: iv.task_name }),
              el('td', { text: `${iv.interval_value} ${iv.interval_unit}` }),
              el('td', { text: iv.remaining != null ? `${iv.remaining} ${iv.interval_unit}` : '—' }),
              el('td', { text: `${iv.fluid_or_part || ''} ${iv.capacity_spec || ''}`.trim() }),
              el('td', {}, [el('span', { className: `sb-badge ${iv.due_state === 'OVERDUE' ? 'crit' : (iv.due_state === 'DUE_SOON' ? 'warn' : 'ok')}`, text: iv.due_state || 'OK' })]),
            ]))),
          ]),
          el('div', { className: 'sb-grid-3' }, [tTask, tInt, tFluid]),
          el('button', { type: 'button', className: 'sb-btn', text: '+ Add Interval', on: { click: async () => { if (!tTask.value.trim()) return; await ctx.action('save_interval', { asset_id: a.id, task_name: tTask.value.trim(), interval_value: Number(tInt.value || 5000), interval_unit: a.meter_unit || 'mi', last_done_meter: Number(a.meter_value || 0), fluid_or_part: tFluid.value.trim() }); await reload(a.id); } } }),
        ])
      );
    }

    // ── TAB 5: TORQUE SPECS & PARTS INVENTORY ──
    function renderTorquePartsTab(container) {
      const b = state.active;
      if (!b) return;
      const a = b.asset;
      const tComp = el('input', { className: 'sb-input', placeholder: 'Component' });
      const tVal = el('input', { className: 'sb-input', placeholder: 'Spec' });
      const tUnit = el('select', { className: 'sb-select' }, [el('option', { value: 'ft-lb', text: 'ft-lb' }), el('option', { value: 'Nm', text: 'Nm' }), el('option', { value: 'in-lb', text: 'in-lb' })]);
      const tSeq = el('input', { className: 'sb-input', placeholder: 'Fastener / Sequence Notes' });
      const pName = el('input', { className: 'sb-input', placeholder: 'Part Name' });
      const pOem = el('input', { className: 'sb-input', placeholder: 'OEM #' });
      const pXref = el('input', { className: 'sb-input', placeholder: 'Aftermarket Cross-Ref' });
      const pQty = el('input', { className: 'sb-input', type: 'number', value: '1' });
      const pBin = el('input', { className: 'sb-input', placeholder: 'Bin' });

      container.append(
        el('div', { className: 'sb-card' }, [
          el('div', { className: 'sb-title', text: 'Torque Specifications' }),
          el('table', { className: 'sb-table' }, [
            el('thead', {}, [el('tr', {}, [el('th', { text: 'Component' }), el('th', { text: 'Torque' }), el('th', { text: 'Notes' })])]),
            el('tbody', {}, (b.torque_specs || []).map((ts) => el('tr', {}, [el('td', { text: ts.component }), el('td', { text: `${ts.spec_value} ${ts.unit}` }), el('td', { text: `${ts.fastener_size || ''} ${ts.sequence_notes || ''}`.trim() })]))),
          ]),
          el('div', { className: 'sb-grid-2' }, [tComp, tVal]),
          el('div', { className: 'sb-grid-2' }, [tUnit, tSeq]),
          el('button', { type: 'button', className: 'sb-btn', text: '+ Save Torque Spec', on: { click: async () => { if (!tComp.value.trim() || !tVal.value.trim()) return; await ctx.action('save_torque', { asset_id: a.id, component: tComp.value.trim(), spec_value: tVal.value.trim(), unit: tUnit.value, sequence_notes: tSeq.value.trim() }); await reload(a.id); } } }),
        ]),
        el('div', { className: 'sb-card' }, [
          el('div', { className: 'sb-title', text: 'Parts & Consumables Cross-Reference Inventory' }),
          el('table', { className: 'sb-table' }, [
            el('thead', {}, [el('tr', {}, [el('th', { text: 'Part' }), el('th', { text: 'OEM' }), el('th', { text: 'Aftermarket' }), el('th', { text: 'Qty' }), el('th', { text: 'Bin' })])]),
            el('tbody', {}, (b.parts || []).map((p) => el('tr', {}, [el('td', { text: p.part_name }), el('td', { text: p.oem_number || '—' }), el('td', { text: p.aftermarket_xref || '—' }), el('td', {}, [el('span', { className: `sb-badge ${p.qty_on_hand <= p.min_qty ? 'warn' : 'ok'}`, text: String(p.qty_on_hand) })]), el('td', { text: p.location_bin || '—' })]))),
          ]),
          el('div', { className: 'sb-grid-2' }, [pName, pOem]),
          el('div', { className: 'sb-grid-3' }, [pXref, pQty, pBin]),
          el('button', { type: 'button', className: 'sb-btn', text: '+ Save Part', on: { click: async () => { if (!pName.value.trim()) return; await ctx.action('save_part', { asset_id: a.id, part_name: pName.value.trim(), oem_number: pOem.value.trim(), aftermarket_xref: pXref.value.trim(), qty_on_hand: Number(pQty.value || 1), location_bin: pBin.value.trim() }); await reload(a.id); } } }),
        ])
      );
    }

    function renderMain() {
      mainHost.replaceChildren();
      buildScanToolHeader().forEach((n) => mainHost.appendChild(n));

      const tabs = [
        ['scantool', 'Code Reader & Live Gauges'],
        ['can', 'CAN-Bus & PID Databases'],
        ['triage', `${agentName} Mechanic Copilot`],
        ['overview', 'Intervals & Nameplate'],
        ['specs', 'Torque & Parts'],
      ];
      mainHost.appendChild(el('div', { className: 'sb-tabs' }, tabs.map(([id, label]) => el('button', {
        type: 'button',
        className: 'sb-btn' + (state.tab === id ? ' active' : ''),
        text: label,
        on: { click: () => { state.tab = id; renderMain(); } },
      }))));

      if (state.tab === 'scantool') renderScanToolTab(mainHost);
      else if (state.tab === 'can') renderCanObserverTab(mainHost);
      else if (state.tab === 'triage') renderTriageLogsTab(mainHost);
      else if (state.tab === 'overview') renderOverviewTab(mainHost);
      else renderTorquePartsTab(mainHost);
    }

    const railCard = el('div', { className: 'sb-card' }, [
      el('div', { className: 'sb-head' }, [
        el('div', { className: 'sb-title', text: 'Fleet & Shop Assets' }),
        el('button', {
          type: 'button', className: 'sb-btn', text: '+ Add',
          on: { click: () => { addAssetBox.style.display = addAssetBox.style.display === 'none' ? 'flex' : 'none'; } },
        }),
      ]),
      railList,
      addAssetBox,
    ]);

    wrap.append(statusEl, el('div', { className: 'sb-layout' }, [railCard, mainHost]));
    host.replaceChildren(wrap);
    reload();
  }

  if (window.LodgeModules && typeof window.LodgeModules.register === 'function') {
    window.LodgeModules.register('shop-bench', { mount });
  }
})();
