const pageConfig = JSON.parse(document.getElementById('pageConfig').textContent);
const usernameInput = document.getElementById('onboardingUsername');
const availability = document.getElementById('usernameAvailability');
let availabilityTimer = null;
let lastAvailability = null;
async function checkUsername(showAlert = false) {
  const username = (usernameInput.value || '').trim();
  if (!username) {
    availability.textContent = '';
    lastAvailability = null;
    return;
  }
  try {
    const qs = new URLSearchParams({ username: username, token: pageConfig.onboardingToken });
    const resp = await fetch(`${pageConfig.checkUsernameUrl}?${qs.toString()}`, { cache: 'no-store' });
    if (!resp.ok) return;
    const payload = await resp.json();
    availability.textContent = payload.message || 'Username available';
    availability.className = payload.available ? 'form-text text-success' : 'form-text text-danger';
    if (!payload.available && showAlert && lastAvailability !== false) {
      window.alert(payload.message || 'Username already exists');
    }
    lastAvailability = payload.available;
  } catch (_) {}
}
usernameInput.addEventListener('input', () => {
  if (availabilityTimer) clearTimeout(availabilityTimer);
  availabilityTimer = setTimeout(() => checkUsername(false), 300);
});
usernameInput.addEventListener('blur', () => checkUsername(true));
