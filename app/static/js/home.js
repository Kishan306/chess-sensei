import { el, playerPicker } from "./players.js";
import { isMasterclass, onMasterclassChange, ready } from "./nav.js";
import { confirmReview, isBusy, spentLabel, stopReview } from "./coach-ui.js";

const form = document.getElementById("upload-form");
const resultBox = document.getElementById("upload-result");
const tbody = document.getElementById("games");
let pollTimer = null;

form.addEventListener("submit", async (e) => {
  e.preventDefault();
  const data = new FormData(form);
  if (!data.get("analyze")) data.set("analyze", "false");
  const button = form.querySelector("button");
  button.disabled = true;
  resultBox.replaceChildren();
  try {
    const res = await fetch("/api/games", { method: "POST", body: data });
    const body = await res.json();
    if (!res.ok) throw new Error(body.detail || res.statusText);
    const parts = [];
    if (body.created.length) parts.push(el("p", {}, `Added ${body.created.length} game(s).`));
    if (body.duplicates?.length) {
      parts.push(el("ul", { class: "duplicates" }, ...body.duplicates.map((d) =>
        el("li", {}, `Game ${d.game_index}: already uploaded (${d.label}) — `,
          el("a", { href: `/review/${d.existing_id}` }, "open it")))));
    }
    if (body.errors.length) {
      parts.push(el("ul", { class: "errors" },
        ...body.errors.map((er) => el("li", {}, er.game_index ? `Game ${er.game_index}: ${er.message}` : er.message))));
    }
    resultBox.replaceChildren(...parts);
    if (body.created.length) form.reset();
    await loadGames();
  } catch (err) {
    resultBox.replaceChildren(el("p", { class: "error" }, String(err.message || err)));
  } finally {
    button.disabled = false;
  }
});

async function analyze(id) {
  await fetch(`/api/games/${id}/analyze`, { method: "POST" });
  loadGames();
}

function statusCell(g) {
  if (g.analysis_status === "done") return el("span", {}, "Done");
  if (g.analysis_status === "running") {
    const bar = el("span", { class: "progress" }, el("div", { style: `width:${g.analysis_progress}%` }));
    return el("span", {}, bar, ` ${g.analysis_progress}%`);
  }
  if (g.analysis_status === "error") return el("span", { class: "error" }, "Failed");
  return el("span", { class: "muted" }, "Not analysed");
}

function coachCell(g) {
  const tag = g.coach_mode === "masterclass" ? [" ", el("span", { class: "mc-badge" }, "Masterclass")] : [];
  const spent = spentLabel(g);
  const cost = spent ? [el("div", { class: "muted small" }, spent)] : [];
  let state;
  if (g.coach_status === "done") state = el("span", {}, "Done", ...tag);
  else if (g.coach_status === "queued") state = el("span", { class: "muted" }, "Queued");
  else if (g.coach_status === "stopping") state = el("span", { class: "muted" }, "Stopping…");
  else if (g.coach_status === "stopped") state = el("span", { class: "muted" }, "Stopped");
  else if (g.coach_status === "running") {
    state = el("span", {}, el("span", { class: "progress" }, el("div", { style: `width:${g.coach_progress}%` })),
      ` ${g.coach_progress}%`);
  } else if (g.coach_status === "error") state = el("span", { class: "error" }, "Failed");
  else state = el("span", { class: "muted" }, "–");
  return el("div", {}, state, ...cost);
}

async function startGameReview(g) {
  if (!(await confirmReview(g.id))) return;
  const res = await fetch(`/api/games/${g.id}/coach`, { method: "POST" });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    alert(body.detail || "Couldn't start the review.");
  }
  loadGames();
}

async function reviewAll() {
  if (!(await confirmReview(null))) return;
  const res = await fetch("/api/coach/all", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({}),
  });
  if (!res.ok) alert("Couldn't start the reviews.");
  loadGames();
}

function renderBulk(games) {
  const box = document.getElementById("bulk");
  if (!games.length) {
    box.replaceChildren();
    return;
  }
  const mc = isMasterclass();
  const busy = games.some((g) => isBusy(g.coach_status));
  const total = games.reduce((sum, g) => (g.coach_cost_usd === null || sum === null ? null : sum + g.coach_cost_usd), 0);
  box.replaceChildren(
    el("button", { onclick: reviewAll }, mc ? "Masterclass review all games" : "Coach review all games"),
    busy ? el("button", { onclick: async () => { await stopReview(null); loadGames(); } }, "Stop all reviews") : "",
    el("span", { class: "muted" }, total ? `Coaching spent so far: $${total.toFixed(2)}` : ""));
}

async function loadGames() {
  const games = await (await fetch("/api/games")).json();
  document.getElementById("empty").hidden = games.length > 0;
  renderBulk(games);
  tbody.replaceChildren(...games.map((g) => {
    const actions = el("td", {});
    if (g.analysis_status !== "running") {
      actions.append(el("button", { onclick: () => analyze(g.id) }, g.analysis_status === "done" ? "Re-analyse" : "Analyse"), " ");
    }
    if (g.analysis_status === "done") {
      actions.append(el("a", { href: `/review/${g.id}`, class: "btn" }, "Review"), " ");
      if (isBusy(g.coach_status)) {
        actions.append(el("button", {
          disabled: g.coach_status === "stopping" ? "" : null,
          onclick: async () => { await stopReview(g.id); loadGames(); },
        }, g.coach_status === "stopping" ? "Stopping…" : "Stop"));
      } else {
        const mc = isMasterclass();
        const again = g.coach_status === "stopped" ? "Continue " : "";
        actions.append(el("button", { class: mc ? "mc" : "", onclick: () => startGameReview(g) },
          again + (mc ? "Masterclass" : "Coach")));
      }
    }
    return el("tr", {},
      el("td", {}, playerPicker(g)),
      el("td", {}, g.result),
      el("td", {}, [g.opening_eco, g.opening_name].filter(Boolean).join(" ")),
      el("td", {}, g.date),
      el("td", {}, statusCell(g)),
      el("td", { title: g.coach_status === "error" ? "Open the game to see the error" : "" }, coachCell(g)),
      actions);
  }));

  // Keep refreshing while any game is being analysed or reviewed.
  clearTimeout(pollTimer);
  const busy = games.some((g) => g.analysis_status === "running" || isBusy(g.coach_status));
  if (busy) pollTimer = setTimeout(loadGames, 2000);
}

onMasterclassChange(() => loadGames());
ready.then(loadGames);
