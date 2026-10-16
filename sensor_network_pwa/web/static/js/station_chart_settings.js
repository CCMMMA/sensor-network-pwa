const pageConfig = JSON.parse(document.getElementById('pageConfig').textContent);
const importForm = document.getElementById('chartSettingsImportForm');
const importFile = document.getElementById('chartSettingsImportFile');
const confirmForeignStation = document.getElementById('confirmForeignStation');
const stationUuid = pageConfig.stationUuid;
if (importForm && importFile && confirmForeignStation) {
  importForm.dataset.sourceStationUuid = '';
  importForm.dataset.fileChecked = '0';

  importFile.addEventListener('change', async () => {
    confirmForeignStation.value = '0';
    const file = importFile.files && importFile.files[0];
    importForm.dataset.sourceStationUuid = '';
    importForm.dataset.fileChecked = file ? '0' : '1';
    if (!file) return;
    try {
      const text = await file.text();
      const payload = JSON.parse(text);
      const sourceStationUuid = String((payload && payload.station_uuid) || '').trim();
      importForm.dataset.sourceStationUuid = sourceStationUuid;
    } catch (_) {
      importForm.dataset.sourceStationUuid = '';
    } finally {
      importForm.dataset.fileChecked = '1';
    }
  });

  importForm.addEventListener('submit', (event) => {
    const file = importFile.files && importFile.files[0];
    if (!file) return;
    if (importForm.dataset.fileChecked !== '1') {
      event.preventDefault();
      window.alert('Please wait for the JSON file to be checked, then submit again.');
      return;
    }
    const sourceStationUuid = String(importForm.dataset.sourceStationUuid || '').trim();
    if (sourceStationUuid && sourceStationUuid !== stationUuid) {
      const ok = window.confirm(`This configuration was exported from station "${sourceStationUuid}" and will be imported into "${stationUuid}". Continue?`);
      if (!ok) {
        event.preventDefault();
        return;
      }
      confirmForeignStation.value = '1';
    }
  });
}
