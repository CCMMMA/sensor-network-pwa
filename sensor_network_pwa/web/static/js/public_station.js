const pageConfig = JSON.parse(document.getElementById('pageConfig').textContent);
const snapshotUrl = pageConfig.snapshotUrl;
let lastTimestamp = pageConfig.snapshot.last_timestamp;
let currentWindow = pageConfig.selectedWindow;
const chartInstances = {};
const chartCards = {};
const windowSelect = document.getElementById('windowSelect');
let focusedChartKey = pageConfig.selectedFocus;
let renderedWindow = currentWindow;
const appLogoUrl = pageConfig.appLogoUrl;
const stationLogoUrl = pageConfig.stationLogoUrl;
const colors = ['#0d6efd', '#20c997', '#fd7e14', '#6f42c1', '#dc3545', '#198754', '#6c757d'];
const SECOND_MS = 1000;
const MINUTE_MS = 60 * SECOND_MS;
const HOUR_MS = 60 * MINUTE_MS;

function normalizeChartPoints(points) {
  return (points || [])
    .map((p) => {
      const x = Date.parse(p.x);
      const y = Number(p.y);
      if (!Number.isFinite(x) || !Number.isFinite(y)) return null;
      return { x, y };
    })
    .filter(Boolean);
}

function ceilToStep(ts, stepMs) {
  if (!Number.isFinite(ts) || !stepMs) return ts;
  return Math.ceil(ts / stepMs) * stepMs;
}

function appendSteppedTicks(ticks, startMs, endMs, stepMs) {
  if (!Number.isFinite(startMs) || !Number.isFinite(endMs) || !stepMs || endMs < startMs) return;
  for (let tick = ceilToStep(startMs, stepMs); tick <= endMs; tick += stepMs) {
    ticks.push(tick);
  }
}

function buildWindowTicks(windowCode, xMin, xMax) {
  if (!Number.isFinite(xMin) || !Number.isFinite(xMax) || xMax <= xMin) return [];
  const ticks = [xMin];
  if (windowCode === '1m') {
    appendSteppedTicks(ticks, xMin, xMax, 10 * SECOND_MS);
  } else if (windowCode === '10m') {
    appendSteppedTicks(ticks, xMin, xMax, 1 * MINUTE_MS);
  } else if (windowCode === 'hour') {
    appendSteppedTicks(ticks, xMin, xMax, 10 * MINUTE_MS);
  } else if (windowCode === '3h') {
    appendSteppedTicks(ticks, xMin, xMax, 15 * MINUTE_MS);
  } else if (windowCode === '6h') {
    appendSteppedTicks(ticks, xMin, xMax, 30 * MINUTE_MS);
  } else if (windowCode === '12h') {
    appendSteppedTicks(ticks, xMin, xMax, 1 * HOUR_MS);
  } else if (windowCode === '24h') {
    appendSteppedTicks(ticks, xMin, xMax - HOUR_MS, 3 * HOUR_MS);
    appendSteppedTicks(ticks, Math.max(xMin, xMax - HOUR_MS), xMax, 15 * MINUTE_MS);
  } else if (windowCode === '72h') {
    appendSteppedTicks(ticks, xMin, xMax - 3 * HOUR_MS, 6 * HOUR_MS);
    appendSteppedTicks(ticks, Math.max(xMin, xMax - 3 * HOUR_MS), xMax - HOUR_MS, 1 * HOUR_MS);
    appendSteppedTicks(ticks, Math.max(xMin, xMax - HOUR_MS), xMax, 15 * MINUTE_MS);
  } else {
    appendSteppedTicks(ticks, xMin, xMax, 1 * HOUR_MS);
  }
  ticks.push(xMax);
  return [...new Set(ticks.map((v) => Math.round(v)))].sort((a, b) => a - b);
}

function formatWindowTick(value, windowCode) {
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return '';
  return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}

function windowLabel(windowCode) {
  const options = new Map(pageConfig.windowOptions);
  return options.get(windowCode) || windowCode || '';
}

function formatWindowTickWithDayChange(value, windowCode, index, ticks) {
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return '';
  if (windowCode === '1m') {
    return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
  }
  const prevTick = (Array.isArray(ticks) && index > 0) ? ticks[index - 1] : null;
  const prevDate = prevTick && Number.isFinite(prevTick.value) ? new Date(prevTick.value) : null;
  const dayChanged = !prevDate || prevDate.toDateString() !== d.toDateString();
  const timeLabel = formatWindowTick(value, windowCode);
  if (dayChanged) {
    // Two lines: the date under the time keeps the label as narrow as the others.
    return [timeLabel, d.toLocaleDateString([], { year: 'numeric', month: '2-digit', day: '2-digit' })];
  }
  return timeLabel;
}

