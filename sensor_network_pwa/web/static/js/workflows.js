(() => {
  "use strict";
  const config = JSON.parse(document.getElementById("workflowConfig").textContent);
  const canvas = document.getElementById("flowCanvas");
  const nodesEl = document.getElementById("flowNodes");
  const select = document.getElementById("flowSelect");
  const editor = document.getElementById("nodeEditor");
  let workflows = [], currentId = null, nodes = [], edges = [];
  let selectedNode = null, selectedEdge = null, connecting = null, sequence = 0;
  const escapeHtml = value => String(value ?? "").replace(/[&<>"']/g, character => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"})[character]);
  const nodeTitle = node => node.type === "station" ? "Station" : node.type === "condition" ? "Condition" : "Send email";
  const nodeSummary = node => node.type === "station" ? (node.data.station || "Choose station") : node.type === "condition" ? `${node.data.field || "field"} ${node.data.operator} ${node.data.value}` : (node.data.recipient || "Choose recipient");

  function message(text, danger = false) {
    const element = document.getElementById("flowMessage");
    element.textContent = text;
    element.className = `alert alert-${danger ? "danger" : "success"}`;
  }
  function clearSelection() { selectedNode = null; selectedEdge = null; }
  function newGraph() {
    currentId = null; nodes = []; edges = []; clearSelection();
    document.getElementById("flowName").value = "New workflow";
    document.getElementById("flowEnabled").checked = true;
    render(); refreshSelect();
  }
  function refreshSelect() {
    select.innerHTML = '<option value="">New workflow</option>' + workflows.map(workflow => `<option value="${workflow.id}" ${workflow.id === currentId ? "selected" : ""}>${escapeHtml(workflow.name)}</option>`).join("");
    document.getElementById("deleteFlow").disabled = !currentId;
  }
  function load(workflow) {
    currentId = workflow.id; nodes = structuredClone(workflow.graph.nodes); edges = structuredClone(workflow.graph.edges);
    document.getElementById("flowName").value = workflow.name;
    document.getElementById("flowEnabled").checked = Boolean(workflow.enabled);
    clearSelection(); render(); refreshSelect();
  }
  function addNode(type) {
    const id = `${type}-${Date.now()}-${sequence++}`;
    const defaults = {station:{station:config.stations[0] || ""},condition:{field:"RainRate",operator:">",value:300},email:{recipient:""}};
    nodes.push({id, type, x:40 + (nodes.length % 3) * 220, y:40 + Math.floor(nodes.length / 3) * 140, data:defaults[type]});
    selectedNode = id; selectedEdge = null; render();
  }
  function removeNode(id) {
    nodes = nodes.filter(node => node.id !== id);
    edges = edges.filter(edge => edge.source !== id && edge.target !== id);
    if (connecting === id) connecting = null;
    clearSelection(); render();
  }
  function render() {
    nodesEl.textContent = "";
    for (const node of nodes) {
      const element = document.createElement("div");
      element.className = `flow-node node-${node.type}${selectedNode === node.id ? " selected" : ""}`;
      element.dataset.id = node.id; element.style.left = `${node.x}px`; element.style.top = `${node.y}px`;
      element.innerHTML = `<div class="node-title">${escapeHtml(nodeTitle(node))}<button type="button" class="node-remove" title="Remove node" aria-label="Remove node">×</button></div><div class="node-body">${escapeHtml(nodeSummary(node))}</div>${node.type !== "station" ? '<button type="button" class="port in" data-port="in" aria-label="Input"></button>' : ""}${node.type !== "email" ? '<button type="button" class="port out" data-port="out" aria-label="Output"></button>' : ""}`;
      nodesEl.appendChild(element); bindNode(element, node);
    }
    drawEdges(); renderEditor();
  }
  function bindNode(element, node) {
    element.querySelector(".node-remove").addEventListener("click", event => { event.stopPropagation(); removeNode(node.id); });
    element.addEventListener("click", event => {
      if (event.target.classList.contains("port")) { portClick(node, event.target); return; }
      selectedNode = node.id; selectedEdge = null; render();
    });
    const title = element.querySelector(".node-title");
    title.addEventListener("pointerdown", event => {
      if (event.target.closest(".node-remove")) return;
      const startX = event.clientX, startY = event.clientY, originX = node.x, originY = node.y;
      title.setPointerCapture(event.pointerId);
      title.onpointermove = move => {
        node.x = Math.max(0, originX + move.clientX - startX); node.y = Math.max(0, originY + move.clientY - startY);
        element.style.left = `${node.x}px`; element.style.top = `${node.y}px`; drawEdges();
      };
      title.onpointerup = () => { title.onpointermove = null; };
    });
  }
  function portClick(node, port) {
    if (port.dataset.port === "out") {
      connecting = node.id;
      document.querySelectorAll(".port.pending").forEach(item => item.classList.remove("pending"));
      port.classList.add("pending"); return;
    }
    if (connecting && connecting !== node.id && !edges.some(edge => edge.source === connecting && edge.target === node.id)) {
      edges.push({source:connecting,target:node.id}); selectedEdge = edges.length - 1; selectedNode = null;
    }
    connecting = null; render();
  }
  function drawEdges() {
    const svg = document.getElementById("flowEdges"); svg.textContent = "";
    edges.forEach((edge, index) => {
      const source = document.querySelector(`[data-id="${CSS.escape(edge.source)}"]`);
      const target = document.querySelector(`[data-id="${CSS.escape(edge.target)}"]`);
      if (!source || !target) return;
      const x1 = source.offsetLeft + source.offsetWidth, y1 = source.offsetTop + source.offsetHeight / 2;
      const x2 = target.offsetLeft, y2 = target.offsetTop + target.offsetHeight / 2;
      const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
      path.setAttribute("d", `M${x1},${y1} C${x1 + 80},${y1} ${x2 - 80},${y2} ${x2},${y2}`);
      path.setAttribute("class", `flow-edge${selectedEdge === index ? " selected" : ""}`);
      path.setAttribute("role", "button"); path.setAttribute("aria-label", `Edit connection from ${edge.source} to ${edge.target}`);
      path.addEventListener("click", event => { event.stopPropagation(); selectedNode = null; selectedEdge = index; render(); });
      svg.appendChild(path);
    });
  }
  function edgeOptions(selected, direction) {
    return nodes.filter(node => direction === "source" ? node.type !== "email" : node.type !== "station").map(node => `<option value="${escapeHtml(node.id)}" ${node.id === selected ? "selected" : ""}>${escapeHtml(`${nodeTitle(node)} — ${nodeSummary(node)}`)}</option>`).join("");
  }
  function renderEditor() {
    if (selectedEdge !== null && edges[selectedEdge]) {
      const edge = edges[selectedEdge]; editor.className = "";
      editor.innerHTML = `<h3 class="h6">Connection</h3><label class="form-label">From</label><select class="form-select mb-2" data-edge-key="source">${edgeOptions(edge.source,"source")}</select><label class="form-label">To</label><select class="form-select mb-3" data-edge-key="target">${edgeOptions(edge.target,"target")}</select><button type="button" class="btn btn-outline-danger btn-sm" id="removeEdge">Remove connection</button>`;
      editor.querySelectorAll("[data-edge-key]").forEach(input => input.addEventListener("change", () => { edge[input.dataset.edgeKey] = input.value; drawEdges(); }));
      document.getElementById("removeEdge").addEventListener("click", () => { edges.splice(selectedEdge, 1); selectedEdge = null; render(); });
      return;
    }
    const node = nodes.find(item => item.id === selectedNode);
    if (!node) { editor.className = "text-muted small"; editor.textContent = "Select a node or connection."; return; }
    editor.className = "";
    if (node.type === "station") editor.innerHTML = `<label class="form-label">Station</label><select class="form-select" data-key="station">${config.stations.map(station => `<option ${station === node.data.station ? "selected" : ""}>${escapeHtml(station)}</option>`).join("")}</select>`;
    else if (node.type === "condition") editor.innerHTML = `<label class="form-label">Field</label><input class="form-control mb-2" data-key="field" value="${escapeHtml(node.data.field)}"><label class="form-label">Operator</label><select class="form-select mb-2" data-key="operator">${[">",">=","<","<=","==","!="].map(operator => `<option ${operator === node.data.operator ? "selected" : ""}>${escapeHtml(operator)}</option>`).join("")}</select><label class="form-label">Value</label><input class="form-control" type="number" step="any" data-key="value" value="${escapeHtml(node.data.value)}">`;
    else editor.innerHTML = `<label class="form-label">Recipient</label><input class="form-control" type="email" data-key="recipient" value="${escapeHtml(node.data.recipient)}">`;
    editor.querySelectorAll("[data-key]").forEach(input => input.addEventListener("input", () => {
      node.data[input.dataset.key] = input.type === "number" ? Number(input.value) : input.value;
      const body = document.querySelector(`[data-id="${CSS.escape(node.id)}"] .node-body`); if (body) body.textContent = nodeSummary(node);
    }));
  }
  async function request(url, options = {}) {
    const response = await fetch(url, {...options, headers:{"Content-Type":"application/json",...(options.headers || {})}});
    const text = response.status === 204 ? "" : await response.text();
    let data = null; if (text) { try { data = JSON.parse(text); } catch (_) { data = null; } }
    if (!response.ok) throw new Error(data?.error || text || `Request failed (${response.status})`);
    return data;
  }
  async function reload() {
    const data = await request("/api/workflows"); workflows = data.workflows;
    if (currentId) { const current = workflows.find(workflow => workflow.id === currentId); current ? load(current) : newGraph(); }
    else if (workflows.length) load(workflows[0]); else newGraph();
  }
  function exportSvg() {
    const padding = 30, width = Math.max(400,...nodes.map(node => node.x + 180 + padding));
    const height = Math.max(240,...nodes.map(node => node.y + 76 + padding));
    const colors = {station:"#198754",condition:"#fd7e14",email:"#0d6efd"};
    const lines = edges.map(edge => {
      const source = nodes.find(node => node.id === edge.source), target = nodes.find(node => node.id === edge.target);
      if (!source || !target) return "";
      const x1 = source.x + 180, y1 = source.y + 38, x2 = target.x, y2 = target.y + 38;
      return `<path d="M${x1},${y1} C${x1 + 80},${y1} ${x2 - 80},${y2} ${x2},${y2}" fill="none" stroke="#6c757d" stroke-width="3"/>`;
    }).join("");
    const boxes = nodes.map(node => `<g><rect x="${node.x}" y="${node.y}" width="180" height="76" rx="7" fill="#fff" stroke="#adb5bd"/><path d="M${node.x + 7},${node.y} H${node.x + 173} Q${node.x + 180},${node.y} ${node.x + 180},${node.y + 7} V${node.y + 32} H${node.x} V${node.y + 7} Q${node.x},${node.y} ${node.x + 7},${node.y}" fill="${colors[node.type]}"/><text x="${node.x + 10}" y="${node.y + 21}" fill="#fff" font-family="sans-serif" font-size="14" font-weight="bold">${escapeHtml(nodeTitle(node))}</text><text x="${node.x + 10}" y="${node.y + 57}" fill="#212529" font-family="sans-serif" font-size="12">${escapeHtml(nodeSummary(node).slice(0,26))}</text></g>`).join("");
    return `<svg xmlns="http://www.w3.org/2000/svg" width="${width}" height="${height}" viewBox="0 0 ${width} ${height}"><rect width="100%" height="100%" fill="#f8f9fa"/>${lines}${boxes}</svg>`;
  }
  function downloadBlob(blob, extension) {
    const name = (document.getElementById("flowName").value || "workflow").replace(/[^A-Za-z0-9._-]+/g,"-");
    const link = document.createElement("a"); link.href = URL.createObjectURL(blob); link.download = `${name}.${extension}`; link.click();
    setTimeout(() => URL.revokeObjectURL(link.href),0);
  }
  function exportImage(format) {
    const svg = exportSvg();
    if (format === "svg") { downloadBlob(new Blob([svg],{type:"image/svg+xml;charset=utf-8"}),"svg"); return; }
    const image = new Image(), source = URL.createObjectURL(new Blob([svg],{type:"image/svg+xml"}));
    image.onload = () => {
      const output = document.createElement("canvas"); output.width = image.naturalWidth * 2; output.height = image.naturalHeight * 2;
      const context = output.getContext("2d"); context.fillStyle = "#f8f9fa"; context.fillRect(0,0,output.width,output.height); context.drawImage(image,0,0,output.width,output.height); URL.revokeObjectURL(source);
      output.toBlob(blob => { if (blob) downloadBlob(blob,format); },`image/${format === "jpg" ? "jpeg" : "png"}`,0.92);
    };
    image.onerror = () => { URL.revokeObjectURL(source); message("Could not render the workflow image.",true); };
    image.src = source;
  }
  document.querySelectorAll(".add-node").forEach(button => button.addEventListener("click", () => addNode(button.dataset.type)));
  document.getElementById("newFlow").addEventListener("click",newGraph);
  document.getElementById("exportFlow").addEventListener("click",() => exportImage(document.getElementById("exportFormat").value));
  select.addEventListener("change",() => select.value ? load(workflows.find(workflow => workflow.id === Number(select.value))) : newGraph());
  canvas.addEventListener("click",event => { if (event.target === canvas || event.target === nodesEl) { clearSelection(); render(); } });
  document.getElementById("saveFlow").addEventListener("click",async () => {
    try {
      const body = JSON.stringify({name:document.getElementById("flowName").value,enabled:document.getElementById("flowEnabled").checked,graph:{nodes,edges}});
      const saved = await request(currentId ? `/api/workflows/${currentId}` : "/api/workflows",{method:currentId ? "PUT" : "POST",body});
      currentId = saved.id; message("Workflow saved."); await reload();
    } catch (error) { message(error.message,true); }
  });
  document.getElementById("deleteFlow").addEventListener("click",async () => {
    if (!currentId || !confirm("Delete this workflow?")) return;
    try { await request(`/api/workflows/${currentId}`,{method:"DELETE"}); currentId = null; message("Workflow deleted."); await reload(); }
    catch (error) { message(error.message,true); }
  });
  reload().catch(error => message(error.message,true));
})();
