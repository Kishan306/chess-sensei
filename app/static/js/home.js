import { el, playerPicker } from "./players.js";

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

async function loadGames() {
  const games = await (await fetch("/api/games")).json();
  document.getElementById("empty").hidden = games.length > 0;
  tbody.replaceChildren(...games.map((g) => {
    const actions = el("td", {});
    if (g.analysis_status !== "running") {
      actions.append(el("button", { onclick: () => analyze(g.id) }, g.analysis_status === "done" ? "Re-analyse" : "Analyse"), " ");
    }
    if (g.analysis_status === "done") actions.append(el("a", { href: `/review/${g.id}`, class: "btn" }, "Review"));
    return el("tr", {},
      el("td", {}, playerPicker(g)),
      el("td", {}, g.result),
      el("td", {}, [g.opening_eco, g.opening_name].filter(Boolean).join(" ")),
      el("td", {}, g.date),
      el("td", {}, statusCell(g)),
      actions);
  }));

  // Keep refreshing while any game is being analysed or queued.
  clearTimeout(pollTimer);
  if (games.some((g) => g.analysis_status === "running")) pollTimer = setTimeout(loadGames, 1500);
}

loadGames();