function formatStatTimestamp(value) {
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return '';
  return d.toLocaleString([], {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit'
  });
}

function cardHtml(card) {
  const value = (card.value === null || card.value === undefined) ? '--' : card.value;
  const unit = card.unit || '';
  return `
    <div class="col-6 col-md-4 col-xl-2">
      <div class="card h-100 shadow-sm">
        <div class="card-body">
          <div class="text-muted metric-label">${card.label}</div>
          <div class="metric-value">${value}<span class="metric-unit">${unit}</span></div>
        </div>
      </div>
    </div>
  `;
}

function renderAqiStatus(snapshot) {
  const el = document.getElementById('aqiStatus');
  const status = snapshot.aqi_status;
  if (!el) return;
  if (!status) {
    el.innerHTML = '';
    return;
  }
  const valueCard = (snapshot.cards || []).find((c) => c.key === 'aqi_current');
  const value = valueCard && valueCard.value !== null && valueCard.value !== undefined ? valueCard.value : '--';
  el.innerHTML = `
    <div class="col-12 col-md-6 col-xl-3">
      <div class="card shadow-sm aqi-card">
        <div class="card-body">
          <div class="text-muted metric-label">Air Quality Status</div>
          <div class="aqi-stack">
            <div class="aqi-lights">
              <span class="aqi-light good ${status.level === 'good' ? 'active' : ''}"></span>
              <span class="aqi-light warning ${status.level === 'warning' ? 'active' : ''}"></span>
              <span class="aqi-light bad ${status.level === 'bad' ? 'active' : ''}"></span>
            </div>
            <div>
              <div class="aqi-status-value">${value}</div>
              <div class="fw-semibold">${status.label}</div>
            </div>
          </div>
        </div>
      </div>
    </div>
  `;
}

function renderCards(snapshot) {
  const cardsEl = document.getElementById('cards');
  cardsEl.innerHTML = (snapshot.cards || []).filter(c => c.value !== null && c.value !== undefined).map(cardHtml).join('');
  renderAqiStatus(snapshot);
}

