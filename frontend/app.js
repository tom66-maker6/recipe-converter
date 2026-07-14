"use strict";
const $ = (s, r=document) => r.querySelector(s);
const api = async (url, opts={}) => {
  const r = await fetch(url, {headers:{"Content-Type":"application/json"}, ...opts});
  if(!r.ok){ throw new Error((await r.json().catch(()=>({detail:r.statusText}))).detail); }
  return r.json();
};
const toast = (m) => { const t=$("#toast"); t.textContent=m; t.hidden=false; setTimeout(()=>t.hidden=true, 3200); };
let ME = {is_admin:false}, CURRENT_BATCH = null, POLL = null;

// ---------- identity ----------
(async () => {
  try{
    ME = await api("/api/me");
    $("#userBox").innerHTML =
      `${ME.name}${ME.is_admin?'<span class="badge-admin">Admin</span>':''}` +
      `<button class="btn small ghost" id="logoutBtn" style="margin-left:12px">Sign out</button>`;
    $("#logoutBtn").onclick = async ()=>{ await fetch("/api/logout",{method:"POST"}); window.location="/login"; };
  }catch(e){ window.location = "/login"; }   // not signed in → go to the login page
})();

// ---------- upload ----------
const dz = $("#dropzone"), input = $("#fileInput");
$("#browseBtn").onclick = (e)=>{ e.stopPropagation(); input.click(); };
dz.onclick = ()=> input.click();
["dragenter","dragover"].forEach(ev=>dz.addEventListener(ev,e=>{e.preventDefault();dz.classList.add("drag");}));
["dragleave","drop"].forEach(ev=>dz.addEventListener(ev,e=>{e.preventDefault();dz.classList.remove("drag");}));
dz.addEventListener("drop", e=> handleFiles(e.dataTransfer.files));
input.addEventListener("change", ()=> handleFiles(input.files));

async function handleFiles(fileList){
  const files=[...fileList]; if(!files.length) return;
  const fd = new FormData(); files.forEach(f=>fd.append("files", f));
  const instr = ($("#aiInstructions")?.value || "").trim();
  if(instr) fd.append("instructions", instr);
  toast(`Uploading ${files.length} file(s)…`);
  try{
    const r = await fetch("/api/upload", {method:"POST", body:fd});
    if(!r.ok) throw new Error((await r.json()).detail);
    const {batch_id} = await r.json();
    CURRENT_BATCH = batch_id;
    $("#results").innerHTML=""; $("#batchActions").hidden=false;
    startPolling();
  }catch(e){ toast("Upload failed: "+e.message); }
}

// ---------- polling ----------
function startPolling(){
  clearInterval(POLL);
  POLL = setInterval(refresh, 1200); refresh();
}
async function refresh(){
  if(!CURRENT_BATCH) return;
  const b = await api(`/api/batch/${CURRENT_BATCH}`);
  renderBatch(b);
  const done = b.files.every(f=>["ready","needs_review","error"].includes(f.status));
  if(done) clearInterval(POLL);
  const ready = b.files.filter(f=>f.status!=="queued"&&f.status!=="processing").length;
  $("#batchSummary").textContent = `${ready}/${b.files.length} file(s) processed`;
}

// ---------- render ----------
function renderBatch(b){
  const root = $("#results");
  b.files.forEach(f=>{
    let el = $(`#file-${f.file_id}`);
    if(!el){ el = document.createElement("div"); el.className="file"; el.id=`file-${f.file_id}`; root.appendChild(el); }
    if(el.dataset.status===f.status && el.dataset.n===String(f.recipes.length)) return; // no change
    el.dataset.status=f.status; el.dataset.n=String(f.recipes.length);
    el.innerHTML = fileHtml(f);
    wireFile(el, f);
  });
}
function statusLabel(s){ return {queued:"Queued",processing:"Processing",ready:"Ready",
  needs_review:"Needs review",error:"Error"}[s]||s; }

function fileHtml(f){
  let head = `<div class="file-head"><span class="file-name">${esc(f.name)}</span>
    <span class="status st-${f.status}">${f.status==="processing"?'<span class="spinner"></span>':''}${statusLabel(f.status)}</span></div>`;
  if(f.status==="error") return head + `<div class="recipe"><div class="flag warn">${esc(f.error||"Could not process.")}</div></div>`;
  if(["queued","processing"].includes(f.status)) return head + `<div class="recipe muted">Analysing…</div>`;
  let note="";
  if(f.detected>1) note=`<div class="detect-note">${f.detected} recipes detected in this document — one Excel file will be generated per recipe.</div>`;
  if(f.ambiguous_multi) note=`<div class="detect-note">This document may contain several recipes or one recipe with sub-components. Please review before generating.</div>`;
  return head + note + f.recipes.map((r,i)=>recipeHtml(f,r,i)).join("");
}

