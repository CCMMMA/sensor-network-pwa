// Service-worker registration, offline banner and install button; loaded by base.html.
(function () {
  var settings = document.currentScript.dataset;
  if ('serviceWorker' in navigator) {
    window.addEventListener('load', function () {
      navigator.serviceWorker.register(settings.swUrl, { scope: settings.scope }).catch(function () {});
    });
  }
  var banner = document.getElementById('pwaOfflineBanner');
  function showConnectivity() { banner.classList.toggle('d-none', navigator.onLine); }
  window.addEventListener('online', showConnectivity);
  window.addEventListener('offline', showConnectivity);
  showConnectivity();
  var button = document.getElementById('pwaInstallButton');
  var installPrompt = null;
  window.addEventListener('beforeinstallprompt', function (event) {
    event.preventDefault();
    installPrompt = event;
    button.classList.remove('d-none');
  });
  button.addEventListener('click', function () {
    if (!installPrompt) return;
    installPrompt.prompt();
    installPrompt = null;
    button.classList.add('d-none');
  });
  window.addEventListener('appinstalled', function () { button.classList.add('d-none'); });
})();