function renderSeries(snapshot) {
  const chartsEl = document.getElementById('charts');
  const series = snapshot.series || [];
  const knownKeys = new Set(series.map((item) => item.key));
  if (focusedChartKey && !knownKeys.has(focusedChartKey)) {
    focusedChartKey = '';
  }
  const xMin = snapshot.window_start ? Date.parse(snapshot.window_start) : undefined;
  const xMax = snapshot.window_end ? Date.parse(snapshot.window_end) : undefined;
  const customTicks = buildWindowTicks(snapshot.window || currentWindow, xMin, xMax);
  // Shared with the double-click handlers, which are bound when a chart is first drawn.
  renderedWindow = snapshot.window || currentWindow;
  const renderStats = (stats, unit) => {
    const items = Array.isArray(stats) ? stats.filter(Boolean) : (stats ? [stats] : []);
    if (!items.length) {
      return '<div class="text-muted small">No numeric data available for the selected window.</div>';
    }
    return items.map((item) => `
      <div class="stat-chip">
        <div class="fw-semibold small">${item.label || ''}</div>
        <div class="stat-chip-grid">
          <div class="stat-chip-section">
            <div class="stat-chip-label">current</div>
            <div class="stat-chip-value">${item.current}${unit ? ` ${unit}` : ''}</div>
          </div>
          <div class="stat-chip-section">
            <div class="stat-chip-label">min</div>
            <div class="stat-chip-value">${item.min}${unit ? ` ${unit}` : ''}</div>
            <div class="stat-chip-time">${item.min_at ? formatStatTimestamp(item.min_at) : ''}</div>
          </div>
          <div class="stat-chip-section">
            <div class="stat-chip-label">max</div>
            <div class="stat-chip-value">${item.max}${unit ? ` ${unit}` : ''}</div>
            <div class="stat-chip-time">${item.max_at ? formatStatTimestamp(item.max_at) : ''}</div>
          </div>
        </div>
      </div>
    `).join('');
  };
  const syncFocusUrl = () => {
    const url = new URL(window.location.href);
    if (focusedChartKey) {
      url.searchParams.set('focus', focusedChartKey);
    } else {
      url.searchParams.delete('focus');
    }
    window.history.replaceState({}, '', url.toString());
  };
  const applyChartFocus = () => {
    document.body.classList.toggle('chart-focus-active', Boolean(focusedChartKey));
    Array.from(chartsEl.children).forEach((col) => {
      const isFocused = focusedChartKey && col.dataset.chartKey === focusedChartKey;
      col.classList.toggle('focus-host', Boolean(isFocused));
      const card = col.querySelector('.chart-card');
      if (card) {
        card.classList.toggle('fullscreen', Boolean(isFocused));
      }
      const titleEl = col.querySelector('.chart-title');
      if (titleEl) {
        const baseLabel = col.dataset.chartLabel || '';
        titleEl.textContent = isFocused ? `${baseLabel} - ${windowLabel(renderedWindow)}` : baseLabel;
      }
    });
    syncFocusUrl();
  };

  Object.keys(chartCards).forEach((key) => {
    if (knownKeys.has(key)) return;
    if (chartInstances[key]) {
      chartInstances[key].destroy();
      delete chartInstances[key];
    }
    if (chartCards[key]) {
      chartCards[key].remove();
      delete chartCards[key];
    }
  });

  const styleDataset = (dataset, color) => {
    const pointCount = Array.isArray(dataset.data) ? dataset.data.length : 0;
    const sparse = pointCount <= 2;
    const datasetType = dataset.type === 'bar' ? 'bar' : 'line';
    return {
      ...dataset,
      type: datasetType,
      borderColor: color,
      backgroundColor: datasetType === 'bar' ? `${color}88` : color,
      borderWidth: 2,
      tension: sparse ? 0 : 0.25,
      spanGaps: true,
      showLine: datasetType === 'bar' ? false : pointCount > 1,
      pointRadius: sparse ? 3 : 0,
      pointHoverRadius: sparse ? 4 : 0
    };
  };

  series.forEach((s, idx) => {
    const id = `chart_${s.key}`;
    const yMin = (s.y_min !== null && s.y_min !== undefined) ? s.y_min : undefined;
    const yMax = (s.y_max !== null && s.y_max !== undefined) ? s.y_max : undefined;
    const yStep = (s.y_step !== null && s.y_step !== undefined) ? s.y_step : undefined;
    const datasets = (s.datasets && Array.isArray(s.datasets))
      ? s.datasets.map((d, j) => styleDataset({
          label: d.label,
          data: normalizeChartPoints(d.points),
          type: d.type,
          yAxisID: d.yAxisID
        }, colors[(idx + j) % colors.length]))
      : [styleDataset({
          label: s.label,
          data: normalizeChartPoints(s.points)
        }, colors[idx % colors.length])];
    const scales = {
      x: {
        type: 'linear',
        min: xMin,
        max: xMax,
        afterBuildTicks: (axis) => {
          // Keep the ticks whose level labels fit side by side, starting
          // from the newest one: the height is left to the plot.
          const span = axis.max - axis.min;
          const minGapPx = 72;
          let lastPx = Infinity;
          const kept = [];
          for (let i = customTicks.length - 1; i >= 0; i -= 1) {
            const px = span > 0 ? ((customTicks[i] - axis.min) / span) * axis.width : 0;
            if (lastPx - px < minGapPx) continue;
            lastPx = px;
            kept.unshift({ value: customTicks[i] });
          }
          axis.ticks = kept;
        },
        ticks: {
          maxRotation: 0,
          autoSkip: false,
          callback: (value, index, ticks) => {
            return formatWindowTickWithDayChange(value, snapshot.window || currentWindow, index, ticks);
          }
        }
      }
    };
    if (s.axes && typeof s.axes === 'object') {
      Object.entries(s.axes).forEach(([axisId, axis]) => {
        const axisStep = (axis.y_step !== null && axis.y_step !== undefined) ? axis.y_step : undefined;
        scales[axisId] = {
          type: 'linear',
          display: true,
          position: axis.position === 'right' ? 'right' : 'left',
          min: axis.y_min !== null && axis.y_min !== undefined ? axis.y_min : undefined,
          max: axis.y_max !== null && axis.y_max !== undefined ? axis.y_max : undefined,
          ticks: axisStep ? { stepSize: axisStep } : {},
          title: { display: Boolean(axis.unit), text: axis.unit || '' },
          grid: { drawOnChartArea: axis.position !== 'right' }
        };
      });
    } else {
      scales.y = {
        beginAtZero: false,
        min: yMin,
        max: yMax,
        ticks: yStep ? { stepSize: yStep } : {},
        title: { display: Boolean(s.unit), text: s.unit || '' }
      };
    }
    let col = chartCards[s.key];
    if (!col) {
      col = document.createElement('div');
      col.className = 'col-12 col-md-6 col-xl-4';
      col.dataset.chartKey = s.key;
      col.innerHTML = `
        <div class="card chart-card shadow-sm">
          <div class="card-body">
            <div class="fullscreen-branding">
              <div class="fullscreen-branding-logos fullscreen-branding-logo-left">
                ${appLogoUrl ? `<img src="${appLogoUrl}" alt="App logo">` : ''}
              </div>
              <div class="fullscreen-station-name"></div>
              <div class="fullscreen-branding-logos fullscreen-branding-logo-right">
                ${stationLogoUrl ? `<img src="${stationLogoUrl}" alt="Station logo">` : ''}
              </div>
            </div>
            <div class="d-flex justify-content-between">
              <h2 class="h6 mb-1 chart-title"></h2>
              <span class="text-muted small chart-unit"></span>
            </div>
            <div class="fullscreen-hint mb-1">Double-click to focus this chart.</div>
            <canvas id="${id}" height="110"></canvas>
            <div class="fullscreen-stats"></div>
          </div>
        </div>
      `;
      chartsEl.appendChild(col);
      chartCards[s.key] = col;

      const card = col.querySelector('.chart-card');
      const canvas = col.querySelector('canvas');
      const toggleFocus = () => {
        focusedChartKey = (focusedChartKey === s.key) ? '' : s.key;
        applyChartFocus();
      };
      if (card) {
        card.addEventListener('dblclick', toggleFocus);
      }
      if (canvas) {
        canvas.addEventListener('dblclick', (event) => {
          event.preventDefault();
          event.stopPropagation();
          toggleFocus();
        });
      }
    } else if (!chartsEl.contains(col)) {
      chartsEl.appendChild(col);
    }

    col.dataset.chartLabel = s.label;
    const titleEl = col.querySelector('.chart-title');
    titleEl.textContent = s.label;
    col.querySelector('.chart-unit').textContent = s.unit || '';
    col.querySelector('.fullscreen-station-name').textContent = snapshot.station_name || snapshot.instrument_uuid;
    col.querySelector('.fullscreen-stats').innerHTML = renderStats(s.stats, s.unit || '');
    const canvas = col.querySelector('canvas');
    if (!canvas) return;

    if (!chartInstances[s.key]) {
      chartInstances[s.key] = new Chart(canvas, {
        type: datasets.some((d) => d.type === 'bar') ? 'bar' : 'line',
        data: {
          labels: s.labels,
          datasets
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          plugins: {
            legend: { display: datasets.length > 1 },
            tooltip: { enabled: false }
          },
          scales
        }
      });
    } else {
      const chart = chartInstances[s.key];
      chart.data.labels = s.labels;
      chart.data.datasets = datasets;
      chart.config.type = datasets.some((d) => d.type === 'bar') ? 'bar' : 'line';
      chart.options.plugins.legend.display = datasets.length > 1;
      chart.options.scales = scales;
      chart.update('none');
    }
  });
  applyChartFocus();
}

