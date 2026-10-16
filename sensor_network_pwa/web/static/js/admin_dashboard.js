const pageConfig = JSON.parse(document.getElementById('pageConfig').textContent);
const endpoint = pageConfig.endpoint;
const headRow = document.getElementById('headRow');
const bodyRows = document.getElementById('bodyRows');
const updatedAt = document.getElementById('updatedAt');

const GROUP_ORDER = ['Atmosphere', 'Wind', 'Rain', 'Air Quality', 'Position', 'System', 'Other'];

function classifyField(field) {
  const f = String(field || '').toLowerCase();
  if (f.includes('aqi') || f.startsWith('pm') || f.includes('partic') || f.includes('airlink')) return 'Air Quality';
  if (f.includes('wind')) return 'Wind';
  if (f.includes('rain') || f.includes('storm') || f.includes('et')) return 'Rain';
  if (f.includes('lat') || f.includes('lon') || f.includes('lng') || f.includes('position')) return 'Position';
  if (
    f.includes('temp') || f.includes('hum') || f.includes('bar') || f.includes('press') ||
    f.includes('dew') || f.includes('wet_bulb') || f.includes('heat')
  ) return 'Atmosphere';
  if (f.includes('battery') || f.includes('volt') || f.includes('status') || f.includes('signal')) return 'System';
  return 'Other';
}

function buildGroupedEntries(st) {
  const values = st.values || {};
  const missing = new Set(st.missingFields || []);
  const keys = new Set(Object.keys(values));
  missing.forEach((k) => keys.add(k));

  const entries = Array.from(keys).map((k) => {
    const isMissing = missing.has(k);
    return {
      group: classifyField(k),
      field: k,
      value: isMissing ? 'MISSING' : (values[k] == null ? '' : String(values[k])),
      missing: isMissing
    };
  });

  entries.sort((a, b) => {
    const ga = GROUP_ORDER.indexOf(a.group);
    const gb = GROUP_ORDER.indexOf(b.group);
    const ia = ga === -1 ? GROUP_ORDER.length : ga;
    const ib = gb === -1 ? GROUP_ORDER.length : gb;
    if (ia !== ib) return ia - ib;
    return a.field.localeCompare(b.field);
  });

  const grouped = new Map();
  entries.forEach((entry) => {
    if (!grouped.has(entry.group)) {
      grouped.set(entry.group, []);
    }
    grouped.get(entry.group).push(entry);
  });

  const out = Array.from(grouped.entries()).map(([group, items]) => {
    items.sort((a, b) => a.field.localeCompare(b.field));
    return { group, items };
  });

  out.sort((a, b) => {
    const ga = GROUP_ORDER.indexOf(a.group);
    const gb = GROUP_ORDER.indexOf(b.group);
    const ia = ga === -1 ? GROUP_ORDER.length : ga;
    const ib = gb === -1 ? GROUP_ORDER.length : gb;
    return ia - ib;
  });

  return out;
}

function render(payload) {
  headRow.innerHTML = '';
  ['Station', 'UUID', 'Timestamp', 'Age(s)', 'Usual update(s)', 'Fail threshold(s)', 'Status', 'Alarms', 'Missing values', 'Battery', 'Group', 'Data'].forEach((h) => {
    const th = document.createElement('th');
    th.textContent = h;
    headRow.appendChild(th);
  });

  bodyRows.innerHTML = '';
  (payload.stations || []).forEach((st) => {
    const entries = buildGroupedEntries(st);
    if (entries.length === 0) {
      entries.push({ group: 'Other', items: [{ field: '-', value: '-', missing: false }] });
    }

    entries.forEach((entry, idx) => {
      const tr = document.createElement('tr');

      if (idx === 0) {
        const fixed = [
          st.name || st.uuid,
          st.uuid || '',
          st.lastTimestamp || '',
          st.ageSeconds == null ? '' : st.ageSeconds,
          st.usualUpdateSeconds == null ? '' : st.usualUpdateSeconds,
          st.failureThresholdSeconds == null ? '' : st.failureThresholdSeconds,
          st.status || '',
          (st.alarms || []).join(', '),
          (st.missingFields && st.missingFields.length) ? st.missingFields.join(', ') : '',
          st.batteryInfo || ''
        ];
        fixed.forEach((value, colIdx) => {
          const td = document.createElement('td');
          td.textContent = String(value);
          td.rowSpan = entries.length;
          if (colIdx === 6) {
            td.className = (String(value) === 'OK') ? 'status-ok' : 'status-alarm';
          }
          tr.appendChild(td);
        });
      }

      const g = document.createElement('td');
      g.textContent = entry.group;
      g.className = 'group-cell';
      tr.appendChild(g);

      const v = document.createElement('td');
      entry.items.forEach((item, itemIdx) => {
        if (itemIdx > 0) v.appendChild(document.createTextNode(' | '));
        if (item.missing) {
          const span = document.createElement('span');
          span.className = 'missing-cell px-1';
          span.textContent = `${item.field}=MISSING`;
          v.appendChild(span);
        } else {
          v.appendChild(document.createTextNode(`${item.field}=${item.value}`));
        }
      });
      tr.appendChild(v);

      bodyRows.appendChild(tr);
    });
  });

  updatedAt.textContent = 'Updated at: ' + (payload.updatedAt || '-');
}

async function refresh() {
  try {
    const resp = await fetch(endpoint, { cache: 'no-store' });
    if (!resp.ok) return;
    const payload = await resp.json();
    render(payload);
  } catch (_) {
    // keep polling on transient errors
  }
}

refresh();
setInterval(refresh, 10000);
