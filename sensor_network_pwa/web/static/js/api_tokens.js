(() => {
  "use strict";
  const rows = document.getElementById("tokenRows");
  const message = (text, danger = false) => {
    const element = document.getElementById("tokenMessage");
    element.textContent = text;
    element.className = `alert alert-${danger ? "danger" : "success"}`;
  };
  const request = async (url, options = {}) => {
    const response = await fetch(url, {...options, headers: {"Content-Type": "application/json"}});
    if (!response.ok) {
      let detail;
      try { detail = (await response.json()).error; } catch (_) { detail = await response.text(); }
      throw new Error(detail || `Request failed (${response.status})`);
    }
    return response.status === 204 ? null : response.json();
  };
  const format = value => value ? new Date(value).toLocaleString() : "Never";
  const expiryValue = value => value ? new Date(value).toISOString().slice(0, 16) : "";
  const payload = (name, expiry) => JSON.stringify({name, expires_at: expiry ? new Date(expiry).toISOString() : null});
  const reload = async () => {
    const data = await request("/api/tokens");
    rows.textContent = "";
    for (const token of data.tokens) {
      const row = document.createElement("tr");
      const name = document.createElement("input"); name.className = "form-control"; name.maxLength = 100; name.value = token.name;
      const expiry = document.createElement("input"); expiry.className = "form-control"; expiry.type = "datetime-local"; expiry.value = expiryValue(token.expires_at);
      const save = document.createElement("button"); save.className = "btn btn-sm btn-outline-primary me-2"; save.textContent = "Save";
      const remove = document.createElement("button"); remove.className = "btn btn-sm btn-outline-danger"; remove.textContent = "Revoke";
      for (const element of [name, token.token_prefix + "…", format(token.created_at), format(token.last_used_at), expiry]) { const cell = document.createElement("td"); typeof element === "string" ? cell.textContent = element : cell.appendChild(element); row.appendChild(cell); }
      const actions = document.createElement("td"); actions.append(save, remove); row.appendChild(actions); rows.appendChild(row);
      save.addEventListener("click", async () => { try { await request(`/api/tokens/${token.id}`, {method: "PUT", body: payload(name.value, expiry.value)}); message("Token updated."); await reload(); } catch (error) { message(error.message, true); } });
      remove.addEventListener("click", async () => { if (!confirm(`Revoke token “${token.name}”?`)) return; try { await request(`/api/tokens/${token.id}`, {method: "DELETE"}); message("Token revoked."); await reload(); } catch (error) { message(error.message, true); } });
    }
  };
  document.getElementById("createToken").addEventListener("click", async () => {
    try {
      const created = await request("/api/tokens", {method: "POST", body: payload(document.getElementById("newTokenName").value, document.getElementById("newTokenExpiry").value)});
      document.getElementById("tokenSecret").value = created.token;
      document.getElementById("newTokenSecret").classList.remove("d-none");
      document.getElementById("newTokenName").value = "";
      document.getElementById("newTokenExpiry").value = "";
      message("Token created."); await reload();
    } catch (error) { message(error.message, true); }
  });
  document.getElementById("copyToken").addEventListener("click", () => navigator.clipboard.writeText(document.getElementById("tokenSecret").value));
  reload().catch(error => message(error.message, true));
})();