function applySnapshot(snapshot) {
  currentWindow = snapshot.window || currentWindow;
  if (windowSelect && windowSelect.value !== currentWindow) {
    windowSelect.value = currentWindow;
  }
  document.getElementById('stationTitle').textContent = `${snapshot.station_name} (${snapshot.instrument_uuid})`;
  document.getElementById('lastUpdate').textContent = snapshot.last_timestamp || '-';
  renderCards(snapshot);
  renderSeries(snapshot);
}

async function pollSnapshot(forceRefresh = false) {
  try {
    const qs = new URLSearchParams({
      since: lastTimestamp || '',
      window: currentWindow,
      force: forceRefresh ? '1' : '0'
    });
    const resp = await fetch(`${snapshotUrl}?${qs.toString()}`, { cache: 'no-store' });
    if (!resp.ok) return;
    const data = await resp.json();
    if (!data.changed) return;
    lastTimestamp = data.snapshot.last_timestamp;
    applySnapshot(data.snapshot);
  } catch (_) {
    // keep page live even if polling occasionally fails
  }
}

applySnapshot(pageConfig.snapshot);
if (windowSelect) {
  windowSelect.addEventListener('change', () => {
    currentWindow = windowSelect.value;
    document.cookie = `public_trend_window=${encodeURIComponent(currentWindow)}; path=/; max-age=31536000; samesite=lax`;
    const url = new URL(window.location.href);
    url.searchParams.set('window', currentWindow);
    window.history.replaceState({}, '', url.toString());
    pollSnapshot(true);
  });
}
window.addEventListener('keydown', (event) => {
  if (event.key === 'Escape' && focusedChartKey) {
    focusedChartKey = '';
    const chartsEl = document.getElementById('charts');
    document.body.classList.remove('chart-focus-active');
    Array.from(chartsEl.children).forEach((col) => {
      col.classList.remove('focus-host');
      const card = col.querySelector('.chart-card');
      if (card) card.classList.remove('fullscreen');
    });
    const url = new URL(window.location.href);
    url.searchParams.delete('focus');
    window.history.replaceState({}, '', url.toString());
  }
});
setInterval(pollSnapshot, 5000);
