const pageConfig = JSON.parse(document.getElementById('pageConfig').textContent);
const stations = pageConfig.stations;
const mapEl = document.getElementById('map');
if (stations.length > 0 && mapEl && window.L) {
  const map = L.map(mapEl).setView([Number(pageConfig.center.lat), Number(pageConfig.center.lon)], Number(pageConfig.center.zoom));
  L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
    maxZoom: 19,
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'
  }).addTo(map);
  L.control.scale({ imperial: false }).addTo(map);

  const positions = [];
  stations.forEach((s) => {
    const lat = Number(s.latitude);
    const lon = Number(s.longitude);
    if (s.latitude == null || s.longitude == null || !Number.isFinite(lat) || !Number.isFinite(lon)
        || Math.abs(lat) > 90 || Math.abs(lon) > 180) {
      return;
    }
    positions.push([lat, lon]);
    const color = s.can_access ? '#1f9d55' : '#666';
    const marker = L.circleMarker([lat, lon], {
      radius: 9,
      color: '#fff',
      weight: 2,
      fillColor: color,
      fillOpacity: 0.9
    }).addTo(map);

    // Station names and timestamps come from sensor data: add them as text, never as HTML.
    const popup = document.createElement('div');
    const title = document.createElement('div');
    title.className = 'fw-bold';
    title.textContent = s.name || s.uuid;
    popup.appendChild(title);
    const addLine = (text) => {
      const line = document.createElement('div');
      line.className = 'small text-muted';
      line.textContent = text;
      popup.appendChild(line);
    };
    addLine(s.uuid);
    addLine(`policy: ${s.policy}`);
    if (s.last_timestamp) {
      addLine(`last data: ${s.last_timestamp}`);
    }
    const actions = document.createElement('div');
    actions.className = 'd-grid gap-1 mt-2';
    const addButton = (href, text, style) => {
      const a = document.createElement('a');
      a.href = href;
      a.className = `btn btn-sm ${style}`;
      a.style.color = style === 'btn-primary' ? '#fff' : '';
      a.textContent = text;
      actions.appendChild(a);
    };
    addButton(s.public_url, 'Open dashboard', 'btn-primary');
    if (s.can_access) {
      addButton(s.browse_url, 'Browse & download data', 'btn-outline-secondary');
    } else {
      addLine('Log in to browse and download data.');
    }
    popup.appendChild(actions);
    marker.bindPopup(popup, { minWidth: 180 });
    marker.bindTooltip(document.createTextNode(s.name || s.uuid), { direction: 'top', offset: [0, -8] });
  });

  // Show every station, whatever the first one is.
  if (positions.length > 1) {
    map.fitBounds(positions, { padding: [30, 30], maxZoom: 12 });
  } else if (positions.length === 1) {
    map.setView(positions[0], 11);
  }
  // The container can change size after the first layout (fonts, installed-app window).
  window.addEventListener('resize', () => map.invalidateSize());
  setTimeout(() => map.invalidateSize(), 0);
}
