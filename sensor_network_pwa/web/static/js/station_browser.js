const pageConfig = JSON.parse(document.getElementById('pageConfig').textContent);
(function () {
  const stationUuid = pageConfig.stationUuid;
  const defaultColors = pageConfig.defaultColors;
  const winStart = Date.parse(pageConfig.winStart);
  const winEnd = Date.parse(pageConfig.winEnd);
  let state = {};
  try {
    state = JSON.parse(document.getElementById('stationBrowseState').textContent || '{}');
  } catch (_) {
    state = {};
  }
  const labels = Array.isArray(state.chart_labels) ? state.chart_labels : [];
  const times = labels.map((label) => Date.parse(label));
  const seriesValues = state.numeric_series_aligned || {};
  const units = state.units_map || {};
  const numericCols = Array.isArray(state.numeric_cols) ? state.numeric_cols : [];
  const tableColumns = Array.isArray(state.all_table_columns) ? state.all_table_columns : [];
  const el = (id) => document.getElementById(id);

  function setCookie(name, value) {
    document.cookie = `${name}=${encodeURIComponent(value)}; path=/; max-age=31536000; samesite=lax`;
  }
  function readStored(key) {
    try {
      const raw = window.localStorage.getItem(key);
      return raw ? JSON.parse(raw) : null;
    } catch (_) {
      return null;
    }
  }
  function writeStored(key, value) {
    try {
      window.localStorage.setItem(key, JSON.stringify(value));
    } catch (_) {
      // The page works without localStorage; the choice is just not remembered.
    }
  }
  function download(blob, filename) {
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
  function compactTime(ms) {
    return new Date(ms).toISOString().replace(/[-:]/g, '').replace(/\.\d+Z$/, 'Z');
  }
  const fileStem = `${stationUuid.replace(/[^A-Za-z0-9._-]+/g, '_')}_${compactTime(winStart)}_${compactTime(winEnd)}`;

  // ---------------------------------------------------------------- time range and table controls
  const intervalSelect = el('intervalSelect');
  if (intervalSelect) {
    intervalSelect.addEventListener('change', () => {
      setCookie('station_trend_window', intervalSelect.value);
      intervalSelect.form.requestSubmit();
    });
  }
  [['tablePageSize', 'station_page_size'], ['tableOrder', 'station_row_order']].forEach(([id, cookie]) => {
    const select = el(id);
    if (!select) return;
    select.addEventListener('change', () => {
      setCookie(cookie, select.value);
      select.form.requestSubmit();
    });
  });

  // ---------------------------------------------------------------- chart setup (remembered per station)
  const chartStorageKey = `station_chart_config:${stationUuid}`;
  const chartConfig = { left: [], right: [] };

  function unitOf(field) {
    return units[field] || '';
  }
  function fieldLabel(field) {
    return unitOf(field) ? `${field} [${unitOf(field)}]` : field;
  }
  function numberOrNull(value) {
    if (value === null || value === undefined || value === '') return null;
    const n = Number(value);
    return Number.isFinite(n) ? n : null;
  }
  function plottedItems() {
    return [...chartConfig.left, ...chartConfig.right];
  }
  function nextColor() {
    const used = new Set(plottedItems().map((item) => item.color));
    return defaultColors.find((color) => !used.has(color)) || defaultColors[plottedItems().length % defaultColors.length];
  }
  function newItem(field) {
    // Axis ranges start automatic, so they follow the data when the time range changes.
    return { field, type: 'line', color: nextColor(), min: null, max: null, step: null };
  }
  function loadChartConfig(source) {
    chartConfig.left = [];
    chartConfig.right = [];
    const seen = new Set();
    ['left', 'right'].forEach((side) => {
      const items = source && Array.isArray(source[side]) ? source[side] : [];
      items.forEach((raw) => {
        if (!raw || typeof raw !== 'object') return;
        const field = String(raw.field || '');
        if (!numericCols.includes(field) || seen.has(field)) return;
        seen.add(field);
        const step = numberOrNull(raw.step);
        chartConfig[side].push({
          field,
          type: raw.type === 'bar' ? 'bar' : 'line',
          color: /^#[0-9a-fA-F]{6}$/.test(String(raw.color || '')) ? String(raw.color).toLowerCase() : nextColor(),
          min: numberOrNull(raw.min),
          max: numberOrNull(raw.max),
          step: step !== null && step > 0 ? step : null
        });
      });
    });
  }
  function saveChartConfig() {
    writeStored(chartStorageKey, chartConfig);
  }

  const stored = readStored(chartStorageKey);
  if (stored && typeof stored === 'object') {
    loadChartConfig(stored);
  } else {
    // First visit: start from the usual pair, or from the first parameter.
    const pick = (names) => names.find((name) => numericCols.includes(name));
    const first = pick(['TempOut', 'temp', 'temperature']) || numericCols[0];
    const second = pick(['HumOut', 'hum', 'humidity']);
    if (first) chartConfig.left.push(newItem(first));
    if (second && second !== first) chartConfig.right.push(newItem(second));
  }

  // ---------------------------------------------------------------- axes: one per unit and side
  function niceStep(rough) {
    if (!(rough > 0)) return 1;
    const exponent = Math.floor(Math.log10(rough));
    const fraction = rough / (10 ** exponent);
    const nice = fraction <= 1 ? 1 : fraction <= 2 ? 2 : fraction <= 5 ? 5 : 10;
    return nice * (10 ** exponent);
  }
  function axisGroups() {
    const groups = [];
    ['left', 'right'].forEach((side) => {
      chartConfig[side].forEach((item) => {
        const unit = unitOf(item.field);
        // Parameters without a unit cannot be assumed comparable: each gets its own axis.
        let group = unit ? groups.find((g) => g.side === side && g.unit === unit) : null;
        if (!group) {
          group = { id: `y${groups.length}`, side, unit, items: [] };
          groups.push(group);
        }
        group.items.push(item);
      });
    });
    groups.forEach((group) => {
      const explicit = (key) => {
        const item = group.items.find((it) => it[key] !== null && it[key] !== undefined);
        return item ? item[key] : null;
      };
      let lo = Infinity;
      let hi = -Infinity;
      group.items.forEach((item) => {
        (seriesValues[item.field] || []).forEach((v) => {
          if (v === null || v === undefined) return;
          if (v < lo) lo = v;
          if (v > hi) hi = v;
        });
      });
      if (!Number.isFinite(lo)) { lo = 0; hi = 1; }
      if (lo === hi) { const pad = Math.max(1, Math.abs(lo) * 0.1); lo -= pad; hi += pad; }
      const exMin = explicit('min');
      const exMax = explicit('max');
      const exStep = explicit('step');
      const spanLo = exMin !== null ? exMin : lo;
      const spanHi = exMax !== null ? exMax : hi;
      // A step that would draw more than 40 ticks is ignored.
      const stepFits = exStep !== null && exStep > 0 && (spanHi - spanLo) / exStep <= 40;
      const step = stepFits ? exStep : niceStep(Math.max(spanHi - spanLo, 1e-9) / 5);
      let min = exMin !== null ? exMin : Math.floor(lo / step) * step;
      let max = exMax !== null ? exMax : Math.ceil(hi / step) * step;
      if (!(max > min)) max = min + step;
      group.min = Number(min.toPrecision(12));
      group.max = Number(max.toPrecision(12));
      group.step = step;
      group.explicit = { min: exMin, max: exMax, step: exStep };
      group.label = group.items.length === 1 ? fieldLabel(group.items[0].field) : group.unit;
    });
    return groups;
  }
  function decimalsFor(step) {
    return Math.min(6, Math.max(0, -Math.floor(Math.log10(step) + 1e-9)));
  }

  // ---------------------------------------------------------------- time axis
  const TIME_STEPS = [1, 5, 10, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200, 10800, 21600, 43200, 86400, 172800, 604800]
    .map((seconds) => seconds * 1000);
  function timeTicks(min, max, maxCount) {
    const span = max - min;
    if (!(span > 0)) return { step: 0, values: [] };
    const step = TIME_STEPS.find((s) => span / s <= Math.max(2, maxCount)) || TIME_STEPS[TIME_STEPS.length - 1];
    const values = [];
    for (let t = Math.ceil(min / step) * step; t <= max; t += step) values.push(t);
    return { step, values };
  }
  function timeLabel(ms, step, previousMs) {
    const iso = new Date(ms).toISOString();
    const day = iso.slice(0, 10);
    if (step >= 86400000) return [day];
    const time = step < 60000 ? iso.slice(11, 19) : iso.slice(11, 16);
    const dayChanged = previousMs === null || new Date(previousMs).toISOString().slice(0, 10) !== day;
    return dayChanged ? [time, day] : [time];
  }

  // ---------------------------------------------------------------- Chart.js model (screen and PNG)
  const DASHES = [[], [6, 3], [2, 2], [8, 3, 2, 3], [1, 3], [10, 4]];
  function buildChartConfig(pub) {
    const groups = axisGroups();
    const mono = Boolean(pub && pub.mono);
    const datasets = [];
    groups.forEach((group) => {
      group.items.forEach((item) => {
        const color = mono ? '#000000' : item.color;
        const values = seriesValues[item.field] || [];
        datasets.push({
          type: item.type === 'bar' ? 'bar' : 'line',
          label: fieldLabel(item.field),
          data: times.map((t, i) => ({ x: t, y: values[i] === undefined ? null : values[i] })),
          borderColor: color,
          backgroundColor: item.type === 'bar' ? (mono ? '#00000055' : `${item.color}99`) : color,
          borderWidth: item.type === 'bar' ? 0 : (pub ? 1 : 1.5),
          borderDash: mono && item.type !== 'bar' ? DASHES[datasets.length % DASHES.length] : [],
          pointRadius: 0,
          pointHoverRadius: pub ? 0 : 3,
          pointStyle: item.type === 'bar' ? 'rect' : 'line',
          // Bars are drawn first, so lines stay readable on top of them.
          order: item.type === 'bar' ? 1 : 0,
          spanGaps: true,
          tension: 0,
          yAxisID: group.id
        });
      });
    });
    const ink = pub ? '#000000' : undefined;
    // Set on every text element: a chart-level font does not reach them all.
    const font = pub ? { family: pub.fontFamily, size: pub.fontPx } : undefined;
    const legendEntries = datasets.map((dataset) => ({
      label: dataset.label,
      bar: dataset.type === 'bar',
      color: dataset.borderColor,
      fill: dataset.backgroundColor,
      dash: dataset.borderDash
    }));
    const header = pub ? publicationHeader(pub, legendEntries) : null;
    const gridColor = pub ? '#d9d9d9' : undefined;
    const showGrid = pub ? pub.grid : true;
    let xStep = 0;
    const scales = {
      x: {
        type: 'linear',
        min: winStart,
        max: winEnd,
        offset: false,
        title: { display: true, text: 'Time (UTC)', color: ink, font },
        grid: { display: showGrid, color: gridColor },
        border: { color: ink },
        afterBuildTicks: (axis) => {
          const ticks = timeTicks(axis.min, axis.max, Math.floor(axis.width / (pub ? pub.fontPx * 7 : 90)));
          xStep = ticks.step;
          axis.ticks = ticks.values.map((value) => ({ value }));
        },
        ticks: {
          color: ink,
          font,
          maxRotation: 0,
          autoSkip: false,
          callback: (value, index, ticks) => timeLabel(value, xStep, index > 0 ? ticks[index - 1].value : null)
        }
      }
    };
    groups.forEach((group, index) => {
      const decimals = decimalsFor(group.step);
      scales[group.id] = {
        type: 'linear',
        position: group.side,
        min: group.min,
        max: group.max,
        title: { display: true, text: group.label, color: ink, font },
        border: { color: ink },
        grid: { display: showGrid, color: gridColor, drawOnChartArea: index === 0 },
        ticks: { color: ink, font, stepSize: group.step, callback: (value) => Number(value).toFixed(decimals) }
      };
    });
    return {
      type: 'line',
      data: { datasets },
      options: {
        responsive: !pub,
        maintainAspectRatio: false,
        animation: false,
        parsing: false,
        normalized: true,
        devicePixelRatio: pub ? pub.dpi / 96 : undefined,
        color: ink,
        layout: { padding: { top: header ? header.height : 0 } },
        interaction: { mode: 'index', intersect: false },
        plugins: {
          // The publication plot draws its own title and legend (see publicationHeader).
          legend: { display: !pub && datasets.length > 1, labels: { usePointStyle: true, pointStyleWidth: 28 } },
          tooltip: {
            enabled: !pub,
            callbacks: {
              title: (items) => (items.length ? new Date(items[0].parsed.x).toISOString().replace('.000Z', 'Z') : '')
            }
          }
        },
        scales
      },
      plugins: pub ? [{
        id: 'publicationFrame',
        beforeDraw: (chart) => {
          const ctx = chart.ctx;
          ctx.save();
          ctx.fillStyle = '#ffffff';
          ctx.fillRect(0, 0, chart.width, chart.height);
          ctx.restore();
        },
        afterDraw: (chart) => header.draw(chart.ctx, chart.chartArea.left, chart.chartArea.right, chart.width)
      }] : []
    };
  }

  // Title and legend of the publication PNG: dashed line samples and bar swatches,
  // laid out like the SVG export.
  function publicationHeader(pub, entries) {
    const fs = pub.fontPx;
    const rowHeight = fs * 1.5;
    const titleHeight = pub.title ? fs * 1.5 : 0;
    const available = pub.widthPx - fs * 8;
    const measure = document.createElement('canvas').getContext('2d');
    measure.font = `${fs}px ${pub.fontFamily}`;
    const rows = [[]];
    if (pub.legend) {
      let cursor = 0;
      entries.forEach((entry) => {
        const width = fs * 2.6 + measure.measureText(entry.label).width + fs;
        if (cursor + width > available && rows[rows.length - 1].length) {
          rows.push([]);
          cursor = 0;
        }
        rows[rows.length - 1].push({ entry, x: cursor, width });
        cursor += width;
      });
    }
    const legendHeight = pub.legend ? rows.length * rowHeight : 0;
    return {
      height: titleHeight + legendHeight + fs * 0.4,
      draw(ctx, left, right, fullWidth) {
        ctx.save();
        ctx.fillStyle = '#000000';
        ctx.textBaseline = 'alphabetic';
        if (pub.title) {
          ctx.font = `bold ${fs}px ${pub.fontFamily}`;
          ctx.textAlign = 'center';
          ctx.fillText(pub.title, fullWidth / 2, fs * 1.1);
        }
        ctx.font = `${fs}px ${pub.fontFamily}`;
        ctx.textAlign = 'left';
        if (pub.legend) {
          rows.forEach((row, rowIndex) => {
            const rowWidth = row.reduce((sum, cell) => sum + cell.width, 0);
            const startX = left + Math.max(0, (right - left - rowWidth) / 2);
            const y = titleHeight + rowIndex * rowHeight + fs;
            row.forEach((cell) => {
              const x = startX + cell.x;
              if (cell.entry.bar) {
                ctx.fillStyle = cell.entry.fill;
                ctx.fillRect(x, y - fs * 0.7, fs * 2, fs * 0.7);
              } else {
                ctx.beginPath();
                ctx.strokeStyle = cell.entry.color;
                ctx.lineWidth = 1.2;
                ctx.setLineDash(cell.entry.dash || []);
                ctx.moveTo(x, y - fs * 0.35);
                ctx.lineTo(x + fs * 2, y - fs * 0.35);
                ctx.stroke();
                ctx.setLineDash([]);
              }
              ctx.fillStyle = '#000000';
              ctx.fillText(cell.entry.label, x + fs * 2.4, y);
            });
          });
        }
        ctx.restore();
      }
    };
  }

  const chartCanvas = el('chart');
  let stationChart = null;
  function renderChart() {
    if (!chartCanvas || !window.Chart) return;
    const hasSeries = plottedItems().length > 0;
    el('chartEmpty').classList.toggle('d-none', hasSeries);
    chartCanvas.parentElement.classList.toggle('d-none', !hasSeries);
    if (stationChart) {
      stationChart.destroy();
      stationChart = null;
    }
    if (hasSeries) stationChart = new Chart(chartCanvas, buildChartConfig(null));
  }

  // ---------------------------------------------------------------- parameter picker
  function sideOf(field) {
    if (chartConfig.left.some((item) => item.field === field)) return 'left';
    if (chartConfig.right.some((item) => item.field === field)) return 'right';
    return null;
  }
  function setSide(field, side) {
    const current = sideOf(field);
    let item = null;
    if (current) {
      item = chartConfig[current].find((it) => it.field === field);
      chartConfig[current] = chartConfig[current].filter((it) => it.field !== field);
    }
    if (side && side !== current) chartConfig[side].push(item || newItem(field));
    refreshChartUi();
  }
  function renderParamList() {
    const list = el('paramList');
    if (!list) return;
    const term = (el('paramSearch').value || '').trim().toLowerCase();
    list.textContent = '';
    const matching = numericCols.filter((field) => !term || fieldLabel(field).toLowerCase().includes(term));
    // Plotted parameters first, so the current selection is always in view.
    matching.sort((a, b) => Number(Boolean(sideOf(b))) - Number(Boolean(sideOf(a))));
    matching.forEach((field) => {
      const side = sideOf(field);
      const row = document.createElement('div');
      row.className = `param-row${side ? ' plotted' : ''}`;
      const name = document.createElement('span');
      name.className = 'param-name';
      name.textContent = fieldLabel(field);
      const group = document.createElement('div');
      group.className = 'btn-group btn-group-sm';
      [['left', 'L', 'left axis'], ['right', 'R', 'right axis']].forEach(([target, text, title]) => {
        const button = document.createElement('button');
        button.type = 'button';
        button.className = `btn ${side === target ? 'btn-primary' : 'btn-outline-secondary'}`;
        button.textContent = text;
        button.title = side === target ? `Remove ${field} from the chart` : `Plot ${field} on the ${title}`;
        button.setAttribute('aria-pressed', side === target ? 'true' : 'false');
        button.addEventListener('click', () => setSide(field, target));
        group.appendChild(button);
      });
      row.append(name, group);
      list.appendChild(row);
    });
    if (!matching.length) {
      const empty = document.createElement('div');
      empty.className = 'text-muted small p-2';
      empty.textContent = 'No parameter matches the search.';
      list.appendChild(empty);
    }
  }

  function renderSeriesList() {
    const list = el('seriesList');
    if (!list) return;
    list.textContent = '';
    ['left', 'right'].forEach((side) => {
      chartConfig[side].forEach((item) => {
        const row = document.createElement('div');
        row.className = 'series-row border-bottom';
        const color = document.createElement('input');
        color.type = 'color';
        color.className = 'form-control form-control-sm form-control-color';
        color.value = item.color;
        color.title = `Colour of ${item.field}`;
        color.addEventListener('change', () => { item.color = color.value; refreshChartUi(false); });
        const name = document.createElement('span');
        name.className = 'fw-semibold small flex-grow-1';
        name.textContent = fieldLabel(item.field);
        const type = document.createElement('select');
        type.className = 'form-select form-select-sm w-auto';
        type.setAttribute('aria-label', `Chart type of ${item.field}`);
        [['line', 'Line'], ['bar', 'Bars']].forEach(([value, text]) => type.add(new Option(text, value, false, item.type === value)));
        type.addEventListener('change', () => { item.type = type.value; refreshChartUi(false); });
        const axis = document.createElement('select');
        axis.className = 'form-select form-select-sm w-auto';
        axis.setAttribute('aria-label', `Axis of ${item.field}`);
        [['left', 'Left axis'], ['right', 'Right axis']].forEach(([value, text]) => axis.add(new Option(text, value, false, side === value)));
        axis.addEventListener('change', () => setSide(item.field, axis.value));
        const remove = document.createElement('button');
        remove.type = 'button';
        remove.className = 'btn btn-outline-danger btn-sm';
        remove.textContent = 'Remove';
        remove.addEventListener('click', () => setSide(item.field, null));
        row.append(color, name, type, axis, remove);
        list.appendChild(row);
      });
    });
  }

  function renderAxisList() {
    const list = el('axisList');
    if (!list) return;
    list.textContent = '';
    axisGroups().forEach((group) => {
      const row = document.createElement('div');
      row.className = 'series-row';
      const name = document.createElement('span');
      name.className = 'small text-muted flex-grow-1';
      name.textContent = `${group.side === 'left' ? 'Left' : 'Right'} axis · ${group.label || 'no unit'}`;
      row.appendChild(name);
      const decimals = decimalsFor(group.step);
      [['min', 'Min', group.min.toFixed(decimals)], ['max', 'Max', group.max.toFixed(decimals)], ['step', 'Step', String(group.step)]]
        .forEach(([key, text, auto]) => {
          const wrap = document.createElement('div');
          wrap.className = 'input-group input-group-sm w-auto';
          const tag = document.createElement('span');
          tag.className = 'input-group-text';
          tag.textContent = text;
          const input = document.createElement('input');
          input.type = 'number';
          input.step = 'any';
          input.className = 'form-control';
          input.style.width = '6.5rem';
          input.placeholder = `auto (${auto})`;
          input.setAttribute('aria-label', `${text} of the ${name.textContent}`);
          if (group.explicit[key] !== null) input.value = group.explicit[key];
          input.addEventListener('change', () => {
            let value = numberOrNull(input.value);
            if (key === 'step' && value !== null && value <= 0) value = null;
            // The range belongs to the axis, so every series on it carries the same values.
            group.items.forEach((item) => { item[key] = value; });
            refreshChartUi(false);
            renderAxisList();
          });
          wrap.append(tag, input);
          row.appendChild(wrap);
        });
      const auto = document.createElement('button');
      auto.type = 'button';
      auto.className = 'btn btn-outline-secondary btn-sm';
      auto.textContent = 'Auto range';
      auto.addEventListener('click', () => {
        group.items.forEach((item) => { item.min = null; item.max = null; item.step = null; });
        refreshChartUi(false);
        renderAxisList();
      });
      row.appendChild(auto);
      list.appendChild(row);
    });
  }

  function updateCsvLinks() {
    const hidden = hiddenColumns();
    const range = el('rangeCsvLink');
    if (range) {
      const url = new URL(range.dataset.base, window.location.origin);
      if (hidden.size) tableColumns.filter((c) => !hidden.has(c)).forEach((c) => url.searchParams.append('col', c));
      range.href = url.pathname + url.search;
    }
    const plotted = el('plottedCsvLink');
    if (plotted && range) {
      const url = new URL(range.dataset.base, window.location.origin);
      const fields = plottedItems().map((item) => item.field);
      ['timestamp', ...fields].forEach((c) => url.searchParams.append('col', c));
      plotted.href = url.pathname + url.search;
      plotted.classList.toggle('disabled', fields.length === 0);
    }
  }

  function refreshChartUi(rebuildLists = true) {
    saveChartConfig();
    if (rebuildLists) {
      renderParamList();
      renderSeriesList();
    }
    renderAxisList();
    renderChart();
    updateCsvLinks();
  }

  if (el('paramSearch')) el('paramSearch').addEventListener('input', renderParamList);
  if (el('chartClearBtn')) {
    el('chartClearBtn').addEventListener('click', () => {
      chartConfig.left = [];
      chartConfig.right = [];
      refreshChartUi();
    });
  }
  if (el('chartExportBtn')) {
    el('chartExportBtn').addEventListener('click', () => {
      const payload = { instrument_uuid: stationUuid, exported_at: new Date().toISOString(), chart_config: chartConfig };
      download(new Blob([JSON.stringify(payload, null, 2)], { type: 'application/json' }), `${fileStem.split('_')[0]}_chart_setup.json`);
    });
  }
  if (el('chartImportFile')) {
    el('chartImportFile').addEventListener('change', async (event) => {
      const input = event.target;
      const file = input.files && input.files[0];
      if (!file) return;
      try {
        const payload = JSON.parse(await file.text());
        if (!payload || typeof payload.chart_config !== 'object') throw new Error('invalid');
        loadChartConfig(payload.chart_config);
        refreshChartUi();
      } catch (_) {
        window.alert('This file is not a chart setup saved from this page.');
      } finally {
        input.value = '';
      }
    });
  }

  // ---------------------------------------------------------------- table columns (remembered per station)
  const columnStorageKey = `station_hidden_columns:${stationUuid}`;
  const columnToggles = Array.from(document.querySelectorAll('.column-toggle'));
  function hiddenColumns() {
    return new Set(columnToggles.filter((toggle) => !toggle.checked).map((toggle) => toggle.value));
  }
  function applyColumns(remember = true) {
    const rules = columnToggles
      .filter((toggle) => !toggle.checked)
      .map((toggle) => `#dataTable .c${toggle.dataset.index}{display:none}`);
    el('hiddenColumnStyle').textContent = rules.join('\n');
    const count = el('columnCount');
    if (count) count.textContent = `${columnToggles.length - rules.length}/${columnToggles.length}`;
    if (remember) writeStored(columnStorageKey, Array.from(hiddenColumns()));
    updateCsvLinks();
  }
  const storedHidden = readStored(columnStorageKey);
  if (Array.isArray(storedHidden)) {
    const hide = new Set(storedHidden);
    columnToggles.forEach((toggle) => { toggle.checked = !hide.has(toggle.value); });
    if (columnToggles.length && columnToggles.every((toggle) => !toggle.checked)) {
      columnToggles.forEach((toggle) => { toggle.checked = true; });
    }
  }
  columnToggles.forEach((toggle) => toggle.addEventListener('change', () => applyColumns()));
  if (el('columnsAll')) {
    el('columnsAll').addEventListener('click', () => {
      columnToggles.forEach((toggle) => { toggle.checked = true; });
      applyColumns();
    });
  }
  if (el('columnsPlotted')) {
    el('columnsPlotted').addEventListener('click', () => {
      const keep = new Set(['timestamp', ...plottedItems().map((item) => item.field)]);
      columnToggles.forEach((toggle) => { toggle.checked = keep.has(toggle.value); });
      applyColumns();
    });
  }

  // ---------------------------------------------------------------- publication export
  function publicationSettings() {
    const [widthMm, heightMm] = el('pubSize').value.split('x').map(Number);
    const fontPt = Number(el('pubFontSize').value);
    return {
      widthMm,
      heightMm,
      widthPx: (widthMm / 25.4) * 96,
      heightPx: (heightMm / 25.4) * 96,
      fontPt,
      fontPx: (fontPt * 96) / 72,
      fontFamily: el('pubFont').value,
      dpi: Number(el('pubDpi').value),
      title: el('pubTitle').value.trim(),
      legend: el('pubLegend').checked,
      grid: el('pubGrid').checked,
      mono: el('pubMono').checked
    };
  }

  // PNG files carry their resolution in a pHYs chunk; the canvas does not write one.
  function crc32(bytes) {
    let crc = -1;
    for (let i = 0; i < bytes.length; i += 1) {
      crc ^= bytes[i];
      for (let k = 0; k < 8; k += 1) crc = (crc >>> 1) ^ (0xEDB88320 & -(crc & 1));
    }
    return (crc ^ -1) >>> 0;
  }
  function withPngResolution(png, dpi) {
    const pixelsPerMetre = Math.round(dpi / 0.0254);
    const chunk = new Uint8Array(21);
    const view = new DataView(chunk.buffer);
    view.setUint32(0, 9);
    chunk.set([0x70, 0x48, 0x59, 0x73], 4);
    view.setUint32(8, pixelsPerMetre);
    view.setUint32(12, pixelsPerMetre);
    chunk[16] = 1;
    view.setUint32(17, crc32(chunk.subarray(4, 17)));
    const headerEnd = 33;
    const out = new Uint8Array(png.length + chunk.length);
    out.set(png.subarray(0, headerEnd), 0);
    out.set(chunk, headerEnd);
    out.set(png.subarray(headerEnd), headerEnd + chunk.length);
    return out;
  }

  function renderPng(pub) {
    return new Promise((resolve, reject) => {
      const canvas = document.createElement('canvas');
      canvas.width = Math.round(pub.widthPx);
      canvas.height = Math.round(pub.heightPx);
      canvas.style.width = `${Math.round(pub.widthPx)}px`;
      canvas.style.height = `${Math.round(pub.heightPx)}px`;
      const holder = document.createElement('div');
      holder.style.cssText = 'position:fixed;left:-100000px;top:0;';
      holder.appendChild(canvas);
      document.body.appendChild(holder);
      // devicePixelRatio = dpi / 96 makes Chart.js draw every line and letter at the print resolution.
      const chart = new Chart(canvas, buildChartConfig(pub));
      canvas.toBlob(async (blob) => {
        try {
          if (!blob) throw new Error('empty');
          resolve(withPngResolution(new Uint8Array(await blob.arrayBuffer()), pub.dpi));
        } catch (error) {
          reject(error);
        } finally {
          chart.destroy();
          holder.remove();
        }
      }, 'image/png');
    });
  }

  async function downloadPng() {
    if (!plottedItems().length) return;
    const pub = publicationSettings();
    try {
      download(new Blob([await renderPng(pub)], { type: 'image/png' }), `${fileStem}_${pub.dpi}dpi.png`);
    } catch (_) {
      window.alert('The image is too large for this browser. Choose a lower resolution or a smaller figure.');
    }
  }

  function buildSvg(pub) {
    const W = pub.widthPx;
    const H = pub.heightPx;
    const fs = pub.fontPx;
    const groups = axisGroups();
    const leftGroups = groups.filter((g) => g.side === 'left');
    const rightGroups = groups.filter((g) => g.side === 'right');
    const esc = (text) => String(text).replace(/[&<>"]/g, (ch) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[ch]));
    const n = (value) => Number(value.toFixed(2));
    const textWidth = (text) => String(text).length * fs * 0.56;
    const axisWidth = (group) => {
      const decimals = decimalsFor(group.step);
      const widest = Math.max(textWidth(group.min.toFixed(decimals)), textWidth(group.max.toFixed(decimals)));
      return widest + fs * 2.9;
    };
    const stroke = 0.75;
    const out = [];
    const text = (x, y, value, anchor = 'middle', extra = '') =>
      out.push(`<text x="${n(x)}" y="${n(y)}" text-anchor="${anchor}"${extra}>${esc(value)}</text>`);

    const series = [];
    groups.forEach((group) => group.items.forEach((item) => series.push({ group, item })));
    series.forEach((entry, index) => {
      entry.color = pub.mono ? '#000000' : entry.item.color;
      entry.dash = pub.mono && entry.item.type !== 'bar' ? DASHES[index % DASHES.length] : [];
    });

    let top = fs * 0.6;
    if (pub.title) top += fs * 1.5;
    const padLeft = Math.max(fs, leftGroups.reduce((sum, g) => sum + axisWidth(g), 0));
    const padRight = Math.max(fs * 1.5, rightGroups.reduce((sum, g) => sum + axisWidth(g), 0));
    const legendRows = [[]];
    if (pub.legend) {
      let cursor = 0;
      series.forEach((entry) => {
        const width = fs * 2.6 + textWidth(fieldLabel(entry.item.field)) + fs;
        if (cursor + width > W - padLeft - padRight && legendRows[legendRows.length - 1].length) {
          legendRows.push([]);
          cursor = 0;
        }
        legendRows[legendRows.length - 1].push({ entry, x: cursor, width });
        cursor += width;
      });
    }
    const legendHeight = pub.legend ? legendRows.length * fs * 1.5 : 0;
    const x0 = padLeft;
    const x1 = W - padRight;
    const y0 = top + legendHeight + fs * 0.4;
    const y1 = H - fs * 4.4;
    const xs = (t) => x0 + ((t - winStart) / (winEnd - winStart)) * (x1 - x0);

    out.push(`<rect width="${n(W)}" height="${n(H)}" fill="#ffffff"/>`);
    if (pub.title) text(W / 2, fs * 1.3, pub.title, 'middle', ' font-weight="bold"');

    if (pub.legend) {
      legendRows.forEach((row, rowIndex) => {
        const rowWidth = row.reduce((sum, cell) => sum + cell.width, 0);
        const startX = x0 + Math.max(0, (x1 - x0 - rowWidth) / 2);
        const y = top + rowIndex * fs * 1.5 + fs * 0.9;
        row.forEach((cell) => {
          const lx = startX + cell.x;
          if (cell.entry.item.type === 'bar') {
            out.push(`<rect x="${n(lx)}" y="${n(y - fs * 0.7)}" width="${n(fs * 2)}" height="${n(fs * 0.7)}" fill="${cell.entry.color}" fill-opacity="${pub.mono ? 0.35 : 0.6}"/>`);
          } else {
            const dash = cell.entry.dash.length ? ` stroke-dasharray="${cell.entry.dash.join(' ')}"` : '';
            out.push(`<line x1="${n(lx)}" y1="${n(y - fs * 0.35)}" x2="${n(lx + fs * 2)}" y2="${n(y - fs * 0.35)}" stroke="${cell.entry.color}" stroke-width="1.2"${dash}/>`);
          }
          text(lx + fs * 2.4, y, fieldLabel(cell.entry.item.field), 'start');
        });
      });
    }

    const ticksX = timeTicks(winStart, winEnd, Math.floor((x1 - x0) / (fs * 7)));
    const yTicks = (group) => {
      const values = [];
      const first = Math.ceil(group.min / group.step - 1e-9) * group.step;
      for (let v = first; v <= group.max + group.step * 1e-6 && values.length < 60; v += group.step) values.push(v);
      return values;
    };
    const ys = (group, v) => y1 - ((v - group.min) / (group.max - group.min)) * (y1 - y0);

    if (pub.grid) {
      out.push('<g stroke="#d9d9d9" stroke-width="0.5">');
      ticksX.values.forEach((t) => out.push(`<line x1="${n(xs(t))}" y1="${n(y0)}" x2="${n(xs(t))}" y2="${n(y1)}"/>`));
      if (groups.length) {
        yTicks(groups[0]).forEach((v) => out.push(`<line x1="${n(x0)}" y1="${n(ys(groups[0], v))}" x2="${n(x1)}" y2="${n(ys(groups[0], v))}"/>`));
      }
      out.push('</g>');
    }

    out.push(`<clipPath id="plotArea"><rect x="${n(x0)}" y="${n(y0)}" width="${n(x1 - x0)}" height="${n(y1 - y0)}"/></clipPath>`);
    out.push('<g clip-path="url(#plotArea)">');
    const barWidth = Math.max(0.4, ((x1 - x0) / Math.max(times.length, 1)) * 0.8);
    // Bars first, so lines stay readable on top of them.
    const drawOrder = [...series].sort((a, b) => Number(b.item.type === 'bar') - Number(a.item.type === 'bar'));
    drawOrder.forEach((entry) => {
      const values = seriesValues[entry.item.field] || [];
      if (entry.item.type === 'bar') {
        const base = ys(entry.group, Math.min(Math.max(0, entry.group.min), entry.group.max));
        const rects = [];
        times.forEach((t, i) => {
          const v = values[i];
          if (v === null || v === undefined || !Number.isFinite(t)) return;
          const y = ys(entry.group, v);
          rects.push(`<rect x="${n(xs(t) - barWidth / 2)}" y="${n(Math.min(y, base))}" width="${n(barWidth)}" height="${n(Math.abs(base - y))}"/>`);
        });
        // Group opacity: overlapping bars of a dense series do not add up.
        out.push(`<g fill="${entry.color}" opacity="${pub.mono ? 0.35 : 0.6}">${rects.join('')}</g>`);
      } else {
        const points = [];
        times.forEach((t, i) => {
          const v = values[i];
          if (v === null || v === undefined || !Number.isFinite(t)) return;
          points.push(`${points.length ? 'L' : 'M'}${n(xs(t))} ${n(ys(entry.group, v))}`);
        });
        const dash = entry.dash.length ? ` stroke-dasharray="${entry.dash.join(' ')}"` : '';
        if (points.length) out.push(`<path d="${points.join('')}" fill="none" stroke="${entry.color}" stroke-width="1" stroke-linejoin="round"${dash}/>`);
      }
    });
    out.push('</g>');

    out.push(`<rect x="${n(x0)}" y="${n(y0)}" width="${n(x1 - x0)}" height="${n(y1 - y0)}" fill="none" stroke="#000000" stroke-width="${stroke}"/>`);

    let previous = null;
    ticksX.values.forEach((t) => {
      const x = xs(t);
      out.push(`<line x1="${n(x)}" y1="${n(y1)}" x2="${n(x)}" y2="${n(y1 + fs * 0.4)}" stroke="#000000" stroke-width="${stroke}"/>`);
      timeLabel(t, ticksX.step, previous).forEach((line, lineIndex) => text(x, y1 + fs * (1.4 + lineIndex * 1.15), line));
      previous = t;
    });
    text((x0 + x1) / 2, H - fs * 0.6, 'Time (UTC)');

    const drawAxis = (group, x, direction) => {
      const decimals = decimalsFor(group.step);
      out.push(`<line x1="${n(x)}" y1="${n(y0)}" x2="${n(x)}" y2="${n(y1)}" stroke="#000000" stroke-width="${stroke}"/>`);
      yTicks(group).forEach((v) => {
        const y = ys(group, v);
        out.push(`<line x1="${n(x)}" y1="${n(y)}" x2="${n(x + direction * fs * 0.4)}" y2="${n(y)}" stroke="#000000" stroke-width="${stroke}"/>`);
        text(x + direction * fs * 0.6, y + fs * 0.35, v.toFixed(decimals), direction < 0 ? 'end' : 'start');
      });
      const titleX = x + direction * (axisWidth(group) - fs * 0.9);
      const titleY = (y0 + y1) / 2;
      text(titleX, titleY, group.label, 'middle', ` transform="rotate(${direction < 0 ? -90 : 90} ${n(titleX)} ${n(titleY)})"`);
    };
    let offset = 0;
    leftGroups.forEach((group) => { drawAxis(group, x0 - offset, -1); offset += axisWidth(group); });
    offset = 0;
    rightGroups.forEach((group) => { drawAxis(group, x1 + offset, 1); offset += axisWidth(group); });

    return [
      '<?xml version="1.0" encoding="UTF-8"?>',
      `<svg xmlns="http://www.w3.org/2000/svg" width="${pub.widthMm}mm" height="${pub.heightMm}mm" viewBox="0 0 ${n(W)} ${n(H)}" font-family="${esc(pub.fontFamily)}" font-size="${n(fs)}" fill="#000000">`,
      ...out,
      '</svg>'
    ].join('\n');
  }

  if (el('pubPngBtn')) el('pubPngBtn').addEventListener('click', downloadPng);
  if (el('pubSvgBtn')) {
    el('pubSvgBtn').addEventListener('click', () => {
      if (!plottedItems().length) return;
      download(new Blob([buildSvg(publicationSettings())], { type: 'image/svg+xml' }), `${fileStem}.svg`);
    });
  }
  // Exposed for automated checks of the exports.
  window.stationBrowser = { chartConfig, buildSvg, renderPng, publicationSettings, axisGroups };

  applyColumns(false);
  if (chartCanvas) refreshChartUi();
})();