function confClass(c){ return c>=95?"hi":c>=80?"mid":"lo"; }
function recipeHtml(f,r,idx){
  const cats = r.categories.map(c=>`<option ${c===r.category?"selected":""}>${c}</option>`).join("");
  const flags = [
    ...r.conversions.map(c=>`<div class="flag conv">⚙ ${esc(c)}</div>`),
    ...r.warnings.map(w=>`<div class="flag warn">⚠ ${esc(w)}</div>`),
  ].join("");
  const rows = r.ingredients.map(ing=>`
    <tr class="${ing.status==='unknown'?'row-flag':''}">
      <td>${ing.no}</td>
      <td><input class="i-name ${ing.status==='unknown'?'unknown':''}" value="${esc(ing.name)}" data-no="${ing.no}"/></td>
      <td class="unit"><input class="i-unit" value="${esc(ing.unit)}" data-no="${ing.no}"/></td>
      <td class="qty"><input class="i-qty" type="number" step="any" value="${ing.qty}" data-no="${ing.no}"/></td>
    </tr>`).join("");
  return `<div class="recipe" id="r-${r.recipe_id}">
    <div class="recipe-top">
      <input class="name" value="${esc(r.recipe_name)}"/>
      <select class="cat">${cats}</select>
      <span class="conf ${confClass(r.confidence)}">Confidence ${r.confidence}%</span>
    </div>
    <div class="flags">${flags||'<div class="flag conv">No issues detected.</div>'}</div>
    <table class="ing"><thead><tr><th>#</th><th>Ingredient</th><th>Unit</th><th>Qty</th></tr></thead>
      <tbody>${rows}</tbody></table>
    <label class="muted">Process</label>
    <textarea class="proc">${esc(r.process||"")}</textarea>
    <div class="recipe-actions">
      <button class="btn gen">✓ Approve &amp; generate Excel</button>
      <span class="dl-slot"></span>
    </div>
  </div>`;
}

function wireFile(el, f){
  el.querySelectorAll(".recipe").forEach((rEl,i)=>{
    const r = f.recipes[i]; if(!r) return;
    const gen = rEl.querySelector(".gen"); if(!gen) return;
    gen.onclick = async ()=>{
      gen.disabled=true; gen.textContent="Generating…";
      const recipe = collect(rEl, r);
      try{
        const res = await api("/api/generate", {method:"POST",
          body: JSON.stringify({recipe, batch_id: CURRENT_BATCH})});
        rEl.querySelector(".dl-slot").innerHTML =
          `<a class="btn small ghost" href="/api/download/${res.token}">⬇ ${esc(res.filename)}</a>`;
        gen.textContent="✓ Generated"; toast("Excel generated: "+res.filename);
      }catch(e){ gen.disabled=false; gen.textContent="✓ Approve & generate Excel"; toast("Failed: "+e.message); }
    };
  });
}
function collect(rEl, r){
  const ings = [...rEl.querySelectorAll("table.ing tbody tr")].map((tr,i)=>({
    no:i+1,
    name: tr.querySelector(".i-name").value.trim(),
    unit: tr.querySelector(".i-unit").value.trim(),
    qty: parseFloat(tr.querySelector(".i-qty").value)||0,
  })).filter(x=>x.name);
  return {...r,
    recipe_name: rEl.querySelector(".name").value.trim(),
    category: rEl.querySelector(".cat").value,
    process: rEl.querySelector(".proc").value,
    ingredients: ings};
}

// ---------- download all ----------
$("#downloadAllBtn").onclick = ()=>{
  if(CURRENT_BATCH) window.location = `/api/batch/${CURRENT_BATCH}/download-all`;
};

// ---------- optional instructions (collapsible) ----------
const instrToggle = $("#instrToggle");
if(instrToggle){
  instrToggle.onclick = ()=>{
    const body = $("#instrBody"), willOpen = body.hidden;
    body.hidden = !willOpen;
    $("#instrChev").textContent = willOpen ? "▾" : "▸";
    instrToggle.setAttribute("aria-expanded", willOpen ? "true" : "false");
  };
}

function esc(s){ return String(s).replace(/[&<>"]/g, c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c])); }
