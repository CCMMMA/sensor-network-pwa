(function () {
  const source = document.getElementById('accessChartData');
  if (!source || typeof Chart === 'undefined') return;
  const data = JSON.parse(source.textContent);
  const daily = data.daily || [];
  const types = data.types || [];
  const common = { responsive: true, maintainAspectRatio: false };
  new Chart(document.getElementById('dailyChart'), {
    type: 'bar',
    data: { labels: daily.map((item) => item.label), datasets: [{ label: 'Events', data: daily.map((item) => item.value), backgroundColor: '#0d6efd' }] },
    options: { ...common, plugins: { legend: { display: false } }, scales: { y: { beginAtZero: true, ticks: { precision: 0 } } } }
  });
  new Chart(document.getElementById('typeChart'), {
    type: 'doughnut',
    data: { labels: types.map((item) => item.label), datasets: [{ data: types.map((item) => item.value), backgroundColor: ['#0d6efd', '#198754', '#ffc107', '#dc3545', '#6f42c1', '#0dcaf0', '#fd7e14', '#6c757d', '#20c997'] }] },
    options: { ...common, plugins: { legend: { position: 'bottom' } } }
  });
}());
